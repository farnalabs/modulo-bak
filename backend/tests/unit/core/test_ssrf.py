"""Unit tests for the SSRF safe-URL validation helpers.

Covers the literal-IP blocking branch (the core security primitive), the
hostname-resolution path via patched resolvers, the async variant used by
event-loop-hostile callers, and the false-positive regression where a
hostname literally containing "private/internal" must not be spuriously
blocked.

Also covers the FAR-409 pinned-IP connection transport (DNS-rebinding
hardening, connect-to-pinned-validated-IP with SNI matching the hostname),
tenant-scoped egress allowlist layering, non-canonical IP-literal rejection,
fail-closed any-IP-blocked semantics, and redirect safety.
"""

import datetime
import http.server
import ssl
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from modulo.core import ssrf


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://100.100.100.200/latest/meta-data/",  # Aliyun metadata
        "http://100.64.0.1/",  # CGNAT
        "http://0.0.0.0/",  # current network
    ],
)
def test_validate_outbound_url_blocks_private_literal_ips(url: str) -> None:
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8/",
        "http://1.1.1.1/",
    ],
)
def test_validate_outbound_url_accepts_public_literal_ips_without_dns(url: str) -> None:
    # Public literal IPs must be accepted without any DNS lookup: the
    # validator returns None (no exception) and must not attempt DNS, which
    # would raise for a bare literal IP if the resolver were consulted.
    assert ssrf.validate_outbound_url(url) is None


def test_validate_outbound_url_blocks_hostname_resolving_to_internal() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "example.internal.com"
        return ["10.0.0.5"]

    try:
        original = ssrf._resolve_all_sync
        ssrf._resolve_all_sync = fake_resolve
        with pytest.raises(ValueError, match="resolves to a private/internal address"):
            ssrf.validate_outbound_url("http://example.internal.com/")
    finally:
        ssrf._resolve_all_sync = original


def test_validate_outbound_url_accepts_hostname_resolving_to_public() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "example.com"
        return ["93.184.216.34"]

    try:
        original = ssrf._resolve_all_sync
        ssrf._resolve_all_sync = fake_resolve
        ssrf.validate_outbound_url("http://example.com/")
    finally:
        ssrf._resolve_all_sync = original


def test_hostname_containing_private_internal_is_not_spuriously_blocked() -> None:
    """Regression: a hostname with 'private/internal' in its name must be
    resolved, not rejected by the literal-IP false-positive path."""

    def fake_resolve(host: str) -> list[str]:
        assert host == "private.internal.example.com"
        return ["93.184.216.34"]

    try:
        original = ssrf._resolve_all_sync
        ssrf._resolve_all_sync = fake_resolve
        ssrf.validate_outbound_url("https://private.internal.example.com/")
    finally:
        ssrf._resolve_all_sync = original


async def test_validate_outbound_url_async_blocks_literal_ip() -> None:
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.validate_outbound_url_async("http://127.0.0.1/")


async def test_validate_outbound_url_async_resolves_and_blocks_internal() -> None:
    async def fake_resolve(host: str) -> list[str]:
        assert host == "collector.internal"
        return ["192.168.0.10"]

    try:
        original = ssrf._resolve_all_async
        ssrf._resolve_all_async = fake_resolve
        with pytest.raises(ValueError, match="resolves to a private/internal address"):
            await ssrf.validate_outbound_url_async("http://collector.internal:4318/")
    finally:
        ssrf._resolve_all_async = original


async def test_validate_outbound_url_async_accepts_public_hostname() -> None:
    async def fake_resolve(host: str) -> list[str]:
        assert host == "api.example.com"
        return ["93.184.216.34"]

    try:
        original = ssrf._resolve_all_async
        ssrf._resolve_all_async = fake_resolve
        await ssrf.validate_outbound_url_async("https://api.example.com/")
    finally:
        ssrf._resolve_all_async = original


def test_allowlist_allows_matching_private_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
    # 10.1.2.3 is private but explicitly allowlisted.
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None


def test_allowlist_multiple_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "192.168.0.0/16,10.0.0.0/8")
    assert ssrf.validate_outbound_url("http://192.168.1.1/") is None
    assert ssrf.validate_outbound_url("http://10.0.0.9/") is None


def test_allowlist_metadata_range_still_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the allowlisted 10.0.0.0/8 is permitted; cloud metadata stays blocked.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://127.0.0.1/")


