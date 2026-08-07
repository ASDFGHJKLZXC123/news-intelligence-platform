"""Deterministic unit tests for the LLM orchestration runtime.

Provider transport is mocked end to end (``httpx.MockTransport``); no test touches the
network.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import httpx
import pytest

from db.models.core import Job, LLMRun
from packages.config.settings import Settings
from services.llm.adapters import LLMInvocationMode, LLMInvocationRequest
from services.llm.cache import (
    InMemoryLLMPromptCache,
    RedisLLMPromptCache,
    make_llm_request_cache_key,
)
from services.llm.contracts import (
    EventExtraction,
    list_contract_schemas,
    llm_contract_json_schema,
    validate_llm_contract_payload,
)
from services.llm.fake_providers import CallableLLMProvider, ScriptedLLMProvider
from services.llm.http_providers import (
    LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODES,
    AnthropicMessagesProvider,
    DeepSeekChatCompletionsProvider,
    GeminiInteractionsProvider,
    LLMBatchModeUnsupported,
    LLMProviderError,
    LLMProviderOutputError,
    OpenAIChatCompletionsProvider,
    _json_schema_example,
    build_providers_by_tier,
    provider_failure_diagnostic_code,
)
from services.llm.limiter import (
    CompositeProviderLimiter,
    InMemoryTokenBucketLimiter,
    RedisTokenBucketLimiter,
    build_provider_rate_limiter,
)
from services.llm.orchestrator import (
    LLMConfigurationError,
    LLMInvocationFailure,
    LLMOrchestrator,
    LLMOrchestratorRequest,
    LLMValidationFailure,
)
from services.llm.policy import LLMBudgetPolicy, LLMRoutingContext, LLMTier
from services.llm.pricing import estimate_completion_cost_usd
from services.llm.repository import InMemoryLLMRuntimeRepository
from services.llm.runtime import build_production_orchestrator
from services.llm.selection import estimate_token_count, select_representative_articles

_ALLOWED_IDS: tuple[str, ...] = ("event-1", "article-1")


def _event_payload(*, with_reason: bool = False) -> dict[str, Any]:
    """A payload that satisfies the real EventExtraction contract."""

    payload: dict[str, Any] = {
        "schema_name": "EventExtraction",
        "schema_version": "1.0",
        "prompt_template_version": "v1",
    }
    if with_reason:
        payload["events"] = []
        payload["no_finding_reason"] = "No high-probability findings remained."
    else:
        payload["events"] = [
            {
                "event_id": "event-1",
                "summary": "Policy surprise",
                "key_facts": ["The central bank raised rates by 50bp."],
                "why_it_matters": "Rate path repricing hits funding-sensitive sectors.",
                "when": {"date": "2026-07-01", "precision": "day"},
                "evidence_article_ids": ["article-1"],
                "impact_direction": "negative",
                "confidence": 0.63,
            }
        ]

    return payload


def _invalid_event_payload() -> dict[str, Any]:
    """Empty findings with no abstain reason: rejected by the contract's model validator."""

    return {
        "schema_name": "EventExtraction",
        "schema_version": "1.0",
        "prompt_template_version": "v1",
        "events": [],
    }


def _make_job(job_key: str) -> Job:
    now = datetime.datetime.now(datetime.UTC)
    return Job(
        job_key=job_key,
        job_type="llm",
        state="queued",
        attempt=1,
        max_attempts=3,
        related_ids=None,
        error=None,
        safe_to_rerun=True,
        created_at=now,
        updated_at=now,
    )


def _risk_warning_payload(*, risk_score: float) -> dict[str, Any]:
    """A RiskWarning payload: its risk_score carries more precision than the contract keeps."""

    return {
        "schema_name": "RiskWarning",
        "schema_version": "1.0",
        "prompt_template_version": "v1",
        "warnings": [
            {
                "title": "Liquidity compression",
                "detail": "Near-term funding markets tighten.",
                "risk_score": risk_score,
                "probability": 0.72,
                "horizon": "within_18m",
                "confidence": 0.83,
            }
        ],
    }


def _forecast_scenarios_payload(*, risk_score: float) -> dict[str, Any]:
    """A MECE ForecastScenarios payload whose base case also carries a derived severity."""

    return {
        "schema_name": "ForecastScenarios",
        "schema_version": "1.0",
        "prompt_template_version": "v1",
        "scenarios": [
            {
                "scenario_name": "base_case",
                "narrative": "Funding conditions stay orderly.",
                "probability": 0.6,
                "risk_score": risk_score,
                "horizon": "within_18m",
                "impact_direction": "mixed",
                "confidence": 0.7,
            },
            {
                "scenario_name": "downside_case",
                "narrative": "A funding squeeze forces asset sales.",
                "probability": 0.4,
                "risk_score": 20.0,
                "horizon": "within_18m",
                "impact_direction": "negative",
                "confidence": 0.55,
            },
        ],
    }


def _make_request(
    *,
    job_key: str,
    requested_tier: LLMTier = LLMTier.T1,
    requested_schema: str = "EventExtraction",
    is_realtime: bool = False,
    ranking: int | None = None,
    is_essential: bool = True,
    articles: tuple[dict[str, Any], ...] = (),
    allowed_ids: tuple[str, ...] | None = _ALLOWED_IDS,
    context: dict[str, Any] | None = None,
) -> LLMOrchestratorRequest:
    return LLMOrchestratorRequest(
        job=_make_job(job_key),
        prompt_name="news",
        prompt_version="v1",
        prompt_template_version="v1",
        requested_schema=requested_schema,
        prompt="Summarize representative events.",
        requested_tier=requested_tier,
        risk_level="low",
        trailing_7d_p90_hotness=None,
        is_realtime=is_realtime,
        ranking=ranking,
        is_essential=is_essential,
        articles=articles,
        allowed_ids=allowed_ids,
        context=context or {"scenario": "test"},
    )


def _cache_key_for(
    request: LLMOrchestratorRequest,
    provider: ScriptedLLMProvider,
    *,
    mode: LLMInvocationMode = LLMInvocationMode.BATCH,
) -> str:
    """Recompute the key the orchestrator derives for `request` when it selects no articles."""

    return make_llm_request_cache_key(
        request=LLMInvocationRequest(
            prompt_name=request.prompt_name,
            prompt_version=request.prompt_version,
            prompt_template_version=request.prompt_template_version,
            prompt=request.prompt,
            requested_schema=request.requested_schema,
            context={
                "selected_article_count": 0,
                "selected_article_tokens": 0,
                "selected_article_dropped": 0,
                "selected_article_truncated_by_budget": False,
                **(request.context or {}),
            },
            mode=mode,
        ),
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        model_version=provider.model_version,
        mode=mode,
    )


def _invocation_request() -> LLMInvocationRequest:
    return LLMInvocationRequest(
        prompt_name="news",
        prompt_version="v1",
        prompt_template_version="v1",
        prompt="Summarize representative events.",
        requested_schema="EventExtraction",
    )


def _routing_context(
    *,
    requested_tier: LLMTier = LLMTier.T1,
    risk_level: str = "low",
    current_hotness: float = 0.0,
    p90_hotness: float | None = None,
    ranking: int | None = None,
    is_realtime: bool = False,
    is_essential: bool = True,
) -> LLMRoutingContext:
    return LLMRoutingContext(
        risk_level=risk_level,
        current_event_hotness=current_hotness,
        trailing_7d_p90_hotness=p90_hotness,
        is_realtime=is_realtime,
        is_essential=is_essential,
        ranking=ranking,
        requested_tier=requested_tier,
    )


def _mock_client(handler: Any, base_url: str) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)


class FakeRedis:
    def __init__(self) -> None:
        self.kv_store: dict[str, Any] = {}
        self.hash_store: dict[str, dict[str, Any]] = {}
        self.calls: list[Any] = []

    def get(self, key: str) -> Any:
        return self.kv_store.get(key)

    def set(self, key: str, value: str) -> None:
        self.calls.append(("set", key, value))
        self.kv_store[key] = value

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        self.calls.append(("setex", key, ttl_seconds, value))
        self.kv_store[key] = value

    def hgetall(self, key: str) -> dict[str, Any]:
        self.calls.append(("hgetall", key))
        return self.hash_store.get(key, {}).copy()

    def hset(self, key: str, *, mapping: Mapping[str, Any]) -> None:
        self.calls.append(("hset", key, dict(mapping)))
        self.hash_store[key] = dict(mapping)

    def expire(self, key: str, seconds: int) -> None:
        self.calls.append(("expire", key, seconds))


class SpendAwareRepository(InMemoryLLMRuntimeRepository):
    def __init__(self, spent_usd: float) -> None:
        super().__init__()
        self._spent_usd = spent_usd

    def monthly_spend_usd(self, as_of: datetime.datetime | None = None) -> float:
        return self._spent_usd


class TrackingProvider(ScriptedLLMProvider):
    def __init__(
        self,
        scripts: tuple[Mapping[str, Any] | str | Exception | None, ...],
        *,
        provider_name: str = "tracking-provider",
        model_name: str = "tracking-model",
        model_version: str = "current",
    ) -> None:
        super().__init__(
            scripts=list(scripts),
            provider_name=provider_name,
            model_name=model_name,
            model_version=model_version,
        )
        self.invoke_calls = 0
        self.invoke_batch_calls = 0

    def invoke(self, request: LLMInvocationRequest):
        self.invoke_calls += 1
        return super().invoke(request)

    def invoke_batch(self, requests: tuple[LLMInvocationRequest, ...]):
        self.invoke_batch_calls += 1
        return super().invoke_batch(requests)


class CloseTrackingProvider(TrackingProvider):
    def __init__(
        self,
        scripts: tuple[Mapping[str, Any] | str | Exception | None, ...],
        *,
        provider_name: str = "tracking-provider",
        model_name: str = "tracking-model",
        close_error: Exception | None = None,
    ) -> None:
        super().__init__(
            scripts,
            provider_name=provider_name,
            model_name=model_name,
        )
        self.close_count = 0
        self.close_error = close_error

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class RecordingLimiter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def consume(
        self,
        key: str,
        tokens: int,
        *,
        now: float | None = None,
    ) -> bool:
        self.calls.append((key, tokens))
        return True


# --- Cache and limiter ------------------------------------------------------------


