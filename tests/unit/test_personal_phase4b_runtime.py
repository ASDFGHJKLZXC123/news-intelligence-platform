"""Phase 4B local cache/pacing proofs; every physical delegate is synthetic."""

from __future__ import annotations

import builtins
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from packages.config.settings import Settings
from services.llm.cache import BoundedLRULLMPromptCache, InMemoryLLMPromptCache
from services.llm.limiter import InMemoryTokenBucketLimiter, LocalProviderRateLimiter
from services.personal.deadlines import RuntimeBudget, RuntimeCancelled, bind_budget
from services.personal.local_runtime import PersonalPaidRuntime
from services.personal.paid_runtime import DurablePaidHTTPClient
from services.personal.spending import PaidRoute, PaidWorkBlocked
from tests.unit.test_personal_spending import model_route

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self):
        self.now = 0.0
        self.origin = dt.datetime(2026, 10, 4, tzinfo=dt.UTC)

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def wall(self):
        return self.origin + dt.timedelta(seconds=self.now)


def local_runtime(clock, *, rpm=60, tpm=120_000):
    return PersonalPaidRuntime(
        LocalProviderRateLimiter(
            rpm_limits={"openai": rpm},
            tpm_limits={"openai": tpm},
            time_func=clock.monotonic,
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        wall_clock=clock.wall,
    )


def route(role="generation"):
    mapping = model_route()[role]
    mapping["deadline_seconds"] = 1
    return PaidRoute.from_mapping(mapping, role=role)


def payload(resolved):
    if resolved.role == "embedding":
        return {"model": resolved.model, "input": ["synthetic"]}
    return {
        "model": resolved.model,
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "synthetic"}],
    }


def successful_response(role="generation"):
    usage = {"prompt_tokens": 10}
    if role == "generation":
        usage["completion_tokens"] = 5
    return httpx.Response(200, json={"usage": usage})


def test_personal_cache_defaults_lru_overflow_and_read_refresh():
    cache = BoundedLRULLMPromptCache()
    assert cache.max_entries == 256
    assert cache.max_bytes == 16 * 1024 * 1024
    assert cache.default_ttl_seconds == 3600
    for i in range(256):
        cache.set(str(i), {"result": i})
    assert cache.get("0") == {"result": 0}
    cache.set("new", {"result": 256})
    assert cache.entry_count == 256
    assert cache.get("1") is None
    assert cache.get("0") == {"result": 0}
    assert cache.encoded_bytes <= cache.max_bytes


def test_personal_cache_counts_utf8_key_value_bytes_and_copies_objects():
    clock = Clock()
    cache = BoundedLRULLMPromptCache(max_bytes=100, time_func=clock.monotonic)
    value = {"text": "é"}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    cache.set("鍵", value)
    assert cache.encoded_bytes == len("鍵".encode()) + len(encoded)
    value["text"] = "mutated"
    result = cache.get("鍵")
    result["text"] = "also mutated"
    assert cache.get("鍵") == {"text": "é"}
    cache.set("鍵", {"text": "replacement"})
    assert cache.entry_count == 1
    assert cache.encoded_bytes == len("鍵".encode()) + len(b'{"text":"replacement"}')


def test_personal_cache_byte_limit_evicts_and_oversize_entry_is_skipped():
    cache = BoundedLRULLMPromptCache()
    cache.set("a", {"text": "x" * (9 * 1024 * 1024)})
    cache.set("b", {"text": "y" * (9 * 1024 * 1024)})
    assert cache.get("a") is None
    assert cache.entry_count == 1
    assert cache.encoded_bytes < cache.max_bytes
    cache.set("oversize", {"text": "z" * cache.max_bytes})
    assert cache.get("oversize") is None
    assert cache.entry_count == 1
    assert cache.encoded_bytes < cache.max_bytes


def test_personal_cache_expires_at_one_hour_and_releases_all_bytes():
    clock = Clock()
    cache = BoundedLRULLMPromptCache(time_func=clock.monotonic)
    cache.set("one", {"value": 1}, ttl_seconds=7200)
    cache.set("short", {"value": 2}, ttl_seconds=1)
    clock.now = 1
    assert cache.get("short") is None
    assert cache.get("one") == {"value": 1}
    clock.now = 3600
    assert cache.get("one") is None
    assert cache.entry_count == cache.encoded_bytes == 0


