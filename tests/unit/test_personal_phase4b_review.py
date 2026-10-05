"""Independent adversarial Phase 4B checks; all transports and accounting are synthetic."""

from __future__ import annotations

import datetime as dt
import json
import threading
from unittest.mock import Mock

import httpx
import pytest

from packages.providers.openai_embeddings import OpenAIEmbeddingProvider
from services.llm.cache import BoundedLRULLMPromptCache
from services.llm.limiter import LocalProviderRateLimiter
from services.personal.deadlines import RuntimeBudget, RuntimeCancelled, bind_budget
from services.personal.local_runtime import PersonalPaidRuntime
from services.personal.paid_runtime import DurablePaidHTTPClient
from services.personal.spending import PaidRoute


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.origin = dt.datetime(2026, 10, 4, tzinfo=dt.UTC)

    def monotonic(self):
        return self.seconds

    def wall(self):
        return self.origin + dt.timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        self.seconds += seconds


def route(role="generation", provider="openai"):
    return PaidRoute.from_mapping(
        {
            "provider": provider,
            "model": "synthetic-model",
            "model_version": "synthetic-v1",
            "price_revision": "synthetic-no-live-prices",
            "price_source_url": "https://openai.com/api/pricing/"
            if provider == "openai"
            else "https://www.anthropic.com/pricing",
            "input_usd_per_million_tokens": "1",
            "output_usd_per_million_tokens": "0" if role == "embedding" else "1",
            "max_input_tokens": 10000,
            "max_output_tokens": 0 if role == "embedding" else 100,
            "deadline_seconds": 30,
        },
        role=role,
    )


def test_cache_counts_unicode_bytes_clones_values_and_clamps_one_hour_expiry():
    clock = Clock()
    value = {"text": "snowman: ☃", "nested": [1, 2]}
    key = "clé"
    expected = len(key.encode()) + len(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    cache = BoundedLRULLMPromptCache(max_bytes=expected, time_func=clock.monotonic)
    cache.set(key, value, ttl_seconds=7200)
    assert cache.encoded_bytes == expected
    value["nested"].append(3)
    decoded = cache.get(key)
    decoded["nested"].append(4)
    assert cache.get(key)["nested"] == [1, 2]
    clock.seconds = 3599.999
    assert cache.get(key) is not None
    clock.seconds = 3600
    assert cache.get(key) is None
    assert cache.encoded_bytes == 0


def test_cache_default_256_entry_lru_and_16_mib_oversize_admission():
    cache = BoundedLRULLMPromptCache()
    for index in range(256):
        cache.set(str(index), {"value": index})
    cache.get("0")
    cache.set("256", {"value": 256})
    assert cache.entry_count == 256
    assert cache.get("0") == {"value": 0}
    assert cache.get("1") is None
    cache.set("too-large", {"value": "x" * (16 * 1024 * 1024)})
    assert cache.get("too-large") is None
    assert cache.entry_count == 256
    assert cache.encoded_bytes <= 16 * 1024 * 1024


def test_fresh_process_equivalents_begin_empty_and_model_names_share_provider_bucket():
    clock = Clock()
    for _ in range(3):
        limiter = LocalProviderRateLimiter(
            {"openai": 60}, {"openai": 60}, time_func=clock.monotonic
        )
        started = clock.seconds
        assert not limiter.consume("openai:model-a", 1)
        clock.seconds = started + 1
        assert limiter.consume("openai:model-a", 1)
        assert not limiter.consume("openai:model-b", 1)
        assert limiter.delay_until_available("openai:model-b", 1) == pytest.approx(1)


def test_embedding_physical_retry_honors_retry_after_and_generation_shares_pacing():
    clock = Clock()
    limiter = LocalProviderRateLimiter({"openai": 60}, {"openai": 60000}, time_func=clock.monotonic)
    runtime = PersonalPaidRuntime(
        limiter, monotonic=clock.monotonic, sleep=clock.sleep, wall_clock=clock.wall
    )
    ledger = Mock()
    ledger.reserve.side_effect = [
        "synthetic-request-1",
        "synthetic-request-2",
        "synthetic-request-3",
        "synthetic-request-4",
    ]
    observed = []

    def post(path, **_kwargs):
        observed.append((path, clock.seconds))
        if len(observed) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "2"}, json={"error": "synthetic throttle"}
            )
        if path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [1.0, 2.0]}],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    delegate = Mock(post=post)
    embeddings = DurablePaidHTTPClient(
        route=route("embedding"), ledger=ledger, delegate=delegate, local_runtime=runtime
    )
    provider = OpenAIEmbeddingProvider(
        api_key="synthetic",
        model_name="synthetic-model",
        model_version="synthetic-v1",
        dimension=2,
        client=embeddings,
        sleep=clock.sleep,
    )
    assert len(provider.embed(["synthetic article"])) == 1
    generation = DurablePaidHTTPClient(
        route=route(), ledger=ledger, delegate=delegate, local_runtime=runtime
    )
    generation.post(
        "/v1/chat/completions",
        json={"model": "synthetic-model", "max_tokens": 1, "messages": []},
        headers={},
    )
    generation.post(
        "/v1/chat/completions",
        json={"model": "synthetic-model", "max_tokens": 1, "messages": []},
        headers={},
    )
    assert observed[0][1] >= 1
    assert observed[1][1] >= observed[0][1] + 2
    # Refill during cooldown earns a finite burst; total dispatches cannot exceed
    # the empty-start refill envelope, even across embedding/generation models.
    for count, (_, instant) in enumerate(observed, start=1):
        assert instant + 1e-6 >= count
    assert observed[3][1] >= 4
    assert ledger.reserve.call_count == 4
    assert ledger.dispatch.call_count == 4
    assert ledger.mark_uncertain.call_count == 1
    assert ledger.reconcile.call_count == 3