def test_cache_key_is_stable_for_equivalent_requests() -> None:
    request = LLMInvocationRequest(
        prompt_name="news",
        prompt_version="v1",
        prompt_template_version="v1",
        prompt="Summarize.",
        requested_schema="EventExtraction",
        context={"x": 1},
    )
    first_key = make_llm_request_cache_key(
        request=request,
        provider_name="anthropic",
        model_name="claude-haiku-4-5",
        model_version="current",
        mode=LLMInvocationMode.BATCH,
    )
    second_key = make_llm_request_cache_key(
        request=request,
        provider_name="anthropic",
        model_name="claude-haiku-4-5",
        model_version="current",
        mode=LLMInvocationMode.BATCH,
    )
    changed_key = make_llm_request_cache_key(
        request=LLMInvocationRequest(
            prompt_name="news",
            prompt_version="v1",
            prompt_template_version="v1",
            prompt="Different prompt",
            requested_schema="EventExtraction",
            context={"x": 1},
        ),
        provider_name="anthropic",
        model_name="claude-haiku-4-5",
        model_version="current",
        mode=LLMInvocationMode.BATCH,
    )
    changed_adapter_key = make_llm_request_cache_key(
        request=request,
        provider_name="gemini",
        model_name="gemini-3.6-flash",
        model_version="current",
        mode=LLMInvocationMode.BATCH,
        adapter_variant={"thinking_level": "low"},
    )
    different_thinking_key = make_llm_request_cache_key(
        request=request,
        provider_name="gemini",
        model_name="gemini-3.6-flash",
        model_version="current",
        mode=LLMInvocationMode.BATCH,
        adapter_variant={"thinking_level": "medium"},
    )

    assert first_key == second_key
    assert first_key != changed_key
    assert changed_adapter_key != different_thinking_key


def test_cache_hit_persists_zero_cost_run() -> None:
    provider = TrackingProvider(
        scripts=(_event_payload(),),
        provider_name="cache-provider",
        model_name="cache-model",
    )
    provider.cache_variant = {"report_composition_budget_instruction": "v1"}
    repository = InMemoryLLMRuntimeRepository()
    cache = InMemoryLLMPromptCache(default_ttl_seconds=None)
    request = _make_request(job_key="zero-cost", context={"scenario": "cached"})

    invocation = LLMInvocationRequest(
        prompt_name=request.prompt_name,
        prompt_version=request.prompt_version,
        prompt_template_version=request.prompt_template_version,
        prompt=request.prompt,
        requested_schema=request.requested_schema,
        context={
            "selected_article_count": 0,
            "selected_article_tokens": 0,
            "selected_article_dropped": 0,
            "selected_article_truncated_by_budget": False,
            "scenario": "cached",
        },
        mode=LLMInvocationMode.BATCH,
    )
    cache_key = make_llm_request_cache_key(
        request=invocation,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        model_version=provider.model_version,
        mode=LLMInvocationMode.BATCH,
        adapter_variant=provider.cache_variant,
    )
    cache.set(cache_key, _event_payload())

    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (provider,)},
        cache=cache,
    )
    result = orchestrator.run(request)

    assert result.cache_hit is True
    assert result.run.cost_usd == 0.0
    assert result.run.status == "cached"
    assert result.run.input_refs["cache_hit"] is True
    assert result.run.model_params["adapter_params"] == {
        "report_composition_budget_instruction": "v1"
    }
    assert provider.invoke_calls == 0
    assert provider.invoke_batch_calls == 0


def test_cached_run_output_is_json_serializable() -> None:
    """The cache round-trips through JSON, so a stored run must carry no date objects."""

    cache = InMemoryLLMPromptCache(default_ttl_seconds=None)
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (TrackingProvider((_event_payload(),)),)},
        cache=cache,
    )
    result = orchestrator.run(_make_request(job_key="json-output"))

    assert result.run.output["events"][0]["when"]["date"] == "2026-07-01"
    json.dumps(result.run.output)  # would raise TypeError on a datetime.date


def test_run_output_derives_severity_while_the_cache_stores_the_provider_shape() -> None:
    """The two representations diverge by design: `output` derives, the cache must replay.

    `output` is the normalized, auditable, downstream contract, so it carries the derived
    band. The cache entry is a stand-in for the provider response and is fed back through
    the same validator, so it must carry only what a provider may legally emit — the
    normalized score, never `severity`.
    """

    cache = InMemoryLLMPromptCache(default_ttl_seconds=None)
    provider = TrackingProvider(
        (_risk_warning_payload(risk_score=75.005),),
        provider_name="score-provider",
        model_name="score-model",
    )
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,)},
        cache=cache,
    )
    request = _make_request(
        job_key="normalized-output",
        requested_schema="RiskWarning",
        allowed_ids=(),
    )

    result = orchestrator.run(request)

    warning = result.run.output["warnings"][0]
    assert warning["risk_score"] == 75.01  # 75.005 rounded half-up, not truncated
    assert warning["severity"] == "critical"  # the band of the normalized score, not of 75.005

    cached = cache.get(_cache_key_for(request, provider))
    cached_warning = cached["warnings"][0]
    assert cached_warning["risk_score"] == 75.01  # normalization survives into the cache
    assert "severity" not in cached_warning  # ...but the computed field does not
    assert cached != result.run.output


@pytest.mark.parametrize(
    ("requested_schema", "findings_key", "payload"),
    [
        ("RiskWarning", "warnings", _risk_warning_payload(risk_score=75.005)),
        ("ForecastScenarios", "scenarios", _forecast_scenarios_payload(risk_score=75.005)),
    ],
)
def test_a_cache_hit_replays_through_validation_and_re_derives_severity(
    requested_schema: str, findings_key: str, payload: dict[str, Any]
) -> None:
    """A cached run must revalidate as a provider payload would, without a second call.

    Regression: the cache used to store `run.output`, whose computed `severity` the
    provider-shaped validation path then rejected under `extra="forbid"` — turning every
    cache hit into two spurious validation_failed rows and a hard failure.
    """

    cache = InMemoryLLMPromptCache(default_ttl_seconds=None)
    repository = InMemoryLLMRuntimeRepository()
    provider = TrackingProvider(
        (payload,), provider_name="replay-provider", model_name="replay-model"
    )
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (provider,)},
        cache=cache,
    )

    # Two distinct jobs whose cache-relevant fields are identical, so the second one hits.
    first_request = _make_request(
        job_key=f"{requested_schema}-first", requested_schema=requested_schema, allowed_ids=()
    )
    replay_request = _make_request(
        job_key=f"{requested_schema}-replay", requested_schema=requested_schema, allowed_ids=()
    )

    first = orchestrator.run(first_request)
    replayed = orchestrator.run(replay_request)

    # The provider is called exactly once: the replay is served entirely from the cache.
    assert (first.cache_hit, replayed.cache_hit) == (False, True)
    assert (provider.invoke_batch_calls, provider.invoke_calls) == (1, 0)
    assert provider.call_count == 1

    # No validation_failed audit row, and the replay is persisted as a zero-cost cached run.
    assert [entry.status for entry in repository.llm_runs] == ["succeeded", "cached"]
    assert replayed.run.cost_usd == 0.0
    assert repository.last_job_for_key(f"{requested_schema}-replay").state == "succeeded"

    # The replay reconstructs the identical downstream contract: normalized score retained,
    # severity re-derived from it rather than read back off the wire.
    finding = replayed.run.output[findings_key][0]
    assert finding["risk_score"] == 75.01
    assert finding["severity"] == "critical"
    assert getattr(replayed.contract, findings_key)[0].severity.value == "critical"
    assert replayed.run.output == first.run.output

    # The cached payload is provider-shaped, and raw_output reports it honestly: a cache hit
    # never claims the provider sent a severity it cannot emit.
    cached = cache.get(_cache_key_for(first_request, provider))
    assert "severity" not in cached[findings_key][0]
    assert "severity" not in replayed.run.raw_output[findings_key][0]
    assert replayed.run.raw_output == cached


def test_successful_run_preserves_the_raw_provider_payload_beside_the_normalized_output() -> None:
    """Audit trail: the run keeps what the provider sent, not only what we normalized it to."""

    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (TrackingProvider((_risk_warning_payload(risk_score=75.005),)),)},
    )

    result = orchestrator.run(
        _make_request(job_key="raw-audit", requested_schema="RiskWarning", allowed_ids=())
    )

    assert result.run.raw_output["warnings"][0]["risk_score"] == 75.005
    assert "severity" not in result.run.raw_output["warnings"][0]
    assert result.run.output["warnings"][0]["risk_score"] == 75.01
    assert repository.llm_runs[-1].raw_output == result.run.raw_output


def test_a_failed_run_records_no_raw_output() -> None:
    """raw_output is the audit companion of a normalized success, not a dumping ground."""

    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (TrackingProvider((_invalid_event_payload(),)),)},
    )

    with pytest.raises(LLMValidationFailure):
        orchestrator.run(_make_request(job_key="no-raw-output"))

    assert repository.llm_runs
    assert all(run.status == "validation_failed" for run in repository.llm_runs)
    assert all(run.raw_output is None for run in repository.llm_runs)


def test_in_memory_and_redis_prompt_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.llm.cache as cache_module

    mem_cache = InMemoryLLMPromptCache(default_ttl_seconds=1)

    monkeypatch.setattr(cache_module.time, "time", lambda: 100.0)
    mem_cache.set("k1", {"value": 1})
    assert mem_cache.get("k1") == {"value": 1}
    monkeypatch.setattr(cache_module.time, "time", lambda: 100.5)
    assert mem_cache.get("k1") == {"value": 1}
    monkeypatch.setattr(cache_module.time, "time", lambda: 102.1)
    assert mem_cache.get("k1") is None

    fake_redis = FakeRedis()
    redis_cache = RedisLLMPromptCache(redis_client=fake_redis, namespace="llm")
    redis_cache.set("k2", {"value": 2}, ttl_seconds=30)
    assert fake_redis.kv_store["llm:k2"] == '{"value":2}'
    assert redis_cache.get("k2") == {"value": 2}


def test_in_memory_and_redis_token_bucket_behavior() -> None:
    in_memory = InMemoryTokenBucketLimiter(capacity=2, refill_per_second=1.0, time_func=lambda: 0.0)
    assert in_memory.consume("a", 2, now=0.0) is True
    assert in_memory.consume("a", 1, now=0.0) is False
    assert in_memory.consume("a", 1, now=2.0) is True

    fake_redis = FakeRedis()
    redis_bucket = RedisTokenBucketLimiter(
        redis_client=fake_redis,
        capacity=2,
        refill_per_second=1.0,
        namespace="bucket",
    )
    assert redis_bucket.consume("a", 2, now=0.0) is True
    assert redis_bucket.consume("a", 1, now=0.0) is False
    assert redis_bucket.consume("a", 1, now=2.0) is True


def test_production_limiter_enforces_provider_rpm_and_tpm_budgets() -> None:
    limiter = build_provider_rate_limiter(
        redis_client=FakeRedis(),
        rpm_limits={"anthropic": 2},
        tpm_limits={"anthropic": 3},
    )

    assert isinstance(limiter, CompositeProviderLimiter)
    assert limiter.consume("anthropic:model", 2, now=0.0) is True
    # The second request fits RPM but would exceed TPM.
    assert limiter.consume("anthropic:model", 2, now=0.0) is False
    # Denied token requests conservatively consume RPM, so no third request leaks through.
    assert limiter.consume("anthropic:model", 1, now=0.0) is False
    # Both minute budgets refill from their settings-derived rates.
    assert limiter.consume("anthropic:model", 3, now=60.0) is True


