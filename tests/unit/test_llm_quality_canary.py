"""Offline coverage for the bounded Gemini/DeepSeek production-workload canary."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from packages.config.settings import Settings
from services.entities.adjudication import (
    ADJUDICATION_PROMPT_TEMPLATE_VERSION,
    build_adjudication_prompt,
)
from services.evaluation.llm_quality_canary import (
    ACTIVE_ROUTE_MAX_CALLS,
    ACTIVE_ROUTE_MAX_COST_USD,
    ACTIVE_ROUTE_PROFILE,
    ACTIVE_ROUTE_REPORT_SCHEMA,
    DEFAULT_CASES_PATH,
    FROZEN_CANARY_FIXTURE_SHA256,
    CanaryAxis,
    CanaryCheck,
    LLMQualityCanaryError,
    ScoredBlock,
    build_active_route_canary_plan,
    build_analogy_fixture,
    build_canary_plan,
    build_composition_fixture,
    build_entity_fixture,
    build_grounding_fixture,
    check_decision,
    load_canary_cases,
    run_active_route_canary,
    run_quality_canary,
    score_analogy_case,
    score_composition_case,
    score_entity_case,
    score_grounding_case,
)
from services.llm.adapters import LLMInvocationRequest, LLMProviderAdapter
from services.llm.fake_providers import CallableLLMProvider
from services.llm.policy import LLMTier
from services.reports.grounding_prompts import (
    GROUNDING_PROMPT_TEMPLATE_VERSION,
    build_grounding_prompt,
)
from services.reports.prompts import (
    COMPOSITION_PROMPT_TEMPLATE_VERSION,
    build_top_event_prompt,
)

ENTITY_A = "10000000-0000-4000-8000-000000000001"
GROUND_A = "20000000-0000-4000-8000-000000000001"
GROUND_B = "20000000-0000-4000-8000-000000000002"
ANALOGY_A = "30000000-0000-4000-8000-000000000002"
COMPOSE_A = "40000000-0000-4000-8000-000000000002"
COMPOSE_B = "40000000-0000-4000-8000-000000000003"

COMPOSITION_TEXT = (
    "Northstar Central Bank cut its benchmark rate from 5.00% to 4.75% on July 30, a "
    "quarter-point move and its first reduction in nine months. The decision signals a cautious "
    "shift after inflation eased for a third consecutive month, but it does not mark an end to "
    "restraint. Officials still describe policy as restrictive, meaning financing conditions "
    "should remain tight for households and businesses even after the cut. The immediate market "
    "significance is a modest reduction in borrowing pressure alongside clearer evidence that "
    "price momentum is cooling. Still, the bank emphasized that future decisions will depend on "
    "incoming data, so investors should not assume a fixed easing path. Additional cuts remain "
    "conditional on inflation continuing to moderate without a renewed acceleration in demand, "
    "wages, or other price pressures. This balance makes each new inflation and activity release "
    "important for expectations about the next meeting."
)


def _settings() -> Settings:
    return Settings(
        gemini_api_key="canary-gemini-secret-marker",
        deepseek_api_key="canary-deepseek-secret-marker",
        llm_models={
            "T0": "text-embedding-3-small",
            "T1": "gemini-3.5-flash-lite",
            "T2": "gemini-3.6-flash",
            "T3": "deepseek-v4-pro",
        },
        llm_tier_providers={
            "T0": "openai",
            "T1": "gemini",
            "T2": "gemini",
            "T3": "deepseek",
        },
        llm_tier_fallbacks={
            "T1": [{"provider": "deepseek", "model": "deepseek-v4-flash"}],
            "T2": [{"provider": "deepseek", "model": "deepseek-v4-pro"}],
        },
    )


def _active_settings() -> Settings:
    return Settings(
        openai_api_key="canary-openai-secret-marker",
        gemini_api_key="canary-gemini-secret-marker",
        deepseek_api_key="canary-deepseek-secret-marker",
        llm_models={
            "T0": "text-embedding-3-small",
            "T1": "gemini-3.5-flash-lite",
            "T2": "gpt-4.1",
            "T3": "deepseek-v4-pro",
        },
        llm_tier_providers={
            "T0": "openai",
            "T1": "gemini",
            "T2": "openai",
            "T3": "deepseek",
        },
        llm_tier_fallbacks={
            "T1": [{"provider": "deepseek", "model": "deepseek-v4-flash"}],
            "T2": [{"provider": "deepseek", "model": "deepseek-v4-pro"}],
        },
    )


def _case(workload: str) -> dict[str, Any]:
    return next(case for case in load_canary_cases() if case["workload"] == workload)


def _all_passed(checks: Sequence[CanaryCheck]) -> bool:
    return all(check.passed for check in checks)


def test_synthetic_fixtures_reach_the_production_prompt_builders() -> None:
    entity_case = _case("entity_adjudication")
    mention, link_result = build_entity_fixture(entity_case)
    entity_prompt = build_adjudication_prompt(mention, link_result)
    assert ENTITY_A in entity_prompt
    assert ADJUDICATION_PROMPT_TEMPLATE_VERSION in entity_prompt

    grounding_case = _case("claim_grounding")
    block_text, grounding_claims = build_grounding_fixture(grounding_case)
    grounding_prompt = build_grounding_prompt(
        block_text=block_text,
        claims=grounding_claims,
    )
    assert GROUND_A in grounding_prompt
    assert GROUND_B in grounding_prompt
    assert GROUNDING_PROMPT_TEMPLATE_VERSION in grounding_prompt

    analogy_case = _case("analogy_rerank")
    event, candidates, regime_tags = build_analogy_fixture(analogy_case)
    assert event.id
    assert [str(candidate.episode_id) for candidate in candidates][0] == ANALOGY_A
    assert regime_tags

    composition_case = _case("report_composition")
    selected_event, composition_claims, _inputs, _context, _material = build_composition_fixture(
        composition_case
    )
    budget = composition_case["budget"]
    composition_prompt = build_top_event_prompt(
        event=selected_event,
        claims=composition_claims,
        target=budget["target"],
        minimum=budget["minimum"],
        maximum=budget["maximum"],
    )
    assert COMPOSE_A in composition_prompt
    assert COMPOSE_B in composition_prompt
    assert COMPOSITION_PROMPT_TEMPLATE_VERSION in composition_prompt


def test_pure_scorecards_accept_known_good_results_and_reject_targeted_errors() -> None:
    entity_checks, _ = score_entity_case(selected_id=ENTITY_A, expected_id=ENTITY_A)
    assert _all_passed(entity_checks)
    wrong_entity, _ = score_entity_case(
        selected_id="10000000-0000-4000-8000-000000000002",
        expected_id=ENTITY_A,
    )
    assert check_decision(wrong_entity) == "failed_safety"

    expected_grounding = {GROUND_A: "supported", GROUND_B: "unsupported"}
    grounding_checks, _ = score_grounding_case(
        verdicts=[(GROUND_A, "supported"), (GROUND_B, "unsupported")],
        expected=expected_grounding,
    )
    assert _all_passed(grounding_checks)
    unsafe_grounding, _ = score_grounding_case(
        verdicts=[(GROUND_A, "supported"), (GROUND_B, "unverifiable")],
        expected=expected_grounding,
    )
    assert check_decision(unsafe_grounding) == "failed_safety"

    analogy_checks, _ = score_analogy_case(
        selected_ids=[ANALOGY_A],
        explanations=["The funding structure and withdrawal channel match."],
        candidate_ids=[ANALOGY_A, "30000000-0000-4000-8000-000000000003"],
        expected_ids=[ANALOGY_A],
        matched=True,
    )
    assert _all_passed(analogy_checks)
    wrong_analogy, _ = score_analogy_case(
        selected_ids=["30000000-0000-4000-8000-000000000003"],
        explanations=["A mobile outage."],
        candidate_ids=[ANALOGY_A, "30000000-0000-4000-8000-000000000003"],
        expected_ids=[ANALOGY_A],
        matched=True,
    )
    assert check_decision(wrong_analogy) == "failed_semantics"

    composition_case = _case("report_composition")
    _event, claims, _inputs, _context, _material = build_composition_fixture(composition_case)
    composition_checks, observations = score_composition_case(
        blocks=(ScoredBlock(text=COMPOSITION_TEXT, claim_ids=(COMPOSE_A, COMPOSE_B)),),
        claims=claims,
        required_claim_ids=(COMPOSE_A, COMPOSE_B),
        minimum=120,
        maximum=180,
    )
    assert _all_passed(composition_checks)
    assert observations["word_count"] == 143

    bad_citation, _ = score_composition_case(
        blocks=(ScoredBlock(text=COMPOSITION_TEXT, claim_ids=(COMPOSE_A,)),),
        claims=claims,
        required_claim_ids=(COMPOSE_A, COMPOSE_B),
        minimum=120,
        maximum=180,
    )
    assert check_decision(bad_citation) == "failed_safety"


def test_gate_is_lexicographic() -> None:
    checks = [
        CanaryCheck("quality", CanaryAxis.QUALITY, False),
        CanaryCheck("semantics", CanaryAxis.SEMANTICS, False),
        CanaryCheck("safety", CanaryAxis.SAFETY, False),
        CanaryCheck("contract", CanaryAxis.CONTRACT, False),
    ]
    assert check_decision(checks) == "failed_contract"
    assert check_decision(checks[:-1]) == "failed_safety"


def test_preflight_reserves_all_retry_paths_before_live_construction() -> None:
    plan = build_canary_plan(_settings())
    assert plan["mode"] == "plan"
    assert plan["guardrails"]["max_network_calls"] == 20
    assert 0 < plan["guardrails"]["reserved_cost_usd"] <= 0.50
    gemini_routes = [route for route in plan["routes"] if route["provider"] == "gemini"]
    assert {route["thinking_level"] for route in gemini_routes} == {"minimal", "low"}
    assert plan["external_io"] == {
        "network": False,
        "database": False,
        "redis": False,
        "broker": False,
        "cache": False,
        "files_written": False,
    }

    with pytest.raises(LLMQualityCanaryError):
        build_canary_plan(_settings(), max_calls=19)
    with pytest.raises(LLMQualityCanaryError):
        build_canary_plan(_settings(), max_cost_usd=0.01)


def test_plan_accepts_a_repo_relative_fixture_path(monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(repository_root)

    plan = build_canary_plan(
        _settings(),
        cases_path=Path("evaluation/llm-canary/cases.v1.json"),
    )

    assert plan["fixture"]["path"] == "evaluation/llm-canary/cases.v1.json"


def test_cli_accepts_a_repo_relative_fixture_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    script_path = repository_root / "scripts" / "run-llm-quality-canary.py"
    spec = importlib.util.spec_from_file_location("llm_quality_canary_cli", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(repository_root)
    monkeypatch.setattr(module, "get_settings", _settings)

    exit_code = module.main(["--cases-file", "evaluation/llm-canary/cases.v1.json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["mode"] == "plan"
    assert report["fixture"]["path"] == "evaluation/llm-canary/cases.v1.json"


def _payload(request: LLMInvocationRequest) -> Mapping[str, Any]:
    envelope = {
        "schema_name": request.requested_schema,
        "schema_version": "1.0",
        "prompt_template_version": request.prompt_template_version,
    }
    if request.prompt_name == "entity_link_adjudication":
        return {**envelope, "decision": ENTITY_A}
    if request.prompt_name == "daily_brief_claim_grounding":
        return {
            **envelope,
            "verdicts": [
                {"claim_id": GROUND_A, "verdict": "supported", "rationale": ""},
                {"claim_id": GROUND_B, "verdict": "unsupported", "rationale": ""},
            ],
        }
    if request.prompt_name == "historical_analogy_rerank":
        return {
            **envelope,
            "analogies": [
                {
                    "historical_episode_id": ANALOGY_A,
                    "explanation": "The deposit concentration, losses, and withdrawal mechanism match.",
                    "regime_caveats": [],
                    "similarity_score": 96,
                    "confidence": 0.96,
                }
            ],
        }
    if request.prompt_name == "daily_brief_top_event":
        return {
            **envelope,
            "blocks": [
                {
                    "text": COMPOSITION_TEXT,
                    "claim_ids": [COMPOSE_A, COMPOSE_B],
                }
            ],
        }
    raise AssertionError("unexpected canary prompt")


def _fake_builder(
    settings: Settings,
    *,
    tiers: Sequence[LLMTier],
) -> dict[str, tuple[LLMProviderAdapter, ...]]:
    return {
        tier.value: (
            CallableLLMProvider(
                _payload,
                provider_name=settings.llm_tier_providers[tier.value],
                model_name=settings.llm_models[tier.value],
                model_version="current",
            ),
        )
        for tier in tiers
    }


def test_full_fake_matrix_passes_without_serializing_secrets_or_raw_content() -> None:
    report = run_quality_canary(_settings(), provider_builder=_fake_builder)
    assert report["decision"] == "advance_to_shadow"
    assert report["totals"]["case_provider_pairs"] == 8
    assert report["totals"]["network_calls"] == 8
    assert all(result["status"] == "passed" for result in report["results"])

    serialized = json.dumps(report, sort_keys=True)
    assert "canary-gemini-secret-marker" not in serialized
    assert "canary-deepseek-secret-marker" not in serialized
    assert COMPOSITION_TEXT not in serialized
    for forbidden_key in ("raw_output", "raw_metadata", "prompt", "structured", "headers"):
        assert f'"{forbidden_key}"' not in serialized


def test_filtered_live_run_is_diagnostic_only_even_when_the_case_passes() -> None:
    case_id = str(_case("entity_adjudication")["case_id"])

    report = run_quality_canary(
        _settings(),
        provider_names=("gemini",),
        case_ids=(case_id,),
        provider_builder=_fake_builder,
    )

    assert report["results"][0]["status"] == "passed"
    summary = report["provider_summaries"][0]
    assert summary["provider"] == "gemini"
    assert summary["passed"] is True
    assert summary["full_workload_coverage"] is False
    assert summary["case_count"] == 1
    assert summary["network_calls"] == 1
    assert summary["estimated_cost_usd"] >= 0.0
    assert report["decision"] == "diagnostic_only"
    assert report["decision_scope"] == "partial_diagnostic_only"


def test_active_route_plan_is_fixed_bounded_and_write_free() -> None:
    plan = build_active_route_canary_plan(_active_settings())

    assert plan["schema"] == ACTIVE_ROUTE_REPORT_SCHEMA
    assert plan["profile"] == ACTIVE_ROUTE_PROFILE
    assert plan["mode"] == "active_route_plan"
    assert plan["fixture"]["sha256"] == FROZEN_CANARY_FIXTURE_SHA256
    assert plan["routes"] == [
        {
            "provider": "gemini",
            "model": "gemini-3.5-flash-lite",
            "tier": "T1",
            "role": "primary",
            "thinking_level": "minimal",
        },
        {
            "provider": "openai",
            "model": "gpt-4.1",
            "tier": "T2",
            "role": "primary",
            "thinking_level": None,
        },
    ]
    assert plan["workloads"] == [
        "entity_adjudication",
        "claim_grounding",
        "analogy_rerank",
        "report_composition",
    ]
    assert plan["guardrails"]["configured_max_network_calls"] == ACTIVE_ROUTE_MAX_CALLS
    assert plan["guardrails"]["max_network_calls"] == ACTIVE_ROUTE_MAX_CALLS
    assert 0 < plan["guardrails"]["reserved_cost_usd"] <= ACTIVE_ROUTE_MAX_COST_USD
    assert [pair["max_network_calls"] for pair in plan["guardrails"]["pairs"]] == [
        1,
        1,
        1,
        3,
    ]
    assert plan["external_io"] == {
        "network": False,
        "database": False,
        "redis": False,
        "broker": False,
        "cache": False,
        "files_written": False,
    }
    assert plan["promotion_eligible"] is False


def test_active_route_plan_rejects_fixture_or_cap_drift(tmp_path: Path) -> None:
    copied = tmp_path / "cases.v1.json"
    copied.write_bytes(DEFAULT_CASES_PATH.read_bytes())

    with pytest.raises(LLMQualityCanaryError):
        build_active_route_canary_plan(_active_settings(), cases_path=copied)
    with pytest.raises(LLMQualityCanaryError):
        build_active_route_canary_plan(_active_settings(), max_calls=ACTIVE_ROUTE_MAX_CALLS + 1)
    with pytest.raises(LLMQualityCanaryError):
        build_active_route_canary_plan(
            _active_settings(), max_cost_usd=ACTIVE_ROUTE_MAX_COST_USD + 0.01
        )


def test_active_route_plan_requires_the_exact_primary_provider_models() -> None:
    wrong_t1 = _active_settings().model_copy(
        update={"llm_tier_providers": {"T1": "openai", "T2": "openai"}}
    )
    wrong_t2_model = _active_settings().model_copy(
        update={
            "llm_models": {
                **_active_settings().llm_models,
                "T2": "gpt-4.1-mini",
            }
        }
    )

    with pytest.raises(LLMQualityCanaryError):
        build_active_route_canary_plan(wrong_t1)
    with pytest.raises(LLMQualityCanaryError):
        build_active_route_canary_plan(wrong_t2_model)


def test_active_route_fake_run_is_mixed_isolated_and_secret_safe() -> None:
    observed: dict[str, Any] = {}

    def builder(
        settings: Settings,
        *,
        tiers: Sequence[LLMTier],
    ) -> dict[str, tuple[LLMProviderAdapter, ...]]:
        observed["providers"] = dict(settings.llm_tier_providers)
        observed["fallbacks"] = dict(settings.llm_tier_fallbacks)
        observed["tiers"] = tuple(tier.value for tier in tiers)
        return _fake_builder(settings, tiers=tiers)

    report = run_active_route_canary(_active_settings(), provider_builder=builder)

    assert report["decision"] == "pass"
    assert report["passed"] is True
    assert report["complete_profile"] is True
    assert report["promotion_eligible"] is False
    assert report["decision_scope"] == "active_route_canary_only"
    assert report["cleanup_failure_count"] == 0
    assert len(report["results"]) == 4
    assert report["guardrails"]["cost_ledger"]["network_calls"] == 4
    assert report["guardrails"]["cost_ledger"]["accounted_cost_upper_bound_usd"] <= 0.25
    assert observed == {
        "providers": {
            "T0": "openai",
            "T1": "gemini",
            "T2": "openai",
            "T3": "deepseek",
        },
        "fallbacks": {},
        "tiers": ("T1", "T2"),
    }
    assert [result["provider"] for result in report["results"]] == [
        "gemini",
        "gemini",
        "openai",
        "openai",
    ]
    assert all(not result["failed_checks"] for result in report["results"])

    serialized = json.dumps(report, sort_keys=True)
    for forbidden in (
        "canary-openai-secret-marker",
        "canary-gemini-secret-marker",
        "canary-deepseek-secret-marker",
        COMPOSITION_TEXT,
        '"observations"',
        '"raw_output"',
        '"raw_metadata"',
        '"prompt"',
        '"structured"',
        '"headers"',
    ):
        assert forbidden not in serialized


def test_active_route_uses_at_most_six_calls_across_both_retry_layers() -> None:
    composition_calls = 0

    def payload(request: LLMInvocationRequest) -> Mapping[str, Any]:
        nonlocal composition_calls
        if request.prompt_name != "daily_brief_top_event":
            return _payload(request)
        composition_calls += 1
        envelope = {
            "schema_name": request.requested_schema,
            "schema_version": "1.0",
            "prompt_template_version": request.prompt_template_version,
        }
        if composition_calls == 1:
            # The orchestrator consumes the first of the profile's two spare calls correcting this
            # invalid contract.
            return {**envelope, "blocks": []}
        if composition_calls == 2:
            # A valid but short response consumes the other spare call via the production word-
            # budget feedback path.
            return {
                **envelope,
                "blocks": [
                    {
                        "text": "Northstar cut rates to 4.75 percent while policy stayed restrictive.",
                        "claim_ids": [COMPOSE_A, COMPOSE_B],
                    }
                ],
            }
        return _payload(request)

    def builder(
        settings: Settings,
        *,
        tiers: Sequence[LLMTier],
    ) -> dict[str, tuple[LLMProviderAdapter, ...]]:
        return {
            tier.value: (
                CallableLLMProvider(
                    payload,
                    provider_name=settings.llm_tier_providers[tier.value],
                    model_name=settings.llm_models[tier.value],
                    model_version="current",
                ),
            )
            for tier in tiers
        }

    report = run_active_route_canary(_active_settings(), provider_builder=builder)

    assert report["decision"] == "pass"
    assert composition_calls == 3
    assert report["guardrails"]["cost_ledger"]["network_calls"] == 6
    assert report["guardrails"]["cost_ledger"]["refused_calls"] == 0
    composition = next(
        result for result in report["results"] if result["workload"] == "report_composition"
    )
    assert composition["status"] == "passed_with_retry"
    assert composition["metrics"]["network_calls"] == 3
    assert composition["metrics"]["validation_retries"] == 1


def test_active_route_stops_scheduling_after_a_guard_refusal() -> None:
    def invalid_entity(request: LLMInvocationRequest) -> Mapping[str, Any]:
        if request.prompt_name == "entity_link_adjudication":
            return {
                "schema_name": request.requested_schema,
                "schema_version": "1.0",
                "prompt_template_version": request.prompt_template_version,
                "decision": "not-a-whitelisted-id",
            }
        return _payload(request)

    def builder(
        settings: Settings,
        *,
        tiers: Sequence[LLMTier],
    ) -> dict[str, tuple[LLMProviderAdapter, ...]]:
        return {
            tier.value: (
                CallableLLMProvider(
                    invalid_entity,
                    provider_name=settings.llm_tier_providers[tier.value],
                    model_name=settings.llm_models[tier.value],
                    model_version="current",
                ),
            )
            for tier in tiers
        }

    report = run_active_route_canary(_active_settings(), provider_builder=builder)

    assert report["decision"] == "hold"
    assert report["execution_status"] == "guard_stopped"
    assert report["complete_profile"] is False
    assert len(report["results"]) == 1
    assert report["guardrails"]["cost_ledger"]["network_calls"] == 1
    assert report["guardrails"]["cost_ledger"]["refused_calls"] == 1


def test_active_route_closes_both_adapters_on_keyboard_interrupt() -> None:
    providers: list[CallableLLMProvider] = []
    closed: list[str] = []

    class ClosingProvider(CallableLLMProvider):
        def close(self) -> None:
            closed.append(self.provider_name)

    def interrupt(_request: LLMInvocationRequest) -> BaseException:
        return KeyboardInterrupt()

    def builder(
        settings: Settings,
        *,
        tiers: Sequence[LLMTier],
    ) -> dict[str, tuple[LLMProviderAdapter, ...]]:
        built: dict[str, tuple[LLMProviderAdapter, ...]] = {}
        for tier in tiers:
            provider = ClosingProvider(
                interrupt if tier is LLMTier.T1 else _payload,
                provider_name=settings.llm_tier_providers[tier.value],
                model_name=settings.llm_models[tier.value],
                model_version="current",
            )
            providers.append(provider)
            built[tier.value] = (provider,)
        return built

    with pytest.raises(KeyboardInterrupt):
        run_active_route_canary(_active_settings(), provider_builder=builder)

    assert len(providers) == 2
    assert sorted(closed) == ["gemini", "openai"]


def test_active_route_missing_key_refuses_before_provider_construction() -> None:
    called = False

    def builder(
        settings: Settings,
        *,
        tiers: Sequence[LLMTier],
    ) -> dict[str, tuple[LLMProviderAdapter, ...]]:
        nonlocal called
        del settings, tiers
        called = True
        return {}

    settings = _active_settings().model_copy(update={"openai_api_key": ""})
    with pytest.raises(LLMQualityCanaryError):
        run_active_route_canary(settings, provider_builder=builder)
    assert called is False


def test_active_route_cli_uses_profile_defaults_and_rejects_filters(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    script_path = repository_root / "scripts" / "run-llm-quality-canary.py"
    spec = importlib.util.spec_from_file_location("active_llm_quality_canary_cli", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_settings", _active_settings)

    exit_code = module.main(["--active-route"])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["schema"] == ACTIVE_ROUTE_REPORT_SCHEMA
    assert report["guardrails"]["configured_max_network_calls"] == ACTIVE_ROUTE_MAX_CALLS
    assert report["guardrails"]["configured_max_cost_usd"] == ACTIVE_ROUTE_MAX_COST_USD

    exit_code = module.main(["--active-route", "--provider", "gemini"])
    refusal = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert refusal == {
        "schema": ACTIVE_ROUTE_REPORT_SCHEMA,
        "mode": "active_route_plan",
        "decision": "hold",
        "failure": {
            "type": "LLMQualityCanaryError",
            "reason": "preflight_or_configuration_refusal",
        },
    }