def test_legacy_local_primitives_preserve_full_start_behavior():
    legacy = InMemoryTokenBucketLimiter(capacity=2, refill_per_second=0, time_func=lambda: 0)
    assert legacy.consume("openai", 2)
    cache = InMemoryLLMPromptCache()
    value = {"result": True}
    cache.set("one", value)
    assert cache.get("one") == value


def test_local_provider_buckets_start_empty_and_admit_rpm_tpm_atomically():
    clock = Clock()
    limiter = LocalProviderRateLimiter(
        rpm_limits={"openai": 60, "anthropic": 120},
        tpm_limits={"openai": 60, "anthropic": 120},
        time_func=clock.monotonic,
    )
    assert not limiter.consume("openai:model", 1)
    clock.now = 1
    # There is one request but only one token: rejection must keep the request.
    assert not limiter.consume("openai:model", 2)
    assert limiter.consume("anthropic:model", 2)
    clock.now = 2
    assert limiter.consume("openai:model", 2)
    assert not limiter.consume("openai:model", 1)
    clock.now = 3
    assert limiter.consume("openai:model", 1)
    assert not limiter.consume("unconfigured:model", 1)
    assert not limiter.consume("openai:model", 61)


def test_physical_generation_embedding_retries_share_pacing_and_reservations():
    clock = Clock()
    runtime = local_runtime(clock)
    ledger = Mock()
    ledger.reserve.side_effect = ["first", "second", "third"]
    sent = []
    generation_route, embedding_route = route(), route("embedding")
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "synthetic"}),
            successful_response("embedding"),
            successful_response(),
        ]
    )

    def send(path, **kwargs):
        sent.append((path, clock.now))
        return next(responses)

    delegate = SimpleNamespace(post=send)
    generation = DurablePaidHTTPClient(
        route=generation_route, ledger=ledger, delegate=delegate, local_runtime=runtime
    )
    embedding = DurablePaidHTTPClient(
        route=embedding_route, ledger=ledger, delegate=delegate, local_runtime=runtime
    )
    assert (
        generation.post("/v1/chat/completions", json=payload(generation_route)).status_code == 429
    )
    embedding.post("/v1/embeddings", json=payload(embedding_route))
    generation.post("/v1/chat/completions", json=payload(generation_route))
    assert sent[0][1] == pytest.approx(1.0)
    assert sent[1][1] >= sent[0][1] + 5
    assert ledger.reserve.call_count == ledger.dispatch.call_count == 3
    assert [call.args[0] for call in ledger.dispatch.call_args_list] == ["first", "second", "third"]
    ledger.mark_uncertain.assert_called_once_with("first", "provider_response_usage_unknown")


def test_retry_after_http_date_defers_provider_without_resetting_deadline():
    clock = Clock()
    runtime = local_runtime(clock)
    response = httpx.Response(503, headers={"Retry-After": "Sun, 04 Oct 2026 00:00:07 GMT"})
    runtime.record_retry_after("openai", response)
    clock.now = 2
    assert runtime.limiter.delay_until_available("openai:model", 1) == 5


