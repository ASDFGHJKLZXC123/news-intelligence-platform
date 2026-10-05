"""Source-only personal composition uses the shared audited composer and grounding gate."""

from dataclasses import FrozenInstanceError, replace

import pytest

from services.reports.composition import DraftDegradationCode, compose_brief
from services.reports.contracts import CompositionPolicy
from services.reports.grounding import GateOutcome, run_grounding_gate
from services.reports.material import SectionKind
from services.reports.prompts import PERSONAL_COMPOSITION_PROMPT_VERSION
from tests.unit._report_composition_fixtures import (
    CLAIM_ID,
    MINTED_ID,
    Harness,
    claim,
    report_payload,
    single_event_brief,
    words,
)
from tests.unit._report_grounding_fixtures import GateOrchestrator, make_ground

SHORT_DESCRIPTION = "The publisher reports that the central bank left borrowing costs unchanged."


def _personal_inputs():
    inputs, context, material = single_event_brief(claims=(claim(),))
    return (
        replace(inputs, composition_policy=CompositionPolicy.PERSONAL_DESCRIPTIVE),
        context,
        material,
    )


def _short_response(request):
    return report_payload([(SHORT_DESCRIPTION, [request.allowed_ids[0]])])


def _compose(orchestrator, brief):
    inputs, context, material = brief
    return compose_brief(orchestrator, inputs=inputs, context=context, material=material)


def test_personal_brevity_uses_only_source_inputs_and_versioned_audit_identity() -> None:
    brief = _personal_inputs()
    orchestrator = GateOrchestrator(compose=_short_response)
    draft = _compose(orchestrator, brief)
    assert len(orchestrator.composition_requests) == 2
    for request, section in zip(orchestrator.composition_requests, draft.sections[:2], strict=True):
        assert section.blocks[0].text == SHORT_DESCRIPTION
        assert section.budget.within_budget
        assert request.prompt_name.startswith("personal_brief_")
        assert request.prompt_version == PERSONAL_COMPOSITION_PROMPT_VERSION
        assert request.context["composition_policy"] == CompositionPolicy.PERSONAL_DESCRIPTIVE
        assert request.context["word_budget_minimum"] == 1
        assert "no required minimum length" in request.prompt
        assert "not evidence of stability" in request.prompt
        for legacy_input in (
            "US market",
            "investor",
            "critical_high_alert",
            "largest_risk_move",
            "risk_provenance",
            "max_linked_risk_score",
            "hotness_score",
        ):
            assert legacy_input not in request.prompt
    with pytest.raises(FrozenInstanceError):
        brief[0].composition_policy = CompositionPolicy.LEGACY


def test_legacy_default_keeps_its_prompt_and_minimum_budgets() -> None:
    brief = single_event_brief(claims=(claim(),))
    assert brief[0].composition_policy is CompositionPolicy.LEGACY
    orchestrator = GateOrchestrator(compose=_short_response)
    draft = _compose(orchestrator, brief)
    for kind, minimum, maximum in (
        (SectionKind.EXECUTIVE_SUMMARY, 96, 120),
        (SectionKind.TOP_EVENT, 120, 180),
    ):
        section = draft.of_kind(kind)[0]
        assert section.degradations[0].code is DraftDegradationCode.BUDGET_UNMET
        assert (section.budget.minimum, section.budget.maximum) == (minimum, maximum)
        assert len(section.attempts) == 2
    request = orchestrator.composition_requests[0]
    assert "before the US market opens" in request.prompt
    assert request.prompt_name == "daily_brief_executive_summary"
    assert request.prompt_version == "v1"
    assert "composition_policy" not in request.context


@pytest.mark.parametrize("kind,maximum", [("executive_summary", 120), ("top_event", 180)])
@pytest.mark.parametrize("excess", [0, 1])
def test_personal_hard_ceiling_remains_inclusive_and_one_retry_only(kind, maximum, excess) -> None:
    def response(request):
        if request.context["section_kind"] == kind:
            return report_payload([(words(maximum + excess), [request.allowed_ids[0]])])
        return _short_response(request)

    orchestrator = GateOrchestrator(compose=response)
    draft = _compose(orchestrator, _personal_inputs())
    section = draft.of_kind(SectionKind(kind))[0]
    assert section.budget.within_budget is (excess == 0)
    assert len(section.attempts) == (1 if excess == 0 else 2)
    if excess:
        assert not section.blocks
        assert section.degradations[0].code is DraftDegradationCode.BUDGET_UNMET
        retry = orchestrator.composition_requests_for(kind)[-1]
        assert "no minimum length" in retry.prompt