def test_production_limiter_rejects_unconfigured_provider() -> None:
    limiter = build_provider_rate_limiter(
        redis_client=FakeRedis(),
        rpm_limits={"anthropic": 1},
        tpm_limits={"anthropic": 1},
    )

    assert limiter.consume("openai:model", 1, now=0.0) is False


def test_orchestrator_limiter_reserves_schema_and_example_overhead() -> None:
    limiter = RecordingLimiter()
    provider = TrackingProvider(
        (_event_payload(),),
        provider_name="deepseek",
        model_name="deepseek-v4-flash",
    )
    settings = Settings()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,)},
        limiter=limiter,
    )
    request = _make_request(job_key="structured-limit-reservation")

    orchestrator.run(request)

    schema_text = json.dumps(
        llm_contract_json_schema("EventExtraction"),
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_tokens = (
        estimate_token_count(request.prompt)
        + (2 * estimate_token_count(schema_text))
        + 64
        + settings.llm_tier_max_output_tokens["T1"]
    )
    assert limiter.calls == [("deepseek:deepseek-v4-flash", expected_tokens)]


# --- Routing ----------------------------------------------------------------------


@pytest.mark.parametrize("tier", [LLMTier.T0, LLMTier.T1, LLMTier.T2])
def test_requested_base_tier_is_directly_reachable(tier: LLMTier) -> None:
    policy = LLMBudgetPolicy(Settings())

    decision = policy.decide(_routing_context(requested_tier=tier), monthly_spend_usd=0.0)

    assert decision.tier is tier
    assert decision.degraded_reasons == ()


def test_t3_cannot_be_requested_directly() -> None:
    with pytest.raises(ValueError, match="cannot be requested directly"):
        _routing_context(requested_tier=LLMTier.T3)


def test_t3_routing_from_high_risk() -> None:
    policy = LLMBudgetPolicy(Settings())

    high_risk = policy.decide(
        _routing_context(risk_level="critical", current_hotness=10.0),
        monthly_spend_usd=0.0,
    )

    assert high_risk.tier is LLMTier.T3


@pytest.mark.parametrize(
    ("current_hotness", "expected_tier"),
    [(74.99, LLMTier.T1), (75.0, LLMTier.T3), (75.01, LLMTier.T3)],
)
def test_t3_hotness_compares_current_event_to_trailing_p90(
    current_hotness: float, expected_tier: LLMTier
) -> None:
    decision = LLMBudgetPolicy(Settings()).decide(
        _routing_context(current_hotness=current_hotness, p90_hotness=75.0),
        monthly_spend_usd=0.0,
    )

    assert decision.tier is expected_tier


def test_t3_hotness_does_not_escalate_without_a_trailing_threshold() -> None:
    decision = LLMBudgetPolicy(Settings()).decide(
        _routing_context(current_hotness=100.0, p90_hotness=None),
        monthly_spend_usd=0.0,
    )

    assert decision.tier is LLMTier.T1


def test_t3_escalation_overrides_the_requested_tier() -> None:
    policy = LLMBudgetPolicy(Settings())

    decision = policy.decide(
        _routing_context(requested_tier=LLMTier.T0, risk_level="high"),
        monthly_spend_usd=0.0,
    )

    assert decision.tier is LLMTier.T3


def test_budget_degradation_ordering_and_enforcement_flag() -> None:
    enforcing = LLMBudgetPolicy(Settings(llm_monthly_budget_usd=10.0, llm_budget_enforced=True))
    disabled = LLMBudgetPolicy(Settings(llm_monthly_budget_usd=10.0, llm_budget_enforced=False))
    context = _routing_context(risk_level="critical", current_hotness=10.0, ranking=11)

    decided = enforcing.decide(context, monthly_spend_usd=20.0)
    denied = disabled.decide(context, monthly_spend_usd=20.0)

    assert decided.tier is LLMTier.T1
    assert decided.degraded_reasons == ("t3_disabled", "t2_top_n_restriction")
    assert denied.tier is LLMTier.T3
    assert denied.degraded_reasons == ()


def test_requested_t2_degrades_to_t1_outside_the_top_n() -> None:
    policy = LLMBudgetPolicy(Settings(llm_monthly_budget_usd=10.0, llm_budget_enforced=True))

    decision = policy.decide(
        _routing_context(requested_tier=LLMTier.T2, ranking=11),
        monthly_spend_usd=20.0,
    )

    assert decision.tier is LLMTier.T1
    assert decision.degraded_reasons == ("t2_top_n_restriction",)


def test_orchestrator_routes_to_the_requested_tier_provider() -> None:
    t1_provider = TrackingProvider((_event_payload(),), provider_name="t1")
    t2_provider = TrackingProvider((_event_payload(),), provider_name="t2")
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (t1_provider,), "T2": (t2_provider,)},
    )

    result = orchestrator.run(_make_request(job_key="tier-2", requested_tier=LLMTier.T2))

    assert result.tier is LLMTier.T2
    assert result.run.provider == "t2"
    assert t1_provider.call_count == 0


# --- Pricing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_name", "model_name", "expected"),
    [
        ("anthropic", "claude-haiku-4-5", 3.50),
        ("gemini", "gemini-3.6-flash", 5.25),
        ("deepseek", "deepseek-v4-flash", 0.28),
    ],
)
def test_estimate_completion_cost_usd_for_supported_models(
    provider_name: str,
    model_name: str,
    expected: float,
) -> None:
    settings = Settings()

    cost = estimate_completion_cost_usd(
        settings=settings,
        provider_name=provider_name,
        model_name=model_name,
        input_tokens=1_000_000,
        output_tokens=500_000,
    )

    assert cost == pytest.approx(expected)


def test_estimator_returns_zero_for_manual_unknown_model_but_factory_rejects_it() -> None:
    settings = Settings()

    missing = estimate_completion_cost_usd(
        settings=settings,
        provider_name="unknown",
        model_name="missing",
        input_tokens=1,
        output_tokens=1,
    )

    assert missing == 0.0


def test_batch_discount_applies_only_to_actual_batch_calls() -> None:
    settings = Settings(llm_batch_discount_multiplier=0.5)
    kwargs = {
        "settings": settings,
        "provider_name": "anthropic",
        "model_name": "claude-haiku-4-5",
        "input_tokens": 1_001,
        "output_tokens": 201,
    }

    realtime = estimate_completion_cost_usd(**kwargs, invocation_mode=LLMInvocationMode.REALTIME)
    batch = estimate_completion_cost_usd(**kwargs, invocation_mode=LLMInvocationMode.BATCH)

    assert realtime == pytest.approx(0.002006)
    assert batch == pytest.approx(0.001003)


def test_per_call_cost_keeps_sub_cent_precision_across_a_month() -> None:
    settings = Settings()

    per_call = estimate_completion_cost_usd(
        settings=settings,
        provider_name="anthropic",
        model_name="claude-haiku-4-5",
        input_tokens=1_000,
        output_tokens=200,
    )

    # A T1 classification costs a fifth of a cent. Rounding per call would record it as
    # $0.00, so month-to-date spend would stay at zero and never reach the budget ceiling.
    assert per_call == pytest.approx(0.002)
    assert round(per_call, 2) == 0.0

    # Hundreds of events/day across the T1 stages: the rounded-per-call total is $0.
    monthly_spend = sum(per_call for _ in range(6_000))
    assert monthly_spend == pytest.approx(12.0)
    assert monthly_spend > settings.llm_monthly_budget_usd


def test_monthly_spend_aggregates_persisted_run_costs() -> None:
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=(repository := InMemoryLLMRuntimeRepository()),
        providers_by_tier={
            "T1": (
                TrackingProvider(
                    (_event_payload(),),
                    provider_name="anthropic",
                    model_name="claude-haiku-4-5",
                ),
            )
        },
    )
    orchestrator.run(_make_request(job_key="spend"))

    assert repository.llm_runs[0].cost_usd > 0.0
    assert repository.monthly_spend_usd() == pytest.approx(repository.llm_runs[0].cost_usd)


# --- Context selection ------------------------------------------------------------


def test_estimate_token_count_is_proportional_to_length() -> None:
    assert estimate_token_count("") == 0
    assert estimate_token_count("   ") == 0
    assert estimate_token_count("a") == 1
    assert estimate_token_count("abcd") == 1
    assert estimate_token_count("s" * 20) == 5
    assert estimate_token_count("s" * 400) == 100


def test_representative_selection_limits_to_twelve_and_budget() -> None:
    # 20 chars of summary => 5 estimated tokens per article.
    articles = [{"summary": "s" * 20, "relevance_score": float(index)} for index in range(20)]

    budget_limited = select_representative_articles(
        articles,
        context_token_budget=20,
        max_articles=12,
        score_key="relevance_score",
        text_fields=("summary",),
    )
    max_only = select_representative_articles(
        articles,
        context_token_budget=1000,
        max_articles=12,
        score_key="relevance_score",
        text_fields=("summary",),
    )

    assert budget_limited.selected_count == 4
    assert budget_limited.used_token_budget == 20
    assert budget_limited.truncated_by_budget is True
    assert max_only.selected_count == 12
    assert max_only.used_token_budget == 60
    assert max_only.dropped_count == 8
    assert max_only.truncated_by_budget is False


def test_selection_ranks_by_score_before_truncating() -> None:
    articles = [
        {"summary": "low", "relevance_score": 1.0},
        {"summary": "high", "relevance_score": 9.0},
    ]

    selection = select_representative_articles(
        articles,
        max_articles=1,
        score_key="relevance_score",
        text_fields=("summary",),
    )

    assert selection.selected_articles[0]["summary"] == "high"
    assert selection.dropped_count == 1


# --- Invocation modes -------------------------------------------------------------


def test_batch_mode_uses_invoke_batch_not_invoke() -> None:
    provider = TrackingProvider((_event_payload(),), provider_name="batch-provider")
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,)},
    )
    result = orchestrator.run(_make_request(job_key="batch-mode"))

    assert result.mode is LLMInvocationMode.BATCH
    assert provider.invoke_calls == 0
    assert provider.invoke_batch_calls == 1


def test_realtime_mode_uses_invoke_not_invoke_batch() -> None:
    provider = TrackingProvider((_event_payload(),), provider_name="realtime-provider")
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,)},
    )
    orchestrator.run(_make_request(job_key="realtime-mode", is_realtime=True))

    assert provider.invoke_calls == 1
    assert provider.invoke_batch_calls == 0


def test_batch_falls_back_to_realtime_when_no_provider_accepts_batch() -> None:
    """Live adapters reject async batch, so routing must degrade rather than fail."""

    settings = Settings(anthropic_api_key="anthropic-key")
    provider = AnthropicMessagesProvider(
        settings=settings,
        model_name="claude-haiku-4-5",
        max_output_tokens=2_000,
        client=_mock_client(_anthropic_handler(), settings.anthropic_base_url),
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )

    result = orchestrator.run(_make_request(job_key="batch-fallback"))

    assert result.mode is LLMInvocationMode.REALTIME
    assert "batch_unsupported_realtime_fallback" in result.route_degradation_reasons
    assert result.run.model_params["mode"] == "realtime"
    assert result.run.status == "succeeded"
    assert result.run.cost_usd == pytest.approx((120 / 1_000_000) + (45 * 5 / 1_000_000))


