"""Claim-backed daily-brief composition, and its section-budget enforcement.

The composition half of Stage 6. Given the accepted deterministic inputs -- the selected
:class:`~services.reports.contracts.BriefInputs`, the loaded
:class:`~services.reports.context.BriefContext`, and the laid-out
:class:`~services.reports.material.BriefMaterial` -- this module writes the brief's prose and
returns an immutable :class:`BriefDraft` for the grounding/persistence item that follows.

Four disciplines are enforced by the code here rather than left to the caller:

* **Every prose call goes through the real orchestrator, requesting :class:`ReportComposition`.**
  T2, essential, temperature 0, with a fail-closed ``allowed_ids`` whitelist that is exactly the
  section's citable Claim UUIDs. No provider is ever called directly and no validation bypassed,
  so a minted claim id, an episode id, an article id, or a risk key can never become a
  ``claim_id`` in the draft.
* **The draft holds no ORM object and no session.** Every LLM run id and every telemetry field is
  copied to a primitive; each section keeps its structured :class:`SectionMaterial` alongside the
  prose, so persistence and export never have to parse a sentence back into data.
* **Budgets are enforced deterministically, with exactly one retry.** The word count of a
  section's combined blocks is measured by :func:`count_words`; a first violation earns one new
  audited invocation with concise feedback, and a second violation degrades the section rather
  than trimming its prose or looping again.
* **A failure degrades one section, never the brief.** A provider or orchestrator error, a
  persistent budget miss, a persistent abstention, or a section with no citable claim all replace
  that one section's prose with an explicit note; the quiet, degraded, or partial brief still
  ships. Nothing here marks anything grounded or published -- that is the next item's job.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from db.models.core import Job
from services.llm.contracts import ReportComposition
from services.llm.orchestrator import LLMOrchestratorError, LLMOrchestratorRequest
from services.llm.policy import LLMTier
from services.reports.context import BriefContext, ClaimContext
from services.reports.contracts import BriefInputs, RiskKey
from services.reports.material import BriefMaterial, SectionKind, SectionMaterial
from services.reports.prompts import (
    COMPOSITION_PROMPT_TEMPLATE_VERSION,
    COMPOSITION_PROMPT_VERSION,
    COMPOSITION_SCHEMA,
    EXECUTIVE_SUMMARY_PROMPT_NAME,
    HISTORICAL_PARALLELS_PROMPT_NAME,
    RISK_COMMENTARY_PROMPT_NAME,
    TOP_EVENT_PROMPT_NAME,
    budget_feedback,
    build_executive_summary_prompt,
    build_historical_parallels_prompt,
    build_risk_commentary_prompt,
    build_top_event_prompt,
)

#: Composition is a reasoning task, not an extraction: T2 (ADR 0008). Essential and deterministic,
#: with routing inputs that cannot escalate past it -- T3 is reached only by a high/critical risk
#: level or an event at/above the trailing P90 hotness, and neither is an input to a brief.
COMPOSITION_TIER: Final = LLMTier.T2
COMPOSITION_RISK_LEVEL: Final = "low"
#: Temperature 0: the same brief composes the same way twice, and the orchestrator's prompt cache
#: is a legitimate replay rather than a coin flip that happened to land the same way.
COMPOSITION_TEMPERATURE: Final[float] = 0.0
COMPOSITION_JOB_TYPE: Final = "daily_brief_composition"

_JOB_NAMESPACE: Final = uuid.UUID("b3d1f0a2-6c4e-4b7a-9f2d-1e5c8a0b7d64")


# --------------------------------------------------------------------------------------
# Word-budget policy (pure)
# --------------------------------------------------------------------------------------


def count_words(text: str) -> int:
    """The one deterministic word counter, applied to the combined text of a section's blocks.

    A word is a maximal run of non-whitespace characters, separated by any Unicode whitespace --
    exactly :meth:`str.split`'s definition. An empty or whitespace-only string is zero words, so a
    :class:`ReportComposition` abstention (empty blocks) counts as zero and fails every non-zero
    minimum, which is what routes it into the one budget retry and then a degrade. The count is a
    function of the text alone: no locale, no clock, no tokenizer, so two runs agree.
    """

    return len(text.split())


@dataclass(frozen=True)
class WordBudget:
    """One section's accepted word range, and its regeneration target."""

    target: int
    minimum: int
    maximum: int

    def contains(self, count: int) -> bool:
        return self.minimum <= count <= self.maximum