@pytest.mark.parametrize("retry_after", ["10", "inf"])
def test_retry_after_beyond_graceful_budget_cancels_before_new_reservation(retry_after):
    clock = Clock()
    runtime = local_runtime(clock)
    resolved = route()
    ledger = Mock()
    ledger.reserve.return_value = "first"
    delegate = SimpleNamespace(
        post=lambda *args, **kwargs: httpx.Response(
            429, headers={"Retry-After": retry_after}, json={"error": "synthetic"}
        )
    )
    client = DurablePaidHTTPClient(
        route=resolved, ledger=ledger, delegate=delegate, local_runtime=runtime
    )
    budget = RuntimeBudget(
        clock.origin + dt.timedelta(seconds=5),
        clock.origin + dt.timedelta(seconds=6),
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    with bind_budget(budget):
        client.post("/v1/chat/completions", json=payload(resolved))
        with pytest.raises(RuntimeCancelled):
            client.post("/v1/chat/completions", json=payload(resolved))
    assert clock.now == pytest.approx(1.0)
    assert ledger.reserve.call_count == ledger.dispatch.call_count == 1
    assert budget.graceful_deadline == clock.origin + dt.timedelta(seconds=5)


def test_fatal_deadline_after_reservation_cancels_pre_dispatch_accounting():
    from services.personal.deadlines import check_budget

    clock = Clock()
    ledger, delegate = Mock(), Mock()
    resolved = route()

    def reserve(*args, **kwargs):
        clock.now = 5
        return "reserved"

    ledger.reserve.side_effect = reserve
    ledger.cancel_before_dispatch.side_effect = lambda *_args: check_budget()
    client = DurablePaidHTTPClient(route=resolved, ledger=ledger, delegate=delegate)
    budget = RuntimeBudget(
        clock.origin + dt.timedelta(seconds=5),
        clock.origin + dt.timedelta(seconds=6),
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )
    with bind_budget(budget), pytest.raises(RuntimeCancelled):
        client.post("/v1/chat/completions", json=payload(resolved))
    ledger.cancel_before_dispatch.assert_called_once_with("reserved")
    ledger.dispatch.assert_not_called()
    delegate.post.assert_not_called()
    ledger.mark_uncertain.assert_not_called()


def test_impossible_provider_tpm_and_invalid_payload_cannot_reserve_or_send():
    clock = Clock()
    runtime = local_runtime(clock, tpm=1)
    resolved = route()
    ledger, delegate = Mock(), Mock()
    client = DurablePaidHTTPClient(
        route=resolved, ledger=ledger, delegate=delegate, local_runtime=runtime
    )
    with pytest.raises(PaidWorkBlocked, match="token ceiling"):
        client.post("/v1/chat/completions", json=payload(resolved))
    with pytest.raises(PaidWorkBlocked):
        client.post("/v1/chat/completions", json={**payload(resolved), "model": "wrong"})
    ledger.reserve.assert_not_called()
    delegate.post.assert_not_called()


def test_actual_provider_calls_are_serialized_across_generation_and_embedding():
    clock = Clock()
    runtime = local_runtime(clock, rpm=600, tpm=1_000_000)
    clock.now = 1  # Synthetic refill, no initial burst.
    started, release, second_attempted = threading.Event(), threading.Event(), threading.Event()
    sent, errors = [], []
    ledger = Mock()
    ledger.reserve.side_effect = ["first", "second"]
    generation_route, embedding_route = route(), route("embedding")

    def send(path, **kwargs):
        sent.append(path)
        if path == "/v1/chat/completions":
            started.set()
            assert release.wait(timeout=2)
        return successful_response("embedding" if path == "/v1/embeddings" else "generation")

    clients = [
        DurablePaidHTTPClient(
            route=resolved,
            ledger=ledger,
            delegate=SimpleNamespace(post=send),
            local_runtime=runtime,
        )
        for resolved in (generation_route, embedding_route)
    ]

    def execute(index):
        if index == 1:
            second_attempted.set()
        try:
            resolved = (generation_route, embedding_route)[index]
            clients[index].post(
                ("/v1/chat/completions", "/v1/embeddings")[index], json=payload(resolved)
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=execute, args=(i,)) for i in range(2)]
    threads[0].start()
    assert started.wait(timeout=2)
    threads[1].start()
    assert second_attempted.wait(timeout=2)
    assert sent == ["/v1/chat/completions"]
    release.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert errors == []
    assert sent == ["/v1/chat/completions", "/v1/embeddings"]


def test_orchestrator_and_embedding_share_one_runner_cache_and_governor():
    from services.personal.paid_runtime import (
        build_paid_embedding_provider,
        build_paid_orchestrator,
    )

    settings = Settings(_env_file=None, app_env="test", openai_api_key="synthetic-key")
    runtime = PersonalPaidRuntime.from_settings(settings, {"openai"})
    ledger = Mock()
    embedding = build_paid_embedding_provider(
        settings=settings, route=route("embedding"), ledger=ledger, local_runtime=runtime
    )
    one = build_paid_orchestrator(
        settings=settings, session=Mock(), route=route(), ledger=ledger, local_runtime=runtime
    )
    two = build_paid_orchestrator(
        settings=settings, session=Mock(), route=route(), ledger=ledger, local_runtime=runtime
    )
    try:
        assert one._cache is two._cache is runtime.cache
        assert one._providers_by_tier["T1"][0]._client.local_runtime is runtime
        assert embedding._client.local_runtime is runtime
        assert one._limiter is None  # Physical admission must not be double charged.
    finally:
        embedding.close()
        one.close()
        two.close()