def test_real_batch_run_receives_configured_discount() -> None:
    provider = TrackingProvider(
        (_event_payload(),),
        provider_name="anthropic",
        model_name="claude-haiku-4-5",
    )
    orchestrator = LLMOrchestrator(
        settings=Settings(llm_batch_discount_multiplier=0.5),
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,)},
    )

    result = orchestrator.run(_make_request(job_key="discounted-batch"))
    undiscounted = estimate_completion_cost_usd(
        settings=Settings(),
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        input_tokens=result.run.input_tokens,
        output_tokens=result.run.output_tokens,
    )

    assert result.mode is LLMInvocationMode.BATCH
    assert result.run.cost_usd == pytest.approx(undiscounted * 0.5)


# --- Orchestration outcomes -------------------------------------------------------


def test_orchestrator_returns_structured_contract_and_persists_trace_and_run() -> None:
    repository = InMemoryLLMRuntimeRepository()
    provider = TrackingProvider((_event_payload(),), provider_name="trace-provider")
    request = _make_request(job_key="trace-run")
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )
    result = orchestrator.run(request)

    assert isinstance(result.contract, EventExtraction)
    assert result.contract.events[0].event_id == "event-1"
    assert result.trace_id and result.run.trace_id == result.trace_id
    assert result.run.input_refs["trace_id"] == result.trace_id
    assert result.run.output_schema_name == "EventExtraction"
    last_job = repository.last_job_for_key(request.job.job_key)
    assert last_job is not None
    assert last_job.state == "succeeded"


def test_unwhitelisted_ids_are_rejected() -> None:
    """The ID whitelist still bites: a minted event_id must not validate."""

    provider = TrackingProvider((_event_payload(),), provider_name="minting-provider")
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )

    with pytest.raises(LLMValidationFailure):
        orchestrator.run(_make_request(job_key="minted", allowed_ids=("article-1",)))

    assert repository.last_job_for_key("minted").state == "llm_validation_failed"


def test_a_missing_whitelist_fails_the_run_before_any_provider_is_called() -> None:
    """No whitelist is misconfiguration, not a licence to accept whatever the model minted."""

    provider = TrackingProvider((_event_payload(),), provider_name="unconfigured-provider")
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )

    with pytest.raises(LLMConfigurationError, match="allowed_ids whitelist is required"):
        orchestrator.run(_make_request(job_key="no-whitelist", allowed_ids=None))

    assert provider.invoke_calls == 0
    assert repository.llm_runs == []


def test_an_empty_whitelist_permits_an_id_free_abstention() -> None:
    """An explicitly empty tuple is configuration: zero IDs allowed, and none were emitted."""

    provider = TrackingProvider(
        (_event_payload(with_reason=True),), provider_name="abstaining-provider"
    )
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,)},
    )

    result = orchestrator.run(_make_request(job_key="empty-whitelist-abstain", allowed_ids=()))

    assert result.contract.events == []
    assert result.contract.no_finding_reason == "No high-probability findings remained."


def test_an_empty_whitelist_still_rejects_emitted_ids() -> None:
    """The empty tuple must survive to the validator rather than collapse to "unset"."""

    provider = TrackingProvider((_event_payload(),), provider_name="empty-whitelist-provider")
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )

    with pytest.raises(LLMValidationFailure, match="event_id must be whitelisted"):
        orchestrator.run(_make_request(job_key="empty-whitelist-minted", allowed_ids=()))

    assert repository.last_job_for_key("empty-whitelist-minted").state == "llm_validation_failed"


def test_the_request_preserves_an_empty_whitelist_distinctly_from_a_missing_one() -> None:
    assert _make_request(job_key="empty", allowed_ids=()).allowed_ids == ()
    assert _make_request(job_key="missing", allowed_ids=None).allowed_ids is None


def test_provider_fallback_is_marked_degraded_and_succeeds() -> None:
    failing = CallableLLMProvider(
        response_factory=lambda _request: (_ for _ in ()).throw(RuntimeError("primary-fail")),
        provider_name="primary",
        model_name="model-primary",
    )
    working = TrackingProvider(
        (_event_payload(),), provider_name="secondary", model_name="model-secondary"
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (failing, working)},
    )

    result = orchestrator.run(_make_request(job_key="fallback-provider"))

    assert result.degraded_provider == "secondary"
    assert result.run.model_params["degraded_provider"] == "secondary"
    assert result.run.provider == "secondary"
    assert repository.llm_runs[0].status == "failed"


def test_validation_retry_succeeds_on_second_attempt() -> None:
    provider = TrackingProvider(
        (_invalid_event_payload(), _event_payload()),
        provider_name="retry-provider",
        model_name="model-retry",
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )

    result = orchestrator.run(_make_request(job_key="validation-retry"))

    assert provider.call_count == 2
    assert result.run.attempt == 2
    assert [run.status for run in repository.llm_runs] == ["validation_failed", "succeeded"]
    assert repository.last_job_for_key("validation-retry") is not None
    assert repository.last_job_for_key("validation-retry").state == "succeeded"


def test_second_validation_failure_records_job_state_llm_validation_failed() -> None:
    provider = TrackingProvider(
        (_invalid_event_payload(), _invalid_event_payload()),
        provider_name="invalid-provider",
        model_name="model-invalid",
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )

    with pytest.raises(LLMValidationFailure):
        orchestrator.run(_make_request(job_key="validation-failed"))

    assert provider.call_count == 2
    assert [run.status for run in repository.llm_runs] == [
        "validation_failed",
        "validation_failed",
    ]
    assert repository.last_job_for_key("validation-failed") is not None
    assert repository.last_job_for_key("validation-failed").state == "llm_validation_failed"


def test_abstain_payload_is_accepted() -> None:
    provider = TrackingProvider((_event_payload(with_reason=True),), provider_name="abstainer")
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,)},
    )

    result = orchestrator.run(_make_request(job_key="abstain"))

    assert result.contract.no_finding_reason == "No high-probability findings remained."
    assert result.run.no_finding_reason == "No high-probability findings remained."


def test_limiter_denial_marks_nonessential_queue_and_raises_failure() -> None:
    limiter = InMemoryTokenBucketLimiter(capacity=0, refill_per_second=0.0, time_func=lambda: 0.0)
    repository = SpendAwareRepository(spent_usd=20.0)
    provider = TrackingProvider((_event_payload(),), provider_name="limited-provider")

    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=repository,
        providers_by_tier={"T1": (provider,)},
        limiter=limiter,
    )

    with pytest.raises(LLMInvocationFailure):
        orchestrator.run(_make_request(job_key="limited", is_essential=False))

    failed_run = repository.llm_runs[0]
    failed_job = repository.last_job_for_key("limited")

    assert failed_job is not None
    assert failed_job.state == "failed"
    assert failed_job.error["code"] == "provider_exhausted"
    assert failed_run.status == "failed"
    assert failed_run.error_message == "token bucket limit reached"
    assert failed_run.model_params["queue"] == "nonessential"


# --- Live HTTP provider adapters (mocked transport) --------------------------------


def _openai_handler(captured: dict[str, Any] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_1",
                "choices": [{"message": {"content": json.dumps(_event_payload())}}],
                "usage": {"prompt_tokens": 90, "completion_tokens": 30},
            },
        )

    return handler


def _anthropic_handler(
    captured: dict[str, Any] | None = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "EventExtraction",
                        "input": _event_payload(),
                    }
                ],
                "usage": {"input_tokens": 120, "output_tokens": 45},
            },
        )

    return handler


def _gemini_handler(captured: dict[str, Any] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
        serialized = json.dumps(_event_payload())
        split_at = len(serialized) // 2
        return httpx.Response(
            200,
            json={
                "id": "interaction_1",
                "model": "gemini-3.6-flash",
                "status": "completed",
                "steps": [
                    {"type": "thought", "signature": "opaque"},
                    {
                        "type": "model_output",
                        "content": [
                            {"type": "text", "text": serialized[:split_at]},
                            {"type": "text", "text": serialized[split_at:]},
                        ],
                    },
                ],
                "usage": {
                    "total_input_tokens": 80,
                    "total_output_tokens": 20,
                    "total_thought_tokens": 12,
                    "total_cached_tokens": 5,
                    "total_tool_use_tokens": 0,
                    "total_tokens": 112,
                },
            },
        )

    return handler


def _deepseek_handler(captured: dict[str, Any] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "deepseek_1",
                "model": "deepseek-v4-flash",
                "system_fingerprint": "fp_1",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(_event_payload())},
                    }
                ],
                "usage": {
                    "prompt_tokens": 70,
                    "completion_tokens": 25,
                    "total_tokens": 95,
                    "prompt_cache_hit_tokens": 30,
                    "prompt_cache_miss_tokens": 40,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    return handler


def test_anthropic_adapter_forces_the_contract_tool_and_reports_usage() -> None:
    captured: dict[str, Any] = {}
    settings = Settings(anthropic_api_key="anthropic-key")
    provider = AnthropicMessagesProvider(
        settings=settings,
        model_name="claude-haiku-4-5",
        max_output_tokens=2_000,
        client=_mock_client(_anthropic_handler(captured), settings.anthropic_base_url),
    )

    response = provider.invoke(_invocation_request())

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "anthropic-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["body"]["model"] == "claude-haiku-4-5"
    assert captured["body"]["max_tokens"] == 2_000
    assert captured["body"]["tool_choice"] == {"type": "tool", "name": "EventExtraction"}
    assert captured["body"]["tools"][0]["input_schema"]["properties"]["events"]

    assert response.provider_name == "anthropic"
    assert response.structured == _event_payload()
    assert response.parsed_payload() == _event_payload()
    assert (response.input_tokens, response.output_tokens) == (120, 45)


def test_openai_adapter_requests_json_schema_and_reports_usage() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_1",
                "choices": [{"message": {"content": json.dumps(_event_payload())}}],
                "usage": {"prompt_tokens": 90, "completion_tokens": 30},
            },
        )

    settings = Settings(openai_api_key="openai-key")
    provider = OpenAIChatCompletionsProvider(
        settings=settings,
        model_name="gpt-4.1",
        max_output_tokens=6_000,
        client=_mock_client(handler, settings.openai_base_url),
    )

    response = provider.invoke(_invocation_request())

    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer openai-key"
    assert captured["body"]["model"] == "gpt-4.1"
    assert captured["body"]["max_completion_tokens"] == 6_000
    assert captured["body"]["response_format"]["type"] == "json_schema"
    assert captured["body"]["response_format"]["json_schema"]["name"] == "EventExtraction"

    assert response.provider_name == "openai"
    assert response.structured == _event_payload()
    assert (response.input_tokens, response.output_tokens) == (90, 30)


