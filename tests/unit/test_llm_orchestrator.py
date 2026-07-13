"""Deterministic unit tests for the LLM orchestration runtime.

Provider transport is mocked end to end (``httpx.MockTransport``); no test touches the
network.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
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
from services.llm.contracts import EventExtraction
from services.llm.fake_providers import CallableLLMProvider, ScriptedLLMProvider
from services.llm.http_providers import (
    AnthropicMessagesProvider,
    LLMBatchModeUnsupported,
    LLMProviderError,
    OpenAIChatCompletionsProvider,
    build_providers_by_tier,
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

    assert first_key == second_key
    assert first_key != changed_key


def test_cache_hit_persists_zero_cost_run() -> None:
    provider = TrackingProvider(
        scripts=(_event_payload(),),
        provider_name="cache-provider",
        model_name="cache-model",
    )
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
    in_memory = InMemoryTokenBucketLimiter(
        capacity=2, refill_per_second=1.0, time_func=lambda: 0.0
    )
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


def test_estimate_completion_cost_usd_known_and_missing_provider() -> None:
    settings = Settings()

    expected = estimate_completion_cost_usd(
        settings=settings,
        provider_name="anthropic",
        model_name="claude-haiku-4-5",
        input_tokens=1_000_000,
        output_tokens=500_000,
    )
    missing = estimate_completion_cost_usd(
        settings=settings,
        provider_name="unknown",
        model_name="missing",
        input_tokens=1,
        output_tokens=1,
    )

    assert expected == pytest.approx(3.50)  # $1.00 input + $2.50 output
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
    articles = [
        {"summary": "s" * 20, "relevance_score": float(index)} for index in range(20)
    ]

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
    limiter = InMemoryTokenBucketLimiter(
        capacity=0, refill_per_second=0.0, time_func=lambda: 0.0
    )
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


def test_live_adapters_reject_async_batch_submission() -> None:
    settings = Settings(anthropic_api_key="key", openai_api_key="key")
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


def test_missing_api_key_is_refused_at_construction() -> None:
    with pytest.raises(LLMProviderError, match="API key"):
        AnthropicMessagesProvider(
            settings=Settings(anthropic_api_key=""),
            model_name="claude-haiku-4-5",
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


def test_provider_factory_rejects_an_unsupported_provider_name() -> None:
    settings = Settings(
        anthropic_api_key="key",
        openai_api_key="key",
        llm_tier_providers={"T1": "deepseek", "T2": "anthropic", "T3": "openai"},
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
def test_a_requested_temperature_reaches_both_provider_bodies(
    provider_name: str, url: str
) -> None:
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
    result = orchestrator.run(
        LLMOrchestratorRequest(**{**request.__dict__, "temperature": 0.0})
    )

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
    orchestrator._repository.save_llm_run(LLMRun(prompt_name="p", prompt_version="v1", provider="anthropic", model="m"))

    assert (session.commits, session.flushes) == (0, 1)