#: Section budgets (report-generation spec). The accepted range is the target +/-20%, *except*
#: the executive summary, whose spec hard cap of 120 words overrides the +20% ceiling: 96..120,
#: not 96..144. The others are the plain +/-20% band around their target.
EXECUTIVE_SUMMARY_BUDGET: Final = WordBudget(target=120, minimum=96, maximum=120)
TOP_EVENT_BUDGET: Final = WordBudget(target=150, minimum=120, maximum=180)
RISK_COMMENTARY_BUDGET: Final = WordBudget(target=60, minimum=48, maximum=72)
HISTORICAL_PARALLELS_BUDGET: Final = WordBudget(target=100, minimum=80, maximum=120)


# --------------------------------------------------------------------------------------
# Immutable draft contracts
# --------------------------------------------------------------------------------------


class DraftDegradationCode(StrEnum):
    """Why a prose section could not be composed as intended. Always declared, never hidden."""

    #: The section had no citable Claim UUID, so no grounded block was possible and no call made.
    NO_ELIGIBLE_CLAIMS = "no_eligible_claims"
    #: The orchestrator or provider failed (exhausted, transport error, or a payload that failed
    #: contract validation twice -- including a minted claim id the whitelist rejected).
    COMPOSITION_FAILED = "composition_failed"
    #: The section's prose stayed outside its word range after the one budget-feedback retry.
    BUDGET_UNMET = "budget_unmet"


@dataclass(frozen=True)
class SectionDegradation:
    """One declared degradation of a section's prose."""

    code: DraftDegradationCode
    detail: str


@dataclass(frozen=True)
class DraftBlock:
    """One claim-tagged prose block, with its claim ids resolved to UUIDs.

    The ids are UUIDs, not strings: they were validated against the section whitelist by the real
    contract and re-checked here, so a value that reaches this type is an existing Claim UUID and
    nothing else -- never a minted id, an episode id, an article id, a risk key, or a string.
    """

    text: str
    claim_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class CompositionAttempt:
    """Telemetry for one successful composition invocation, copied entirely to primitives.

    Held per attempt, so both a first violation and its retry keep a full record. No ORM handle
    lives here: ``llm_run_id`` is the run's UUID (or ``None`` before it is flushed), never the row.
    """

    attempt: int
    prompt_name: str
    prompt_version: str
    prompt_template_version: str | None
    schema_name: str
    schema_version: str
    requested_tier: str
    actual_tier: str
    #: Recorded explicitly: the actual tier came back below the requested T2.
    tier_degraded: bool
    route_degradation_reasons: tuple[str, ...]
    degraded_provider: str | None
    mode: str
    queue: str
    cache_hit: bool
    trace_id: str
    llm_run_id: uuid.UUID | None
    word_count: int
    within_budget: bool


@dataclass(frozen=True)
class BudgetOutcome:
    """The budget verdict retained on a composed or degraded section."""

    target: int
    minimum: int
    maximum: int
    word_count: int
    within_budget: bool