def test_gemini_interactions_adapter_requests_structured_json_and_reports_usage() -> None:
    captured: dict[str, Any] = {}
    settings = Settings(gemini_api_key="gemini-key")
    provider = GeminiInteractionsProvider(
        settings=settings,
        model_name="gemini-3.6-flash",
        max_output_tokens=4_000,
        client=_mock_client(_gemini_handler(captured), settings.gemini_base_url),
    )

    response = provider.invoke(_invocation_request())

    assert captured["url"] == "https://generativelanguage.googleapis.com/v1/interactions"
    assert captured["headers"]["x-goog-api-key"] == "gemini-key"
    assert "gemini-key" not in captured["url"]
    assert captured["body"]["model"] == "gemini-3.6-flash"
    assert captured["body"]["input"] == "Summarize representative events."
    assert captured["body"]["store"] is False
    assert captured["body"]["stream"] is False
    assert captured["body"]["generation_config"] == {
        "max_output_tokens": 4_000,
        "thinking_level": "low",
    }

    response_format = captured["body"]["response_format"]
    assert response_format["type"] == "text"
    assert response_format["mime_type"] == "application/json"
    schema = response_format["schema"]
    assert schema["properties"]["schema_name"]["enum"] == ["EventExtraction"]
    assert "const" not in schema["properties"]["schema_name"]
    assert "minLength" not in schema["properties"]["prompt_template_version"]
    assert "default" not in schema["properties"]["no_finding_reason"]

    assert response.provider_name == "gemini"
    assert response.structured == _event_payload()
    assert (response.input_tokens, response.output_tokens) == (80, 32)
    assert response.raw_metadata == {
        "response_id": "interaction_1",
        "provider_model": "gemini-3.6-flash",
        "status": "completed",
        "thought_tokens": 12,
        "cached_tokens": 5,
        "tool_use_tokens": 0,
        "total_tokens": 112,
        "thinking_level": "low",
        "temperature_requested": None,
        "temperature_sent": False,
        "temperature_strategy": "provider_default",
    }


@pytest.mark.parametrize(
    ("response_body", "message"),
    [
        (
            {
                "status": "incomplete",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "{}"}],
                    }
                ],
            },
            "status",
        ),
        ({"status": "completed", "steps": []}, "model_output"),
        (
            {
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "[]"}],
                    }
                ],
            },
            "JSON object",
        ),
    ],
)
def test_gemini_adapter_rejects_incomplete_or_unusable_interactions(
    response_body: dict[str, Any],
    message: str,
) -> None:
    settings = Settings(gemini_api_key="key")
    provider = GeminiInteractionsProvider(
        settings=settings,
        model_name="gemini-3.6-flash",
        max_output_tokens=2_000,
        client=_mock_client(
            lambda _request: httpx.Response(200, json=response_body),
            settings.gemini_base_url,
        ),
    )

    with pytest.raises(LLMProviderError, match=message):
        provider.invoke(_invocation_request())


def test_gemini_incomplete_interaction_retains_billed_usage() -> None:
    body = {
        "id": "interaction_incomplete",
        "model": "gemini-3.6-flash",
        "status": "incomplete",
        "steps": [{"type": "thought"}, {"type": "model_output", "content": []}],
        "usage": {
            "total_input_tokens": 80,
            "total_output_tokens": 20,
            "total_thought_tokens": 12,
            "total_tokens": 112,
        },
    }
    settings = Settings(
        gemini_api_key="key",
        llm_models={"T1": "gemini-3.6-flash"},
        llm_tier_providers={"T1": "gemini"},
        llm_tier_fallbacks={},
    )
    provider = GeminiInteractionsProvider(
        settings=settings,
        model_name="gemini-3.6-flash",
        max_output_tokens=2_000,
        client=_mock_client(
            lambda _request: httpx.Response(200, json=body),
            settings.gemini_base_url,
        ),
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )

    with pytest.raises(LLMInvocationFailure):
        orchestrator.run(_make_request(job_key="gemini-incomplete-usage"))

    assert len(repository.llm_runs) == 1
    failed = repository.llm_runs[0]
    assert failed.status == "failed"
    assert (failed.input_tokens, failed.output_tokens) == (80, 32)
    assert failed.cost_usd == pytest.approx((80 * 1.5 + 32 * 7.5) / 1_000_000)
    assert failed.model_params["adapter_params"]["thinking_level"] == "low"
    assert failed.error_details["provider_failure_code"] == "output_incomplete"

    direct = GeminiInteractionsProvider(
        settings=settings,
        model_name="gemini-3.6-flash",
        max_output_tokens=2_000,
        client=_mock_client(
            lambda _request: httpx.Response(200, json=body),
            settings.gemini_base_url,
        ),
    )
    with pytest.raises(LLMProviderOutputError) as caught:
        direct.invoke(_invocation_request())
    assert (caught.value.response.input_tokens, caught.value.response.output_tokens) == (80, 32)
    assert caught.value.diagnostic_code == "output_incomplete"


@pytest.mark.parametrize(
    ("steps", "message", "diagnostic_code"),
    [
        ([], "model_output", "output_missing"),
        (
            [{"type": "model_output", "content": [{"type": "text", "text": "{"}]}],
            "valid JSON",
            "output_invalid_json",
        ),
        (
            [{"type": "model_output", "content": [{"type": "text", "text": "[]"}]}],
            "JSON object",
            "output_not_object",
        ),
    ],
)
def test_gemini_completed_unusable_interaction_retains_billed_usage(
    steps: list[dict[str, Any]],
    message: str,
    diagnostic_code: str,
) -> None:
    body = {
        "id": "interaction_completed_unusable",
        "model": "gemini-3.6-flash",
        "status": "completed",
        "steps": steps,
        "usage": {
            "total_input_tokens": 80,
            "total_output_tokens": 20,
            "total_thought_tokens": 12,
            "total_tokens": 112,
        },
    }
    settings = Settings(
        gemini_api_key="key",
        llm_models={"T1": "gemini-3.6-flash"},
        llm_tier_providers={"T1": "gemini"},
        llm_tier_fallbacks={},
    )
    provider = GeminiInteractionsProvider(
        settings=settings,
        model_name="gemini-3.6-flash",
        max_output_tokens=2_000,
        client=_mock_client(
            lambda _request: httpx.Response(200, json=body),
            settings.gemini_base_url,
        ),
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )

    with pytest.raises(LLMInvocationFailure):
        orchestrator.run(_make_request(job_key=f"gemini-completed-unusable-{message}"))

    assert len(repository.llm_runs) == 1
    failed = repository.llm_runs[0]
    assert failed.status == "failed"
    assert (failed.input_tokens, failed.output_tokens) == (80, 32)
    assert failed.cost_usd == pytest.approx((80 * 1.5 + 32 * 7.5) / 1_000_000)
    assert failed.model_params["adapter_params"]["thinking_level"] == "low"
    assert message in failed.error_details["error"]
    assert failed.error_details["provider_failure_code"] == diagnostic_code


def test_gemini_temperature_translation_is_explicit_and_nonzero_is_refused() -> None:
    settings = Settings(gemini_api_key="key")
    current_calls = 0

    captured_current: dict[str, Any] = {}

    def current_handler(request: httpx.Request) -> httpx.Response:
        nonlocal current_calls
        current_calls += 1
        return _gemini_handler(captured_current)(request)

    current = GeminiInteractionsProvider(
        settings=settings,
        model_name="gemini-3.6-flash",
        max_output_tokens=2_000,
        client=_mock_client(current_handler, settings.gemini_base_url),
    )
    request = LLMInvocationRequest(**{**_invocation_request().__dict__, "temperature": 0.0})

    response = current.invoke(request)

    assert current_calls == 1
    assert "temperature" not in captured_current["body"]["generation_config"]
    assert "deterministically" in captured_current["body"]["system_instruction"]
    assert response.raw_metadata["temperature_requested"] == 0.0
    assert response.raw_metadata["temperature_sent"] is False
    assert response.raw_metadata["temperature_strategy"] == "deterministic_system_instruction"

    nonzero_request = LLMInvocationRequest(**{**_invocation_request().__dict__, "temperature": 0.7})
    with pytest.raises(LLMProviderError, match="sampling-parameter support"):
        current.invoke(nonzero_request)
    assert current_calls == 1

    future_settings = Settings(
        gemini_api_key="key",
        gemini_model_thinking_levels={
            **settings.gemini_model_thinking_levels,
            "gemini-future-custom": "low",
        },
    )
    future = GeminiInteractionsProvider(
        settings=future_settings,
        model_name="gemini-future-custom",
        max_output_tokens=2_000,
        client=_mock_client(current_handler, future_settings.gemini_base_url),
    )
    with pytest.raises(LLMProviderError, match="sampling-parameter support"):
        future.invoke(nonzero_request)
    assert current_calls == 1


def test_gemini_thinking_level_is_model_scoped() -> None:
    settings = Settings(
        gemini_api_key="key",
        gemini_model_thinking_levels={
            "gemini-3.5-flash-lite": "minimal",
            "gemini-3.6-flash": "low",
        },
    )
    captured_lite: dict[str, Any] = {}
    lite = GeminiInteractionsProvider(
        settings=settings,
        model_name="gemini-3.5-flash-lite",
        max_output_tokens=2_000,
        client=_mock_client(_gemini_handler(captured_lite), settings.gemini_base_url),
    )
    lite.invoke(_invocation_request())
    assert captured_lite["body"]["generation_config"]["thinking_level"] == "minimal"

    with pytest.raises(LLMProviderError, match="thinking-level configuration"):
        GeminiInteractionsProvider(
            settings=settings,
            model_name="gemini-future-custom",
            max_output_tokens=2_000,
            client=_mock_client(_gemini_handler(), settings.gemini_base_url),
        )


def test_deepseek_adapter_requests_json_mode_and_reports_usage() -> None:
    captured: dict[str, Any] = {}
    settings = Settings(deepseek_api_key="deepseek-key")
    provider = DeepSeekChatCompletionsProvider(
        settings=settings,
        model_name="deepseek-v4-flash",
        max_output_tokens=2_000,
        client=_mock_client(_deepseek_handler(captured), settings.deepseek_base_url),
    )
    request = LLMInvocationRequest(**{**_invocation_request().__dict__, "temperature": 0.0})

    response = provider.invoke(request)

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer deepseek-key"
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert captured["body"]["max_tokens"] == 2_000
    assert captured["body"]["stream"] is False
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["temperature"] == 0.0
    assert captured["body"]["messages"][1] == {
        "role": "user",
        "content": "Summarize representative events.",
    }
    system_instruction = captured["body"]["messages"][0]["content"]
    assert "JSON" in system_instruction
    assert "EXAMPLE JSON OUTPUT" in system_instruction
    assert "REPORT COMPOSITION WORD-BUDGET CONTROL" not in system_instruction
    assert '"schema_name"' in system_instruction
    assert '"events"' in system_instruction
    example = json.loads(
        system_instruction.split(
            "EXAMPLE JSON OUTPUT (shape only; replace example values with task-specific values):\n",
            1,
        )[1]
    )
    assert example["events"] == []
    assert example["no_finding_reason"] == "No qualifying findings were found."
    validate_llm_contract_payload(
        schema_name="EventExtraction",
        payload=example,
        allowed_ids=(),
    )

    assert response.provider_name == "deepseek"
    assert response.structured == _event_payload()
    assert (response.input_tokens, response.output_tokens) == (70, 25)
    assert response.raw_metadata == {
        "response_id": "deepseek_1",
        "provider_model": "deepseek-v4-flash",
        "finish_reason": "stop",
        "system_fingerprint": "fp_1",
        "total_tokens": 95,
        "prompt_cache_hit_tokens": 30,
        "prompt_cache_miss_tokens": 40,
        "reasoning_tokens": 0,
        "thinking_mode": "disabled",
    }
    assert provider.cache_variant == {"report_composition_budget_instruction": "v3"}


