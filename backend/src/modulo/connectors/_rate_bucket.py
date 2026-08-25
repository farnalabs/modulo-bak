"""Connector-local per-destination rate limiting buckets (FAR-411 / FAR-439).

The REST connector's per-destination outbound rate limit lives here rather than
reaching into ``modulo.core`` — the import-linter contract forbids connectors
importing core, so ``modulo.core.rate_limiter.TokenBucket`` cannot be reused.
Semantics mirror the core bucket: continuous refill up to ``burst``, guarded by
an ``asyncio.Lock`` so concurrent tasks never go below zero tokens.

FAR-439 adds a SHARED, Redis-backed limiter so a fleet of uvicorn/SAQ workers
enforces ONE budget per destination instead of ``N`` independent per-process
budgets. A destination is keyed per-tenant (``<tenant_id>:<destination>``) where
the destination is the resolved host + path, so different tenants never share a
budget. The shared limiter composes with the local bucket:

* **Redis available** — every worker acquires a token through a single atomic
  Redis token bucket (a Lua script), so concurrent workers across the fleet can
  never over-spend the shared budget (no lost-token race).
* **Redis unavailable** — the connector-local :class:`TokenBucket` is the
  fallback, so single-worker dev (or a Redis outage) still enforces a per-process
  budget rather than failing open.

Best-effort: the limiter never raises on Redis failure — it logs a structured
warning and degrades to the local bucket, preserving "no regression in
single-worker mode".
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from redis.exceptions import RedisError

_log = logging.getLogger(__name__)


class TokenBucket:
    """In-memory, per-process, async-safe token bucket."""

    def __init__(self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        self.rate = float(rate)
        self.burst = int(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()
        self._lock = asyncio.Lock()

    async def consume(self, tokens: float = 1.0) -> bool:
        """Consume ``tokens`` (default one). Returns False when the bucket is low."""
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        async with self._lock:
            now = self._clock()
            self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            return True


# Lua script executed atomically on the Redis server. Reads the per-key token
# bucket (tokens + last-refill timestamp), applies continuous refill, and either
# consumes ``cost`` tokens (returning 1) or persists the refilled state and
# returns 0. Atomicity on the server is what prevents concurrent workers from
# reading the same token count and both spending it (no lost-token race). A
# PEXPIRE on every call reclaims an abandoned bucket rather than accumulating
# forever, while comfortably outlasting a burst-worth of refill.
_CONSUME_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])

local st = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(st[1])
local ts = tonumber(st[2])
if tokens == nil then
    tokens = burst
    ts = now
end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = tokens + elapsed * rate
if tokens > burst then tokens = burst end
ts = now
if tokens >= cost then
    redis.call('HSET', key, 'tokens', tokens - cost, 'ts', ts)
    redis.call('PEXPIRE', key, ttl_ms)
    return 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', ts)
redis.call('PEXPIRE', key, ttl_ms)
return 0
"""


class RedisTokenBucket:
    """Redis-backed, atomic token bucket shared across workers (FAR-439).

    Unlike :class:`TokenBucket` (per-process, using ``time.monotonic``), a shared
    bucket must use a **wall clock** (``time.time``) so every worker agrees on
    "now". The Lua script runs atomically on the Redis server, so concurrent
    workers never double-spend a token (no lost-token race).
    """

    def __init__(self, redis_client: Any, rate: float, burst: int, key_prefix: str = "rest_rate_limit:") -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        self.rate = float(rate)
        self.burst = int(burst)
        self._key_prefix = key_prefix
        self._script = redis_client.register_script(_CONSUME_LUA)

    def _key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    async def consume(self, key: str, tokens: float = 1.0, now: float | None = None) -> bool:
        """Consume ``tokens`` from the shared bucket at ``key``; False when low.

        ``now`` defaults to the wall clock so concurrent workers agree on the
        refill base. Redis errors propagate to the caller (the
        :class:`PerDestinationRateLimiter` degrades to the local bucket).
        """
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        now = float(now) if now is not None else time.time()
        # TTL must comfortably outlive a burst-worth of refill so the bucket is
        # reclaimed only when truly idle (never mid-window).
        ttl_ms = int(max(60.0, (self.burst / self.rate if self.rate > 0 else 60.0) * 2) * 1000)
        keys = [self._key(key)]
        args = [self.rate, self.burst, tokens, now, ttl_ms]
        result = await self._script(keys=keys, args=args)
        return bool(result)


