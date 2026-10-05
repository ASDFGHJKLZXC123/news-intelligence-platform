"""Token-bucket limiter primitives for shared invocation guardrails."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenBucketLimiter(Protocol):
    """Rate limiter protocol used by provider adapters."""

    def consume(
        self,
        key: str,
        tokens: int,
        *,
        now: float | None = None,
    ) -> bool: ...


@dataclass
class InMemoryTokenBucketLimiter:
    """Deterministic local token-bucket with configurable refill."""

    capacity: int
    refill_per_second: float
    time_func: Callable[[], float] = time.time
    _state: dict[str, tuple[float, float]] = field(default_factory=dict)

    def consume(self, key: str, tokens: int, *, now: float | None = None) -> bool:
        if tokens <= 0:
            return True

        current_time = self.time_func() if now is None else now
        available, last_refill = self._state.get(key, (self.capacity, current_time))

        elapsed = max(0.0, current_time - last_refill)
        available = min(self.capacity, available + elapsed * self.refill_per_second)
        if available >= tokens:
            available -= tokens
            self._state[key] = (available, current_time)
            return True

        self._state[key] = (available, current_time)
        return False


@dataclass
class RedisTokenBucketLimiter(TokenBucketLimiter):
    """Redis-backed limiter for shared worker fleets.

    Behavior is intentionally conservative and suitable for tests as a drop-in class;
    callers can mock redis with a tiny local fake.
    """

    redis_client: object
    capacity: int
    refill_per_second: float
    namespace: str = "llm:bucket"

    _consume_script = """
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])
local now_ts = tonumber(ARGV[4])
local available = tonumber(redis.call('HGET', KEYS[1], 'available')) or capacity
local last_refill = tonumber(redis.call('HGET', KEYS[1], 'last_refill')) or now_ts
local elapsed = math.max(0, now_ts - last_refill)
available = math.min(capacity, available + elapsed * refill_per_second)
local allowed = 0
if available >= requested then
    available = available - requested
    allowed = 1