def test_deepseek_report_composition_gets_a_provider_only_word_budget_control() -> None:
    captured: dict[str, Any] = {}
    payload = {
        "schema_name": "ReportComposition",
        "schema_version": "1.0",
        "prompt_template_version": "v1",
        "blocks": [
            {
                "text": "A concise claim-backed section.",
                "claim_ids": ["40000000-0000-4000-8000-000000000002"],
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "deepseek_composition",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(payload)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 40,
                    "total_tokens": 160,
                },
            },
        )

    settings = Settings(deepseek_api_key="deepseek-key")
    provider = DeepSeekChatCompletionsProvider(
        settings=settings,
        model_name="deepseek-v4-pro",
        max_output_tokens=4_000,
        client=_mock_client(handler, settings.deepseek_base_url),
    )
    request = LLMInvocationRequest(
        prompt_name="daily_brief_top_event",
        prompt_version="v1",
        prompt_template_version="v1",
        prompt=(
            "Write approximately 150 words for this section, and keep the whole section "
            "(all blocks combined) between 120 and 180 words."
        ),
        requested_schema="ReportComposition",
        context={
            "budget_attempt": 1,
            "word_budget_target": 150,
            "word_budget_minimum": 120,
            "word_budget_maximum": 180,
        },
        temperature=0.0,
    )

    response = provider.invoke(request)

    system_instruction = captured["body"]["messages"][0]["content"]
    assert "REPORT COMPOSITION WORD-BUDGET CONTROL (v3)" in system_instruction
    assert "hard acceptance condition" in system_instruction
    assert "shape-only" in system_instruction
    assert "do not copy its empty blocks or abstention reason" in system_instruction
    assert "at least one non-empty block" in system_instruction
    assert "cover every required fact and concept" in system_instruction
    assert "preserve the exact supporting claim_ids" in system_instruction
    assert "120-180 words is the accepted outer range" in system_instruction
    assert "safer 140-160 word band" in system_instruction
    assert "150-word target" in system_instruction
    assert "single corrective budget attempt" not in system_instruction
    assert "all blocks[].text" in system_instruction
    assert "maximal run of non-whitespace characters" in system_instruction
    assert "Do not count JSON syntax or claim_ids" in system_instruction
    assert "do not add a word-count field" in system_instruction
    assert captured["body"]["messages"][1] == {
        "role": "user",
        "content": request.prompt,
    }
    assert response.structured == payload
    assert response.raw_metadata["report_composition_budget_instruction"] == "v3"

    retry_request = replace(
        request,
        prompt=(
            f"{request.prompt}\n\nYour previous response was 220 words, which is too long. "
            "Rewrite this section in approximately 150 words."
        ),
        context={**request.context, "budget_attempt": 2},
    )
    provider.invoke(retry_request)

    retry_instruction = captured["body"]["messages"][0]["content"]
    assert "single corrective budget attempt" in retry_instruction
    assert "previous word count and too-short or too-long direction" in retry_instruction
    assert "Rewrite the full section" in retry_instruction
    assert "retain every required fact, concept, and exact claim_id" in retry_instruction
    assert captured["body"]["messages"][1] == {
        "role": "user",
        "content": retry_request.prompt,
    }


@pytest.mark.parametrize(
    ("choice", "message", "diagnostic_code"),
    [
        (
            {
                "finish_reason": "length",
                "message": {"content": json.dumps(_event_payload())},
            },
            "finish_reason",
            "output_incomplete",
        ),
        (
            {"finish_reason": "stop", "message": {"content": ""}},
            "empty JSON-mode",
            "output_missing",
        ),
        (
            {"finish_reason": "stop", "message": {"content": "{"}},
            "valid JSON",
            "output_invalid_json",
        ),
        (
            {"finish_reason": "stop", "message": {"content": "[]"}},
            "JSON object",
            "output_not_object",
        ),
    ],
)
def test_deepseek_adapter_rejects_unusable_output_and_retains_billed_usage(
    choice: dict[str, Any],
    message: str,
    diagnostic_code: str,
) -> None:
    settings = Settings(deepseek_api_key="key")
    provider = DeepSeekChatCompletionsProvider(
        settings=settings,
        model_name="deepseek-v4-pro",
        max_output_tokens=2_000,
        client=_mock_client(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [choice],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 10,
                        "total_tokens": 30,
                    },
                },
            ),
            settings.deepseek_base_url,
        ),
    )

    with pytest.raises(LLMProviderOutputError, match=message) as caught:
        provider.invoke(_invocation_request())

    assert caught.value.diagnostic_code == diagnostic_code
    assert (caught.value.response.input_tokens, caught.value.response.output_tokens) == (20, 10)


def test_deepseek_truncated_output_is_billed_by_the_orchestrator() -> None:
    settings = Settings(
        deepseek_api_key="deepseek-key",
        llm_models={"T1": "deepseek-v4-flash"},
        llm_tier_providers={"T1": "deepseek"},
        llm_tier_fallbacks={},
    )
    provider = DeepSeekChatCompletionsProvider(
        settings=settings,
        model_name="deepseek-v4-flash",
        max_output_tokens=2_000,
        client=_mock_client(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": json.dumps(_event_payload())},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 10,
                        "total_tokens": 30,
                    },
                },
            ),
            settings.deepseek_base_url,
        ),
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )

    with pytest.raises(LLMInvocationFailure):
        orchestrator.run(_make_request(job_key="deepseek-truncated-usage", is_realtime=True))

    failed = repository.llm_runs[0]
    assert failed.status == "failed"
    assert (failed.input_tokens, failed.output_tokens) == (20, 10)
    assert failed.cost_usd == pytest.approx((20 * 0.14 + 10 * 0.28) / 1_000_000)
    assert failed.error_details["provider_failure_code"] == "output_incomplete"


@pytest.mark.parametrize("schema_name", list_contract_schemas())
def test_deepseek_schema_examples_are_complete_contract_valid_abstentions(
    schema_name: str,
) -> None:
    schema = llm_contract_json_schema(schema_name)
    example = _json_schema_example(schema)

    assert set(example) == set(schema["properties"])
    validate_llm_contract_payload(
        schema_name=schema_name,
        payload=example,
        allowed_ids=(),
    )


def test_deepseek_empty_json_output_gets_one_corrective_retry() -> None:
    settings = Settings(
        deepseek_api_key="deepseek-key",
        llm_models={"T1": "deepseek-v4-flash"},
        llm_tier_providers={"T1": "deepseek"},
        llm_tier_fallbacks={},
    )
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "deepseek_empty",
                    "model": "deepseek-v4-flash",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": ""},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 0,
                        "total_tokens": 20,
                    },
                },
            )
        return _deepseek_handler()(request)

    provider = DeepSeekChatCompletionsProvider(
        settings=settings,
        model_name="deepseek-v4-flash",
        max_output_tokens=2_000,
        client=_mock_client(handler, settings.deepseek_base_url),
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=repository,
        providers_by_tier={"T1": (provider,)},
    )
    try:
        result = orchestrator.run(_make_request(job_key="deepseek-empty-retry"))
    finally:
        orchestrator.close()

    assert result.run.provider == "deepseek"
    assert len(bodies) == 2
    assert "Validation feedback:" in bodies[1]["messages"][1]["content"]
    assert [run.status for run in repository.llm_runs] == [
        "validation_failed",
        "succeeded",
    ]
    first_attempt = repository.llm_runs[0]
    assert (first_attempt.input_tokens, first_attempt.output_tokens) == (20, 0)
    assert first_attempt.cost_usd == pytest.approx((20 / 1_000_000) * 0.14)


def test_live_adapters_reject_async_batch_submission() -> None:
    settings = Settings(
        anthropic_api_key="key",
        openai_api_key="key",
        gemini_api_key="key",
        deepseek_api_key="key",
    )
    providers = (
        AnthropicMessagesProvider(
            settings=settings,
            model_name="claude-haiku-4-5",
            max_output_tokens=2_000,
            client=_mock_client(_anthropic_handler(), settings.anthropic_base_url),
        ),
        OpenAIChatCompletionsProvider(
            settings=settings,
            model_name="gpt-4.1",
            max_output_tokens=6_000,
            client=_mock_client(_anthropic_handler(), settings.openai_base_url),
        ),
        GeminiInteractionsProvider(
            settings=settings,
            model_name="gemini-3.6-flash",
            max_output_tokens=4_000,
            client=_mock_client(_gemini_handler(), settings.gemini_base_url),
        ),
        DeepSeekChatCompletionsProvider(
            settings=settings,
            model_name="deepseek-v4-flash",
            max_output_tokens=4_000,
            client=_mock_client(_deepseek_handler(), settings.deepseek_base_url),
        ),
    )

    for provider in providers:
        assert provider.supports_mode(LLMInvocationMode.REALTIME) is True
        assert provider.supports_mode(LLMInvocationMode.BATCH) is False
        assert provider.supports_structured_schema("EventExtraction") is True
        assert provider.supports_structured_schema("NotAContract") is False
        with pytest.raises(LLMBatchModeUnsupported):
            provider.invoke_batch((_invocation_request(),))


def test_anthropic_adapter_surfaces_http_and_payload_errors() -> None:
    settings = Settings(anthropic_api_key="key")

    failing = AnthropicMessagesProvider(
        settings=settings,
        model_name="claude-haiku-4-5",
        max_output_tokens=2_000,
        client=_mock_client(
            lambda _request: httpx.Response(500, json={"error": "overloaded"}),
            settings.anthropic_base_url,
        ),
    )
    toolless = AnthropicMessagesProvider(
        settings=settings,
        model_name="claude-haiku-4-5",
        max_output_tokens=2_000,
        client=_mock_client(
            lambda _request: httpx.Response(
                200, json={"content": [{"type": "text", "text": "no tool call"}]}
            ),
            settings.anthropic_base_url,
        ),
    )

    with pytest.raises(LLMProviderError, match="request failed"):
        failing.invoke(_invocation_request())
    with pytest.raises(LLMProviderError, match="tool_use"):
        toolless.invoke(_invocation_request())