def test_cooldown_past_persisted_deadline_cancels_before_reservation_or_network():
    clock = Clock()
    limiter = LocalProviderRateLimiter({"openai": 60}, {"openai": 60000}, time_func=clock.monotonic)
    runtime = PersonalPaidRuntime(
        limiter, monotonic=clock.monotonic, sleep=clock.sleep, wall_clock=clock.wall
    )
    runtime.record_retry_after("openai", httpx.Response(429, headers={"Retry-After": "120"}))
    budget = RuntimeBudget(
        clock.origin + dt.timedelta(seconds=60),
        clock.origin + dt.timedelta(seconds=90),
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    ledger, delegate = Mock(), Mock()
    client = DurablePaidHTTPClient(
        route=route(), ledger=ledger, delegate=delegate, local_runtime=runtime
    )
    with bind_budget(budget), pytest.raises(RuntimeCancelled):
        client.post(
            "/v1/chat/completions",
            json={"model": "synthetic-model", "max_tokens": 1, "messages": []},
            headers={},
        )
    ledger.reserve.assert_not_called()
    delegate.post.assert_not_called()
    assert clock.seconds == 0


def test_serialization_covers_different_providers_at_physical_transport_boundary():
    runtime = PersonalPaidRuntime(
        LocalProviderRateLimiter(
            {"openai": 60000000, "anthropic": 60000000},
            {"openai": 1000000000, "anthropic": 1000000000},
        )
    )
    first_entered, release_first, second_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    failures = []

    def first_post(*_args, **_kwargs):
        first_entered.set()
        assert release_first.wait(2)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    def second_post(*_args, **_kwargs):
        second_entered.set()
        return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}})

    def invoke(provider, delegate, path):
        try:
            client = DurablePaidHTTPClient(
                route=route(provider=provider),
                ledger=Mock(),
                delegate=Mock(post=delegate),
                local_runtime=runtime,
            )
            client.post(
                path, json={"model": "synthetic-model", "max_tokens": 1, "messages": []}, headers={}
            )
        except BaseException as exc:
            failures.append(exc)

    first = threading.Thread(target=invoke, args=("openai", first_post, "/v1/chat/completions"))
    second = threading.Thread(target=invoke, args=("anthropic", second_post, "/v1/messages"))
    try:
        first.start()
        assert first_entered.wait(1)
        second.start()
        assert not second_entered.wait(0.1)
    finally:
        release_first.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert second_entered.is_set()
    assert failures == []