@dataclass(frozen=True)
class DraftSection:
    """One section of the draft: its structured source, its prose, and how the prose came to be.

    ``material`` is the original :class:`SectionMaterial`, retained verbatim so persistence and
    export read structured rows rather than parse prose. ``blocks`` are the generated, claim-tagged
    prose blocks in returned order (empty for deterministic or degraded sections); ``rendered`` is
    the deterministic text for table/list/diff/note/disclaimer sections and for a degraded note.
    """

    kind: SectionKind
    order: int
    title: str
    material: SectionMaterial
    blocks: tuple[DraftBlock, ...] = ()
    rendered: str = ""
    attempts: tuple[CompositionAttempt, ...] = ()
    degradations: tuple[SectionDegradation, ...] = ()
    budget: BudgetOutcome | None = None

    @property
    def is_generated(self) -> bool:
        """The section carries model-written, claim-tagged prose."""
        return bool(self.blocks)

    @property
    def is_degraded(self) -> bool:
        return bool(self.degradations)

    @property
    def text(self) -> str:
        """Human-readable text: the blocks for a generated section, else the deterministic render."""
        if self.blocks:
            return "\n\n".join(block.text for block in self.blocks)
        return self.rendered


@dataclass(frozen=True)
class BriefDraft:
    """The whole brief, composed, immutable, and holding no ORM object or session.

    Not grounded and not published: this is the draft the grounding gate and the persistence item
    consume next. The section order mirrors :class:`BriefMaterial`, so What Changed stays before
    the final disclaimer and there is no Watchlist.
    """

    brief_date: datetime.date
    is_quiet_day: bool
    sections: tuple[DraftSection, ...]

    @property
    def kinds(self) -> tuple[SectionKind, ...]:
        return tuple(section.kind for section in self.sections)

    def of_kind(self, kind: SectionKind) -> tuple[DraftSection, ...]:
        return tuple(section for section in self.sections if section.kind is kind)

    @property
    def disclaimer(self) -> DraftSection:
        """Always the last section; its prose is the fixed template, never composed."""
        return self.sections[-1]

    @property
    def generated_sections(self) -> tuple[DraftSection, ...]:
        return tuple(section for section in self.sections if section.is_generated)

    @property
    def degraded_sections(self) -> tuple[DraftSection, ...]:
        return tuple(section for section in self.sections if section.is_degraded)

    @property
    def llm_run_ids(self) -> tuple[uuid.UUID, ...]:
        """Every LLM run id behind the draft, in section then attempt order. Primitives only."""
        return tuple(
            attempt.llm_run_id
            for section in self.sections
            for attempt in section.attempts
            if attempt.llm_run_id is not None
        )


class ReportCompositionError(RuntimeError):
    """A composition response that cannot be used: wrong schema, or a claim id off the whitelist."""


class _Orchestrator(Protocol):
    """The seam the composer depends on, so tests inject the real runtime or a narrow fake."""

    def run(self, request: LLMOrchestratorRequest) -> Any: ...


# --------------------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------------------


def composition_job(brief_date: datetime.date) -> Job:
    """The one job row every section call of a brief runs under: stable per brief_date.

    The id is derived from the key, so the orchestrator's ``merge`` updates the same row across
    the brief's several section invocations rather than inserting a job per call that would
    collide on the unique ``job_key``. This is the orchestrator's job, not a Report row -- no
    Report is persisted here.
    """

    job_key = f"{COMPOSITION_JOB_TYPE}:{brief_date.isoformat()}"
    now = datetime.datetime.now(datetime.UTC)
    return Job(
        id=uuid.uuid5(_JOB_NAMESPACE, job_key),
        job_key=job_key,
        job_type=COMPOSITION_JOB_TYPE,
        state="queued",
        attempt=1,
        max_attempts=3,
        related_ids={"brief_date": brief_date.isoformat()},
        error=None,
        safe_to_rerun=True,
        created_at=now,
        updated_at=now,
    )


# --------------------------------------------------------------------------------------
# Claim helpers
# --------------------------------------------------------------------------------------


def _claims_for_event(context: BriefContext, event_id: uuid.UUID) -> tuple[ClaimContext, ...]:
    evidence = context.evidence_for(event_id)
    return evidence.claims if evidence else ()