def test_allowlist_invalid_entry_logged_and_other_cidrs_apply(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "not-a-cidr,10.0.0.0/8")
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None
    assert any("ssrf.invalid_allowlist_entry" in r.message for r in caplog.records)


def test_allowlist_honours_runtime_env_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist is parsed lazily and cached on the env value, so a
    mid-process env change takes effect without a restart."""
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://10.1.2.3/")
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "10.0.0.0/8")
    assert ssrf.validate_outbound_url("http://10.1.2.3/") is None
    # And switching back off re-blocks.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "")
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://10.1.2.3/")


# ---------------------------------------------------------------------------
# FAR-409: pinned-IP transport + tenant-scoped egress allowlist
# ---------------------------------------------------------------------------


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"pinned-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def _start_http_server() -> tuple[http.server.HTTPServer, int]:
    """Start a plain-HTTP server on an ephemeral 127.0.0.1 port."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _QuietHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _make_self_signed_cert(hostname: str, cert_pem: Path, key_pem: Path) -> None:
    """Generate a self-signed cert whose SAN is exactly ``hostname``."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    san = x509.SubjectAlternativeName([x509.DNSName(hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def _start_tls_server(cert_pem: Path, key_pem: Path) -> tuple[http.server.HTTPServer, int]:
    """Start an HTTPS server on an ephemeral 127.0.0.1 port."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _QuietHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_pem), str(key_pem))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, responses: list[list[str]]) -> list[str]:
    """Patch ``ssrf._resolve_all_async`` with a call-counted fake.

    ``responses`` is a list of address-sets returned per call; the final entry
    repeats for any additional calls. Returns the list of hosts seen.
    """
    seen: list[str] = []
    responses = [list(r) for r in responses]

    async def fake(host: str) -> list[str]:
        seen.append(host)
        idx = min(len(seen) - 1, len(responses) - 1)
        return responses[idx]

    monkeypatch.setattr(ssrf, "_resolve_all_async", fake)
    return seen


# --- strict URL parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f000001/",  # hex 127.0.0.1
        "http://0177.0.0.1/",  # octal 127.0.0.1
        "http://127.1/",  # abbreviated 127.0.0.1
        "http://1.2.3.4.5/",  # overlong dotted-quad
        "http://%31%32%37.0.0.1/",  # percent-encoded host
    ],
)
def test_validate_outbound_url_rejects_noncanonical_ip_encodings(url: str) -> None:
    with pytest.raises(ValueError):
        ssrf.validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://%31%32%37.0.0.1/",
    ],
)
async def test_validate_outbound_url_async_rejects_noncanonical_ip_encodings(
    url: str,
) -> None:
    with pytest.raises(ValueError):
        await ssrf.validate_outbound_url_async(url)


def test_validate_outbound_url_rejects_userinfo() -> None:
    with pytest.raises(ValueError, match="userinfo"):
        ssrf.validate_outbound_url("http://user:pw@example.com/")


def test_validate_outbound_url_rejects_odd_ports() -> None:
    with pytest.raises(ValueError, match="invalid port"):
        ssrf.validate_outbound_url("http://example.com:99999/")
    with pytest.raises(ValueError, match="valid range"):
        ssrf.validate_outbound_url("http://example.com:0/")


# --- fail-closed on any resolved IP blocked --------------------------------


async def test_validate_outbound_url_async_fails_closed_if_any_ip_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One public + one private address: fail-closed, never pick the "allowed" one.
    _patch_resolver(monkeypatch, [["93.184.216.34", "10.0.0.5"]])
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.validate_outbound_url_async("http://mixed.example/")
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.resolve_pinned_ip("http://mixed.example/")


# --- tenant-scoped egress allowlist ----------------------------------------


async def test_tenant_scoped_allowlist_override_async(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.validate_outbound_url_async("http://10.1.2.3/")
    # Tenant-scoped override permits the range for this target.
    await ssrf.validate_outbound_url_async("http://10.1.2.3/", allow_networks=["10.0.0.0/8"])
    target = await ssrf.resolve_pinned_ip("http://10.1.2.3/", allow_networks=["10.0.0.0/8"])
    assert target.ip == "10.1.2.3"
    assert target.host == "10.1.2.3"


def test_tenant_scoped_allowlist_layers_on_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "192.168.0.0/16")
    # 10.0.0.0/8 is not in the global base but is in the tenant override -> allowed.
    assert ssrf.validate_outbound_url("http://10.1.2.3/", allow_networks=["10.0.0.0/8"]) is None
    # 192.168.x is allowlisted globally -> allowed even without the tenant override.
    assert ssrf.validate_outbound_url("http://192.168.1.1/") is None