def test_personal_abstention_is_still_empty_and_degrades_after_one_retry() -> None:
    orchestrator = GateOrchestrator(
        compose=lambda _: report_payload([], no_finding_reason="No supported description.")
    )
    draft = _compose(orchestrator, _personal_inputs())
    for kind in (SectionKind.EXECUTIVE_SUMMARY, SectionKind.TOP_EVENT):
        section = draft.of_kind(kind)[0]
        assert section.degradations[0].code is DraftDegradationCode.BUDGET_UNMET
        assert [attempt.word_count for attempt in section.attempts] == [0, 0]
        assert not section.blocks


def test_personal_no_eligible_claims_makes_no_model_call() -> None:
    inputs, context, material = single_event_brief(claims=())
    inputs = replace(inputs, composition_policy=CompositionPolicy.PERSONAL_DESCRIPTIVE)
    orchestrator = GateOrchestrator(compose=_short_response)
    draft = _compose(orchestrator, (inputs, context, material))
    assert not orchestrator.requests
    assert draft.of_kind(SectionKind.TOP_EVENT)[0].degradations[0].code is (
        DraftDegradationCode.NO_ELIGIBLE_CLAIMS
    )


def test_personal_policy_survives_grounding_regeneration_with_the_same_claim_whitelist() -> None:
    inputs, context, material = _personal_inputs()
    orchestrator = GateOrchestrator(
        compose=_short_response,
        ground=make_ground(unsupported={str(CLAIM_ID)}, only_round=1),
    )
    draft = _compose(orchestrator, (inputs, context, material))
    gate = run_grounding_gate(orchestrator, inputs=inputs, context=context, draft=draft)
    assert gate.outcome is GateOutcome.PASS
    for kind in ("executive_summary", "top_event"):
        (regeneration,) = orchestrator.regenerations_for(kind)
        assert regeneration.context["composition_policy"] == CompositionPolicy.PERSONAL_DESCRIPTIVE
        assert regeneration.prompt_name.startswith("personal_brief_")
        assert regeneration.prompt_version == PERSONAL_COMPOSITION_PROMPT_VERSION
        assert regeneration.allowed_ids == (str(CLAIM_ID),)
        assert "unsupported" in regeneration.prompt
        assert "US market" not in regeneration.prompt
        assert "no required minimum length" in regeneration.prompt


def test_persistently_unsupported_personal_prose_is_withheld_after_regeneration() -> None:
    inputs, context, material = _personal_inputs()
    unsupported_text = "The policy decision will definitely double economic growth next year."
    orchestrator = GateOrchestrator(
        compose=lambda request: report_payload([(unsupported_text, [request.allowed_ids[0]])]),
        ground=make_ground(unsupported={str(CLAIM_ID)}),
    )
    draft = _compose(orchestrator, (inputs, context, material))
    gate = run_grounding_gate(orchestrator, inputs=inputs, context=context, draft=draft)
    for section in gate.sections[:2]:
        assert section.regenerated
        assert section.claims_before == 1 and section.claims_after == 0
        assert not section.final_blocks
        assert unsupported_text not in section.final_text
        assert "section prose withheld" in section.final_text
    assert len(orchestrator.composition_requests) == 4


@pytest.mark.parametrize("invalid", ["minted_claim", "whitespace"])
def test_personal_prose_still_passes_the_real_schema_and_citation_boundary(invalid) -> None:
    def response(request):
        if invalid == "minted_claim":
            return report_payload([(SHORT_DESCRIPTION, [str(MINTED_ID)])])
        return report_payload([(" \t\n ", [request.context["allowed_claim_ids"][0]])])

    orchestrator = Harness(response)
    draft = _compose(orchestrator, _personal_inputs())
    assert all(not section.blocks for section in draft.sections)
    if invalid == "minted_claim":
        assert any(run.status == "validation_failed" for run in orchestrator.repository.llm_runs)
    else:
        # Whitespace is structurally a string; the shared deterministic counter rejects it.
        for kind in (SectionKind.EXECUTIVE_SUMMARY, SectionKind.TOP_EVENT):
            section = draft.of_kind(kind)[0]
            assert section.degradations[0].code is DraftDegradationCode.BUDGET_UNMET
            assert [attempt.word_count for attempt in section.attempts] == [0, 0]


def test_unknown_policy_is_rejected_before_model_calls() -> None:
    inputs, context, material = _personal_inputs()
    orchestrator = GateOrchestrator(compose=_short_response)
    with pytest.raises(ValueError, match="CompositionPolicy"):
        _compose(
            orchestrator, (replace(inputs, composition_policy="unknown.v2"), context, material)
        )
    assert not orchestrator.requests
