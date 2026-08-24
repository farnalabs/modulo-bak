"""Unit tests for the generic REST connector (FAR-408)."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.rest import RestConnector
from tests.connectors._conformance import assert_result_shape, assert_write_result_shape


def _default_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


def _make_connector(
    config: dict[str, Any] | None,
    creds: dict[str, Any] | None,
) -> RestConnector:
    """Build a RestConnector against a stub HTTP transport (no real network)."""
    return RestConnector(
        config,
        creds,
        transport=httpx.MockTransport(_default_handler),
        ssrf_validator=lambda url: None,
    )


# ── Connector type + capabilities ──────────────────────────────────────────


def test_connector_type_is_rest() -> None:
    c = RestConnector({"base_url": "https://api.example.com", "path": "/items"}, {"auth_mode": "bearer", "token": "t"})
    assert c.connector_type is ConnectorType.REST
    assert ConnectorType.REST.capabilities == frozenset({"read", "write"})


# ── Query: records transform ────────────────────────────────────────────────


def test_query_records_path_extracts_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "records_path": "data.items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 3
    assert [r["id"] for r in result.records] == [1, 2, 3]
    assert result.total == 3


def test_query_top_level_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"a": 1}, {"a": 2}])

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 2


def test_query_passthrough_raw_body_feeds_shape() -> None:
    """Raw/passthrough content-type still yields a list-of-dicts record shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="a,b\n1,2", headers={"content-type": "text/csv"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "passthrough": True},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 1
    assert result.records[0]["content_type"] == "text/csv"
    assert result.records[0]["body"] == "a,b\n1,2"


def test_query_unknown_resource_raises() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "operations": {"users": {"path": "/items"}}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="Unsupported REST resource"):
        asyncio_run(c.query(ConnectorQuery(resource="nope")))


def test_query_empty_resource_raises() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError):
        asyncio_run(c.query(ConnectorQuery(resource="")))


# ── Query: templating from filters ─────────────────────────────────────────


def test_query_templates_url_and_params_from_filters() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users/{{ user_id }}",
            "params": {"page": "{{ page }}"},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default", filters={"user_id": 42, "page": 2})))
    assert str(captured["url"]) == "https://api.example.com/users/42?page=2"
    assert captured["params"] == {"page": "2"}


# ── Auth modes ─────────────────────────────────────────────────────────────


def test_auth_bearer() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "sec-token"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["authorization"] == "Bearer sec-token"


def test_auth_api_key_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "k123", "in": "header", "header_name": "X-API-Key"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["x-api-key"] == "k123"


def test_auth_api_key_query() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "k456", "in": "query", "query_param_name": "api_key"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["params"] == {"api_key": "k456"}


def test_auth_basic() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "basic", "username": "u", "password": "p"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["authorization"].startswith("Basic ")


# ── Injection guard ────────────────────────────────────────────────────────


def test_guard_rejects_auth_header_override() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "headers": {"Authorization": "evil"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="protected header"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_guard_rejects_crlf_in_header_value() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "headers": {"X-Foo": "ok\r\nX-Evil: 1"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="control characters"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_guard_rejects_host_not_in_allowlist() -> None:
    c = _make_connector(
        {
            "base_url": "https://evil.example.com",
            "path": "/items",
            "allowed_hosts": ["api.example.com"],
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="not in allowed_hosts"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_guard_rejects_non_http_scheme() -> None:
    c = _make_connector(
        {"base_url": "file:///etc/passwd", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="scheme"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


# ── Write ──────────────────────────────────────────────────────────────────


def test_write_posts_json_and_returns_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.read() or request.content
        return httpx.Response(200, json={"method": request.method, "name": json.loads(raw)["name"]})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users", "body": {"name": "{{ name }}"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Ada"})))
    assert_write_result_shape(result)
    assert result["method"] == "POST"
    assert result["name"] == "Ada"


# ── Health check ───────────────────────────────────────────────────────────


def test_health_check_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is True


# ── ConnectorHub multi-field creds round-trip ──────────────────────────────


def test_hub_ciphertext_round_trips_multi_field_json_dict() -> None:
    """A JSON-dict credentials_ciphertext round-trips as multi-field creds (REST)."""
    from cryptography.fernet import Fernet

    from modulo.core.connector_hub import ConnectorHub
    from modulo.core.secrets_backend import create_secrets_backend

    key = Fernet.generate_key().decode()
    creds = {"auth_mode": "bearer", "token": "secret-token"}
    ciphertext = Fernet(key.encode()).encrypt(json.dumps(creds).encode())

    class _CI:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.connector_type_id = "rest"
            self.config_json = {"base_url": "https://api.example.com", "path": "/items"}
            self.credentials_ciphertext = ciphertext
            self.visibility = "org"
            self.allowed_operations = None

    ci = _CI()
    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = key
        hub = ConnectorHub(secrets_backend=backend)
        asyncio_run(hub.initialise([ci]))

    connector = hub.get(ci.id)
    assert isinstance(connector._inner, RestConnector)
    assert connector.connector_type is ConnectorType.REST
    assert connector._inner._auth["mode"] == "bearer"
    assert connector._inner._auth["token"] == "secret-token"


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
