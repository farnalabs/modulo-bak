"""Shared SSRF-safe URL validation for outbound requests.

Blocks private/loopback/link-local/cloud-metadata/CGNAT ranges via DNS
resolution. Used by notification endpoints, SSO test connections,
observability test, and error-forwarder test paths.

Beyond *validating* a URL, this module owns the **pinned-IP connection
transport**: an ``httpx`` transport that resolves and validates a target once,
then forces the TCP connection to the validated address while keeping the
**original** hostname for TLS SNI and certificate verification. This closes the
DNS-rebinding window that a validate-then-connect pattern leaves open (see the
note on :func:`validate_outbound_url`).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import socket
import ssl
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

import httpcore
import httpx
from httpcore._backends.base import SOCKET_OPTION

_log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# Extra ranges not covered by ipaddress.is_private (cloud metadata, CGNAT).
_EXCLUDED_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/GCP/Azure link-local metadata
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("0.0.0.0/8"),  # current network
    ipaddress.ip_network("100.100.100.200/32"),  # Aliyun metadata
]

# Configurable allowlist for self-hosted deployments on private networks.
# Comma-separated CIDR list in SSRF_ALLOW_PRIVATE_RANGES env var. Parsed
# lazily and cached keyed on the raw env value: a stable value parses once for
# the process lifetime, while a mid-process change to the env var is honoured
# immediately (no import-time side effect, no mutable module global). Returns
# an immutable tuple so readers never race a mutation.
_allowlist_cache_key: str | None = None
_allowlist_parsed: tuple[Network, ...] = ()


@dataclass(frozen=True)
class PinnedTarget:
    """A validated outbound target with its pinned connect address.

    ``host`` is the **original** hostname as given in the URL — it is what the
    connection uses for TLS SNI and certificate verification, and it must never
    be replaced by ``ip`` (rewriting the URL to the IP breaks TLS). ``ip`` is
    the validated address the TCP connection is pinned to.
    """

    scheme: str
    host: str
    port: int | None
    ip: str


def _get_allowlist() -> tuple[Network, ...]:
    global _allowlist_cache_key, _allowlist_parsed
    raw = os.environ.get("SSRF_ALLOW_PRIVATE_RANGES", "")
    if raw == _allowlist_cache_key:
        return _allowlist_parsed
    parsed: list[Network] = []
    for cidr in raw.split(","):
        cidr = cidr.strip()
        if cidr:
            try:
                parsed.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                _log.warning("ssrf.invalid_allowlist_entry", extra={"cidr": cidr})
    _allowlist_cache_key = raw
    _allowlist_parsed = tuple(parsed)
    return _allowlist_parsed


def normalize_allow_networks(raw: Sequence[str] | None) -> tuple[Network, ...]:
    """Normalise a tenant-scoped egress allowlist to an immutable network tuple.

    Accepts CIDR strings (``"10.0.0.0/8"``) or ``ip_network`` objects. Returns
    ``()`` for ``None``. Invalid entries are logged and skipped, mirroring the
    global env allowlist so a malformed tenant rule never injects a fatal config
    error — the base global policy still applies as the floor.
    """
    if raw is None:
        return ()
    parsed: list[Network] = []
    for entry in raw:
        try:
            if isinstance(entry, str):
                parsed.append(ipaddress.ip_network(entry, strict=False))
            elif isinstance(entry, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                parsed.append(entry)
            else:
                _log.warning("ssrf.invalid_allowlist_entry_type", extra={"type": type(entry).__name__})
        except ValueError:
            _log.warning("ssrf.invalid_allowlist_entry", extra={"cidr": entry})
    return tuple(parsed)


def _get_effective_allowlist(extra: tuple[Network, ...]) -> tuple[Network, ...]:
    """Layer a tenant-scoped allowlist on top of the global base.

    The global ``SSRF_ALLOW_PRIVATE_RANGES`` stays the floor; ``extra`` adds to
    it for a single tenant. Returns the union, deduplicated, as an immutable
    tuple. Every enforcement path funnels through this so there is exactly one
    implementation of "is this address permitted?".
    """
    base = _get_allowlist()
    if not extra:
        return base
    return base + tuple(net for net in extra if net not in base)


def _is_blocked_ip(ip_str: str, extra_allow: tuple[Network, ...] = ()) -> bool:
    """Check if an IP address should be blocked."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # fail-closed on unparseable

    # Check configurable allowlist first (global + tenant-scoped overlay).
    for net in _get_effective_allowlist(extra_allow):
        if addr in net:
            return False

    # Standard private/loopback/link-local
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_unspecified:
        return True

    # Extra networks not in is_private
    return any(addr in net for net in _EXCLUDED_NETWORKS)