def _dedupe_claims(groups: Iterable[Sequence[ClaimContext]]) -> tuple[ClaimContext, ...]:
    """Union claims across events, de-duplicated by id, first occurrence kept, order stable."""
    seen: dict[uuid.UUID, ClaimContext] = {}
    for group in groups:
        for claim in group:
            seen.setdefault(claim.claim_id, claim)
    return tuple(seen.values())


def _map_blocks(contract: ReportComposition, allowed: set[uuid.UUID]) -> tuple[DraftBlock, ...]:
    """Convert validated blocks to UUID-tagged draft blocks, preserving order, re-checking ids.

    The contract already rejected any claim id outside the injected whitelist; re-checking against
    the set *this module* built is what makes the guarantee independent of the validator, exactly
    as the analogy reranker re-checks its episode ids. Only a Claim UUID survives.
    """

    blocks: list[DraftBlock] = []
    for block in contract.blocks:
        try:
            claim_ids = tuple(uuid.UUID(str(value)) for value in block.claim_ids)
        except (ValueError, AttributeError) as exc:
            raise ReportCompositionError(f"block cited a non-uuid claim id: {exc}") from exc
        for claim_id in claim_ids:
            if claim_id not in allowed:
                raise ReportCompositionError(
                    f"block cited a claim id outside the section whitelist: {claim_id}"
                )
        blocks.append(DraftBlock(text=block.text, claim_ids=claim_ids))
    return tuple(blocks)


# --------------------------------------------------------------------------------------
# One composition attempt
# --------------------------------------------------------------------------------------


def _telemetry(
    outcome: Any, *, attempt: int, word_count: int, within_budget: bool
) -> CompositionAttempt:
    run = outcome.run
    contract = outcome.contract
    return CompositionAttempt(
        attempt=attempt,
        prompt_name=run.prompt_name,
        prompt_version=run.prompt_version,
        prompt_template_version=run.prompt_template_version,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        requested_tier=COMPOSITION_TIER.value,
        actual_tier=outcome.tier.value,
        tier_degraded=outcome.tier is not COMPOSITION_TIER,
        route_degradation_reasons=tuple(outcome.route_degradation_reasons),
        degraded_provider=outcome.degraded_provider,
        mode=outcome.mode.value,
        queue=outcome.queue,
        cache_hit=outcome.cache_hit,
        trace_id=outcome.trace_id,
        llm_run_id=getattr(run, "id", None),
        word_count=word_count,
        within_budget=within_budget,
    )


def _run_attempt(
    orchestrator: _Orchestrator,
    *,
    brief_date: datetime.date,
    prompt_name: str,
    prompt: str,
    allowed_ids: tuple[str, ...],
    allowed_uuids: set[uuid.UUID],
    section_kind: SectionKind,
    attempt: int,
    budget: WordBudget,
    regeneration: bool = False,
) -> tuple[tuple[DraftBlock, ...], CompositionAttempt]:
    """Run one composition invocation through the orchestrator and measure its budget.

    ``regeneration`` is recorded in the run context so a grounding-driven regeneration is
    queryable in ``llm_runs`` separately from the first composition of the same section.
    """

    request = LLMOrchestratorRequest(
        job=composition_job(brief_date),
        prompt_name=prompt_name,
        prompt_version=COMPOSITION_PROMPT_VERSION,
        prompt_template_version=COMPOSITION_PROMPT_TEMPLATE_VERSION,
        requested_schema=COMPOSITION_SCHEMA,
        prompt=prompt,
        requested_tier=COMPOSITION_TIER,
        risk_level=COMPOSITION_RISK_LEVEL,
        current_event_hotness=0.0,
        trailing_7d_p90_hotness=None,
        # The caller blocks on composition, so it is a realtime invocation: routing it as a batch
        # would only make the orchestrator degrade it back to realtime and say so.
        is_realtime=True,
        is_essential=True,
        # The claims are already in the prompt; the article selector must not append a second,
        # differently-shaped copy of anything.
        articles=(),
        # Exactly the section's citable Claim UUIDs, so the contract's whitelist fails closed.
        allowed_ids=allowed_ids,
        temperature=COMPOSITION_TEMPERATURE,
        context={
            "brief_date": brief_date.isoformat(),
            "section_kind": section_kind.value,
            "allowed_claim_ids": list(allowed_ids),
            "budget_attempt": attempt,
            "grounding_regeneration": regeneration,
        },
    )
    outcome = orchestrator.run(request)
    contract = outcome.contract
    if not isinstance(contract, ReportComposition):  # defensive: wrong schema came back
        raise ReportCompositionError(
            f"composition returned a {type(contract).__name__}, not a {COMPOSITION_SCHEMA}"
        )
    blocks = _map_blocks(contract, allowed_uuids)
    word_count = count_words(" ".join(block.text for block in blocks))
    telemetry = _telemetry(
        outcome, attempt=attempt, word_count=word_count, within_budget=budget.contains(word_count)
    )
    return blocks, telemetry