@pytest.mark.parametrize(
    ("status_code", "diagnostic_code"),
    [
        (400, "http_request_rejected"),
        (401, "http_authentication"),
        (403, "http_permission"),
        (404, "http_not_found"),
        (408, "http_timeout"),
        (429, "http_rate_limited"),
        (500, "http_server_error"),
    ],
)
def test_http_failures_carry_only_fixed_diagnostic_codes(
    status_code: int,
    diagnostic_code: str,
) -> None:
    settings = Settings(anthropic_api_key="key")
    provider = AnthropicMessagesProvider(
        settings=settings,
        model_name="claude-haiku-4-5",
        max_output_tokens=2_000,
        client=_mock_client(
            lambda _request: httpx.Response(
                status_code,
                json={"error": "secret-provider-body-marker"},
            ),
            settings.anthropic_base_url,
        ),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider.invoke(_invocation_request())

    assert caught.value.diagnostic_code == diagnostic_code
    assert diagnostic_code in LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODES
    assert "secret-provider-body-marker" not in diagnostic_code


@pytest.mark.parametrize(
    ("handler", "diagnostic_code"),
    [
        (
            lambda request: (_ for _ in ()).throw(
                httpx.ReadTimeout("secret-timeout-marker", request=request)
            ),
            "transport_timeout",
        ),
        (
            lambda request: (_ for _ in ()).throw(
                httpx.ConnectError("secret-transport-marker", request=request)
            ),
            "transport_error",
        ),
        (lambda _request: httpx.Response(200, text="not-json"), "response_non_json"),
        (lambda _request: httpx.Response(200, json=[]), "response_not_object"),
    ],
)
def test_transport_and_response_failures_have_fixed_diagnostic_codes(
    handler: Any,
    diagnostic_code: str,
) -> None:
    settings = Settings(anthropic_api_key="key")
    provider = AnthropicMessagesProvider(
        settings=settings,
        model_name="claude-haiku-4-5",
        max_output_tokens=2_000,
        client=_mock_client(handler, settings.anthropic_base_url),
    )

    with pytest.raises(LLMProviderError) as caught:
        provider.invoke(_invocation_request())

    assert caught.value.diagnostic_code == diagnostic_code
    assert provider_failure_diagnostic_code(caught.value) == diagnostic_code
    assert provider_failure_diagnostic_code(RuntimeError("secret-unknown-marker")) == "unclassified"


@pytest.mark.parametrize(
    ("provider_class", "model_name"),
    [
        (AnthropicMessagesProvider, "claude-haiku-4-5"),
        (OpenAIChatCompletionsProvider, "gpt-4.1"),
        (GeminiInteractionsProvider, "gemini-3.6-flash"),
        (DeepSeekChatCompletionsProvider, "deepseek-v4-flash"),
    ],
)
def test_missing_api_key_is_refused_at_construction(
    provider_class: type[Any],
    model_name: str,
) -> None:
    with pytest.raises(LLMProviderError, match="API key"):
        provider_class(
            settings=Settings(),
            model_name=model_name,
            max_output_tokens=2_000,
        )


def test_provider_factory_uses_explicit_per_tier_configuration() -> None:
    settings = Settings(anthropic_api_key="anthropic-key", openai_api_key="openai-key")

    providers = build_providers_by_tier(settings)
    try:
        assert providers["T1"][0].provider_name == "anthropic"
        assert providers["T1"][0].model_name == "claude-haiku-4-5"
        assert providers["T1"][1].provider_name == "openai"
        assert providers["T1"][1].model_name == "gpt-4.1-mini"
        assert providers["T2"][0].provider_name == "anthropic"
        assert providers["T2"][0].model_name == "claude-sonnet-5"
        assert providers["T2"][1].provider_name == "openai"
        assert providers["T2"][1].model_name == "gpt-4.1"
        assert providers["T3"][0].provider_name == "openai"
        assert providers["T3"][0].model_name == "gpt-4.1"
        assert len(providers["T3"]) == 1
    finally:
        for tier_providers in providers.values():
            for provider in tier_providers:
                provider.close()


def test_provider_factory_builds_opt_in_gemini_and_deepseek_routes() -> None:
    settings = Settings(
        gemini_api_key="gemini-key",
        deepseek_api_key="deepseek-key",
        llm_models={"T1": "gemini-3.6-flash"},
        llm_tier_providers={"T1": "gemini"},
        llm_tier_fallbacks={"T1": [{"provider": "deepseek", "model": "deepseek-v4-flash"}]},
    )

    providers = build_providers_by_tier(settings, tiers=(LLMTier.T1,))
    try:
        assert [provider.provider_name for provider in providers["T1"]] == [
            "gemini",
            "deepseek",
        ]
        assert [provider.model_name for provider in providers["T1"]] == [
            "gemini-3.6-flash",
            "deepseek-v4-flash",
        ]
    finally:
        for provider in providers["T1"]:
            provider.close()


def test_provider_factory_closes_completed_adapters_when_a_later_route_is_invalid() -> None:
    settings = Settings(
        gemini_api_key="gemini-key",
        llm_models={"T1": "gemini-3.6-flash"},
        llm_tier_providers={"T1": "gemini"},
        llm_tier_fallbacks={"T1": [{"provider": "not-a-provider", "model": "not-a-model"}]},
    )
    client = _mock_client(_gemini_handler(), settings.gemini_base_url)

    with pytest.raises(LLMProviderError, match="unsupported provider"):
        build_providers_by_tier(
            settings,
            tiers=(LLMTier.T1,),
            client=client,
        )

    assert client.is_closed


def test_orchestrator_closes_each_provider_once_even_when_routes_share_it() -> None:
    provider = CloseTrackingProvider((_event_payload(),))
    orchestrator = LLMOrchestrator(
        settings=Settings(),
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,), "T2": (provider,)},
    )

    orchestrator.close()
    orchestrator.close()

    assert provider.close_count == 1


def test_production_runtime_closes_providers_if_limiter_assembly_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.llm.runtime as runtime

    provider = CloseTrackingProvider((_event_payload(),), provider_name="gemini")
    monkeypatch.setattr(
        runtime,
        "build_providers_by_tier",
        lambda _settings: {"T1": (provider,), "T2": (provider,)},
    )

    def fail_limiter(**_kwargs: Any) -> None:
        raise RuntimeError("limiter configuration failed")

    monkeypatch.setattr(runtime, "build_provider_rate_limiter", fail_limiter)

    with pytest.raises(RuntimeError, match="limiter configuration failed"):
        runtime.build_production_orchestrator(
            settings=Settings(),
            session=object(),  # type: ignore[arg-type]
            redis_client=FakeRedis(),
        )

    assert provider.close_count == 1


def test_limiter_failure_remains_primary_when_provider_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.llm.runtime as runtime

    close_error = RuntimeError("provider close failed")
    provider = CloseTrackingProvider(
        (_event_payload(),),
        provider_name="gemini",
        close_error=close_error,
    )
    monkeypatch.setattr(
        runtime,
        "build_providers_by_tier",
        lambda _settings: {"T1": (provider,)},
    )
    limiter_error = RuntimeError("limiter configuration failed")

    def fail_limiter(**_kwargs: Any) -> None:
        raise limiter_error

    monkeypatch.setattr(runtime, "build_provider_rate_limiter", fail_limiter)

    with pytest.raises(RuntimeError, match="limiter configuration failed") as captured:
        runtime.build_production_orchestrator(
            settings=Settings(),
            session=object(),  # type: ignore[arg-type]
            redis_client=FakeRedis(),
        )

    assert captured.value is limiter_error
    assert provider.close_count == 1
    assert any("provider close failed" in note for note in limiter_error.__notes__)


def test_a_current_deterministic_workload_can_run_on_the_gemini_route() -> None:
    settings = Settings(
        gemini_api_key="gemini-key",
        deepseek_api_key="deepseek-key",
        llm_models={"T1": "gemini-3.6-flash"},
        llm_tier_providers={"T1": "gemini"},
        llm_tier_fallbacks={"T1": [{"provider": "deepseek", "model": "deepseek-v4-flash"}]},
    )
    clients = {
        "gemini": _mock_client(_gemini_handler(), settings.gemini_base_url),
        "deepseek": _mock_client(_deepseek_handler(), settings.deepseek_base_url),
    }
    providers = build_providers_by_tier(
        settings,
        tiers=(LLMTier.T1,),
        clients_by_provider=clients,
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=repository,
        providers_by_tier=providers,
    )
    base_request = _make_request(job_key="gemini-temperature-fallback")

    try:
        result = orchestrator.run(
            LLMOrchestratorRequest(**{**base_request.__dict__, "temperature": 0.0})
        )

        assert result.run.provider == "gemini"
        assert result.degraded_provider is None
        assert len(repository.llm_runs) == 1
        assert result.run.model_params["adapter_params"] == {
            "thinking_level": "low",
            "temperature_requested": 0.0,
            "temperature_sent": False,
            "temperature_strategy": "deterministic_system_instruction",
        }
    finally:
        orchestrator.close()


def test_provider_factory_requires_explicit_pricing_for_every_live_model() -> None:
    unpriced = Settings(
        gemini_api_key="key",
        llm_models={"T1": "gemini-custom"},
        llm_tier_providers={"T1": "gemini"},
        llm_tier_fallbacks={},
    )
    with pytest.raises(LLMProviderError, match="has no token pricing"):
        build_providers_by_tier(unpriced, tiers=(LLMTier.T1,))

    priced = Settings(
        gemini_api_key="key",
        llm_models={"T1": "gemini-custom"},
        llm_tier_providers={"T1": "gemini"},
        llm_tier_fallbacks={},
        gemini_model_thinking_levels={"gemini-custom": "low"},
        llm_provider_token_price_usd_per_1m={"gemini:gemini-custom": {"input": 0.1, "output": 0.2}},
    )
    providers = build_providers_by_tier(priced, tiers=(LLMTier.T1,))
    try:
        assert providers["T1"][0].model_name == "gemini-custom"
    finally:
        providers["T1"][0].close()


