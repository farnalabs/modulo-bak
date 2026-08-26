"""FAR-442: shared Redis per-destination rate limiter wired into the trigger/polling path.

A trigger-invoked REST connector must enforce the SAME fleet-wide shared budget as a
run-executor connector (not a per-process local bucket). These tests build the
connector through ``trigger_engine.polling._build_polling_connector`` (the trigger fire
path) and assert:

* the ``redis_client`` / ``tenant_id`` (org id) are threaded into the connector;
* the shared budget is enforced across simulated workers (no lost-token race);
* each tenant/org gets its own budget (cross-org isolation, no ``"default"`` key);
* a shared limiter never falls back to the per-process bucket when Redis is configured;
* a Redis outage fails closed (SharedBudgetUnavailableError) rather than minting a token.

No real Redis is used - the fake client reproduces the Lua token-bucket semantics
atomically, exactly as in ``tests/unit/connectors/test_rest_observability.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from redis.exceptions import RedisError

from modulo.connectors._rate_bucket import SharedBudgetUnavailableError
from modulo.core.trigger_engine.polling import _build_polling_connector


def _rest_config() -> dict[str, Any]:
    return {
        "base_url": "https://api.example.com",
        "path": "/widgets",
        "rate_limit": {"requests_per_second": 0.0001, "burst": 3},
    }


_REST_CREDS: dict[str, Any] = {"auth_mode": "bearer", "token": "t"}


class _FakeRedis:
    """Atomic fake of the Lua ``_CONSUME_LUA`` token bucket shared across clients."""

    def __init__(self, store: dict[str, dict[str, float]] | None = None, clock: Any = None) -> None:
        self._store: dict[str, dict[str, float]] = store if store is not None else {}
        self._lock = asyncio.Lock()
        self._clock = clock

    def register_script(self, script: str) -> Any:
        of_self = self

        async def run(keys: list[str], args: list[Any]) -> int:
            key = keys[0]
            rate = float(args[0])
            burst = float(args[1])
            cost = float(args[2])
            now = of_self._clock() if of_self._clock is not None else 1000.0
            async with of_self._lock:
                st = of_self._store.get(key)
                if st is None:
                    st = {"tokens": burst, "ts": now}
                elapsed = max(0.0, now - st["ts"])
                st["tokens"] = min(burst, st["tokens"] + elapsed * rate)
                st["ts"] = now
                if st["tokens"] >= cost:
                    st["tokens"] -= cost
                    of_self._store[key] = st
                    return 1
                of_self._store[key] = st
                return 0

        return run


class _BrokenRedis:
    """A Redis client whose script execution always fails (unavailable)."""

    def register_script(self, script: str) -> Any:
        async def run(keys: list[str], args: list[Any]) -> int:
            raise RedisError("Redis unavailable")

        return run


# Wiring: the polling builder threads redis_client + tenant_id into the connector.


def test_polling_builder_wires_shared_redis_and_tenant_into_rest_connector() -> None:
    """The trigger fire path passes redis_client + tenant_id (org) to the REST connector."""
    redis = _FakeRedis({})
    connector = _build_polling_connector("rest", _rest_config(), _REST_CREDS, redis_client=redis, tenant_id="org-abc")
    assert connector._redis_client is redis
    assert connector._tenant_id == "org-abc"


def test_polling_builder_without_redis_stays_per_process() -> None:
    """Without a redis_client the trigger connector stays on the local bucket."""
    connector = _build_polling_connector("rest", _rest_config(), _REST_CREDS)
    assert connector._redis_client is None
    assert connector._tenant_id is None


# Shared budget enforced on the trigger path.


async def test_trigger_rest_connector_enforces_shared_redis_budget_across_workers() -> None:
    """Two trigger connectors (two simulated workers) share ONE Redis budget."""
    store: dict[str, dict[str, float]] = {}
    redis = _FakeRedis(store)

    worker_a = _build_polling_connector("rest", _rest_config(), _REST_CREDS, redis_client=redis, tenant_id="org-1")
    worker_b = _build_polling_connector("rest", _rest_config(), _REST_CREDS, redis_client=redis, tenant_id="org-1")

    limiter_a = worker_a._get_rate_limiter(0.0001, 3)
    limiter_b = worker_b._get_rate_limiter(0.0001, 3)

    results = []
    for _ in range(5):
        results.append(await limiter_a.consume("api.example.com/widgets"))
        results.append(await limiter_b.consume("api.example.com/widgets"))

    assert results.count(True) == 3
    assert worker_a._rate_buckets == {}
    assert worker_b._rate_buckets == {}


# Per-tenant (cross-org) isolation on the trigger path.


async def test_trigger_rest_connector_per_tenant_isolation() -> None:
    """Different orgs get independent shared budgets for the same destination."""
    store: dict[str, dict[str, float]] = {}
    redis = _FakeRedis(store)

    org_a = _build_polling_connector("rest", _rest_config(), _REST_CREDS, redis_client=redis, tenant_id="org-A")
    org_b = _build_polling_connector("rest", _rest_config(), _REST_CREDS, redis_client=redis, tenant_id="org-B")

    limiter_a = org_a._get_rate_limiter(0.0001, 2)
    limiter_b = org_b._get_rate_limiter(0.0001, 2)

    assert await limiter_a.consume("dest") is True
    assert await limiter_a.consume("dest") is True
    assert await limiter_a.consume("dest") is False

    assert await limiter_b.consume("dest") is True
    assert await limiter_b.consume("dest") is True
    assert await limiter_b.consume("dest") is False

    assert limiter_a.key("dest") != limiter_b.key("dest")
    assert "default" not in limiter_a.key("dest")
    assert "default" not in limiter_b.key("dest")


async def test_trigger_rest_connector_shared_limiter_without_tenant_raises() -> None:
    """A shared (Redis) limiter configured without a tenant FAILS LOUDLY (FAR-439)."""
    redis = _FakeRedis({})
    connector = _build_polling_connector("rest", _rest_config(), _REST_CREDS, redis_client=redis, tenant_id=None)
    # The shared limiter is built lazily on first rate-limit use; a redis_client
    # wired without a tenant must raise there (never coerce to a "default" budget).
    with pytest.raises(ValueError, match="requires a non-empty tenant_id"):
        connector._get_rate_limiter(1.0, 2)


# No local-bucket fallback + fail-closed on Redis outage.


async def test_trigger_rest_connector_no_local_bucket_fallback_when_redis_configured() -> None:
    """A configured Redis must be authoritative - never a per-process fallback bucket."""
    store: dict[str, dict[str, float]] = {}
    redis = _FakeRedis(store)

    connector = _build_polling_connector("rest", _rest_config(), _REST_CREDS, redis_client=redis, tenant_id="org-1")
    limiter = connector._get_rate_limiter(1.0, 2)

    assert await limiter.consume("dest") is True
    assert await limiter.consume("dest") is True
    assert await limiter.consume("dest") is False

    assert connector._rate_buckets == {}
    assert limiter.buckets == {}


async def test_trigger_rest_connector_fails_closed_on_redis_outage() -> None:
    """A Redis outage when configured FAILS CLOSED - never mints a per-worker budget."""
    connector = _build_polling_connector(
        "rest", _rest_config(), _REST_CREDS, redis_client=_BrokenRedis(), tenant_id="org-1"
    )
    limiter = connector._get_rate_limiter(1.0, 2)

    with pytest.raises(SharedBudgetUnavailableError):
        await limiter.consume("dest")

    assert connector._rate_buckets == {}
    assert limiter.buckets == {}


# Composition root resolver: resolve_shared_rate_limit_redis.


def test_resolver_returns_none_when_redis_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis genuinely not configured -> None (no shared budget exists to multiply)."""
    from modulo.core import connector_hub

    monkeypatch.setattr("modulo.settings.get_settings", lambda: MagicMock(redis_url="", modulo_db="postgresql"))
    assert connector_hub.resolve_shared_rate_limit_redis("org-1") is None