# --------------------------------------------------------------------------------------
# Section drivers
# --------------------------------------------------------------------------------------


def _generated_section(
    material: SectionMaterial,
    blocks: tuple[DraftBlock, ...],
    attempts: tuple[CompositionAttempt, ...],
    budget: BudgetOutcome,
) -> DraftSection:
    return DraftSection(
        kind=material.kind,
        order=material.order,
        title=material.title,
        material=material,
        blocks=blocks,
        attempts=attempts,
        budget=budget,
    )


def _degraded_section(
    material: SectionMaterial,
    *,
    code: DraftDegradationCode,
    detail: str,
    attempts: tuple[CompositionAttempt, ...] = (),
    budget: BudgetOutcome | None = None,
) -> DraftSection:
    return DraftSection(
        kind=material.kind,
        order=material.order,
        title=material.title,
        material=material,
        blocks=(),
        rendered=f"[{code.value}] {detail}",
        attempts=attempts,
        degradations=(SectionDegradation(code=code, detail=detail),),
        budget=budget,
    )


def _no_claims_detail(kind: SectionKind) -> str:
    if kind is SectionKind.RISK_RADAR:
        return (
            "No claim on record can ground risk-radar commentary; the risk table is retained and "
            "commentary is omitted."
        )
    if kind is SectionKind.HISTORICAL_PARALLELS:
        return (
            "No current-event claim is on record to ground the historical parallels; prose omitted."
        )
    if kind is SectionKind.EXECUTIVE_SUMMARY:
        return "No citable event claim is on record for the executive summary; prose omitted."
    return "No supportive article-linked claim is on record for this section; prose omitted."