@pytest.mark.parametrize("transport", ["subprocess", "celery"])
def test_personal_runner_live_assembly_never_imports_constructs_or_closes_redis(
    monkeypatch, transport
):
    from services.personal import runner

    configured = Settings(
        _env_file=None,
        app_env="test",
        personal_processing_mode="personal",
        personal_processing_transport=transport,
        redis_url="",
    )
    spec = runner._RuntimeSpec(
        route=model_route(),
        authorized_spend_usd=0,
        workspace_id=uuid.UUID(int=1),
    )
    monkeypatch.setattr(runner, "_runtime_spec", lambda *args, **kwargs: spec)
    ledger = SimpleNamespace(blocked_code=None)
    monkeypatch.setattr(runner, "SpendingLedger", lambda **kwargs: ledger)
    assemblies = []
    monkeypatch.setattr(
        runner,
        "build_paid_embedding_provider",
        lambda **kwargs: assemblies.append(kwargs) or SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        runner,
        "build_paid_orchestrator",
        lambda **kwargs: assemblies.append(kwargs) or object(),
    )

    def coordinate(*args, **kwargs):
        kwargs["orchestrator_factory"](object(), spec.route)
        return SimpleNamespace(
            run_id=uuid.UUID(int=2),
            report=None,
            feeds_attempted=0,
            feeds_succeeded=0,
            feeds_failed=0,
            articles_captured=0,
            articles_admitted=0,
            events_observed=0,
            claims_supported=0,
        )

    monkeypatch.setattr(runner, "run_personal_daily", coordinate)
    imports = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "redis" and not name.startswith("redis.")
        return imports(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def scalar(self, *args):
            return "succeeded"

    result = runner.execute_personal_task(
        uuid.UUID(int=2),
        uuid.UUID(int=3),
        settings_factory=lambda: configured,
        session_factory=Session,
    )
    assert result["status"] == "succeeded"
    assert len(assemblies) == 2
    assert assemblies[0]["local_runtime"] is assemblies[1]["local_runtime"]
    assert assemblies[1].get("redis_client") is None


def test_fresh_runner_import_has_no_redis_or_celery_side_effects():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import services.personal.runner; "
            "assert not any(name in sys.modules "
            "for name in ('redis', 'celery', 'workers.celery_app'))",
        ],
        cwd=ROOT,
        env={**os.environ, "APP_ENV": "test"},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_repeated_fresh_interpreters_have_empty_buckets_and_physical_retry_pacing():
    script = """
import json, os, time
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
from services.llm.limiter import LocalProviderRateLimiter
from services.personal.local_runtime import PersonalPaidRuntime
from services.personal.paid_runtime import DurablePaidHTTPClient
from services.personal.spending import PaidRoute
from tests.unit.test_personal_spending import route_mapping

mapping = route_mapping()
mapping["deadline_seconds"] = 1
route = PaidRoute.from_mapping(mapping, role="generation")
started = time.monotonic()
limiter = LocalProviderRateLimiter(
    rpm_limits={"openai": 6000}, tpm_limits={"openai": 10_000_000}
)
runtime = PersonalPaidRuntime(limiter)
assert not limiter.consume("openai:model", 1)
sent = []
ledger = Mock()
ledger.reserve.side_effect = ["first", "second"]
responses = iter([
    httpx.Response(429, headers={"Retry-After": ".02"}, json={"error": "synthetic"}),
    httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
])
def send(*args, **kwargs):
    sent.append(time.monotonic() - started)
    return next(responses)
client = DurablePaidHTTPClient(
    route=route, ledger=ledger, delegate=SimpleNamespace(post=send), local_runtime=runtime
)
for _ in range(2):
    client.post("/v1/chat/completions", json={"model": route.model, "max_tokens": 10})
print(json.dumps({"pid": os.getpid(), "sent": sent, "reservations": ledger.reserve.call_count}))
"""
    observed = []
    for _ in range(3):
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env={**os.environ, "APP_ENV": "test"},
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        observed.append(json.loads(result.stdout))
    assert len({sample["pid"] for sample in observed}) == 3
    for sample in observed:
        assert sample["sent"][0] >= 0.009
        assert sample["sent"][1] - sample["sent"][0] >= 0.019
        assert sample["reservations"] == 2
