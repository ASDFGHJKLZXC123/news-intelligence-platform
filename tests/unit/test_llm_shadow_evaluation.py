"""Offline coverage for the bounded Gemini/DeepSeek shadow evaluation."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from packages.config.settings import Settings
from services.analogies.rerank import build_rerank_prompt
from services.entities.adjudication import AdjudicationDecision, build_adjudication_prompt
from services.evaluation import llm_shadow_evaluation as shadow_evaluation_module
from services.evaluation.llm_quality_canary import CanaryAxis, ScoredBlock
from services.evaluation.llm_shadow_evaluation import (
    DEFAULT_CASES_PATH,
    DIAGNOSTIC_MAX_CALLS,
    DIAGNOSTIC_MAX_COST_USD,
    FROZEN_SHADOW_FIXTURE_SHA256,
    LLMShadowEvaluationError,
    aggregate_shadow_results,
    build_shadow_diagnostic_plan,
    build_shadow_plan,
    load_shadow_cases,
    run_shadow_diagnostic,
    run_shadow_evaluation,
    score_shadow_analogy_case,
    score_shadow_composition_case,
    score_shadow_entity_case,
    score_shadow_grounding_case,
    shadow_fixture_sha256,
)
from services.llm.adapters import LLMInvocationRequest, LLMProviderAdapter
from services.llm.contracts import NIL_DECISION
from services.llm.fake_providers import CallableLLMProvider
from services.llm.policy import LLMTier
from services.reports.composition import count_words
from services.reports.grounding_prompts import build_grounding_prompt
from services.reports.prompts import build_top_event_prompt


def _settings() -> Settings:
    return Settings(
        gemini_api_key="shadow-gemini-secret-marker",
        deepseek_api_key="shadow-deepseek-secret-marker",
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


def _expected_ids(cases: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    return {
        workload: tuple(str(case["case_id"]) for case in cases if case["workload"] == workload)
        for workload in (
            "entity_adjudication",
            "claim_grounding",
            "analogy_rerank",
            "report_composition",
        )
    }


def _all_passed(checks: Sequence[Any]) -> bool:
    return all(check.passed for check in checks)


def test_shadow_corpus_expands_deterministically_with_closed_references() -> None:
    cases = load_shadow_cases()
    assert cases == load_shadow_cases()
    assert len(cases) == 125
    assert len({case["case_id"] for case in cases}) == 125
    assert Counter(case["workload"] for case in cases) == {
        "entity_adjudication": 25,
        "claim_grounding": 25,
        "analogy_rerank": 25,
        "report_composition": 50,
    }
    assert shadow_fixture_sha256() == FROZEN_SHADOW_FIXTURE_SHA256

    all_ids: set[str] = set()
    for case in cases:
        workload = case["workload"]
        assert case["tier"] == (
            "T1" if workload in {"entity_adjudication", "claim_grounding"} else "T2"
        )
        if workload == "entity_adjudication":
            allowed = {candidate["id"] for candidate in case["candidates"]}
            expected = case["expected"]["selected_id"]
            assert expected is None or expected in allowed
            identifiers = allowed
        elif workload == "claim_grounding":
            allowed = {claim["id"] for claim in case["claims"]}
            assert set(case["expected"]["verdicts"]) == allowed
            identifiers = allowed
        elif workload == "analogy_rerank":
            allowed = {candidate["id"] for candidate in case["candidates"]}
            assert set(case["expected"]["selected_ids"]) <= allowed
            assert set(case["expected"]["required_caveat_ids"]) <= allowed
            identifiers = allowed | {case["event"]["id"]}
        else:
            allowed = {claim["id"] for claim in case["claims"]}
            assert set(case["expected"]["required_claim_ids"]) <= allowed
            assert all(rule["claim_id"] in allowed for rule in case["expected"]["citation_rules"])
            assert case["budget"] == {"minimum": 120, "target": 150, "maximum": 180}
            identifiers = allowed | {case["event"]["id"]}
        assert not (all_ids & identifiers)
        all_ids.update(identifiers)
        for identifier in identifiers:
            uuid.UUID(identifier)
        for claim in case.get("claims", ()):
            assert str(claim["url"]).startswith("https://")
            assert ".test/" in str(claim["url"])
            assert len(str(claim["excerpt"])) <= 200


def test_every_case_reaches_its_production_prompt_builder() -> None:
    prompts: list[str] = []
    for case in load_shadow_cases():
        workload = case["workload"]
        if workload == "entity_adjudication":
            from services.evaluation.llm_quality_canary import build_entity_fixture

            mention, result = build_entity_fixture(case)
            prompt = build_adjudication_prompt(mention, result)
        elif workload == "claim_grounding":
            from services.evaluation.llm_quality_canary import build_grounding_fixture

            block, claims = build_grounding_fixture(case)
            prompt = build_grounding_prompt(block_text=block, claims=claims)
        elif workload == "analogy_rerank":
            from services.evaluation.llm_quality_canary import build_analogy_fixture

            event, candidates, tags = build_analogy_fixture(case)
            prompt = build_rerank_prompt(event, candidates, current_regime_tags=tags)
        else:
            from services.evaluation.llm_quality_canary import build_composition_fixture

            event, claims, _inputs, _context, _material = build_composition_fixture(case)
            prompt = build_top_event_prompt(
                event=event,
                claims=claims,
                target=case["budget"]["target"],
                minimum=case["budget"]["minimum"],
                maximum=case["budget"]["maximum"],
            )
        assert prompt
        prompts.append(prompt)
    assert len(set(prompts)) == 125


def test_pure_scorers_cover_nil_abstention_caveats_and_safety() -> None:
    assert _all_passed(
        score_shadow_entity_case(
            decision=AdjudicationDecision.NIL,
            selected_id=None,
            expected_selected_id=None,
        )
    )
    unsafe_entity = score_shadow_entity_case(
        decision=AdjudicationDecision.SELECTED,
        selected_id="wrong",
        expected_selected_id=None,
    )
    assert any(check.axis is CanaryAxis.SAFETY and not check.passed for check in unsafe_entity)

    grounding = score_shadow_grounding_case(
        verdicts=(("a", "supported"), ("b", "unsupported"), ("c", "unverifiable")),
        expected={"a": "supported", "b": "unsupported", "c": "unverifiable"},
    )
    assert _all_passed(grounding)
    unsafe_grounding = score_shadow_grounding_case(
        verdicts=(("a", "supported"), ("b", "unverifiable")),
        expected={"a": "supported", "b": "unsupported"},
    )
    assert any(check.axis is CanaryAxis.SAFETY and not check.passed for check in unsafe_grounding)

    abstention = score_shadow_analogy_case(
        selected_ids=(),
        explanations=(),
        caveats_by_id={},
        candidate_ids=("a", "b"),
        caveat_required_ids=(),
        expected_ids=(),
        matched=False,
        expected_matched=False,
    )
    assert _all_passed(abstention)
    missing_caveat = score_shadow_analogy_case(
        selected_ids=("a",),
        explanations=("The onset mechanism and funding channel align.",),
        caveats_by_id={"a": ()},
        candidate_ids=("a", "b"),
        caveat_required_ids=("a",),
        expected_ids=("a",),
        matched=True,
        expected_matched=True,
    )
    assert any(check.axis is CanaryAxis.SAFETY and not check.passed for check in missing_caveat)
    hindsight_caveat = score_shadow_analogy_case(
        selected_ids=("a",),
        explanations=("The onset mechanism and funding channel align.",),
        caveats_by_id={"a": ("The earlier institution eventually failed.",)},
        candidate_ids=("a", "b"),
        caveat_required_ids=("a",),
        expected_ids=("a",),
        matched=True,
        expected_matched=True,
    )
    assert any(
        check.name == "no_hindsight_outcome_claims" and not check.passed
        for check in hindsight_caveat
    )


def _composition_text(case: Mapping[str, Any], *, target: int = 130) -> str:
    concepts = [
        str(group[0])
        for group in case["expected"]["required_concepts"]
        if isinstance(group, list) and group
    ]
    words = " ".join(concepts).split()
    filler = (
        "This synthetic assessment presents measured context and a cautious comparison while "
        "keeping conclusions conditional on the documented evidence and stated timing"
    ).split()
    index = 0
    while len(words) < target:
        words.append(filler[index % len(filler)])
        index += 1
    return " ".join(words) + "."


def test_composition_scorer_accepts_fixture_driven_semantics_and_rejects_foreign_ids() -> None:
    from services.evaluation.llm_quality_canary import build_composition_fixture

    case = next(case for case in load_shadow_cases() if case["workload"] == "report_composition")
    _event, claims, _inputs, _context, _material = build_composition_fixture(case)
    claim_ids = tuple(str(claim.claim_id) for claim in claims)
    text = _composition_text(case)
    assert 120 <= count_words(text) <= 180
    checks = score_shadow_composition_case(
        blocks=(ScoredBlock(text=text, claim_ids=claim_ids),),
        claims=claims,
        expected=case["expected"],
        minimum=120,
        maximum=180,
    )
    assert _all_passed(checks)

    foreign = score_shadow_composition_case(
        blocks=(ScoredBlock(text=text, claim_ids=(*claim_ids, str(uuid.uuid4()))),),
        claims=claims,
        expected=case["expected"],
        minimum=120,
        maximum=180,
    )
    assert any(check.axis is CanaryAxis.CONTRACT and not check.passed for check in foreign)


def _result(case: Mapping[str, Any], provider: str) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "workload": case["workload"],
        "provider": provider,
        "status": "passed",
        "checks": [
            {"name": "contract", "axis": "contract", "passed": True},
            {"name": "safety", "axis": "safety", "passed": True},
        ],
        "observations": {"first_attempt_within_budget": case["workload"] == "report_composition"},
        "metrics": {
            "network_calls": 1,
            "workload_attempts": 1,
            "validation_retries": 0,
            "input_tokens": 1,
            "output_tokens": 1,
            "latency_ms": 1,
            "estimated_cost_usd": 0.0,
        },
    }


def _gate_matrix() -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...]]:
    cases = load_shadow_cases()
    return (
        [_result(case, provider) for provider in ("gemini", "deepseek") for case in cases],
        cases,
    )


def test_gate_boundaries_are_per_provider_and_partial_runs_hold() -> None:
    results, cases = _gate_matrix()
    expected = _expected_ids(cases)
    assert aggregate_shadow_results(results, expected_case_ids=expected)["decision"] == (
        "advance_to_limited_rollout"
    )

    for provider in ("gemini", "deepseek"):
        changed = 0
        for result in results:
            if result["provider"] == provider and changed < 2:
                result["status"] = "failed_semantics"
                changed += 1
    assert aggregate_shadow_results(results, expected_case_ids=expected)["decision"] == (
        "advance_to_limited_rollout"
    )
    next(
        result
        for result in results
        if result["provider"] == "gemini" and result["status"] == "passed"
    )["status"] = "failed_semantics"
    assert aggregate_shadow_results(results, expected_case_ids=expected)["decision"] == "hold"

    results, cases = _gate_matrix()
    compositions = [
        result
        for result in results
        if result["provider"] == "gemini" and result["workload"] == "report_composition"
    ]
    for result in compositions[:5]:
        result["observations"]["first_attempt_within_budget"] = False
    assert aggregate_shadow_results(results, expected_case_ids=_expected_ids(cases))[
        "decision"
    ] == ("advance_to_limited_rollout")
    compositions[5]["observations"]["first_attempt_within_budget"] = False
    assert aggregate_shadow_results(results, expected_case_ids=_expected_ids(cases))[
        "decision"
    ] == ("hold")

    results, cases = _gate_matrix()
    results[0]["checks"][0]["passed"] = False
    assert aggregate_shadow_results(results, expected_case_ids=_expected_ids(cases))[
        "decision"
    ] == ("hold")
    results, cases = _gate_matrix()
    assert (
        aggregate_shadow_results(results[:-1], expected_case_ids=_expected_ids(cases))["decision"]
        == "hold"
    )


def test_failure_fingerprint_is_fixed_count_only_and_deduplicated_per_case() -> None:
    cases = load_shadow_cases()
    case = next(case for case in cases if case["workload"] == "entity_adjudication")
    result = _result(case, "gemini")
    result["status"] = "secret-result-status-marker"
    result["checks"] = [
        {"name": "terminal_contract_valid", "axis": "contract", "passed": False},
        {"name": "terminal_contract_valid", "axis": "contract", "passed": False},
        {"name": "no_wrong_entity_attachment", "axis": "safety", "passed": False},
        {"name": "expected_entity_or_nil", "axis": "semantics", "passed": False},
        {"name": "secret-check-name-marker", "axis": "quality", "passed": False},
    ]
    result["audit"] = {
        "audit_run_status_counts": {
            "succeeded": 1,
            "secret-audit-status-marker": 1,
        },
        "failure_stage_counts": {"secret-failure-stage-marker": 1},
        "provider_failure_code_counts": {
            "http_rate_limited": 1,
            "secret-provider-failure-code-marker": 1,
        },
    }

    aggregate = aggregate_shadow_results(
        [result],
        expected_case_ids=_expected_ids(cases),
    )
    gemini = next(item for item in aggregate["provider_summaries"] if item["provider"] == "gemini")
    entity = next(item for item in gemini["workloads"] if item["workload"] == "entity_adjudication")

    assert set(entity["status_counts"]) == {
        "passed",
        "passed_with_retry",
        "failed_contract",
        "failed_safety",
        "failed_semantics",
        "failed_quality",
        "unrecognized",
    }
    assert sum(entity["status_counts"].values()) == entity["completed_cases"] == 1
    assert entity["status_counts"]["unrecognized"] == 1
    assert entity["failure_cases_by_axis"] == {
        "contract": 1,
        "safety": 1,
        "semantics": 1,
        "quality": 1,
    }
    assert entity["failed_check_cases"] == {
        "expected_entity_or_nil": 1,
        "no_wrong_entity_attachment": 1,
        "terminal_contract_valid": 1,
    }
    assert entity["unrecognized_failed_check_cases"] == 1
    assert entity["audit_run_status_counts"]["succeeded"] == 1
    assert entity["audit_run_status_counts"]["unrecognized"] == 1
    assert entity["failure_stage_counts"]["unclassified"] == 1
    assert entity["provider_failure_code_counts"]["http_rate_limited"] == 1
    assert entity["provider_failure_code_counts"]["unclassified"] == 1

    serialized = json.dumps(aggregate, sort_keys=True)
    for marker in (
        "secret-result-status-marker",
        "secret-check-name-marker",
        "secret-audit-status-marker",
        "secret-failure-stage-marker",
        "secret-provider-failure-code-marker",
    ):
        assert marker not in serialized


def test_composition_budget_diagnostics_use_only_fixed_direction_buckets() -> None:
    results = [
        {
            "observations": {
                "first_attempt_within_budget": status == "within",
                "first_attempt_budget_status": status,
                "degraded": False,
            },
            "metrics": {"workload_attempts": 1},
            "checks": [],
        }
        for status in (
            "within",
            "too_short",
            "too_long",
            "not_completed",
            "secret-budget-status-marker",
        )
    ]

    diagnostics = shadow_evaluation_module._composition_diagnostics(results)

    assert diagnostics["first_attempt_within_budget_cases"] == 1
    assert diagnostics["first_attempt_too_short_cases"] == 1
    assert diagnostics["first_attempt_too_long_cases"] == 1
    assert diagnostics["first_attempt_not_completed_cases"] == 1
    assert "secret-budget-status-marker" not in json.dumps(diagnostics, sort_keys=True)


def test_plan_is_no_io_and_rejects_upward_ceiling_overrides() -> None:
    plan = build_shadow_plan(_settings())
    assert plan["mode"] == "plan"
    assert plan["guardrails"]["configured_max_network_calls"] == 700
    assert plan["guardrails"]["configured_max_cost_usd"] == 5.0
    assert plan["external_io"] == {
        "network": False,
        "database": False,
        "redis": False,
        "broker": False,
        "cache": False,
        "files_written": False,
    }
    with pytest.raises(LLMShadowEvaluationError):
        build_shadow_plan(_settings(), max_calls=701)
    with pytest.raises(LLMShadowEvaluationError):
        build_shadow_plan(_settings(), max_cost_usd=5.01)
    with pytest.raises(LLMShadowEvaluationError):
        build_shadow_plan(
            _settings().model_copy(update={"gemini_base_url": "https://shadow-proxy.test"})
        )


def _payload_for(case: Mapping[str, Any], request: LLMInvocationRequest) -> Mapping[str, Any]:
    envelope = {
        "schema_name": request.requested_schema,
        "schema_version": "1.0",
        "prompt_template_version": request.prompt_template_version,
    }
    workload = case["workload"]
    if workload == "entity_adjudication":
        selected = case["expected"]["selected_id"]
        payload = {**envelope, "decision": selected if selected is not None else NIL_DECISION}
        if selected is None:
            payload["no_finding_reason"] = "No candidate matches the synthetic mention."
        return payload
    if workload == "claim_grounding":
        return {
            **envelope,
            "verdicts": [
                {"claim_id": claim_id, "verdict": verdict, "rationale": ""}
                for claim_id, verdict in case["expected"]["verdicts"].items()
            ],
        }
    if workload == "analogy_rerank":
        selected = list(case["expected"]["selected_ids"])
        if not selected:
            return {**envelope, "analogies": [], "no_finding_reason": "No reliable analogy."}
        required_caveats = set(case["expected"]["required_caveat_ids"])
        return {
            **envelope,
            "analogies": [
                {
                    "historical_episode_id": episode_id,
                    "explanation": (
                        "The onset mechanism, funding channel, and contemporaneous indicators "
                        "align structurally."
                    ),
                    "regime_caveats": (
                        ["The documented policy and funding regime differs."]
                        if episode_id in required_caveats
                        else []
                    ),
                    "similarity_score": 95 - index,
                    "confidence": 0.9,
                }
                for index, episode_id in enumerate(selected)
            ],
        }
    return {
        **envelope,
        "blocks": [
            {
                "text": _composition_text(case),
                "claim_ids": list(case["expected"]["required_claim_ids"]),
            }
        ],
    }


def _fake_builder(
    settings: Settings,
    *,
    tiers: Sequence[LLMTier],
) -> dict[str, tuple[LLMProviderAdapter, ...]]:
    cases = load_shadow_cases()
    queues = {
        "entity_link_adjudication": [
            case for case in cases if case["workload"] == "entity_adjudication"
        ],
        "daily_brief_claim_grounding": [
            case for case in cases if case["workload"] == "claim_grounding"
        ],
        "historical_analogy_rerank": [
            case for case in cases if case["workload"] == "analogy_rerank"
        ],
        "daily_brief_top_event": [
            case for case in cases if case["workload"] == "report_composition"
        ],
    }
    positions = Counter()

    def payload(request: LLMInvocationRequest) -> Mapping[str, Any]:
        position = positions[request.prompt_name]
        positions[request.prompt_name] += 1
        return _payload_for(queues[request.prompt_name][position], request)

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


_DIAGNOSTIC_CASE_IDS = {
    "gemini": {
        "claim_grounding": (
            "claim_grounding.grounding.supported_paraphrase.v1",
            "claim_grounding.grounding.direct_contradiction.v1",
            "claim_grounding.grounding.insufficient_evidence.v1",
            "claim_grounding.grounding.mixed_multiclaim.v1",
            "claim_grounding.grounding.instruction_decoy.v1",
        ),
        "report_composition": (
            "report_composition.composition.rate_policy.v1",
            "report_composition.composition.currency_intervention.v5",
        ),
    },
    "deepseek": {
        "report_composition": (
            "report_composition.composition.rate_policy.v1",
            "report_composition.composition.currency_intervention.v5",
        ),
    },
}


def _diagnostic_fake_builder(
    settings: Settings,
    *,
    tiers: Sequence[LLMTier],
) -> dict[str, tuple[LLMProviderAdapter, ...]]:
    provider = settings.llm_tier_providers[tiers[0].value]
    by_id = {str(case["case_id"]): case for case in load_shadow_cases()}
    prompt_queues = {
        "daily_brief_claim_grounding": [
            by_id[case_id]
            for case_id in _DIAGNOSTIC_CASE_IDS.get(provider, {}).get("claim_grounding", ())
        ],
        "daily_brief_top_event": [
            by_id[case_id]
            for case_id in _DIAGNOSTIC_CASE_IDS.get(provider, {}).get("report_composition", ())
        ],
    }
    positions = Counter()

    def payload(request: LLMInvocationRequest) -> Mapping[str, Any]:
        position = positions[request.prompt_name]
        positions[request.prompt_name] += 1
        return _payload_for(prompt_queues[request.prompt_name][position], request)

    return {
        tier.value: (
            CallableLLMProvider(
                payload,
                provider_name=provider,
                model_name=settings.llm_models[tier.value],
                model_version="current",
            ),
        )
        for tier in tiers
    }


def _forbidden_report_key(value: object) -> bool:
    if isinstance(value, Mapping):
        if set(value) & {
            "prompt",
            "text",
            "excerpt",
            "raw_output",
            "raw_metadata",
            "structured",
            "headers",
            "expected",
            "selected_id",
            "claim_ids",
        }:
            return True
        return any(_forbidden_report_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_forbidden_report_key(item) for item in value)
    return False


def test_full_fake_matrix_passes_and_report_is_aggregate_only() -> None:
    progress: list[Mapping[str, Any]] = []
    report = run_shadow_evaluation(
        _settings(),
        provider_builder=_fake_builder,
        progress=progress.append,
    )
    assert report["decision"] == "advance_to_limited_rollout"
    assert report["execution_status"] == "complete"
    assert report["totals"]["completed_case_provider_pairs"] == 250
    assert report["totals"]["network_calls"] == 250
    assert len(progress) == 25
    assert progress[-1]["completed_pairs"] == 250
    assert not _forbidden_report_key(report)

    expected_status_keys = {
        "passed",
        "passed_with_retry",
        "failed_contract",
        "failed_safety",
        "failed_semantics",
        "failed_quality",
        "unrecognized",
    }
    for provider in report["provider_summaries"]:
        assert set(provider["status_counts"]) == expected_status_keys
        assert provider["audit_run_status_counts"]["succeeded"] == 125
        assert sum(provider["failure_stage_counts"].values()) == 0
        assert provider["failure_cases_by_axis"] == {
            "contract": 0,
            "safety": 0,
            "semantics": 0,
            "quality": 0,
        }
        composition = next(
            item for item in provider["workloads"] if item["workload"] == "report_composition"
        )
        assert composition["composition_counters"] == {
            "scored_cases": 50,
            "first_attempt_within_budget_cases": 50,
            "final_within_budget_cases": 50,
            "degraded_cases": 0,
            "multi_attempt_cases": 0,
            "recorded_workload_attempts": 50,
            "first_attempt_too_short_cases": 0,
            "first_attempt_too_long_cases": 0,
            "first_attempt_not_completed_cases": 0,
        }
        for workload in provider["workloads"]:
            assert set(workload["status_counts"]) == expected_status_keys
            assert sum(workload["status_counts"].values()) == workload["completed_cases"]
            assert set(workload["failure_cases_by_axis"]) == {
                "contract",
                "safety",
                "semantics",
                "quality",
            }
            assert workload["audit_run_status_counts"]["succeeded"] == workload["network_calls"]

    serialized = json.dumps(report, sort_keys=True)
    for marker in (
        "shadow-gemini-secret-marker",
        "shadow-deepseek-secret-marker",
        "Atlas Storage Systems",
        "No reliable analogy",
    ):
        assert marker not in serialized
    assert "case_id" not in serialized


def test_diagnostic_plan_is_fixed_bounded_and_never_promotion_eligible() -> None:
    plan = build_shadow_diagnostic_plan(_settings())
    assert plan["mode"] == "diagnostic_plan"
    assert plan["decision"] == "hold"
    assert plan["decision_scope"] == "diagnostic_only"
    assert plan["promotion_eligible"] is False
    assert plan["guardrails"] == {
        "configured_max_network_calls": DIAGNOSTIC_MAX_CALLS,
        "required_worst_case_network_calls": DIAGNOSTIC_MAX_CALLS,
        "normal_path_network_calls": 9,
        "configured_max_cost_usd": DIAGNOSTIC_MAX_COST_USD,
        "cost_cap_kind": "rolling_conservative_per_call_reservation",
        "execution_may_stop_before_complete_profile": True,
    }
    assert plan["sample_counts"] == [
        {"provider": "gemini", "workload": "claim_grounding", "cases": 5},
        {"provider": "gemini", "workload": "report_composition", "cases": 2},
        {"provider": "deepseek", "workload": "report_composition", "cases": 2},
    ]
    assert [(route["provider"], route["tier"]) for route in plan["routes"]] == [
        ("gemini", "T1"),
        ("gemini", "T2"),
        ("deepseek", "T2"),
    ]
    assert plan["external_io"] == {
        "network": False,
        "database": False,
        "redis": False,
        "broker": False,
        "cache": False,
        "files_written": False,
    }
    serialized = json.dumps(plan, sort_keys=True)
    assert "case_id" not in serialized
    for provider_cases in _DIAGNOSTIC_CASE_IDS.values():
        for case_ids in provider_cases.values():
            assert all(case_id not in serialized for case_id in case_ids)

    with pytest.raises(LLMShadowEvaluationError):
        build_shadow_diagnostic_plan(_settings(), max_calls=DIAGNOSTIC_MAX_CALLS + 1)
    with pytest.raises(LLMShadowEvaluationError):
        build_shadow_diagnostic_plan(_settings(), max_cost_usd=DIAGNOSTIC_MAX_COST_USD + 0.01)


def test_shadow_profiles_reject_noncanonical_fixture_even_with_identical_bytes(
    tmp_path: Path,
) -> None:
    copied_fixture = tmp_path / "cases.v1.json"
    copied_fixture.write_bytes(DEFAULT_CASES_PATH.read_bytes())
    assert shadow_fixture_sha256(copied_fixture) == FROZEN_SHADOW_FIXTURE_SHA256

    with pytest.raises(LLMShadowEvaluationError, match="canonical frozen fixture path"):
        build_shadow_diagnostic_plan(_settings(), cases_path=copied_fixture)
    with pytest.raises(LLMShadowEvaluationError, match="canonical frozen fixture path"):
        build_shadow_plan(_settings(), cases_path=copied_fixture)


def test_full_fake_diagnostic_is_complete_count_only_and_still_holds() -> None:
    builder_calls: list[tuple[str, tuple[str, ...]]] = []

    def builder(
        settings: Settings,
        *,
        tiers: Sequence[LLMTier],
    ) -> dict[str, tuple[LLMProviderAdapter, ...]]:
        provider = settings.llm_tier_providers[tiers[0].value]
        builder_calls.append((provider, tuple(tier.value for tier in tiers)))
        return _diagnostic_fake_builder(settings, tiers=tiers)

    progress: list[Mapping[str, Any]] = []
    report = run_shadow_diagnostic(
        _settings(),
        provider_builder=builder,
        progress=progress.append,
    )

    assert report["execution_status"] == "complete"
    assert report["complete_profile"] is True
    assert report["decision"] == "hold"
    assert report["decision_scope"] == "diagnostic_only"
    assert report["promotion_eligible"] is False
    assert report["totals"]["expected_case_provider_pairs"] == 9
    assert report["totals"]["completed_case_provider_pairs"] == 9
    assert report["totals"]["network_calls"] == 9
    assert builder_calls == [("gemini", ("T1", "T2")), ("deepseek", ("T2",))]
    assert len(progress) == 9
    assert progress[-1]["completed_pairs"] == 9
    assert all(
        set(item)
        == {
            "completed_pairs",
            "expected_pairs",
            "network_calls",
            "accounted_cost_upper_bound_usd",
        }
        for item in progress
    )
    assert report["external_io"] == {
        "network": True,
        "database": False,
        "redis": False,
        "broker": False,
        "cache": False,
        "files_written": False,
    }
    assert not _forbidden_report_key(report)

    serialized = json.dumps(report, sort_keys=True)
    assert "case_id" not in serialized
    for provider_cases in _DIAGNOSTIC_CASE_IDS.values():
        for case_ids in provider_cases.values():
            assert all(case_id not in serialized for case_id in case_ids)
    for marker in (
        "shadow-gemini-secret-marker",
        "shadow-deepseek-secret-marker",
        "No reliable analogy",
    ):
        assert marker not in serialized


def test_diagnostic_guard_stop_remains_partial_and_ineligible() -> None:
    report = run_shadow_diagnostic(
        _settings(),
        max_cost_usd=0.000001,
        provider_builder=_diagnostic_fake_builder,
    )
    assert report["execution_status"] == "guard_stopped"
    assert report["complete_profile"] is False
    assert report["decision"] == "hold"
    assert report["promotion_eligible"] is False
    assert report["totals"]["missing_profile_pairs"] > 0
    assert report["guardrails"]["cost_ledger"]["refused_calls"] == 1
    assert report["external_io"]["files_written"] is False


def test_base_exception_clears_transient_rows_and_closes_all_runtimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories: list[Any] = []
    orchestrators: list[Any] = []
    repository_base = shadow_evaluation_module.InMemoryLLMRuntimeRepository
    orchestrator_base = shadow_evaluation_module.LLMOrchestrator

    class TrackingRepository(repository_base):
        def __init__(self) -> None:
            super().__init__()
            repositories.append(self)

    class TrackingOrchestrator(orchestrator_base):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            orchestrators.append(self)

    def interrupt_after_provider_result(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        shadow_evaluation_module,
        "InMemoryLLMRuntimeRepository",
        TrackingRepository,
    )
    monkeypatch.setattr(shadow_evaluation_module, "LLMOrchestrator", TrackingOrchestrator)
    monkeypatch.setattr(
        shadow_evaluation_module,
        "score_shadow_composition_case",
        interrupt_after_provider_result,
    )

    with pytest.raises(KeyboardInterrupt):
        run_shadow_diagnostic(
            _settings(),
            provider_builder=_diagnostic_fake_builder,
        )

    assert len(repositories) == 2
    assert all(not repository.llm_runs and not repository.jobs for repository in repositories)
    assert len(orchestrators) == 2
    assert all(orchestrator._closed is True for orchestrator in orchestrators)


def test_default_cases_path_is_repository_local() -> None:
    root = Path(__file__).resolve().parents[2]
    assert DEFAULT_CASES_PATH == root / "evaluation" / "llm-shadow" / "cases.v1.json"
