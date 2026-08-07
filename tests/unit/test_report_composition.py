"""Composition through the orchestrator: routing, the whitelist boundary, telemetry, degradation.

Two orchestrators appear here on purpose. The whitelist/T2/audit tests drive the *production*
:class:`LLMOrchestrator` with a scripted provider (:class:`Harness`), so "a minted claim id
cannot become a block" is an assertion about the real contract boundary rather than a mock of
it. The telemetry, quiet-day, deterministic-render, and immutability tests drive the narrow
:class:`FakeOrchestrator`, which still returns real validated :class:`ReportComposition`
contracts.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from services.llm.contracts import validate_llm_contract_payload
from services.llm.orchestrator import LLMValidationFailure
from services.llm.policy import LLMTier
from services.reports.composition import (
    COMPOSITION_TEMPERATURE,
    COMPOSITION_TIER,
    DraftDegradationCode,
    compose_brief,
)
from services.reports.contracts import LinkedRisk, RiskProvenance, RiskRadar
from services.reports.material import FINAL_DISCLAIMER, SECTION_ORDER, SectionKind
from services.reports.prompts import COMPOSITION_PROMPT_TEMPLATE_VERSION, COMPOSITION_SCHEMA
from tests.unit._report_composition_fixtures import (
    CLAIM_ID,
    MINTED_ID,
    FakeOrchestrator,
    Harness,
    alert_change,
    brief_context,
    brief_inputs,
    claim,
    one_block,
    radar_entry,
    report_payload,
    risk_move,
    script,
    single_event_brief,
    valid_provider_response,
    words,
)

EXEC_OK = one_block(108)


def _compose(orchestrator: object, brief: tuple) -> object:
    inputs, context, material = brief
    return compose_brief(
        orchestrator,
        inputs=inputs,
        context=context,
        material=material,
        prediction_backed_outputs_enabled=True,
    )


def _section(draft: object, kind: SectionKind) -> object:
    return draft.of_kind(kind)[0]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# The real orchestrator: routing and the whitelist
# --------------------------------------------------------------------------------------


def test_every_prose_call_is_t2_reportcomposition_essential_temp0_with_a_claim_whitelist() -> None:
    harness = Harness(valid_provider_response)
    _compose(harness, single_event_brief(claims=(claim(),)))

    (top_event_request,) = [r for r in harness.requests if r.context["section_kind"] == "top_event"]
    assert top_event_request.requested_tier is COMPOSITION_TIER is LLMTier.T2
    assert top_event_request.requested_schema == COMPOSITION_SCHEMA == "ReportComposition"
    assert top_event_request.is_essential is True
    assert top_event_request.temperature == COMPOSITION_TEMPERATURE == 0.0
    # The whitelist is exactly the section's citable Claim UUID -- fail-closed, never None.
    assert top_event_request.allowed_ids == (str(CLAIM_ID),)
    # Routing inputs that cannot escalate to T3 (ADR 0008).
    assert top_event_request.risk_level == "low"
    assert top_event_request.trailing_7d_p90_hotness is None


def test_the_run_is_audited_at_t2_requesting_reportcomposition() -> None:
    harness = Harness(valid_provider_response)
    _compose(harness, single_event_brief(claims=(claim(),)))

    succeeded = [run for run in harness.repository.llm_runs if run.status == "succeeded"]
    assert succeeded
    run = succeeded[0]
    assert run.output_schema_name == COMPOSITION_SCHEMA
    assert run.model_params["tier"] == "T2"
    assert run.prompt_template_version == COMPOSITION_PROMPT_TEMPLATE_VERSION
    assert run.temperature == 0.0
    assert harness.repository.jobs[-1].job_key.startswith("daily_brief_composition:")


def test_a_minted_claim_id_is_rejected_at_the_real_contract_boundary_and_degrades_the_section() -> (
    None
):
    def respond(request: object) -> dict:
        if request.context.get("section_kind") == "top_event":  # type: ignore[attr-defined]
            return report_payload([(words(150), [str(MINTED_ID)])])
        return valid_provider_response(request)  # type: ignore[arg-type]

    harness = Harness(respond)
    draft = _compose(harness, single_event_brief(claims=(claim(),)))

    section = _section(draft, SectionKind.TOP_EVENT)
    assert not section.is_generated
    assert section.degradations[0].code is DraftDegradationCode.COMPOSITION_FAILED
    # The minted id never reaches a block on any section of the draft.
    minted = str(MINTED_ID)
    assert all(
        minted not in {str(cid) for cid in block.claim_ids}
        for s in draft.sections
        for block in s.blocks
    )
    # And it was the contract boundary that rejected it: the orchestrator logged validation failures.
    assert any(run.status == "validation_failed" for run in harness.repository.llm_runs)


def test_the_contract_boundary_rejects_a_minted_claim_id_independently() -> None:
    """The same guarantee, proven directly against the contract validator (no orchestrator)."""
    payload = report_payload([(words(10), [str(MINTED_ID)])])
    with pytest.raises(ValueError, match="must be whitelisted"):
        validate_llm_contract_payload(
            schema_name=COMPOSITION_SCHEMA, payload=payload, allowed_ids=[str(CLAIM_ID)]
        )


def test_only_claim_uuids_survive_into_the_draft_blocks() -> None:
    harness = Harness(valid_provider_response)
    draft = _compose(harness, single_event_brief(claims=(claim(),)))
    (block,) = _section(draft, SectionKind.TOP_EVENT).blocks
    assert block.claim_ids == (CLAIM_ID,)
    assert all(isinstance(cid, uuid.UUID) for cid in block.claim_ids)


# --------------------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------------------


def test_a_tier_degradation_from_t2_is_recorded_explicitly() -> None:
    fake = FakeOrchestrator([script(EXEC_OK, tier=LLMTier.T1), one_block(150)])
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    attempt = _section(draft, SectionKind.EXECUTIVE_SUMMARY).attempts[0]
    assert attempt.requested_tier == "T2"
    assert attempt.actual_tier == "T1"
    assert attempt.tier_degraded is True


def test_route_provider_cache_and_trace_telemetry_is_captured() -> None:
    run_id = uuid.uuid4()
    fake = FakeOrchestrator(
        [
            script(
                EXEC_OK,
                cache_hit=True,
                trace_id="trace-xyz",
                degraded_provider="secondary",
                route_degradation_reasons=("t3_disabled",),
                queue="nonessential",
                run_id=run_id,
            ),
            one_block(150),
        ]
    )
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    attempt = _section(draft, SectionKind.EXECUTIVE_SUMMARY).attempts[0]
    assert attempt.cache_hit is True
    assert attempt.trace_id == "trace-xyz"
    assert attempt.degraded_provider == "secondary"
    assert attempt.route_degradation_reasons == ("t3_disabled",)
    assert attempt.queue == "nonessential"
    assert attempt.mode == "realtime"
    assert attempt.llm_run_id == run_id
    assert attempt.schema_name == COMPOSITION_SCHEMA
    assert attempt.schema_version == "1.0"
    assert attempt.prompt_template_version == COMPOSITION_PROMPT_TEMPLATE_VERSION


# --------------------------------------------------------------------------------------
# Quiet day, and the sections that never call
# --------------------------------------------------------------------------------------


def test_a_quiet_day_makes_zero_llm_calls() -> None:
    inputs = brief_inputs(
        top_events=(),
        radar=RiskRadar(current=(radar_entry(45.0, "medium"),), previous=(), moves=()),
    )
    from services.reports.material import build_brief_material

    material = build_brief_material(inputs, brief_context())
    fake = FakeOrchestrator([one_block(100)])  # would be consumed only if a call were made
    draft = compose_brief(fake, inputs=inputs, context=brief_context(), material=material)

    assert fake.requests == []
    assert draft.is_quiet_day is True
    assert SectionKind.WHAT_CHANGED in draft.kinds
    assert draft.disclaimer.rendered == FINAL_DISCLAIMER
    # Risk radar keeps its table even though commentary was omitted.
    risk = _section(draft, SectionKind.RISK_RADAR)
    assert risk.degradations[0].code is DraftDegradationCode.NO_ELIGIBLE_CLAIMS
    assert risk.material.risk_rows  # the deterministic table survives


def test_forecasts_and_alerts_are_deterministic_and_make_no_call() -> None:
    fake = FakeOrchestrator([EXEC_OK, one_block(150)])
    draft = _compose(
        fake,
        single_event_brief(
            claims=(claim(),),
            alert_changes=(alert_change(is_all_clear=True),),
            forecasts_present=True,
        ),
    )
    kinds_called = {r.context["section_kind"] for r in fake.requests}
    assert "forecasts" not in kinds_called and "alerts" not in kinds_called

    forecasts = _section(draft, SectionKind.FORECASTS)
    alerts = _section(draft, SectionKind.ALERTS)
    assert forecasts.attempts == () and alerts.attempts == ()
    assert forecasts.material.forecast_rows and "base_case" in forecasts.rendered
    assert "ALL-CLEAR" in alerts.rendered  # all-clears retained and labelled


def test_closed_gate_sanitizes_contaminated_inputs_context_and_material() -> None:
    inputs, context, material = single_event_brief(
        claims=(claim(),),
        alert_changes=(alert_change(),),
        forecasts_present=True,
    )
    predictive_event = dataclasses.replace(
        inputs.top_events[0],
        max_linked_risk=LinkedRisk(
            score=99.0,
            provenance=RiskProvenance.EVENT_COMPANY,
        ),
        ranking_score=99.0,
    )
    contaminated_prior = dataclasses.replace(
        inputs,
        top_events=(predictive_event,),
        prior_brief=object(),
    )
    material = dataclasses.replace(
        material,
        sections=tuple(
            (
                dataclasses.replace(section, event=predictive_event)
                if section.kind is SectionKind.TOP_EVENT
                else section
            )
            for section in material.sections
        ),
    )
    fake = FakeOrchestrator([EXEC_OK, one_block(150)])

    draft = compose_brief(
        fake,
        inputs=contaminated_prior,
        context=context,
        material=material,
    )

    assert SectionKind.FORECASTS not in draft.kinds
    assert SectionKind.ALERTS not in draft.kinds
    assert [section.order for section in draft.sections] == list(range(1, len(draft.sections) + 1))
    prompts = "\n".join(request.prompt for request in fake.requests)
    assert "Bank-run risk" not in prompts
    assert "PRIOR_BRIEF" not in prompts
    assert "tail_risk_case" not in prompts
    assert "event_company" not in prompts
    assert '"max_linked_risk_score": 99.0' not in prompts
    assert '"risk_provenance": "none"' in prompts


# --------------------------------------------------------------------------------------
# What Changed: deterministic diff, reversals first, no invented causes
# --------------------------------------------------------------------------------------


def test_what_changed_renders_signed_moves_with_reversals_first() -> None:
    reversal = risk_move(prev_level="medium", cur_level="high", prev=50.0, cur=57.0)
    bigger_non_reversal = risk_move(prev_level="high", cur_level="high", prev=60.0, cur=90.0)
    radar = RiskRadar(
        current=(radar_entry(57.0, "high"),),
        previous=(),
        moves=(bigger_non_reversal, reversal),
    )
    inputs = brief_inputs(top_events=(), radar=radar)
    from services.reports.material import build_brief_material

    material = build_brief_material(inputs, brief_context())
    draft = compose_brief(
        FakeOrchestrator([one_block(100)]),
        inputs=inputs,
        context=brief_context(),
        material=material,
    )

    rendered = _section(draft, SectionKind.WHAT_CHANGED).rendered
    lines = rendered.splitlines()
    assert lines[0].startswith("REVERSAL --")  # the reversal leads
    assert "+30.0" in rendered  # the larger, signed, level-preserving move is still shown
    assert "rose" in rendered


def test_what_changed_states_missing_comparison_when_no_prior_data() -> None:
    inputs = brief_inputs(
        top_events=(), prior_brief=None, radar=RiskRadar(current=(), previous=(), moves=())
    )
    from services.reports.material import build_brief_material

    material = build_brief_material(inputs, brief_context())
    draft = compose_brief(
        FakeOrchestrator([one_block(100)]),
        inputs=inputs,
        context=brief_context(),
        material=material,
    )
    rendered = _section(draft, SectionKind.WHAT_CHANGED).rendered
    assert "previous day" in rendered or "day-over-day movement" in rendered


# --------------------------------------------------------------------------------------
# Order, disclaimer, immutability
# --------------------------------------------------------------------------------------


def test_draft_order_mirrors_material_with_what_changed_before_the_final_disclaimer() -> None:
    draft = _compose(
        FakeOrchestrator([EXEC_OK, one_block(150)]), single_event_brief(claims=(claim(),))
    )
    kinds = list(draft.kinds)
    ordered = [k for k in SECTION_ORDER if k in kinds]
    assert kinds == ordered  # relative order preserved
    assert kinds[-1] is SectionKind.DISCLAIMER
    assert kinds.index(SectionKind.WHAT_CHANGED) < kinds.index(SectionKind.DISCLAIMER)
    assert not any(k.value == "watchlist" for k in kinds)
    assert draft.disclaimer.rendered == FINAL_DISCLAIMER


def test_the_draft_is_immutable_and_carries_no_orm_handle() -> None:
    draft = _compose(
        FakeOrchestrator([EXEC_OK, one_block(150)]), single_event_brief(claims=(claim(),))
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        draft.sections = ()  # type: ignore[misc]
    section = _section(draft, SectionKind.TOP_EVENT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        section.blocks = ()  # type: ignore[misc]

    # Run ids are primitives, not ORM rows; nothing is marked grounded or published.
    assert all(isinstance(run_id, uuid.UUID) for run_id in draft.llm_run_ids)
    assert not hasattr(draft, "grounded") and not hasattr(draft, "published")
    assert not hasattr(section, "grounded")


def test_a_provider_failure_degrades_only_that_section() -> None:
    from services.llm.orchestrator import LLMInvocationFailure

    fake = FakeOrchestrator([EXEC_OK, LLMInvocationFailure("providers exhausted")])
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    exec_section = _section(draft, SectionKind.EXECUTIVE_SUMMARY)
    top_event = _section(draft, SectionKind.TOP_EVENT)
    assert exec_section.is_generated  # the healthy section still ships
    assert not top_event.is_generated
    assert top_event.degradations[0].code is DraftDegradationCode.COMPOSITION_FAILED
    # A provider failure is terminal for that section: no budget retry after it.
    assert len(fake.requests_for("top_event")) == 1


def test_llm_validation_failure_is_caught_and_degrades_the_section() -> None:
    fake = FakeOrchestrator([EXEC_OK, LLMValidationFailure("schema validation failed")])
    draft = _compose(fake, single_event_brief(claims=(claim(),)))
    assert _section(draft, SectionKind.TOP_EVENT).degradations[0].code is (
        DraftDegradationCode.COMPOSITION_FAILED
    )