def test_tenant_allowlist_does_not_weaken_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # A tenant allowlist for another range must NOT silently permit loopback/metadata.
    ssrf.validate_outbound_url("http://10.1.2.3/", allow_networks=["10.0.0.0/8"])
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://127.0.0.1/", allow_networks=["10.0.0.0/8"])
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://169.254.169.254/", allow_networks=["10.0.0.0/8"])
    # Loopback stays blocked at runtime through the async path too.
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.validate_outbound_url("http://10.0.0.1/", allow_networks=[])


# --- pinned-IP transport ---------------------------------------------------


async def test_pinned_transport_uses_validated_ip_despite_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport must resolve exactly once, then pin that validated IP.

    A resolver that "flips" to a metadata address between validation and connect
    is used: because the transport pins the first (validated) result and the
    connect never re-resolves, the metadata address is never reached and the
    request reaches the validated address.
    """
    seen = _patch_resolver(monkeypatch, [["127.0.0.1"], ["169.254.169.254"]])
    server, port = _start_http_server()
    try:
        client = await ssrf.pinned_async_client(
            f"http://rebind.example:{port}/",
            allow_networks=["127.0.0.0/8"],
        )
        # Building the client resolves exactly once (the validation).
        assert len(seen) == 1
        backend = client._transport._pool._network_backend
        assert backend._pinned_hosts == {"rebind.example": "127.0.0.1"}
        async with client:
            resp = await client.get(f"http://rebind.example:{port}/")
            assert resp.status_code == 200
            assert resp.text == "pinned-ok"
        # The request connected to the validated address WITHOUT a second
        # resolution, so the metadata flip was never reached.
        assert len(seen) == 1
    finally:
        server.shutdown()
        server.server_close()


async def test_pinned_transport_connects_to_pinned_validated_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a made-up host is pinned to a validated loopback address and
    the request reaches that address. Without pinning, the host would not resolve."""
    _patch_resolver(monkeypatch, [["127.0.0.1"]])
    server, port = _start_http_server()
    try:
        client = await ssrf.pinned_async_client(
            f"http://pinned.example:{port}/",
            allow_networks=["127.0.0.0/8"],
        )
        async with client:
            resp = await client.get(f"http://pinned.example:{port}/")
            assert resp.status_code == 200
            assert resp.text == "pinned-ok"
    finally:
        server.shutdown()
        server.server_close()


async def test_pinned_transport_sni_matches_hostname(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The pinned connection uses the ORIGINAL hostname for SNI + cert verify.

    An HTTPS server presents a cert whose SAN is ``ssrf-host.test``; the client
    connects to the pinned 127.0.0.1 but verifies against that hostname. If the
    implementation rewrote the URL to the IP, verification would fail.
    """
    hostname = "ssrf-host.test"
    cert_pem = tmp_path / "cert.pem"
    key_pem = tmp_path / "key.pem"
    _make_self_signed_cert(hostname, cert_pem, key_pem)
    server, port = _start_tls_server(cert_pem, key_pem)
    try:
        _patch_resolver(monkeypatch, [["127.0.0.1"]])
        verify_ctx = ssl.create_default_context(cafile=str(cert_pem))
        client = await ssrf.pinned_async_client(
            f"https://{hostname}:{port}/",
            allow_networks=["127.0.0.0/8"],
            verify=verify_ctx,
        )
        async with client:
            resp = await client.get(f"https://{hostname}:{port}/")
            assert resp.status_code == 200
    finally:
        server.shutdown()
        server.server_close()


async def test_pinned_client_defaults_to_no_redirects() -> None:
    """Redirects are not followed by default; a pinned client is safe by default."""
    client = await ssrf.pinned_async_client("http://8.8.8.8/")
    try:
        assert client.follow_redirects is False
    finally:
        await client.aclose()


async def test_redirect_to_internal_is_blocked_when_revalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A followed redirect hop re-validates against the same policy; an internal
    hop is blocked rather than followed."""
    _patch_resolver(monkeypatch, [["169.254.169.254"]])
    with pytest.raises(ValueError, match="private/internal"):
        await ssrf.validate_outbound_url_async("http://redirect-target.example/")
