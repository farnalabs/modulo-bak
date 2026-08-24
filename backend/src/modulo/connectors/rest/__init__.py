"""RESTConnector — generic verb-agnostic REST integration connector (FAR-408).

A declarative connector that lets a ConnectorInstance declare an arbitrary
HTTP endpoint (URL, method, headers, body, records extraction) and have pipeline
nodes call it with runtime variables rendered into the request. It is the
``ConnectorType.REST`` implementation of the FAR-401 "point Modulo at any
external system" design: no per-vendor client, just a templated HTTP call.

CONFIG SHAPE (``config_json``)
------------------------------
A single connector instance describes ONE endpoint (or a map of named
resources, each with its own endpoint). All template fields are rendered with
Jinja2 (``SandboxedEnvironment`` — the same sandbox ``node_runner`` uses) against
the runtime variables supplied per call:
``ConnectorQuery.filters`` for ``query()`` and ``ConnectorPayload.data`` for
``write()``.

    base_url:      "https://api.example.com"            # required
    method:        "GET"                                # default verb (query path)
    path:          "/v1/users/{{ user_id }}"            # URL template
    headers:       {"Accept": "application/json"}       # header templates
    params:        {"page": "{{ page }}"}               # query params (URL-encoded by httpx)
    body:          {"name": "{{ name }}"}               # JSON body template (write path)
    operations:    { "<resource>": { "method": ..., "path": ..., "headers": {},
                                      "params": {}, "body": {}, "records_path": ...,
                                      "next_cursor_path": ..., "passthrough": ... } }
    records_path:      "data.items"                     # dot-path into JSON response for records
    next_cursor_path:  "data.next_cursor"               # optional pagination cursor (no JMESPath)
    allowed_hosts:     ["api.example.com"]              # optional scheme/host allowlist
    passthrough:       false                            # wrap non-JSON/raw bodies as a single record

VERB-AGNOSTIC READ/WRITE MAPPING
--------------------------------
A REST connector is verb-agnostic, so the capability contract (``read`` /
``write``) maps onto the two public surfaces, not onto any single HTTP verb:

* ``query()`` is the **read** surface — ``_TracedConnector`` calls it with
  ``acl_operation="read"``. It performs the operation's configured verb
  (default ``GET`` ; safe, idempotent verbs make sense here).
* ``write()`` is the **write** surface — ``_TracedConnector`` calls it with
  ``acl_operation="write"``. It performs the operation's configured verb
  (default ``POST`` ; mutable verbs belong here).

A ``PUT`` / ``DELETE`` / ``PATCH`` is neither cleanly read nor write, but it
*mutates* the remote system, so it belongs on the **write** surface (ACL
``write``). The connector does not infer semantics from the verb — the author
declares the method; the surface (query/write) fixes the ACL gate. The
capability set is ``{Capability.READ, Capability.WRITE}``.

AUTH MODES (``credentials_ciphertext`` / ``creds`` dict)
-------------------------------------------------------
Credentials are stored as an ENCRYPTED **JSON dict** (matching the
``secrets_backend`` JSON-dict shape) so multi-field creds round-trip — never the
single ``api_key`` fallback. Read ``auth_mode`` + named fields from that dict:

    auth_mode:  "bearer" | "api_key" | "basic"
    # bearer ->  token
    # api_key -> api_key + header_name (default "X-API-Key") and/or
    #            in: "header" (default) | "query" + query_param_name (default "api_key")
    # basic   -> username + password

Templating uses the existing ``node_runner`` ``jinja2.sandbox.SandboxedEnvironment``;
the only runtime dependencies added here are ``httpx`` and ``jinja2``.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from jinja2.sandbox import SandboxedEnvironment

from modulo.connectors._retry_headers import parse_retry_after
from modulo.connectors._safe_page import safe_records_list
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_log = logging.getLogger(__name__)

# Standard HTTP verbs the connector will issue (all else is rejected).
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

# Auth/transport headers the user/agent must never be able to override through a
# rendered request header (FAR-408 injection guard). ``host``/``content-length``
# are derived by httpx, so a rendered override would corrupt the request.
_AUTH_PROTECTED_HEADERS = frozenset({"authorization", "proxy-authorization", "host", "content-length"})

# C0 control chars (minus tab, which is legal in header values) + DEL.
_CONTROL_CHARS = frozenset({chr(c) for c in range(0x20) if c != 0x09} | {"\x7f"})

_DEFAULT_TIMEOUT = 30.0

SsrfValidator = Callable[[str], Any]


def _dot_get(data: Any, path: str | None) -> Any:
    """Navigate a dot-path (``"data.items"``) into a JSON structure.

    Lists are indexable via a numeric segment (``"items.0"``). Replaces JMESPath
    so the connector adds no JQ-style runtime dependency; the fields that carry
    it are the connector-only ``records_path`` / ``next_cursor_path``.
    """
    if not path:
        return data
    current = data
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            idx = int(segment)
            current = current[idx] if 0 <= idx < len(current) else None
        else:
            return None
    return current


def _collect_strings(value: Any) -> list[str]:
    """Collect every leaf string inside *value* (dicts, lists, scalars)."""
    result: list[str] = []
    if isinstance(value, str):
        result.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            result.extend(_collect_strings(v))
    elif isinstance(value, list):
        for v in value:
            result.extend(_collect_strings(v))
    return result


def _reject_control_chars(value: str, *, what: str) -> None:
    """Raise ValueError if *value* contains CR/LF or other control characters."""
    bad = _CONTROL_CHARS.intersection(value)
    if bad:
        offending = " ".join(repr(c) for c in sorted(bad))
        raise ValueError(f"REST {what} contains control characters (header injection): {offending}")


def _filter_injection_strings(values: list[str], *, resource: str) -> None:
    """Reject LLM-output injection markers in any string that will reach the wire.

    The write path already applies ``filter_payload_for_injection`` to the
    ``ConnectorPayload``; the query path has no payload, so this applies the
    same ``filter_output_for_injection`` check to every rendered string that
    goes into the request (URL, params, headers, body). Reuses the existing
    output-filter machinery —     no new tooling.
    """
    # Lazy import: output_filter sits behind modulo.core.pipeline_engine, whose
    # package __init__ imports the executor -> connector_hub -> connectors.
    # Importing it at module load here would create a circular import, so it is
    # resolved only when a request is actually built.
    from modulo.core.pipeline_engine.output_filter import OutputRejectedError, filter_output_for_injection

    for value in values:
        result = filter_output_for_injection(value)
        if not result.passed:
            raise OutputRejectedError(f"{result.reason} (REST resource: {resource!r})")


class RestConnector(ConnectorBase):
    """A declarative, verb-agnostic REST connector.

    ``config`` is the ``config_json`` and ``creds`` the decrypted credentials
    dict (see the module docstring for both shapes). ``transport`` and
    ``ssrf_validator`` are test seams — production callers pass neither.
    """

    def __init__(
        self,
        config: dict[str, Any] | None,
        creds: dict[str, Any] | None,
        *,
        transport: httpx.BaseTransport | None = None,
        ssrf_validator: SsrfValidator | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._config = config or {}
        self._creds = creds or {}
        self._transport = transport
        self._timeout = float(timeout or _DEFAULT_TIMEOUT)
        self._ssrf_validator = ssrf_validator
        self._base_url = str(self._config.get("base_url", "")).rstrip("/")
        if not self._base_url:
            raise ValueError("REST connector requires 'base_url' in config_json")
        self._env = SandboxedEnvironment()
        self._auth = self._normalise_auth(self._creds)

    # ── ConnectorBase surface ──────────────────────────────────────────────

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.REST

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": self._timeout, "follow_redirects": False}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def health_check(self) -> HealthResult:
        """Verify the target is reachable and the credentials are accepted.

        Issues a ``HEAD`` (falling back to a ``GET``) against ``base_url``. A
        sub-400 status means the endpoint + credentials are live; any other
        status is a non-OK result. Never raises — like the other connectors.
        """
        try:
            async with self._client() as client:
                resp: httpx.Response | None = None
                for method in ("HEAD", "GET"):
                    resp = await client.request(method, self._base_url)
                    if resp.status_code < 400:
                        return HealthResult(ok=True, detail=f"HTTP {resp.status_code}: {self._base_url}")
                assert resp is not None
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {self._base_url}")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return HealthResult(ok=False, detail=f"Cannot connect to {self._base_url}: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        """Read surface. Renders the operation from ``q.filters`` and issues it."""
        resource = q.resource
        self._require_resource(resource)
        context = dict(q.filters or {})
        context.setdefault("resource", resource)
        request = await self._build_request(resource, context, surface="read")
        return await self._send_and_transform(request)

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Write surface. Renders the operation from ``payload.data`` and issues it."""
        resource = payload.resource
        self._require_resource(resource)
        context = dict(payload.data or {})
        context.setdefault("resource", resource)
        request = await self._build_request(resource, context, surface="write")
        return await self._send_write(request)

    # ── Operation resolution ───────────────────────────────────────────────

    def _require_resource(self, resource: str) -> None:
        if resource is None or resource == "":
            raise ValueError("REST connector requires a resource name")
        ops = self._config.get("operations")
        if isinstance(ops, dict) and ops and resource not in ops:
            raise ValueError(f"Unsupported REST resource: {resource!r}")

    def _operation_spec(self, resource: str, *, default_method: str) -> dict[str, Any]:
        ops = self._config.get("operations")
        spec: dict[str, Any] = {}
        if isinstance(ops, dict) and resource in ops and isinstance(ops[resource], dict):
            spec = ops[resource]
        method = str(spec.get("method") or self._config.get("method") or default_method).upper()
        if method not in _ALLOWED_METHODS:
            raise ValueError(f"REST method {method!r} is not allowed (expected one of {sorted(_ALLOWED_METHODS)})")
        return {
            "method": method,
            "path": spec.get("path") if "path" in spec else self._config.get("path"),
            "headers": spec.get("headers") if "headers" in spec else self._config.get("headers", {}),
            "params": spec.get("params") if "params" in spec else self._config.get("params", {}),
            "body": spec.get("body", self._config.get("body")) if "body" in spec else self._config.get("body"),
            "records_path": spec.get("records_path", self._config.get("records_path")),
            "next_cursor_path": spec.get("next_cursor_path", self._config.get("next_cursor_path")),
            "passthrough": bool(spec.get("passthrough", self._config.get("passthrough", False))),
        }

    # ── Auth ───────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_auth(creds: dict[str, Any]) -> dict[str, Any]:
        mode = str(creds.get("auth_mode", "")).strip().lower()
        if mode not in {"bearer", "api_key", "basic"}:
            raise ValueError(
                f"REST connector requires creds['auth_mode'] to be one of 'bearer', 'api_key', 'basic' — got {mode!r}"
            )
        auth: dict[str, Any] = {"mode": mode}
        if mode == "bearer":
            if not creds.get("token"):
                raise ValueError("REST bearer auth requires creds['token']")
            auth["token"] = str(creds["token"])
        elif mode == "basic":
            if not creds.get("username") or not creds.get("password"):
                raise ValueError("REST basic auth requires creds['username'] and creds['password']")
            auth["username"] = str(creds["username"])
            auth["password"] = str(creds["password"])
        else:  # api_key
            if not creds.get("api_key"):
                raise ValueError("REST api_key auth requires creds['api_key']")
            auth["api_key"] = str(creds["api_key"])
            auth["in"] = str(creds.get("in", "header")).lower()
            if auth["in"] not in {"header", "query"}:
                raise ValueError(f"REST api_key auth 'in' must be 'header' or 'query' — got {auth['in']!r}")
            if auth["in"] == "header":
                auth["header_name"] = str(creds.get("header_name", "X-API-Key"))
            else:
                auth["query_param_name"] = str(creds.get("query_param_name", "api_key"))
        return auth

    @property
    def _protected_header_names(self) -> frozenset[str]:
        """Set of header names (lowercased) the rendered headers may not override."""
        names = set(_AUTH_PROTECTED_HEADERS)
        if self._auth["mode"] == "api_key" and self._auth["in"] == "header":
            names.add(self._auth["header_name"].lower())
            names.add("x-api-key")
        return frozenset(names)

    def _auth_query_params(self) -> dict[str, Any]:
        if self._auth["mode"] == "api_key" and self._auth["in"] == "query":
            return {self._auth["query_param_name"]: self._auth["api_key"]}
        return {}

    def apply_auth(self, headers: dict[str, str]) -> dict[str, str]:
        """Inject the credential headers into *headers* (post-injection-guard)."""
        mode = self._auth["mode"]
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {self._auth['token']}"
        elif mode == "basic":
            raw = f"{self._auth['username']}:{self._auth['password']}"
            headers["Authorization"] = f"Basic {base64.b64encode(raw.encode()).decode()}"
        elif mode == "api_key" and self._auth["in"] == "header":
            headers[self._auth["header_name"]] = self._auth["api_key"]
        return headers

    # ── Templating ─────────────────────────────────────────────────────────

    def _render(self, value: Any, context: dict[str, Any]) -> Any:
        """Recursively render Jinja template strings in *value* against *context*.

        Non-string leaves are returned unchanged so structured bodies and lists
        round-trip; only the string templates are rendered. Undefined variables
        render to empty under the sandbox's default ``Undefined`` (the same
        lenient behaviour ``node_runner`` relies on).
        """
        if isinstance(value, str):
            try:
                return self._env.from_string(value).render(**context)
            except Exception as exc:  # sandbox raises on unsafe access
                raise ValueError(f"REST template error: {exc}") from exc
        if isinstance(value, dict):
            return {k: self._render(v, context) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render(v, context) for v in value]
        return value

    # ── Request builder (injection guard) ──────────────────────────────────

    async def _build_request(
        self,
        resource: str,
        context: dict[str, Any],
        *,
        surface: str,
    ) -> dict[str, Any]:
        """Render the operation into a validated request dict.

        The injection guard runs "write-path-style" filtering on EVERY surface:
        (a) header names/values must not contain CR/LF or control chars;
        (b) rendered headers may not override auth/transport headers;
        (c) the target URL must pass scheme/host allowlist + SSRF validation;
        (d) the rendered URL/params/headers/body are screened with the same
        ``filter_output_for_injection`` the write path uses.
        """
        default_method = "GET" if surface == "read" else "POST"
        spec = self._operation_spec(resource, default_method=default_method)

        headers: dict[str, str] = {}
        rendered_headers = self._render(spec["headers"], context)
        if isinstance(rendered_headers, dict):
            for name, value in rendered_headers.items():
                name_str = str(name)
                value_str = str(value)
                _reject_control_chars(name_str, what="header name")
                _reject_control_chars(value_str, what="header value")
                if name_str.lower() in self._protected_header_names:
                    raise ValueError(f"REST rendered header overrides protected header {name_str!r}")
                headers[name_str] = value_str

        params: dict[str, Any] = {}
        rendered_params = self._render(spec["params"], context)
        if isinstance(rendered_params, dict):
            for key, value in rendered_params.items():
                if value is not None:
                    params[str(key)] = value
        # API-key-in-query creds are applied after the guard (never overridable).
        params.update({k: v for k, v in self._auth_query_params().items() if k not in params})

        body: Any = None
        if spec["body"] is not None:
            body = self._render(spec["body"], context)

        path = spec["path"]
        if path is None or path == "":
            raise ValueError("REST connector requires a 'path' in config_json (or per-resource operation)")
        url = self._base_url + str(self._render(path, context))

        await self._validate_target_url(url)

        # Write-path-style injection screening of everything that reaches the wire.
        screened: list[str] = [url]
        screened.extend(headers.values())
        screened.extend(str(v) for v in params.values())
        screened.extend(_collect_strings(body))
        _filter_injection_strings(screened, resource=resource)

        return {
            "method": spec["method"],
            "url": url,
            "headers": headers,
            "params": params,
            "body": body,
            "records_path": spec["records_path"],
            "next_cursor_path": spec["next_cursor_path"],
            "passthrough": spec["passthrough"],
        }

    async def _validate_target_url(self, url: str) -> None:
        """Enforce the scheme/host allowlist and SSRF safety on *url*.

        Scheme must be ``http``/``https``. When ``config['allowed_hosts']`` is
        set, the host must be in that list (or a subdomain of an entry). Always
        runs SSRF validation (default ``validate_outbound_url_async``) to block
        private/loopback/metadata targets — unless a test seam is injected.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"REST URL must use http:// or https:// scheme — got {parsed.scheme!r}")
        host = (parsed.hostname or "").rstrip(".").strip("[]")
        if not host:
            raise ValueError(f"REST URL must have a hostname: {url!r}")
        allowed = self._config.get("allowed_hosts")
        if (
            isinstance(allowed, list)
            and allowed
            and not any(host == str(allowed_host) or host.endswith("." + str(allowed_host)) for allowed_host in allowed)
        ):
            raise ValueError(f"REST URL host {host!r} is not in allowed_hosts: {allowed!r}")
        if self._ssrf_validator is not None:
            result = self._ssrf_validator(url)
            if inspect.isawaitable(result):
                await result
            return
        from modulo.core.ssrf import validate_outbound_url_async

        await validate_outbound_url_async(url)

    # ── Send + transform ───────────────────────────────────────────────────

    async def _send_and_transform(self, request: dict[str, Any]) -> ConnectorResult:
        async with self._client() as client:
            resp = await self._request(client, request)
            return self._transform(resp, request)

    async def _send_write(self, request: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            resp = await self._request(client, request)
            return self._write_result(resp)

    async def _request(self, client: httpx.AsyncClient, request: dict[str, Any]) -> httpx.Response:
        kwargs: dict[str, Any] = {
            "headers": self.apply_auth(dict(request["headers"])),
            "params": dict(request["params"]),
        }
        body = request["body"]
        if body is not None:
            if isinstance(body, (dict, list)):
                kwargs["json"] = body
            else:
                kwargs["content"] = str(body)
        resp = await client.request(request["method"], request["url"], **kwargs)
        if resp.status_code >= 400:
            raise ValueError(f"REST HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    def _transform(self, resp: httpx.Response, request: dict[str, Any]) -> ConnectorResult:
        """Map a REST response onto :class:`ConnectorResult`.

        JSON responses yield a list of dicts for ``records`` (via
        ``records_path`` when configured, else a top-level array, else the whole
        object as a single record). Raw/passthrough content-types (text, CSV,
        binary) yield a single ``{"body": ..., "content_type": ...,
        "status_code": ..., "headers": ...}`` record so the downstream JMESPath
        consumer still gets a uniform list-of-dicts shape.
        """
        content_type = resp.headers.get("content-type", "")
        body_text = resp.text
        parsed: Any = None
        if "json" in content_type.lower() or body_text.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(body_text)
            except json.JSONDecodeError:
                parsed = None

        records: list[dict[str, Any]] = []
        next_cursor: str | None = None
        if isinstance(parsed, list):
            records = safe_records_list(parsed)
        elif isinstance(parsed, dict):
            records_path = request.get("records_path")
            if records_path:
                source = _dot_get(parsed, records_path)
                if isinstance(source, list):
                    records = safe_records_list(source)
                elif isinstance(source, dict):
                    records = [source]
            else:
                records = [parsed] if parsed else []
            next_cursor = self._extract_cursor(resp, parsed, request.get("next_cursor_path"))
        elif parsed is None and not request.get("records_path"):
            records = self._passthrough_record(resp, body_text, content_type)

        metadata: dict[str, Any] = {
            "status_code": resp.status_code,
            "content_type": content_type,
            "url": str(request["url"]),
            "method": str(request["method"]),
        }
        retry_after = parse_retry_after(resp)
        if retry_after is not None:
            metadata["retry_after"] = retry_after
        return ConnectorResult(
            records=records,
            next_cursor=next_cursor,
            total=len(records),
            metadata=metadata,
        )

    def _passthrough_record(
        self,
        resp: httpx.Response,
        body_text: str,
        content_type: str,
    ) -> list[dict[str, Any]]:
        return [
            {
                "body": body_text,
                "content_type": content_type,
                "status_code": resp.status_code,
                "headers": dict(resp.headers.items()),
            }
        ]

    @staticmethod
    def _extract_cursor(resp: httpx.Response, parsed: dict[str, Any], path: str | None) -> str | None:
        if not path:
            return None
        value = _dot_get(parsed, path)
        return str(value) if isinstance(value, str) and value else None

    def _write_result(self, resp: httpx.Response) -> dict[str, Any]:
        """Map a REST write response onto a JSON-serialisable result dict."""
        body_text = resp.text
        if body_text:
            try:
                parsed = json.loads(body_text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"records": parsed}
        return {
            "status_code": resp.status_code,
            "body": body_text,
            "content_type": resp.headers.get("content-type", ""),
        }