def _reject_noncanonical_ip_literal(host: str) -> None:
    """Reject alternate IP-literal encodings a resolver may interpret as an address.

    ``ipaddress.ip_address`` only accepts canonical dotted-quad IPv4 and IPv6;
    it rejects decimal integer (``2130706433``), hex (``0x7f000001``), octal
    (``0177.0.0.1``), abbreviated (``127.1``), overlong (``1.2.3.4.5``) and
    %-encoded/zone-id forms. Some resolvers are more permissive and treat these
    as addresses, so validation must reject them rather than reach the
    resolver. This deliberately does not regex-validate the domain — non-IP
    hostnames simply pass through to ``getaddrinfo``, and the resolved set is
    validated.
    """
    if "%" in host:
        raise ValueError("URL host contains a scope/percent-encoded IP literal")
    if not host:
        return
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return  # canonical IP literal — validated by the caller
    if host.startswith(("0x", "0X")):
        raise ValueError("URL host is a hex-encoded IP literal")
    if host.isdigit():
        raise ValueError("URL host is a decimal/octal integer IP literal")
    if "." in host and host.replace(".", "").isdigit():
        raise ValueError("URL host is a non-canonical dotted-numeric IP literal")


def _validate_port(parsed: object) -> int | None:
    """Extract and validate the URL port. Fails closed on odd/out-of-range ports."""
    try:
        port: int | None = parsed.port  # type: ignore[attr-defined]
    except ValueError:
        raise ValueError("URL has an invalid port") from None
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("URL port is out of the valid range (1-65535)")
    return port


@dataclass(frozen=True)
class _UrlTarget:
    scheme: str
    host: str
    port: int | None
    literal_ip: str | None  # populated iff host is a canonical IP literal