def test_configured_cross_vendor_http_fallback_survives_primary_outage() -> None:
    def openai_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_fallback",
                "choices": [{"message": {"content": json.dumps(_event_payload())}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    settings = Settings(anthropic_api_key="anthropic-key", openai_api_key="openai-key")
    clients = {
        "anthropic": _mock_client(
            lambda _request: httpx.Response(503, json={"error": "outage"}),
            settings.anthropic_base_url,
        ),
        "openai": _mock_client(openai_handler, settings.openai_base_url),
    }
    providers = build_providers_by_tier(
        settings,
        tiers=(LLMTier.T1,),
        clients_by_provider=clients,
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=repository,
        providers_by_tier=providers,
    )

    result = orchestrator.run(_make_request(job_key="http-cross-vendor-fallback"))

    assert result.run.provider == "openai"
    assert result.run.model == "gpt-4.1-mini"
    assert result.degraded_provider == "openai"
    assert repository.llm_runs[0].provider == "anthropic"
    assert repository.llm_runs[0].status == "failed"

    for provider_client in clients.values():
        provider_client.close()


def test_configured_deepseek_fallback_handles_gemini_rate_limit() -> None:
    def deepseek_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "deepseek_rate_limit_fallback",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(_event_payload())},
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    settings = Settings(
        gemini_api_key="gemini-key",
        deepseek_api_key="deepseek-key",
        llm_models={"T2": "gemini-3.6-flash"},
        llm_tier_providers={"T2": "gemini"},
        llm_tier_fallbacks={"T2": [{"provider": "deepseek", "model": "deepseek-v4-pro"}]},
    )
    clients = {
        "gemini": _mock_client(
            lambda _request: httpx.Response(429, json={"error": "quota marker"}),
            settings.gemini_base_url,
        ),
        "deepseek": _mock_client(deepseek_handler, settings.deepseek_base_url),
    }
    providers = build_providers_by_tier(
        settings,
        tiers=(LLMTier.T2,),
        clients_by_provider=clients,
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=repository,
        providers_by_tier=providers,
    )

    try:
        result = orchestrator.run(
            _make_request(
                job_key="gemini-rate-limit-fallback",
                requested_tier=LLMTier.T2,
                is_realtime=True,
            )
        )
    finally:
        orchestrator.close()

    assert result.run.provider == "deepseek"
    assert result.run.model == "deepseek-v4-pro"
    assert result.degraded_provider == "deepseek"
    assert repository.llm_runs[0].provider == "gemini"
    assert repository.llm_runs[0].status == "failed"
    assert repository.llm_runs[0].error_details["provider_failure_code"] == "http_rate_limited"


def test_current_t2_openai_rate_limit_falls_back_to_deepseek_with_safe_audit() -> None:
    prompt_marker = "private-news-prompt-marker"
    response_marker = "openai-private-response-marker"
    openai_key_marker = "openai-private-key-marker"
    deepseek_key_marker = "deepseek-private-key-marker"

    def deepseek_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "deepseek_openai_rate_limit_fallback",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(_event_payload())},
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    settings = Settings(
        openai_api_key=openai_key_marker,
        deepseek_api_key=deepseek_key_marker,
        llm_models={"T2": "gpt-4.1"},
        llm_tier_providers={"T2": "openai"},
        llm_tier_fallbacks={"T2": [{"provider": "deepseek", "model": "deepseek-v4-pro"}]},
    )
    clients = {
        "openai": _mock_client(
            lambda _request: httpx.Response(429, json={"error": response_marker}),
            settings.openai_base_url,
        ),
        "deepseek": _mock_client(deepseek_handler, settings.deepseek_base_url),
    }
    providers = build_providers_by_tier(
        settings,
        tiers=(LLMTier.T2,),
        clients_by_provider=clients,
    )
    repository = InMemoryLLMRuntimeRepository()
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=repository,
        providers_by_tier=providers,
    )
    request = replace(
        _make_request(
            job_key="openai-rate-limit-fallback",
            requested_tier=LLMTier.T2,
            is_realtime=True,
        ),
        prompt=prompt_marker,
    )

    try:
        result = orchestrator.run(request)
    finally:
        orchestrator.close()

    failed_run, successful_run = repository.llm_runs
    assert (failed_run.provider, failed_run.model, failed_run.status) == (
        "openai",
        "gpt-4.1",
        "failed",
    )
    assert failed_run.model_params["degraded_provider"] is None
    assert failed_run.error_message == "provider invocation failure"
    assert failed_run.error_details["provider_failure_code"] == "http_rate_limited"
    assert failed_run.raw_output is None
    assert failed_run.output == {}

    assert successful_run is result.run
    assert (successful_run.provider, successful_run.model, successful_run.status) == (
        "deepseek",
        "deepseek-v4-pro",
        "succeeded",
    )
    assert result.degraded_provider == "deepseek"
    assert successful_run.model_params["degraded_provider"] == "deepseek"
    assert successful_run.attempt == 2
    assert repository.last_job_for_key(request.job.job_key).state == "succeeded"

    audit_payload = json.dumps(
        [
            {
                "error_message": run.error_message,
                "error_details": run.error_details,
                "input_refs": run.input_refs,
                "model_params": run.model_params,
                "output": run.output,
                "raw_output": run.raw_output,
            }
            for run in repository.llm_runs
        ],
        sort_keys=True,
    )
    for private_marker in (
        prompt_marker,
        response_marker,
        openai_key_marker,
        deepseek_key_marker,
    ):
        assert private_marker not in audit_payload


def test_provider_factory_rejects_an_unsupported_provider_name() -> None:
    settings = Settings(
        anthropic_api_key="key",
        openai_api_key="key",
        llm_tier_providers={
            "T1": "not-a-provider",
            "T2": "anthropic",
            "T3": "openai",
        },
    )

    with pytest.raises(LLMProviderError, match="unsupported provider"):
        build_providers_by_tier(settings, tiers=(LLMTier.T1,))


# --- Temperature: opt-in, validated, and carried end to end ---------------------------


def test_a_request_without_a_temperature_sends_none_and_records_none() -> None:
    """The default is backward-compatible: no sampling field in the body, no value on the run."""

    captured: dict[str, Any] = {}
    settings = Settings(anthropic_api_key="anthropic-key")
    provider = AnthropicMessagesProvider(
        settings=settings,
        model_name="claude-haiku-4-5",
        max_output_tokens=2_000,
        client=_mock_client(_anthropic_handler(captured), settings.anthropic_base_url),
    )
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,)},
    )

    result = orchestrator.run(_make_request(job_key="no-temperature"))

    assert "temperature" not in captured["body"]
    assert result.run.temperature is None


@pytest.mark.parametrize(
    ("provider_name", "url"),
    [
        ("anthropic", "https://api.anthropic.com/v1/messages"),
        ("openai", "https://api.openai.com/v1/chat/completions"),
    ],
)
def test_a_requested_temperature_reaches_both_provider_bodies(provider_name: str, url: str) -> None:
    captured: dict[str, Any] = {}
    settings = Settings(anthropic_api_key="anthropic-key", openai_api_key="openai-key")
    if provider_name == "anthropic":
        provider: Any = AnthropicMessagesProvider(
            settings=settings,
            model_name="claude-haiku-4-5",
            max_output_tokens=2_000,
            client=_mock_client(_anthropic_handler(captured), settings.anthropic_base_url),
        )
    else:
        provider = OpenAIChatCompletionsProvider(
            settings=settings,
            model_name="gpt-5",
            max_output_tokens=2_000,
            client=_mock_client(_openai_handler(captured), settings.openai_base_url),
        )

    provider.invoke(
        LLMInvocationRequest(
            prompt_name="news",
            prompt_version="v1",
            prompt_template_version="v1",
            prompt="Adjudicate.",
            requested_schema="EventExtraction",
            temperature=0.0,
        )
    )

    assert captured["url"] == url
    assert captured["body"]["temperature"] == 0.0


def test_the_orchestrator_persists_the_requested_temperature_on_the_run() -> None:
    settings = Settings(anthropic_api_key="anthropic-key")
    provider = AnthropicMessagesProvider(
        settings=settings,
        model_name="claude-haiku-4-5",
        max_output_tokens=2_000,
        client=_mock_client(_anthropic_handler(), settings.anthropic_base_url),
    )
    orchestrator = LLMOrchestrator(
        settings=settings,
        repository=InMemoryLLMRuntimeRepository(),
        providers_by_tier={"T1": (provider,)},
    )

    request = _make_request(job_key="temperature-run")
    result = orchestrator.run(LLMOrchestratorRequest(**{**request.__dict__, "temperature": 0.0}))

    # `llm_runs.temperature` is what makes a deterministic run auditable as one.
    assert result.run.temperature == 0.0


def test_temperature_is_part_of_the_cache_identity() -> None:
    """Two runs that asked for different sampling are different runs, and never replay each other."""

    base = _invocation_request()
    keys = {
        make_llm_request_cache_key(
            request=LLMInvocationRequest(**{**base.__dict__, "temperature": temperature}),
            provider_name="anthropic",
            model_name="claude-haiku-4-5",
            model_version="current",
            mode=LLMInvocationMode.REALTIME,
        )
        for temperature in (None, 0.0, 1.0)
    }

    assert len(keys) == 3


def test_a_validation_retry_re_asks_at_the_temperature_it_asked_at() -> None:
    request = LLMInvocationRequest(
        prompt_name="news",
        prompt_version="v1",
        prompt_template_version="v1",
        prompt="Adjudicate.",
        requested_schema="EntityLinkAdjudication",
        temperature=0.0,
        metadata={"origin": "adjudication"},
    )

    retried = request.with_feedback("decision must be whitelisted")

    assert retried.temperature == 0.0
    assert retried.metadata == {"origin": "adjudication"}
    assert retried.prompt.endswith("Validation feedback: decision must be whitelisted")


@pytest.mark.parametrize("invalid", [-0.1, 2.1, "0", True])
def test_an_out_of_range_temperature_is_rejected_before_any_provider_is_called(
    invalid: Any,
) -> None:
    """The 0-2 bound is the providers' and `ck_llm_runs_temperature_range`'s; it fails fast here."""

    with pytest.raises(ValueError, match="temperature"):
        LLMInvocationRequest(
            prompt_name="news",
            prompt_version="v1",
            prompt_template_version="v1",
            prompt="Adjudicate.",
            requested_schema="EventExtraction",
            temperature=invalid,
        )


def test_production_runtime_scopes_limits_to_actually_routed_providers() -> None:
    settings = Settings(
        anthropic_api_key="key",
        openai_api_key="key",
        # A pre-Gemini/DeepSeek deployment override remains valid because neither new
        # provider is in the default primary/fallback routes.
        llm_provider_rpm_limits={"anthropic": 11, "openai": 42},
    )

    orchestrator = build_production_orchestrator(
        settings=settings,
        session=object(),  # type: ignore[arg-type]
        redis_client=FakeRedis(),
    )

    limiter = orchestrator._limiter
    assert isinstance(limiter, CompositeProviderLimiter)
    assert set(limiter.request_limiters) == {"anthropic", "openai"}
    assert set(limiter.token_limiters) == {"anthropic", "openai"}
    orchestrator.close()


def test_the_production_orchestrator_can_leave_the_transaction_to_its_caller() -> None:
    """ADR 0005 linking runs the orchestrator inside its own unit of work, so it must not commit."""

    class SpySession:
        def __init__(self) -> None:
            self.commits = 0
            self.flushes = 0

        def add(self, _obj: Any) -> None: ...

        def commit(self) -> None:
            self.commits += 1

        def flush(self) -> None:
            self.flushes += 1

    settings = Settings(anthropic_api_key="key", openai_api_key="key")
    session = SpySession()
    orchestrator = build_production_orchestrator(
        settings=settings,
        session=session,
        redis_client=FakeRedis(),
        commit_on_write=False,
    )

    # The repository is internal to the orchestrator, and the switch it was built with is the
    # whole point of this assertion: the caller's transaction has to survive an audit write.
    orchestrator._repository.save_llm_run(
        LLMRun(prompt_name="p", prompt_version="v1", provider="anthropic", model="m")
    )

    assert (session.commits, session.flushes) == (0, 1)