def _compose_generated(
    orchestrator: _Orchestrator,
    *,
    brief_date: datetime.date,
    material: SectionMaterial,
    claims: Sequence[ClaimContext],
    budget: WordBudget,
    prompt_name: str,
    build_prompt: Any,
    feedback: str | None = None,
) -> DraftSection:
    """Compose one prose section: fail-closed whitelist, one budget retry, then degrade.

    No citable claim means no grounded block is possible, so no call is made at all -- the section
    degrades with an explicit note (and, for the risk radar, its table survives via ``material``).

    ``feedback`` (the grounding gate's exact failed-claim/verdict note) is appended to the section's
    base prompt before the first attempt, so a regeneration reuses this exact path -- same budget,
    same fail-closed whitelist, its own separately-audited runs -- and the appended note persists
    through the one budget retry. The grounding loop that calls this never calls it twice.
    """

    allowed_ids = tuple(str(claim.claim_id) for claim in claims)
    if not allowed_ids:
        return _degraded_section(
            material,
            code=DraftDegradationCode.NO_ELIGIBLE_CLAIMS,
            detail=_no_claims_detail(material.kind),
        )
    allowed_uuids = {claim.claim_id for claim in claims}
    base_prompt = build_prompt(budget)
    regeneration = feedback is not None
    if regeneration:
        base_prompt = f"{base_prompt}\n\n{feedback}"

    try:
        blocks, telemetry = _run_attempt(
            orchestrator,
            brief_date=brief_date,
            prompt_name=prompt_name,
            prompt=base_prompt,
            allowed_ids=allowed_ids,
            allowed_uuids=allowed_uuids,
            section_kind=material.kind,
            attempt=1,
            budget=budget,
            regeneration=regeneration,
        )
    except (LLMOrchestratorError, ReportCompositionError) as exc:
        return _degraded_section(
            material, code=DraftDegradationCode.COMPOSITION_FAILED, detail=f"composition failed: {exc}"
        )

    attempts: list[CompositionAttempt] = [telemetry]
    if budget.contains(telemetry.word_count):
        return _generated_section(
            material, blocks, tuple(attempts), _budget_outcome(budget, telemetry.word_count, True)
        )

    # One budget retry: a new, separately audited invocation with concise feedback appended to a
    # fresh prompt (distinct from the orchestrator's own schema-validation retry).
    retry_prompt = (
        f"{base_prompt}\n\n"
        + budget_feedback(
            target=budget.target,
            minimum=budget.minimum,
            maximum=budget.maximum,
            actual=telemetry.word_count,
        )
    )
    try:
        retry_blocks, retry_telemetry = _run_attempt(
            orchestrator,
            brief_date=brief_date,
            prompt_name=prompt_name,
            prompt=retry_prompt,
            allowed_ids=allowed_ids,
            allowed_uuids=allowed_uuids,
            section_kind=material.kind,
            attempt=2,
            budget=budget,
            regeneration=regeneration,
        )
    except (LLMOrchestratorError, ReportCompositionError) as exc:
        return _degraded_section(
            material,
            code=DraftDegradationCode.COMPOSITION_FAILED,
            detail=f"composition retry failed: {exc}",
            attempts=tuple(attempts),
        )

    attempts.append(retry_telemetry)
    if budget.contains(retry_telemetry.word_count):
        return _generated_section(
            material,
            retry_blocks,
            tuple(attempts),
            _budget_outcome(budget, retry_telemetry.word_count, True),
        )

    # Second output still violates: degrade, retain both attempts, never trim, never a third call.
    return _degraded_section(
        material,
        code=DraftDegradationCode.BUDGET_UNMET,
        detail=(
            f"section prose was {retry_telemetry.word_count} words after one budget-feedback "
            f"retry, outside the {budget.minimum}-{budget.maximum} word range; generated prose "
            "omitted."
        ),
        attempts=tuple(attempts),
        budget=_budget_outcome(budget, retry_telemetry.word_count, False),
    )


def _budget_outcome(budget: WordBudget, word_count: int, within: bool) -> BudgetOutcome:
    return BudgetOutcome(
        target=budget.target,
        minimum=budget.minimum,
        maximum=budget.maximum,
        word_count=word_count,
        within_budget=within,
    )


# --------------------------------------------------------------------------------------
# Deterministic section rendering (no LLM, budget-exempt, rows retained via material)
# --------------------------------------------------------------------------------------


def _render_key(key: RiskKey) -> str:
    return f"{key.target_type}:{key.target_id}:{key.risk_type}"


def _signed(value: float) -> str:
    return f"+{value}" if value > 0 else str(value)


def _render_what_changed(material: SectionMaterial) -> str:
    """Render the day-over-day diff: reversals first and labelled, then any missing-data notes.

    The rows arrive already reversals-first from the deterministic layer. This states each signed
    move plainly and never invents a cause -- the spec forbids an unacknowledged reversal, not an
    unexplained one, and inventing a reason here would be the ungrounded sentence the gate cannot
    check.
    """

    lines = [
        f"{'REVERSAL -- ' if row.is_reversal else ''}{_render_key(row.key)} {row.direction} "
        f"from {row.previous_level} ({row.previous_score}) to {row.current_level} "
        f"({row.current_score}); delta {_signed(row.delta)}."
        for row in material.change_rows
    ]
    lines.extend(material.notes)
    return "\n".join(lines)


