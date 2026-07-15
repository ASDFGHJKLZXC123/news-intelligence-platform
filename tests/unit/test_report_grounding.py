"""The grounding gate: verdicts, one regeneration, trimming, the 30% rule, fail-closed, telemetry.

Two orchestrators appear here on purpose. The contract-boundary and T1-audit tests drive the
*production* :class:`LLMOrchestrator` through :class:`Harness` with a scripted provider, so "a
grounding verdict for a non-whitelisted claim cannot pass" is an assertion about the real contract
boundary. The behavioural tests drive the narrow :class:`GateOrchestrator`, which still returns
real validated :class:`ClaimGrounding`/:class:`ReportComposition` contracts.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from services.reports.composition import compose_brief
from services.reports.contracts import RiskRadar
from services.reports.grounding import (
    GROUNDING_TEMPERATURE,
    GROUNDING_TIER,
    GateOutcome,
    SectionGroundingStatus,
    run_grounding_gate,
)
from services.reports.material import SECTION_ORDER, SectionKind
from tests.unit._report_composition_fixtures import (
    CLAIM_ID,
    CLAIM_ID_2,
    EPISODE_ID,
    MINTED_ID,
    Harness,
    brief_context,
    brief_inputs,
    claim,
    radar_entry,
    report_payload,
    single_event_brief,
    words,
)
from tests.unit._report_grounding_fixtures import (
    GateOrchestrator,
    all_supported,
    gate_provider_response,
    grounding_payload,
    make_ground,
    per_claim_blocks,
)


def _gate(orch: GateOrchestrator, brief: tuple) -> object:
    inputs, context, material = brief
    draft = compose_brief(orch, inputs=inputs, context=context, material=material)
    return draft, run_grounding_gate(orch, inputs=inputs, context=context, draft=draft)


def _section(result: object, kind: SectionKind) -> object:
    return next(s for s in result.sections if s.kind is kind)  # type: ignore[attr-defined]


def _n_claim_brief(n: int) -> tuple[tuple, tuple]:
    claims = tuple(
        claim(claim_id=uuid.uuid4(), text=f"claim number {index}") for index in range(n)
    )
    return single_event_brief(claims=claims), claims


# --------------------------------------------------------------------------------------
# The real orchestrator: T1 routing, ClaimGrounding contract, audit, whitelist boundary
# --------------------------------------------------------------------------------------


def test_grounding_runs_are_t1_reportcomposition_grounding_essential_temp0_audited() -> None:
    harness = Harness(gate_provider_response)
    inputs, context, material = single_event_brief(claims=(claim(),))
    draft = compose_brief(harness, inputs=inputs, context=context, material=material)
    result = run_grounding_gate(harness, inputs=inputs, context=context, draft=draft)

    assert result.outcome is GateOutcome.PASS
    grounding_requests = [r for r in harness.requests if r.requested_schema == "ClaimGrounding"]
    assert grounding_requests
    for request in grounding_requests:
        assert request.requested_tier is GROUNDING_TIER
        assert request.is_essential is True
        assert request.temperature == GROUNDING_TEMPERATURE == 0.0
        assert request.risk_level == "low"
        assert request.trailing_7d_p90_hotness is None

    grounding_runs = [r for r in harness.repository.llm_runs if r.output_schema_name == "ClaimGrounding"]
    assert grounding_runs
    assert all(run.model_params["tier"] == "T1" for run in grounding_runs)
    assert all(run.temperature == 0.0 for run in grounding_runs)
    assert all(run.trace_id for run in grounding_runs)  # trace-linked
    assert any(job.job_key.startswith("daily_brief_grounding:") for job in harness.repository.jobs)


def test_a_verdict_for_a_non_whitelisted_claim_is_rejected_at_the_contract_boundary_and_blocks() -> None:
    def respond(request: object) -> dict:
        if request.requested_schema == "ClaimGrounding":  # type: ignore[attr-defined]
            return grounding_payload([(str(MINTED_ID), "supported")])  # not on the block whitelist
        return gate_provider_response(request)  # type: ignore[arg-type]

    harness = Harness(respond)
    inputs, context, material = single_event_brief(claims=(claim(),))
    draft = compose_brief(harness, inputs=inputs, context=context, material=material)
    result = run_grounding_gate(harness, inputs=inputs, context=context, draft=draft)

    assert result.outcome is GateOutcome.BLOCKED
    # The contract boundary rejected the minted id: the orchestrator logged validation failures.
    assert any(run.status == "validation_failed" for run in harness.repository.llm_runs)
    top = _section(result, SectionKind.TOP_EVENT)
    assert top.status is SectionGroundingStatus.FAILED
    assert top.blocks_publication is True
    assert top.final_blocks == ()


# --------------------------------------------------------------------------------------
# The three verdicts
# --------------------------------------------------------------------------------------


def test_supported_claims_pass_without_regeneration() -> None:
    orch = GateOrchestrator(ground=all_supported)
    _, result = _gate(orch, single_event_brief(claims=(claim(),)))

    top = _section(result, SectionKind.TOP_EVENT)
    assert top.status is SectionGroundingStatus.PASSED
    assert top.regenerated is False
    assert [v.verdict for v in top.verdicts] == ["supported"]
    assert result.outcome is GateOutcome.PASS
    assert orch.regenerations_for("top_event") == []


def test_an_unverifiable_claim_passes_and_never_triggers_regeneration() -> None:
    orch = GateOrchestrator(ground=make_ground(unverifiable={str(CLAIM_ID)}))
    _, result = _gate(orch, single_event_brief(claims=(claim(),)))

    top = _section(result, SectionKind.TOP_EVENT)
    assert top.status is SectionGroundingStatus.PASSED
    assert top.regenerated is False
    assert top.claims_after == 1  # the unverifiable block is preserved
    assert [v.verdict for v in top.verdicts] == ["unverifiable"]
    # No regeneration, and only round 1 was ever grounded.
    assert orch.regenerations_for("top_event") == []
    assert {r.context["grounding_round"] for r in orch.grounding_requests_for("top_event")} == {1}


def test_an_unsupported_claim_triggers_exactly_one_regeneration_that_can_fix_it() -> None:
    # Unsupported on round 1 only: the regeneration fixes the section.
    orch = GateOrchestrator(ground=make_ground(unsupported={str(CLAIM_ID)}, only_round=1))
    _, result = _gate(orch, single_event_brief(claims=(claim(),)))

    top = _section(result, SectionKind.TOP_EVENT)
    assert top.regenerated is True
    assert top.status is SectionGroundingStatus.PASSED
    assert top.claims_after == 1  # the regenerated, now-supported block ships
    # Exactly one regeneration, and grounding ran twice (round 1, then round 2) -- never a third.
    assert len(orch.regenerations_for("top_event")) == 1
    rounds = [r.context["grounding_round"] for r in orch.grounding_requests_for("top_event")]
    assert rounds == [1, 2]
    # The regeneration carried the exact failed-claim feedback down the same path.
    regen = orch.regenerations_for("top_event")[0]
    assert regen.context["grounding_regeneration"] is True
    assert str(CLAIM_ID) in regen.prompt and "unsupported" in regen.prompt


def test_a_still_unsupported_claim_has_its_block_removed_deterministically() -> None:
    # Four claims, one unsupported on both rounds: the block is cut, 1/4 = 25% <= 30%, section kept.
    brief, claims = _n_claim_brief(4)
    inputs, context, material = brief
    offending = str(claims[0].claim_id)
    orch = GateOrchestrator(ground=make_ground(unsupported={offending}))
    _, result = _gate(orch, brief)

    top = _section(result, SectionKind.TOP_EVENT)
    assert top.status is SectionGroundingStatus.PASSED
    assert top.claims_before == 4
    assert top.claims_after == 3
    # The final result never retains a block containing the still-unsupported claim.
    surviving = {str(cid) for block in top.final_blocks for cid in block.claim_ids}
    assert offending not in surviving
    assert any("removed block" in line for line in top.transformations)


# --------------------------------------------------------------------------------------
# Claim-loss boundary: > 30% replaces, exactly 30% keeps
# --------------------------------------------------------------------------------------


def test_claim_loss_above_30pct_replaces_the_section_with_a_data_quality_note() -> None:
    brief, claims = _n_claim_brief(10)
    unsupported = {str(claims[index].claim_id) for index in range(4)}  # 4/10 = 40%
    orch = GateOrchestrator(ground=make_ground(unsupported=unsupported))
    _, result = _gate(orch, brief)

    top = _section(result, SectionKind.TOP_EVENT)
    assert top.status is SectionGroundingStatus.DATA_QUALITY_NOTE
    assert top.claims_before == 10
    assert top.final_blocks == ()
    assert "data_quality_note" in top.final_text
    # A data-quality note ships (ADR 0009): it degrades the section, it does not block the brief.
    assert top.blocks_publication is False


def test_claim_loss_at_exactly_30pct_keeps_the_trimmed_section() -> None:
    brief, claims = _n_claim_brief(10)
    unsupported = {str(claims[index].claim_id) for index in range(3)}  # 3/10 = exactly 30%
    orch = GateOrchestrator(ground=make_ground(unsupported=unsupported))
    _, result = _gate(orch, brief)

    top = _section(result, SectionKind.TOP_EVENT)
    assert top.status is SectionGroundingStatus.PASSED
    assert top.claims_before == 10
    assert top.claims_after == 7
    assert top.final_blocks  # the trimmed prose survives at exactly 30%


# --------------------------------------------------------------------------------------
# Duplicate claim handling
# --------------------------------------------------------------------------------------


def test_a_block_citing_the_same_claim_twice_is_grounded_once() -> None:
    def compose(request: object) -> dict:
        if request.context["section_kind"] == "top_event":  # type: ignore[attr-defined]
            return report_payload([(words(150), [str(CLAIM_ID), str(CLAIM_ID)])])
        return per_claim_blocks(request)  # type: ignore[arg-type]

    orch = GateOrchestrator(compose=compose, ground=all_supported)
    _, result = _gate(orch, single_event_brief(claims=(claim(),)))

    top = _section(result, SectionKind.TOP_EVENT)
    # One grounding call for that block, whose whitelist carried the claim exactly once.
    (request,) = orch.grounding_requests_for("top_event")
    assert request.context["allowed_claim_ids"] == [str(CLAIM_ID)]
    assert request.allowed_ids == (str(CLAIM_ID),)
    # Claim loss is measured over distinct cited ids: one claim, one verdict.
    assert top.claims_before == 1
    assert [v.claim_id for v in top.verdicts] == [CLAIM_ID]


# --------------------------------------------------------------------------------------
# Fail-closed: model failure, coverage mismatch, duplicate verdict
# --------------------------------------------------------------------------------------


def test_a_grounding_model_failure_fails_the_section_closed_and_blocks_the_gate() -> None:
    from services.llm.orchestrator import LLMInvocationFailure

    orch = GateOrchestrator(ground=lambda request: LLMInvocationFailure("providers exhausted"))
    _, result = _gate(orch, single_event_brief(claims=(claim(),)))

    assert result.outcome is GateOutcome.BLOCKED
    top = _section(result, SectionKind.TOP_EVENT)
    assert top.status is SectionGroundingStatus.FAILED
    assert top.blocks_publication is True
    assert top.final_blocks == ()
    assert "grounding" in top.final_text
    # A model failure is not silently passed off as a verdict; nothing was regenerated on it.
    assert top.regenerated is False


def test_incomplete_verdict_coverage_fails_closed() -> None:
    def compose(request: object) -> dict:
        if request.context["section_kind"] == "top_event":  # type: ignore[attr-defined]
            return report_payload([(words(150), [str(CLAIM_ID), str(CLAIM_ID_2)])])
        return per_claim_blocks(request)  # type: ignore[arg-type]

    def ground(request: object) -> dict:
        allowed = request.context["allowed_claim_ids"]  # type: ignore[attr-defined]
        if request.context["section_kind"] == "top_event":  # type: ignore[attr-defined]
            # Only one of the block's two cited claims is verdicted -- incomplete coverage.
            return grounding_payload([(str(CLAIM_ID), "supported")])
        return grounding_payload([(cid, "supported") for cid in allowed])

    orch = GateOrchestrator(compose=compose, ground=ground)
    two = (claim(), claim(claim_id=CLAIM_ID_2, text="second claim"))
    _, result = _gate(orch, single_event_brief(claims=two))

    assert result.outcome is GateOutcome.BLOCKED
    assert _section(result, SectionKind.TOP_EVENT).status is SectionGroundingStatus.FAILED


def test_a_duplicate_verdict_for_one_claim_fails_closed() -> None:
    orch = GateOrchestrator(
        ground=lambda request: grounding_payload([(str(CLAIM_ID), "supported"), (str(CLAIM_ID), "unsupported")])
    )
    _, result = _gate(orch, single_event_brief(claims=(claim(),)))

    assert result.outcome is GateOutcome.BLOCKED
    assert _section(result, SectionKind.TOP_EVENT).status is SectionGroundingStatus.FAILED


def test_a_copyright_violation_blocks_the_gate_and_is_not_trimmed() -> None:
    excerpt = " ".join(f"copied{index}" for index in range(1, 21))  # a 20-word source snippet
    copied16 = " ".join(f"copied{index}" for index in range(1, 17))  # 16 verbatim words

    def compose(request: object) -> dict:
        if request.context["section_kind"] == "top_event":  # type: ignore[attr-defined]
            return report_payload([(f"{copied16} {words(140)}", [str(CLAIM_ID)])])
        return per_claim_blocks(request)  # type: ignore[arg-type]

    orch = GateOrchestrator(compose=compose, ground=all_supported)
    _, result = _gate(orch, single_event_brief(claims=(claim(excerpt_text=excerpt),)))

    top = _section(result, SectionKind.TOP_EVENT)
    assert top.status is SectionGroundingStatus.FAILED
    assert top.copyright_findings and top.copyright_findings[0].word_count == 16
    assert result.outcome is GateOutcome.BLOCKED  # a copyright failure blocks, never trims silently


# --------------------------------------------------------------------------------------
# Quiet / deterministic / historical: no grounding calls where none are due
# --------------------------------------------------------------------------------------


def test_a_quiet_day_makes_zero_grounding_calls() -> None:
    inputs = brief_inputs(
        top_events=(),
        radar=RiskRadar(current=(radar_entry(45.0, "medium"),), previous=(), moves=()),
    )
    from services.reports.material import build_brief_material

    material = build_brief_material(inputs, brief_context())
    orch = GateOrchestrator()
    draft = compose_brief(orch, inputs=inputs, context=brief_context(), material=material)
    result = run_grounding_gate(orch, inputs=inputs, context=brief_context(), draft=draft)

    assert orch.grounding_requests == []
    assert result.outcome is GateOutcome.PASS
    # The deterministic what-changed and disclaimer pass; the claimless radar ships a note.
    assert _section(result, SectionKind.WHAT_CHANGED).status is SectionGroundingStatus.PASSED
    assert _section(result, SectionKind.DISCLAIMER).status is SectionGroundingStatus.PASSED
    assert _section(result, SectionKind.RISK_RADAR).status is SectionGroundingStatus.DATA_QUALITY_NOTE


def test_deterministic_sections_are_not_grounded_and_the_disclaimer_is_untouched_and_last() -> None:
    from services.reports.material import FINAL_DISCLAIMER

    orch = GateOrchestrator()
    _, result = _gate(orch, single_event_brief(claims=(claim(),), forecasts_present=True))

    grounded_kinds = {r.context["section_kind"] for r in orch.grounding_requests}
    assert "forecasts" not in grounded_kinds and "what_changed" not in grounded_kinds
    disclaimer = result.sections[-1]
    assert disclaimer.kind is SectionKind.DISCLAIMER
    assert disclaimer.final_text == FINAL_DISCLAIMER
    assert disclaimer.grounding_attempts == ()


def test_historical_episode_ids_are_never_grounded_as_claim_ids() -> None:
    orch = GateOrchestrator()
    _, result = _gate(orch, single_event_brief(claims=(claim(),), analogies_present=True))

    # The parallels section is grounded, but only on claim ids -- never the episode id.
    assert orch.grounding_requests_for("historical_parallels")
    all_grounded_ids = {cid for r in orch.grounding_requests for cid in r.context["allowed_claim_ids"]}
    assert str(EPISODE_ID) not in all_grounded_ids
    assert str(CLAIM_ID) in all_grounded_ids


# --------------------------------------------------------------------------------------
# Order, immutability, aggregate state
# --------------------------------------------------------------------------------------


def test_gate_result_sections_are_in_original_order_and_immutable() -> None:
    orch = GateOrchestrator()
    draft, result = _gate(orch, single_event_brief(claims=(claim(),)))

    kinds = [s.kind for s in result.sections]
    assert kinds == [s.kind for s in draft.sections]  # original order preserved
    ordered = [k for k in SECTION_ORDER if k in set(kinds)]
    assert kinds == ordered
    assert kinds[-1] is SectionKind.DISCLAIMER

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.sections = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.sections[0].status = SectionGroundingStatus.FAILED  # type: ignore[misc]
    # The gate mutates neither the draft nor its sections.
    assert all(hasattr(s, "material") for s in draft.sections)


def test_the_gate_does_not_mutate_the_input_draft() -> None:
    orch = GateOrchestrator(ground=make_ground(unsupported={str(CLAIM_ID)}))
    inputs, context, material = single_event_brief(claims=(claim(),))
    draft = compose_brief(orch, inputs=inputs, context=context, material=material)
    before = tuple(section.text for section in draft.sections)
    run_grounding_gate(orch, inputs=inputs, context=context, draft=draft)
    after = tuple(section.text for section in draft.sections)
    assert before == after


# --------------------------------------------------------------------------------------
# The strict grounding contract and its whitelist (direct), and attempt telemetry
# --------------------------------------------------------------------------------------


def test_the_grounding_contract_is_strict_versioned_and_whitelisted() -> None:
    from services.llm.contracts import (
        LLMContractConfigurationError,
        validate_llm_contract_payload,
    )

    # All three verdicts validate against a whitelisted claim id.
    for verdict in ("supported", "unsupported", "unverifiable"):
        contract = validate_llm_contract_payload(
            schema_name="ClaimGrounding",
            payload=grounding_payload([(str(CLAIM_ID), verdict)]),
            allowed_ids=[str(CLAIM_ID)],
        )
        assert contract.verdicts[0].verdict.value == verdict  # type: ignore[attr-defined]

    # A minted claim id is rejected at the contract boundary.
    with pytest.raises(ValueError, match="must be whitelisted"):
        validate_llm_contract_payload(
            schema_name="ClaimGrounding",
            payload=grounding_payload([(str(MINTED_ID), "supported")]),
            allowed_ids=[str(CLAIM_ID)],
        )

    # A missing whitelist is fail-closed configuration error, even for a well-formed payload.
    with pytest.raises(LLMContractConfigurationError):
        validate_llm_contract_payload(
            schema_name="ClaimGrounding",
            payload=grounding_payload([(str(CLAIM_ID), "supported")]),
            allowed_ids=None,
        )

    # The envelope is required and never defaulted.
    missing_envelope = grounding_payload([(str(CLAIM_ID), "supported")])
    del missing_envelope["prompt_template_version"]
    with pytest.raises(ValueError):
        validate_llm_contract_payload(
            schema_name="ClaimGrounding", payload=missing_envelope, allowed_ids=[str(CLAIM_ID)]
        )


def test_a_grounding_attempt_retains_primitive_telemetry() -> None:
    orch = GateOrchestrator()
    _, result = _gate(orch, single_event_brief(claims=(claim(),)))

    top = _section(result, SectionKind.TOP_EVENT)
    (attempt,) = top.grounding_attempts
    assert attempt.requested_tier == "T1"
    assert attempt.actual_tier == "T1"
    assert attempt.tier_degraded is False
    assert attempt.schema_name == "ClaimGrounding"
    assert attempt.schema_version == "1.0"
    assert attempt.mode == "realtime"
    assert attempt.queue == "essential"
    assert attempt.cache_hit is False
    assert attempt.degraded_provider is None
    assert attempt.route_degradation_reasons == ()
    assert attempt.trace_id
    assert isinstance(attempt.llm_run_id, uuid.UUID)
    assert attempt.llm_run_id in result.llm_run_ids
