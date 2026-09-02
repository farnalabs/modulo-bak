"""Process-local demo rate-limit floor tests (FAR-535 qa-iterate iteration 2).

When RateLimitMiddleware's registry degrades to ``_NoopRateLimiter`` (Redis
unconfigured/unreachable, or sqlite mode) every route is normally fail-open —
existing behaviour. The demo auto-login endpoint is the exception: it mints a
real session with zero user input, so its 10/hour per-IP cap must survive the
fallback via a bounded in-process TokenBucket. Locks here:

  * with a noop registry, the 11th rapid demo request from one IP is 429'd
    (with Retry-After) while the first 10 pass;
  * a different IP's bucket is untouched (per-IP isolation);
  * OTHER ruled routes keep their fail-open noop behaviour (no floor).

Uses direct ``dispatch`` calls with mock requests — no HTTP stack — mirroring
the middleware-internals test style in tests/unit/rate_limiter/.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.responses import Response

from modulo.api.middleware.rate_limiter import (
    DEMO_RULE_PREFIX,
    RateLimitMiddleware,
    _demo_floor_buckets,
    _NoopRateLimiter,
)
from modulo.core.rate_limiter import TokenBucket
from modulo.settings import Settings

_VALID_32 = "a" * 32


def _settings() -> Settings:
    """Settings whose registry creation degrades to the noop limiter.

    Empty ``redis_url`` skips the Redis branch of _create_registry, yielding
    the same _NoopRateLimiter a runtime Redis blip produces.
    """
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        redis_url="",
    )


def _middleware() -> RateLimitMiddleware:
    app = FastAPI()

    @app.post(DEMO_RULE_PREFIX)
    async def demo() -> dict[str, bool]:
        return {"ok": True}

    mw = RateLimitMiddleware(app=app, settings=_settings())
    assert isinstance(mw._registry, _NoopRateLimiter)
    return mw


def _demo_request(ip: str) -> MagicMock:
    """POST /api/v1/auth/demo from ``ip`` (the anonymous client-key fallback)."""
    req = MagicMock()
    req.method = "POST"
    req.url.path = DEMO_RULE_PREFIX
    req.scope = {}
    req.headers.get = MagicMock(side_effect=lambda name, default="": default)
    req.client = MagicMock(host=ip)
    return req


def _call_next() -> AsyncMock:
    return AsyncMock(return_value=Response(content=b"ok", status_code=200))


@pytest.fixture(autouse=True)
def _reset_demo_floor_buckets():
    """The module-level floor dict is process-global — isolate between tests."""
    _demo_floor_buckets.clear()
    yield
    _demo_floor_buckets.clear()


async def test_demo_floor_limits_11th_request_per_ip_with_noop_registry() -> None:
    """11 rapid demo requests from one IP: the 11th is 429'd with Retry-After."""
    mw = _middleware()
    call_next = _call_next()

    responses = [await mw.dispatch(_demo_request("203.0.113.9"), call_next) for _ in range(11)]

    first_ten = [resp.status_code for resp in responses[:10]]
    assert all(status == 200 for status in first_ten)
    assert responses[10].status_code == 429
    assert "Retry-After" in responses[10].headers
    assert call_next.await_count == 10


async def test_demo_floor_is_isolated_per_ip() -> None:
    """Exhausting one IP's demo bucket must not affect a different IP."""
    mw = _middleware()
    call_next = _call_next()

    for _ in range(10):
        resp = await mw.dispatch(_demo_request("203.0.113.9"), call_next)
        assert resp.status_code == 200

    other_ip_resp = await mw.dispatch(_demo_request("198.51.100.7"), call_next)

    assert other_ip_resp.status_code == 200


async def test_other_routes_stay_fail_open_with_noop_registry() -> None:
    """Non-demo ruled routes keep the existing noop fail-open behaviour."""
    app = FastAPI()

    @app.post("/api/v1/runs")
    async def runs() -> dict[str, bool]:
        return {"ok": True}

    mw = RateLimitMiddleware(app=app, settings=_settings())
    assert isinstance(mw._registry, _NoopRateLimiter)

    req = MagicMock()
    req.method = "POST"
    req.url.path = "/api/v1/runs"
    req.scope = {}
    req.headers.get = MagicMock(side_effect=lambda name, default="": default)
    req.client = MagicMock(host="203.0.113.9")
    call_next = _call_next()

    for _ in range(15):
        resp = await mw.dispatch(req, call_next)
        assert resp.status_code == 200


def test_demo_floor_uses_bounded_token_bucket_shape() -> None:
    """The floor bucket dict starts empty before any demo request."""
    bucket = _demo_floor_buckets.get("ip:203.0.113.9:/api/v1/auth/demo")
    assert bucket is None


async def test_demo_floor_bucket_shape_after_first_request() -> None:
    """The first demo request seeds a real TokenBucket sized 10/hour."""
    mw = _middleware()
    call_next = _call_next()

    resp = await mw.dispatch(_demo_request("203.0.113.9"), call_next)

    assert resp.status_code == 200
    bucket = _demo_floor_buckets.get("ip:203.0.113.9:/api/v1/auth/demo")
    assert isinstance(bucket, TokenBucket)
    assert bucket.burst == 10
    assert abs(bucket.rate - 10 / 3600) < 1e-12