class PerDestinationRateLimiter:
    """Per-destination rate limiter: shared Redis budget with local fallback.

    Holds one bucket per destination (host + path), keyed per-tenant so tenants
    never share a budget. When a ``redis_client`` is supplied the shared Redis
    bucket is authoritative; any Redis failure degrades to the connector-local
    :class:`TokenBucket` (retained in ``buckets``). Without a ``redis_client`` it
    is purely per-process — identical to the pre-FAR-439 single-worker behaviour.

    Saturation is surfaced as a monotonically increasing counter plus a
    structured log so an operator can alert when a destination is throttling.
    """

    def __init__(
        self,
        *,
        rate: float,
        burst: int,
        redis_client: Any = None,
        tenant_id: str | None = None,
        key_prefix: str = "rest_rate_limit:",
        buckets: dict[str, TokenBucket] | None = None,
    ) -> None:
        self.rate = float(rate)
        self.burst = int(burst)
        self._redis_client = redis_client
        self._tenant_id = tenant_id or "default"
        self._key_prefix = key_prefix
        self.buckets: dict[str, TokenBucket] = buckets if buckets is not None else {}
        self._lock = asyncio.Lock()
        self._redis_buckets: dict[str, RedisTokenBucket] = {}
        self.saturation_count = 0
        self.saturations: dict[str, int] = {}

    def key(self, destination: str) -> str:
        """Per-tenant Redis key for ``destination`` (host + path)."""
        return f"{self._tenant_id}:{destination}"

    async def consume(self, destination: str, tokens: float = 1.0) -> bool:
        """Consume ``tokens`` for ``destination``; True on acquire, False on saturate.

        Redis-first when a client is available; a Redis failure falls back to the
        local bucket rather than failing open. Returns False when the budget is
        exhausted (and records a saturation signal).
        """
        if self._redis_client is not None:
            try:
                ok = await self._redis_consume(destination, tokens)
            except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
                _log.warning(
                    "rest.rate_limit.redis_fallback",
                    extra={"destination": destination, "tenant": self._tenant_id, "error": str(exc)},
                )
                ok = await self._local_consume(destination, tokens)
            if not ok:
                self._record_saturation(destination)
            return ok
        ok = await self._local_consume(destination, tokens)
        if not ok:
            self._record_saturation(destination)
        return ok

    async def _redis_consume(self, destination: str, tokens: float) -> bool:
        bucket = self._redis_buckets.get(destination)
        if bucket is None:
            bucket = RedisTokenBucket(self._redis_client, self.rate, self.burst, key_prefix=self._key_prefix)
            self._redis_buckets[destination] = bucket
        return await bucket.consume(self.key(destination), tokens=tokens)

    async def _local_consume(self, destination: str, tokens: float) -> bool:
        bucket = self.buckets.get(destination)
        if bucket is None:
            async with self._lock:
                bucket = self.buckets.get(destination)
                if bucket is None:
                    bucket = TokenBucket(rate=self.rate, burst=self.burst)
                    self.buckets[destination] = bucket
        return await bucket.consume(tokens)

    def _record_saturation(self, destination: str) -> None:
        self.saturation_count += 1
        count = self.saturations.get(destination, 0) + 1
        self.saturations[destination] = count
        _log.warning(
            "rest.rate_limit.saturated",
            extra={
                "destination": destination,
                "tenant": self._tenant_id,
                "saturation_count": count,
                "saturation_total": self.saturation_count,
            },
        )
