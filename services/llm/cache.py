"""Prompt-cache utilities used by the LLM orchestration layer."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from services.llm.adapters import LLMInvocationMode, LLMInvocationRequest


def make_llm_request_cache_key(
    *,
    request: LLMInvocationRequest,
    provider_name: str,
    model_name: str,
    model_version: str,
    mode: LLMInvocationMode,
    adapter_variant: Mapping[str, Any] | None = None,
) -> str:
    """Compute deterministic SHA-256 key for a request/provider mode tuple."""

    # `temperature` is part of the cache identity: a run asked for at 0 and a run asked for at
    # 1 are different invocations, and replaying one as the other would silently change what a
    # caller that pinned determinism actually gets back.
    payload = {
        "mode": mode.value,
        "provider_name": provider_name,
        "model_name": model_name,
        "model_version": model_version,
        "prompt_name": request.prompt_name,
        "prompt_version": request.prompt_version,
        "prompt_template_version": request.prompt_template_version,
        "requested_schema": request.requested_schema,
        "requested_schema_version": request.requested_schema_version,
        "prompt": request.prompt,
        "context": request.context,
        "metadata": request.metadata,
        "temperature": request.temperature,
        # Provider controls such as Gemini thinking level are configured on the adapter rather
        # than the canonical request, but they still change the generated response. Replaying a
        # medium-thinking result after switching to low would erase that operational decision.
        "adapter_variant": dict(adapter_variant or {}),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@runtime_checkable
class PromptCache(Protocol):
    """Cache protocol for structured LLM outputs."""

    def get(self, key: str) -> Mapping[str, Any] | None: ...

    def set(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None: ...


@dataclass
class InMemoryLLMPromptCache:
    """Deterministic in-memory cache for tests and local dev."""

    default_ttl_seconds: int | None = 3600
    _store: dict[str, tuple[float | None, Mapping[str, Any]]] = field(default_factory=dict)

    def get(self, key: str) -> Mapping[str, Any] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and expires_at < time.time():
            self._store.pop(key, None)
            return None
        return value

    def set(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        expires_at = None if ttl is None else time.time() + ttl
        self._store[key] = (expires_at, value)


@dataclass
class BoundedLRULLMPromptCache:
    """Runner-local JSON cache with encoded-byte accounting and a fixed TTL ceiling."""

    max_entries: int = 256
    max_bytes: int = 16 * 1024 * 1024
    default_ttl_seconds: int = 3600
    time_func: Callable[[], float] = time.monotonic
    _store: OrderedDict[str, tuple[float, bytes, int]] = field(
        default_factory=OrderedDict, init=False
    )
    _encoded_bytes: int = field(default=0, init=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_entries <= 0 or self.max_bytes <= 0 or self.default_ttl_seconds <= 0:
            raise ValueError("cache entry, byte and TTL limits must be positive")

    @property
    def encoded_bytes(self) -> int:
        with self._lock:
            self._expire(self.time_func())
            return self._encoded_bytes

    @property
    def entry_count(self) -> int:
        with self._lock:
            self._expire(self.time_func())
            return len(self._store)

    def _remove(self, key: str) -> None:
        entry = self._store.pop(key, None)
        if entry is not None:
            self._encoded_bytes -= entry[2]

    def _expire(self, now: float) -> None:
        for key, (expires_at, _, _) in tuple(self._store.items()):
            if expires_at <= now:
                self._remove(key)

    def get(self, key: str) -> Mapping[str, Any] | None:
        with self._lock:
            self._expire(self.time_func())
            entry = self._store.get(key)
            if entry is None:
                return None
            self._store.move_to_end(key)
            # Decode a new object: caller mutation cannot change byte accounting.
            return json.loads(entry[1])

    def set(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        encoded = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        size = len(key.encode("utf-8")) + len(encoded)
        ttl = min(
            self.default_ttl_seconds,
            self.default_ttl_seconds if ttl_seconds is None else ttl_seconds,
        )
        with self._lock:
            now = self.time_func()
            self._expire(now)
            self._remove(key)
            if size > self.max_bytes or ttl <= 0:
                return
            while (
                len(self._store) >= self.max_entries or self._encoded_bytes + size > self.max_bytes
            ):
                self._remove(next(iter(self._store)))
            self._store[key] = (now + ttl, encoded, size)
            self._encoded_bytes += size


@dataclass
class RedisLLMPromptCache(PromptCache):
    """Redis cache for prompt outputs when available in production."""

    redis_client: Any
    namespace: str = "llm:prompt"
    default_ttl_seconds: int | None = 3600

    @staticmethod
    def _normalize_redis_payload(value: Mapping[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def _cache_key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def get(self, key: str) -> Mapping[str, Any] | None:
        raw = self.redis_client.get(self._cache_key(key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def set(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        payload = self._normalize_redis_payload(value)
        redis_key = self._cache_key(key)
        if ttl is None:
            self.redis_client.set(redis_key, payload)
        else:
            self.redis_client.setex(redis_key, ttl, payload)