def _parse_url_target(url: str) -> _UrlTarget:
    """Strictly parse and syntax-validate a URL before any resolution.

    Rejects: non-http(s) schemes, embedded userinfo, missing hostname,
    non-canonical IP literals, and odd/out-of-range ports. Does not resolve the
    host — resolution happens separately and the resolved set is validated.
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL is required")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http:// or https:// scheme")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain userinfo credentials")

    host = parsed.hostname
    if not host:
        raise ValueError("URL must have a valid hostname")
    host = host.rstrip(".").strip("[]")
    if not host:
        raise ValueError("URL must have a valid hostname")

    _reject_noncanonical_ip_literal(host)
    port = _validate_port(parsed)

    literal_ip: str | None = None
    with contextlib.suppress(ValueError):
        literal_ip = str(ipaddress.ip_address(host))

    return _UrlTarget(scheme=parsed.scheme, host=host, port=port, literal_ip=literal_ip)


def _validate_literal_ip(decoded: str, extra_allow: tuple[Network, ...] = ()) -> bool:
    """Block private/internal literal IPs; return True when ``decoded`` is an IP.

    Returns False when ``decoded`` is a hostname (not a literal IP), signalling
    that the caller must DNS-resolve it. Raises ValueError for blocked literal
    IPs (fail-closed).
    """
    try:
        ip = ipaddress.ip_address(decoded)
    except ValueError:
        return False
    if _is_blocked_ip(str(ip), extra_allow):
        raise ValueError(
            f"URL targets a private/internal network address: {decoded}. "
            "Use a public URL or add the address to SSRF_ALLOW_PRIVATE_RANGES."
        )
    return True


def _check_resolved(decoded: str, ip_strings: Sequence[str], extra_allow: tuple[Network, ...] = ()) -> None:
    """Raise if **any** resolved address of a hostname is blocked (fail-closed)."""
    for ip_str in ip_strings:
        if _is_blocked_ip(ip_str, extra_allow):
            raise ValueError(
                f"URL hostname {decoded} resolves to a private/internal address ({ip_str}). Use a public URL."
            )


def _resolve_all_sync(host: str) -> list[str]:
    try:
        addrinfos = socket.getaddrinfo(host, 0, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        # Fail-closed on DNS resolution failure
        raise ValueError(f"DNS resolution failed for {host}. Cannot verify the target is not internal.") from None
    result: list[str] = []
    for _fam, _typ, _proto, _canon, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        if isinstance(ip_str, str):  # O-safe: assert would vanish under python -O
            result.append(ip_str)
    return result


async def _resolve_all_async(host: str) -> list[str]:
    """Async DNS resolution that does not block the event loop."""
    loop = asyncio.get_running_loop()
    try:
        addrinfos = await loop.getaddrinfo(host, 0, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        raise ValueError(f"DNS resolution failed for {host}. Cannot verify the target is not internal.") from None
    result: list[str] = []
    for _fam, _typ, _proto, _canon, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        if isinstance(ip_str, str):  # O-safe: assert would vanish under python -O
            result.append(ip_str)
    return result


def validate_outbound_url(url: str, *, allow_networks: Sequence[str] | None = None) -> None:
    """Validate that a URL does not point to an internal/private destination.

    Performs synchronous DNS resolution. For use in sync contexts (Pydantic
    validators, synchronous call sites). Raises ValueError if the URL is unsafe.

    ``allow_networks`` layers a tenant-scoped CIDR allowlist on top of the
    global ``SSRF_ALLOW_PRIVATE_RANGES`` floor.

    For async callers use :func:`validate_outbound_url_async` so DNS resolution
    does not block the event loop. For callers that actually perform the
    outbound request, use :func:`pinned_async_transport` (or
    :func:`resolve_pinned_ip`) so the validated address is pinned onto the
    connection, closing the DNS-rebinding window described below.

    NOTE — formerly accepted residual risk (DNS rebinding): this validation
    function resolves the hostname, verifies all resolved addresses are
    non-internal, then returns. It does NOT pin the validated address onto the
    subsequent outbound connection. A hostname under DNS control can therefore
    resolve to a public address during validation and to an internal/metadata
    address during the actual request performed by a call site on its own,
    bypassing this check. The pinned transport built here closes that window by
    connecting to the validated address while keeping the original hostname for
    SNI/cert.
    """
    target = _parse_url_target(url)
    extra = normalize_allow_networks(allow_networks)
    if target.literal_ip is not None:
        _validate_literal_ip(target.literal_ip, extra)
        return  # literal IP handled above
    _check_resolved(target.host, _resolve_all_sync(target.host), extra)


async def validate_outbound_url_async(url: str, *, allow_networks: Sequence[str] | None = None) -> None:
    """Async variant of :func:`validate_outbound_url` for event-loop-hostile callers.

    Resolves the hostname with ``asyncio.get_running_loop().getaddrinfo`` so the
    DNS lookup does not block the event loop. Raises ValueError if the URL is
    unsafe. ``allow_networks`` layers a tenant-scoped CIDR allowlist on the
    global floor.
    """
    target = _parse_url_target(url)
    extra = normalize_allow_networks(allow_networks)
    if target.literal_ip is not None:
        _validate_literal_ip(target.literal_ip, extra)
        return  # literal IP handled above
    _check_resolved(target.host, await _resolve_all_async(target.host), extra)


async def resolve_pinned_ip(url: str, *, allow_networks: Sequence[str] | None = None) -> PinnedTarget:
    """Resolve and validate a URL, returning the pinned connect target.

    Mirrors the validation performed by :func:`validate_outbound_url_async` but
    *returns* the validated address so a caller can pin the connection to it.
    Fails closed if **any** resolved address is blocked, or if a canonical
    literal-IP target is private/internal. Returns a :class:`PinnedTarget` whose
    ``host`` is the original hostname (for SNI/cert) and ``ip`` is the address
    to connect to.
    """
    target = _parse_url_target(url)
    extra = normalize_allow_networks(allow_networks)
    if target.literal_ip is not None:
        if _is_blocked_ip(target.literal_ip, extra):
            raise ValueError(
                f"URL targets a private/internal network address: {target.literal_ip}. "
                "Use a public URL or add the address to SSRF_ALLOW_PRIVATE_RANGES."
            )
        return PinnedTarget(scheme=target.scheme, host=target.host, port=target.port, ip=target.literal_ip)
    ips = await _resolve_all_async(target.host)
    _check_resolved(target.host, ips, extra)
    return PinnedTarget(scheme=target.scheme, host=target.host, port=target.port, ip=ips[0])


class _PinnedAsyncNetworkBackend(httpcore.AnyIOBackend):
    """httpcore async backend that substitutes the pinned IP at connect time.

    ``connect_tcp`` is called by httpcore with the **origin hostname**; this
    backend forwards the connection to the pinned validated address instead. TLS
    SNI and certificate verification still use the origin hostname (handled by
    httpcore at a higher layer), so the URL is never rewritten to the IP.
    """

    def __init__(self, pinned_hosts: Mapping[str, str]) -> None:
        super().__init__()
        self._pinned_hosts = dict(pinned_hosts)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        connect_host = self._pinned_hosts.get(host, host)
        return await super().connect_tcp(
            connect_host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """An ``httpx.AsyncHTTPTransport`` pinned to validated addresses.

    Built with ``pinned_async_transport`` / ``pinned_async_client``. Pins the
    ``{hostname: validated_ip}`` mapping onto the underlying httpcore pool so the
    TCP connection goes to the validated IP while SNI/cert stay on the hostname.
    """

    def __init__(
        self,
        pinned_hosts: Mapping[str, str],
        verify: ssl.SSLContext | str | bool = True,
        http2: bool = False,
        trust_env: bool = True,
    ) -> None:
        super().__init__(verify=verify, http2=http2, trust_env=trust_env)
        self._pool._network_backend = _PinnedAsyncNetworkBackend(pinned_hosts)


async def pinned_async_transport(
    url: str,
    *,
    allow_networks: Sequence[str] | None = None,
    verify: ssl.SSLContext | str | bool = True,
    http2: bool = False,
    trust_env: bool = True,
) -> httpx.AsyncHTTPTransport:
    """Build a pinned-IP async transport for ``url``.

    Resolves and validates ``url`` (fails closed), then returns an
    :class:`httpx.AsyncHTTPTransport` that connects to the validated address
    while keeping the original hostname for TLS SNI and certificate
    verification. ``allow_networks`` layers a tenant-scoped CIDR allowlist on
    the global floor.
    """
    target = await resolve_pinned_ip(url, allow_networks=allow_networks)
    return PinnedAsyncHTTPTransport(
        {target.host: target.ip},
        verify=verify,
        http2=http2,
        trust_env=trust_env,
    )


async def pinned_async_client(
    url: str,
    *,
    allow_networks: Sequence[str] | None = None,
    verify: ssl.SSLContext | str | bool = True,
    http2: bool = False,
    trust_env: bool = True,
    timeout: float | httpx.Timeout | None = None,  # noqa: ASYNC109
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    """Build a pinned-IP ``httpx.AsyncClient`` for ``url``.

    The client's transport is pinned to the validated address while SNI/cert use
    the original hostname. Redirects default to not-followed; if
    ``follow_redirects`` is enabled the caller MUST re-validate each hop with
    :func:`resolve_pinned_ip` / :func:`validate_outbound_url_async` against the
    same policy (the pinned transport only protects the primary origin).
    """
    transport = await pinned_async_transport(
        url,
        allow_networks=allow_networks,
        verify=verify,
        http2=http2,
        trust_env=trust_env,
    )
    return httpx.AsyncClient(
        transport=transport,
        verify=verify,
        http2=http2,
        trust_env=trust_env,
        timeout=timeout,
        follow_redirects=follow_redirects,
    )