def _render_data_quality(material: SectionMaterial) -> str:
    return "\n".join(f"[{note.code.value}] {note.detail}" for note in material.quality_notes)


def _render_forecasts(material: SectionMaterial) -> str:
    return "\n".join(
        f"{row.event_title} | {row.scenario_name}: probability {row.probability}, risk "
        f"{row.risk_score} ({row.severity}), horizon {row.horizon}."
        for row in material.forecast_rows
    )


def _render_alerts(material: SectionMaterial) -> str:
    return "\n".join(
        f"[{'ALL-CLEAR' if row.is_all_clear else row.severity.upper()}] {row.title} -- "
        f"{row.state} ({row.changed_at.isoformat()})."
        for row in material.alert_rows
    )


def _render_deterministic(material: SectionMaterial) -> str:
    if material.kind is SectionKind.DISCLAIMER:
        return material.prose
    if material.kind is SectionKind.WHAT_CHANGED:
        return _render_what_changed(material)
    if material.kind is SectionKind.DATA_QUALITY:
        return _render_data_quality(material)
    if material.kind is SectionKind.FORECASTS:
        return _render_forecasts(material)
    if material.kind is SectionKind.ALERTS:
        return _render_alerts(material)
    return material.prose


def _deterministic_section(material: SectionMaterial) -> DraftSection:
    return DraftSection(
        kind=material.kind,
        order=material.order,
        title=material.title,
        material=material,
        rendered=_render_deterministic(material),
    )


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def _compose_section(
    orchestrator: _Orchestrator,
    *,
    inputs: BriefInputs,
    context: BriefContext,
    material: SectionMaterial,
    feedback: str | None = None,
) -> DraftSection:
    kind = material.kind
    brief_date = inputs.window.brief_date

    if kind is SectionKind.EXECUTIVE_SUMMARY:
        events_with_claims = [
            (event, _claims_for_event(context, event.event_id))
            for event in inputs.executive_summary.top_events
        ]
        claims = _dedupe_claims(claim_group for _, claim_group in events_with_claims)
        return _compose_generated(
            orchestrator,
            brief_date=brief_date,
            material=material,
            claims=claims,
            budget=EXECUTIVE_SUMMARY_BUDGET,
            prompt_name=EXECUTIVE_SUMMARY_PROMPT_NAME,
            feedback=feedback,
            build_prompt=lambda budget: build_executive_summary_prompt(
                alert_changes=inputs.executive_summary.alert_state_changes,
                events_with_claims=events_with_claims,
                largest_move=inputs.executive_summary.largest_risk_move,
                prior_brief=inputs.prior_brief,
                target=budget.target,
                minimum=budget.minimum,
                maximum=budget.maximum,
            ),
        )

    if kind is SectionKind.TOP_EVENT:
        event = material.event
        claims = material.evidence.claims if material.evidence else ()
        if event is None:  # defensive: a top-event section always carries its event
            return _degraded_section(
                material,
                code=DraftDegradationCode.COMPOSITION_FAILED,
                detail="top-event section is missing its event.",
            )
        return _compose_generated(
            orchestrator,
            brief_date=brief_date,
            material=material,
            claims=claims,
            budget=TOP_EVENT_BUDGET,
            prompt_name=TOP_EVENT_PROMPT_NAME,
            feedback=feedback,
            build_prompt=lambda budget: build_top_event_prompt(
                event=event,
                claims=claims,
                target=budget.target,
                minimum=budget.minimum,
                maximum=budget.maximum,
            ),
        )

    if kind is SectionKind.RISK_RADAR:
        # Risk-radar commentary is grounded in the brief's evidence claims; the table is retained
        # deterministically either way, so a brief with no citable claim keeps its table and omits
        # only the commentary.
        claims = _dedupe_claims(context_evidence.claims for context_evidence in context.evidence)
        return _compose_generated(
            orchestrator,
            brief_date=brief_date,
            material=material,
            claims=claims,
            budget=RISK_COMMENTARY_BUDGET,
            prompt_name=RISK_COMMENTARY_PROMPT_NAME,
            feedback=feedback,
            build_prompt=lambda budget: build_risk_commentary_prompt(
                risk_rows=material.risk_rows,
                claims=claims,
                prior_brief=inputs.prior_brief,
                target=budget.target,
                minimum=budget.minimum,
                maximum=budget.maximum,
            ),
        )

    if kind is SectionKind.HISTORICAL_PARALLELS:
        titles = {event.event_id: event.title for event in inputs.top_events}
        parallels = [
            (
                analogy,
                titles.get(analogy.event_id, str(analogy.event_id)),
                _claims_for_event(context, analogy.event_id),
            )
            for analogy in material.analogies
        ]
        claims = _dedupe_claims(claim_group for _, _, claim_group in parallels)
        return _compose_generated(
            orchestrator,
            brief_date=brief_date,
            material=material,
            claims=claims,
            budget=HISTORICAL_PARALLELS_BUDGET,
            prompt_name=HISTORICAL_PARALLELS_PROMPT_NAME,
            feedback=feedback,
            build_prompt=lambda budget: build_historical_parallels_prompt(
                parallels=parallels,
                target=budget.target,
                minimum=budget.minimum,
                maximum=budget.maximum,
            ),
        )

    # Forecasts (table), Alerts (list), What Changed (diff), Data Quality (notes), Disclaimer
    # (fixed): deterministic, budget-exempt, no LLM call, structured rows retained via material.
    return _deterministic_section(material)