def test_resolver_returns_none_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite DB -> no shared Redis budget to wire."""
    from modulo.core import connector_hub

    monkeypatch.setattr(
        "modulo.settings.get_settings",
        lambda: MagicMock(redis_url="redis://localhost:6379/0", modulo_db="sqlite"),
    )
    assert connector_hub.resolve_shared_rate_limit_redis("org-1") is None


def test_resolver_returns_none_without_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-tenant probe path: never wire a shared budget (cross-tenant leak guard)."""
    from modulo.core import connector_hub

    monkeypatch.setattr(
        "modulo.settings.get_settings",
        lambda: MagicMock(redis_url="redis://localhost:6379/0", modulo_db="postgresql"),
    )
    assert connector_hub.resolve_shared_rate_limit_redis(None) is None


def test_resolver_builds_shared_client_when_configured_with_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured Redis on a tenant path returns a Redis client (authoritative)."""
    from modulo.core import connector_hub

    monkeypatch.setattr(
        "modulo.settings.get_settings",
        lambda: MagicMock(redis_url="redis://localhost:6379/0", modulo_db="postgresql"),
    )
    client = connector_hub.resolve_shared_rate_limit_redis("org-1")
    assert client is not None


def test_resolver_fails_closed_on_settings_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings unreadable on a tenant path FAILS CLOSED (SharedBudgetUnavailableError)."""
    from modulo.core import connector_hub

    def boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("modulo.settings.get_settings", boom)
    with pytest.raises(SharedBudgetUnavailableError, match="settings could not be read"):
        connector_hub.resolve_shared_rate_limit_redis("org-1")