end
redis.call('HSET', KEYS[1], 'available', available, 'last_refill', now_ts)
redis.call('EXPIRE', KEYS[1], 120)
return allowed
"""

    def _cache_key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def consume(self, key: str, tokens: int, *, now: float | None = None) -> bool:
        if tokens <= 0:
            return True

        now_ts = now if now is not None else time.time()
        cache_key = self._cache_key(key)
        eval_command = getattr(self.redis_client, "eval", None)
        if callable(eval_command):
            return bool(
                eval_command(
                    self._consume_script,
                    1,
                    cache_key,
                    self.capacity,
                    self.refill_per_second,
                    tokens,
                    now_ts,
                )
            )

        # Small redis fakes used by unit tests need no Lua interpreter. Production Redis
        # always takes the atomic script path above, preventing Celery-worker races.
        state = self.redis_client.hgetall(cache_key)
        if state:
            raw_available = state.get(b"available", state.get("available"))  # type: ignore[union-attr]
            raw_last_refill = state.get(b"last_refill", state.get("last_refill"))  # type: ignore[union-attr]
            available = float(raw_available)  # type: ignore[arg-type]
            last_refill = float(raw_last_refill)  # type: ignore[arg-type]
        else:
            available = float(self.capacity)
            last_refill = now_ts

        elapsed = max(0.0, now_ts - last_refill)
        available = min(self.capacity, available + elapsed * self.refill_per_second)

        if available >= tokens:
            available -= tokens
            allowed = True
        else:
            allowed = False

        self.redis_client.hset(cache_key, mapping={"available": available, "last_refill": now_ts})
        self.redis_client.expire(cache_key, 120)
        return allowed


@dataclass
class CompositeProviderLimiter(TokenBucketLimiter):
    """Enforce both request and token budgets for each configured provider.

    The orchestrator's limiter protocol remains unchanged, so deterministic tests may
    continue injecting a single token bucket. Production assembly uses this composite.
    A token-budget rejection after request admission conservatively consumes the request;
    it can never permit the fleet to exceed either configured ceiling.
    """

    request_limiters: Mapping[str, TokenBucketLimiter]
    token_limiters: Mapping[str, TokenBucketLimiter]

    def consume(self, key: str, tokens: int, *, now: float | None = None) -> bool:
        provider_name = key.partition(":")[0]
        request_limiter = self.request_limiters.get(provider_name)
        token_limiter = self.token_limiters.get(provider_name)
        if request_limiter is None or token_limiter is None:
            return False
        if not request_limiter.consume(f"{provider_name}:requests", 1, now=now):
            return False
        return token_limiter.consume(
            f"{provider_name}:tokens",
            max(1, tokens),
            now=now,
        )


@dataclass
class LocalProviderRateLimiter(TokenBucketLimiter):
    """Atomic provider RPM/TPM buckets, empty when a runner process starts.

    Admission consumes both buckets together. A failed admission consumes neither,
    so waiting for token refill cannot silently exhaust the request allowance.
    """

    rpm_limits: Mapping[str, int]
    tpm_limits: Mapping[str, int]
    time_func: Callable[[], float] = time.monotonic
    _state: dict[str, tuple[float, float, float]] = field(default_factory=dict, init=False)
    _cooldowns: dict[str, float] = field(default_factory=dict, init=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.rpm_limits, self.tpm_limits = dict(self.rpm_limits), dict(self.tpm_limits)
        started = self.time_func()
        for provider in set(self.rpm_limits) | set(self.tpm_limits):
            rpm, tpm = self.rpm_limits.get(provider, 0), self.tpm_limits.get(provider, 0)
            if type(rpm) is not int or type(tpm) is not int or rpm <= 0 or tpm <= 0:
                raise ValueError(f"positive RPM and TPM limits are required for {provider}")
            self._state[provider] = (0.0, 0.0, started)

    def _refill(self, provider: str, now: float) -> tuple[float, float]:
        requests, tokens, previous = self._state[provider]
        elapsed = max(0.0, now - previous)
        requests = min(
            self.rpm_limits[provider], requests + elapsed * self.rpm_limits[provider] / 60.0
        )
        tokens = min(self.tpm_limits[provider], tokens + elapsed * self.tpm_limits[provider] / 60.0)
        self._state[provider] = (requests, tokens, max(previous, now))
        return requests, tokens

    def delay_until_available(self, key: str, tokens: int, *, now: float | None = None) -> float:
        provider = key.partition(":")[0]
        with self._lock:
            if provider not in self._state or tokens > self.tpm_limits[provider]:
                return math.inf
            current = self.time_func() if now is None else now
            requests, available_tokens = self._refill(provider, current)
            return max(
                0.0,
                (1.0 - requests) * 60.0 / self.rpm_limits[provider],
                (max(1, tokens) - available_tokens) * 60.0 / self.tpm_limits[provider],
                self._cooldowns.get(provider, current) - current,
            )

    def consume(self, key: str, tokens: int, *, now: float | None = None) -> bool:
        provider = key.partition(":")[0]
        with self._lock:
            current = self.time_func() if now is None else now
            if self.delay_until_available(key, tokens, now=current) > 0:
                return False
            requests, available_tokens, previous = self._state[provider]
            self._state[provider] = (
                requests - 1.0,
                available_tokens - max(1, tokens),
                previous,
            )
            return True

    def defer(self, provider: str, seconds: float) -> None:
        """Provider Retry-After applies to every subsequent call on that provider."""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("provider cooldown must be finite and non-negative")
        with self._lock:
            if provider not in self._state:
                return
            until = self.time_func() + seconds
            self._cooldowns[provider] = max(self._cooldowns.get(provider, until), until)


def build_provider_rate_limiter(
    *,
    redis_client: object,
    rpm_limits: Mapping[str, int],
    tpm_limits: Mapping[str, int],
) -> CompositeProviderLimiter:
    """Build the shared Redis limiter used by production LLM workers."""

    providers = set(rpm_limits) | set(tpm_limits)
    request_limiters: dict[str, TokenBucketLimiter] = {}
    token_limiters: dict[str, TokenBucketLimiter] = {}
    for provider_name in providers:
        rpm = rpm_limits.get(provider_name, 0)
        tpm = tpm_limits.get(provider_name, 0)
        if rpm <= 0 or tpm <= 0:
            raise ValueError(f"positive RPM and TPM limits are required for {provider_name}")
        request_limiters[provider_name] = RedisTokenBucketLimiter(
            redis_client=redis_client,
            capacity=rpm,
            refill_per_second=rpm / 60.0,
            namespace="llm:rate:requests",
        )
        token_limiters[provider_name] = RedisTokenBucketLimiter(
            redis_client=redis_client,
            capacity=tpm,
            refill_per_second=tpm / 60.0,
            namespace="llm:rate:tokens",
        )
    return CompositeProviderLimiter(
        request_limiters=request_limiters,
        token_limiters=token_limiters,
    )


__all__ = [
    "TokenBucketLimiter",
    "CompositeProviderLimiter",
    "InMemoryTokenBucketLimiter",
    "LocalProviderRateLimiter",
    "RedisTokenBucketLimiter",
    "build_provider_rate_limiter",
]