def compose_section(
    orchestrator: _Orchestrator,
    *,
    inputs: BriefInputs,
    context: BriefContext,
    material: SectionMaterial,
    feedback: str | None = None,
) -> DraftSection:
    """Compose (or regenerate) exactly one section, touching no other.

    The smallest seam the grounding gate needs: a section's composition depends only on the shared
    ``inputs``/``context`` and its own :class:`SectionMaterial`, with no cross-section state, so
    one section can be regenerated in isolation. ``feedback`` carries the grounding gate's exact
    failed-claim/verdict note into a fresh, separately-audited run down the same T2 path.
    """

    return _compose_section(
        orchestrator, inputs=inputs, context=context, material=material, feedback=feedback
    )


def compose_brief(
    orchestrator: _Orchestrator,
    *,
    inputs: BriefInputs,
    context: BriefContext,
    material: BriefMaterial,
) -> BriefDraft:
    """Compose one daily brief into an immutable, ungrounded draft.

    Walks the material's sections in reading order, composing the prose sections through the
    orchestrator and rendering the deterministic ones. The result holds no ORM object and claims
    no grounding; a quiet day makes zero LLM calls, and any failure degrades one section rather
    than the brief.
    """

    sections = tuple(
        _compose_section(orchestrator, inputs=inputs, context=context, material=material_section)
        for material_section in material.sections
    )
    return BriefDraft(
        brief_date=inputs.window.brief_date,
        is_quiet_day=inputs.is_quiet_day,
        sections=sections,
    )


__all__ = [
    "COMPOSITION_JOB_TYPE",
    "COMPOSITION_RISK_LEVEL",
    "COMPOSITION_TEMPERATURE",
    "COMPOSITION_TIER",
    "EXECUTIVE_SUMMARY_BUDGET",
    "HISTORICAL_PARALLELS_BUDGET",
    "RISK_COMMENTARY_BUDGET",
    "TOP_EVENT_BUDGET",
    "BriefDraft",
    "BudgetOutcome",
    "CompositionAttempt",
    "DraftBlock",
    "DraftDegradationCode",
    "DraftSection",
    "ReportCompositionError",
    "SectionDegradation",
    "WordBudget",
    "compose_brief",
    "compose_section",
    "composition_job",
    "count_words",
]