def test_resolver_fails_closed_on_client_construction_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured-but-unconstructable Redis client FAILS CLOSED (no local bucket)."""
    from modulo.core import connector_hub

    monkeypatch.setattr(
        "modulo.settings.get_settings",
        lambda: MagicMock(redis_url="redis://localhost:6379/0", modulo_db="postgresql"),
    )

    class _BrokenFromUrl:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("bad url")

    monkeypatch.setattr("redis.asyncio.Redis.from_url", _BrokenFromUrl)
    with pytest.raises(SharedBudgetUnavailableError, match="could not be constructed"):
        connector_hub.resolve_shared_rate_limit_redis("org-1")


# cron_helpers._build_polling_connector wiring (the SAQ fire path).

ORG = uuid.UUID("7f000000-0000-0000-0000-000000000001")


class _FakeSecretsBackend:
    async def get_secret(self, _cid: str) -> str:
        return '{"token": "x"}'


def test_cron_build_polling_connector_wires_shared_redis_and_tenant() -> None:
    """The cron fire-path wrapper threads redis_client + tenant_id = str(org_id)."""
    from modulo.core import cron_helpers

    shared_redis = object()
    captured: dict[str, Any] = {}

    def fake_resolver(org: str) -> Any:
        assert org == str(ORG)
        return shared_redis

    def fake_build_connector(type_id: str, config: dict[str, Any], creds: dict[str, Any], **kwargs: Any) -> Any:
        captured["type_id"] = type_id
        captured["kwargs"] = kwargs
        return "connector"

    session = MagicMock()
    with (
        patch.object(cron_helpers, "get_settings", return_value=MagicMock(fernet_key="b" * 44)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=_FakeSecretsBackend()),
        patch("modulo.core.connector_hub.resolve_shared_rate_limit_redis", side_effect=fake_resolver),
        patch("modulo.core.trigger_engine.polling._build_polling_connector", side_effect=fake_build_connector),
    ):
        result = asyncio.run(
            cron_helpers._build_polling_connector(
                session,
                SimpleNamespace(id=uuid.uuid4(), connector_type_id="rest", config_json={}),
                SimpleNamespace(id=uuid.uuid4()),
                ORG,
                uuid.uuid4(),
            )
        )

    assert result == "connector"
    assert captured["type_id"] == "rest"
    assert captured["kwargs"]["redis_client"] is shared_redis
    assert captured["kwargs"]["tenant_id"] == str(ORG)


def test_cron_build_polling_connector_propagates_shared_budget_error() -> None:
    """A configured-but-unresolvable shared budget PROPAGATES (fail-closed), not swallowed."""
    from modulo.core import cron_helpers

    def fake_resolver(_org: str) -> Any:
        raise SharedBudgetUnavailableError("shared rate-limit Redis client is configured but could not be constructed")

    session = MagicMock()
    with (
        patch.object(cron_helpers, "get_settings", return_value=MagicMock(fernet_key="b" * 44)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=_FakeSecretsBackend()),
        patch("modulo.core.connector_hub.resolve_shared_rate_limit_redis", side_effect=fake_resolver),
        pytest.raises(SharedBudgetUnavailableError),
    ):
        asyncio.run(
            cron_helpers._build_polling_connector(
                session,
                SimpleNamespace(id=uuid.uuid4(), connector_type_id="rest", config_json={}),
                SimpleNamespace(id=uuid.uuid4()),
                ORG,
                uuid.uuid4(),
            )
        )
