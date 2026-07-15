"""The grounding gate: claim grounding, copyright, and day-over-day consistency (spec, ADR 0009).

Post-composition and pre-publish, this is the last thing Stage 6 does to a
:class:`~services.reports.composition.BriefDraft`. It never marks or persists a report -- it
returns an immutable verdict the later lifecycle service consumes to decide *publish* or *block*.

What it enforces, exactly as the report-generation spec draws it:

* **Claim grounding** is a T1 model check *per claim-tagged block*: every cited claim gets one
  verdict -- ``supported | unsupported | unverifiable`` -- judged only against that claim's
  supportive evidence snippets, through the real orchestrator with a fail-closed claim whitelist.
* **One regeneration.** Any ``unsupported`` claim triggers exactly one regeneration of *that*
  section (never a loop, never for a merely ``unverifiable`` claim) down the existing T2 path with
  the exact failed-claim feedback. The regenerated section is re-grounded; any claim still
  unsupported has its block removed deterministically. If a section loses **more than** 30% of its
  distinct cited claims it is replaced by a data-quality note; at exactly 30% it is not.
* **Copyright.** Composer prose reproducing more than 15 consecutive source words, or quoting
  without attribution/link or beyond 25 words, blocks the gate (:mod:`services.reports.copyright`).
* **Consistency.** Every day-over-day risk reversal must be acknowledged in the final brief
  (:mod:`services.reports.consistency`); an unacknowledged one blocks the gate.

A model or validation failure never silently passes: it fails the section closed and blocks the
gate. Nothing here mutates the draft, the inputs, or yesterday's context, and nothing holds an ORM
object or a session -- every telemetry field is copied to a primitive.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final, Protocol

from db.models.core import GROUNDING_STATUSES, Job
from services.llm.contracts import ClaimGrounding, GroundingVerdict
from services.llm.orchestrator import LLMOrchestratorError, LLMOrchestratorRequest
from services.llm.policy import LLMTier
from services.reports.composition import (
    BriefDraft,
    CompositionAttempt,
    DraftBlock,
    DraftSection,
    compose_section,
)
from services.reports.consistency import (
    ConsistencyFinding,
    brief_reversals,
    check_reversal_acknowledgement,
)
from services.reports.context import BriefContext, ClaimContext
from services.reports.contracts import BriefInputs
from services.reports.copyright import CopyrightFinding, SnippetSource, check_block_copyright
from services.reports.grounding_prompts import (
    GROUNDING_PROMPT_NAME,
    GROUNDING_PROMPT_TEMPLATE_VERSION,
    GROUNDING_PROMPT_VERSION,
    GROUNDING_SCHEMA,
    GROUNDING_SCHEMA_VERSION,
    build_grounding_prompt,
)
from services.reports.material import SectionKind

#: The grounding gate is T1 (ADR 0008): a check, not a reasoning task. Deterministic and
#: essential, with routing inputs that keep it at T1 -- a low risk level and no hotness escalation.
GROUNDING_TIER: Final = LLMTier.T1
GROUNDING_RISK_LEVEL: Final = "low"
GROUNDING_TEMPERATURE: Final[float] = 0.0
GROUNDING_JOB_TYPE: Final = "daily_brief_grounding"

#: Section is replaced by a data-quality note when it loses *more than* this share of its distinct
#: cited claims. Exactly the share is allowed (spec: "> 30%"), so the boundary is tested with exact
#: integer arithmetic (:data:`_CLAIM_LOSS_NUM`/:data:`_CLAIM_LOSS_DEN`) rather than a float compare.
MAX_CLAIM_LOSS_RATIO: Final[float] = 0.30
_CLAIM_LOSS_NUM: Final = 3
_CLAIM_LOSS_DEN: Final = 10

_JOB_NAMESPACE: Final = uuid.UUID("6f1e2c3a-4b5d-4e6f-8a9b-0c1d2e3f4a5b")


class SectionGroundingStatus(StrEnum):
    """A section's grounding verdict, in the exact vocabulary the ``report_sections`` model stores.

    ``passed`` is a fully-grounded section (possibly after unsupported blocks were trimmed within
    the 30% budget); ``data_quality_note`` is a section replaced by a note because too much was
    lost; ``failed`` is a hard block -- a copyright violation, an unacknowledged reversal, or a
    grounding check that could not be verified. ``pending`` is the pre-gate default.
    """

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    DATA_QUALITY_NOTE = "data_quality_note"


# The enum is the source of truth the DB check constraint mirrors; keep them from drifting.
assert tuple(status.value for status in SectionGroundingStatus) == GROUNDING_STATUSES


class GateOutcome(StrEnum):
    """The brief-level verdict: ships (with any per-section notes) or is blocked from publishing."""

    PASS = "pass"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ClaimVerdict:
    """One claim's grounding verdict in one block, on one grounding round. Immutable, primitive."""

    block_index: int
    claim_id: uuid.UUID
    verdict: str
    rationale: str
    grounding_round: int


@dataclass(frozen=True)
class GroundingAttempt:
    """Telemetry for one T1 grounding invocation, copied entirely to primitives (no ORM handle)."""

    block_index: int
    grounding_round: int
    prompt_name: str
    prompt_version: str
    prompt_template_version: str | None
    schema_name: str
    schema_version: str
    requested_tier: str
    actual_tier: str
    #: The actual tier came back below the requested T1 (only possible if T1 itself is unavailable).
    tier_degraded: bool
    route_degradation_reasons: tuple[str, ...]
    degraded_provider: str | None
    mode: str
    queue: str
    cache_hit: bool
    trace_id: str
    llm_run_id: uuid.UUID | None


@dataclass(frozen=True)
class SectionGroundingResult:
    """One section's gate outcome: its final text, verdicts, findings, transformations, telemetry."""

    kind: SectionKind
    order: int
    title: str
    status: SectionGroundingStatus
    #: The text that would publish: trimmed blocks, a data-quality note, or the deterministic render.
    final_text: str
    #: The generated blocks that survived grounding (empty for deterministic/withheld sections).
    final_blocks: tuple[DraftBlock, ...]
    verdicts: tuple[ClaimVerdict, ...]
    copyright_findings: tuple[CopyrightFinding, ...]
    consistency_findings: tuple[ConsistencyFinding, ...]
    #: The section's first-composition attempts (from the draft), retained for the lifecycle audit.
    composition_attempts: tuple[CompositionAttempt, ...]
    #: The one regeneration's attempts, if a regeneration happened.
    regeneration_attempts: tuple[CompositionAttempt, ...]
    grounding_attempts: tuple[GroundingAttempt, ...]
    #: Distinct cited claim ids in the candidate section, before and after deterministic trimming.
    claims_before: int
    claims_after: int
    regenerated: bool
    #: This section prevents publication (grounding failure, copyright, or unacknowledged reversal).
    blocks_publication: bool
    #: Every deterministic transformation applied, and why. Explicit and immutable.
    transformations: tuple[str, ...]


@dataclass(frozen=True)
class GroundingGateResult:
    """The whole brief's gate result: final sections in order, and the aggregate pass/block state."""

    brief_date: datetime.date
    sections: tuple[SectionGroundingResult, ...]
    outcome: GateOutcome

    @property
    def blocked(self) -> bool:
        return self.outcome is GateOutcome.BLOCKED

    @property
    def claim_verdicts(self) -> tuple[ClaimVerdict, ...]:
        return tuple(v for section in self.sections for v in section.verdicts)

    @property
    def copyright_findings(self) -> tuple[CopyrightFinding, ...]:
        return tuple(f for section in self.sections for f in section.copyright_findings)

    @property
    def consistency_findings(self) -> tuple[ConsistencyFinding, ...]:
        return tuple(f for section in self.sections for f in section.consistency_findings)

    @property
    def grounding_attempts(self) -> tuple[GroundingAttempt, ...]:
        return tuple(a for section in self.sections for a in section.grounding_attempts)

    @property
    def llm_run_ids(self) -> tuple[uuid.UUID, ...]:
        """Every LLM run id behind the gate -- composition, regeneration, and grounding."""
        ids: list[uuid.UUID] = []
        for section in self.sections:
            for attempt in (*section.composition_attempts, *section.regeneration_attempts):
                if attempt.llm_run_id is not None:
                    ids.append(attempt.llm_run_id)
            for grounding in section.grounding_attempts:
                if grounding.llm_run_id is not None:
                    ids.append(grounding.llm_run_id)
        return tuple(ids)


class _Orchestrator(Protocol):
    """The seam the gate depends on, so tests inject the real runtime or a narrow fake."""

    def run(self, request: LLMOrchestratorRequest) -> Any: ...


def grounding_job(brief_date: datetime.date) -> Job:
    """The one job row every grounding call of a brief runs under: stable per ``brief_date``.

    A distinct job type from composition, so grounding runs are queryable on their own, and a
    stable id so the orchestrator's ``merge`` updates one row across a brief's many block checks.
    """

    job_key = f"{GROUNDING_JOB_TYPE}:{brief_date.isoformat()}"
    now = datetime.datetime.now(datetime.UTC)
    return Job(
        id=uuid.uuid5(_JOB_NAMESPACE, job_key),
        job_key=job_key,
        job_type=GROUNDING_JOB_TYPE,
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
# Evidence helpers (pure)
# --------------------------------------------------------------------------------------


def _claims_by_id(context: BriefContext) -> dict[uuid.UUID, ClaimContext]:
    """Every citable claim across the brief, by id -- the grounding lookup for a block's claim ids."""

    return {claim.claim_id: claim for evidence in context.evidence for claim in evidence.claims}


def _distinct_claim_ids(blocks: Sequence[DraftBlock]) -> set[uuid.UUID]:
    return {claim_id for block in blocks for claim_id in block.claim_ids}


def _snippets_for_block(block: DraftBlock, claims_by_id: dict[uuid.UUID, ClaimContext]) -> tuple[SnippetSource, ...]:
    """The supportive source snippets available to one block, for the copyright check. No bodies."""

    snippets: list[SnippetSource] = []
    for claim_id in dict.fromkeys(block.claim_ids):
        claim = claims_by_id.get(claim_id)
        if claim is None:
            continue
        for link in claim.links:
            article = link.article
            snippets.append(
                SnippetSource(
                    article_id=article.article_id,
                    publisher=article.publisher,
                    url=article.url,
                    text=article.excerpt.text,
                )
            )
    return tuple(snippets)


# --------------------------------------------------------------------------------------
# One block's grounding
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _BlockGrounding:
    """One block's grounding outcome: verdicts and telemetry, or a fail-closed failure."""

    ok: bool
    verdicts: tuple[ClaimVerdict, ...]
    attempt: GroundingAttempt | None
    failure: str | None


def _grounding_attempt(outcome: Any, *, block_index: int, grounding_round: int) -> GroundingAttempt:
    run = outcome.run
    contract = outcome.contract
    return GroundingAttempt(
        block_index=block_index,
        grounding_round=grounding_round,
        prompt_name=run.prompt_name,
        prompt_version=run.prompt_version,
        prompt_template_version=run.prompt_template_version,
        schema_name=getattr(contract, "schema_name", GROUNDING_SCHEMA),
        schema_version=getattr(contract, "schema_version", GROUNDING_SCHEMA_VERSION),
        requested_tier=GROUNDING_TIER.value,
        actual_tier=outcome.tier.value,
        tier_degraded=outcome.tier is not GROUNDING_TIER,
        route_degradation_reasons=tuple(outcome.route_degradation_reasons),
        degraded_provider=outcome.degraded_provider,
        mode=outcome.mode.value,
        queue=outcome.queue,
        cache_hit=outcome.cache_hit,
        trace_id=outcome.trace_id,
        llm_run_id=getattr(run, "id", None),
    )


def _ground_block(
    orchestrator: _Orchestrator,
    *,
    brief_date: datetime.date,
    section_kind: SectionKind,
    block_index: int,
    block: DraftBlock,
    claims_by_id: dict[uuid.UUID, ClaimContext],
    grounding_round: int,
) -> _BlockGrounding:
    """Ground one claim-tagged block: one T1 call, one verdict per distinct cited claim, fail-closed.

    Duplicate claim ids in a block are de-duplicated to distinct claims first, so each cited claim
    is grounded exactly once. Coverage is enforced here (not in the contract, which cannot know the
    block's cited set): a missing, extra, or duplicated verdict fails the block closed.
    """

    distinct = tuple(dict.fromkeys(block.claim_ids))
    claims = [claims_by_id[cid] for cid in distinct if cid in claims_by_id]
    if len(claims) != len(distinct):
        return _BlockGrounding(
            ok=False,
            verdicts=(),
            attempt=None,
            failure=f"block {block_index} cites a claim with no evidence context; cannot ground it",
        )

    allowed_ids = tuple(str(cid) for cid in distinct)
    request = LLMOrchestratorRequest(
        job=grounding_job(brief_date),
        prompt_name=GROUNDING_PROMPT_NAME,
        prompt_version=GROUNDING_PROMPT_VERSION,
        prompt_template_version=GROUNDING_PROMPT_TEMPLATE_VERSION,
        requested_schema=GROUNDING_SCHEMA,
        prompt=build_grounding_prompt(block_text=block.text, claims=claims),
        requested_tier=GROUNDING_TIER,
        risk_level=GROUNDING_RISK_LEVEL,
        current_event_hotness=0.0,
        trailing_7d_p90_hotness=None,
        is_realtime=True,
        is_essential=True,
        articles=(),
        allowed_ids=allowed_ids,
        temperature=GROUNDING_TEMPERATURE,
        context={
            "brief_date": brief_date.isoformat(),
            "section_kind": section_kind.value,
            "block_index": block_index,
            "grounding_round": grounding_round,
            "allowed_claim_ids": list(allowed_ids),
        },
    )

    try:
        outcome = orchestrator.run(request)
    except LLMOrchestratorError as exc:
        return _BlockGrounding(
            ok=False,
            verdicts=(),
            attempt=None,
            failure=f"grounding call failed for block {block_index}: {exc}",
        )

    attempt = _grounding_attempt(outcome, block_index=block_index, grounding_round=grounding_round)
    contract = outcome.contract
    if not isinstance(contract, ClaimGrounding):
        return _BlockGrounding(
            ok=False,
            verdicts=(),
            attempt=attempt,
            failure=f"grounding returned a {type(contract).__name__}, not a {GROUNDING_SCHEMA}",
        )

    verdict_ids = [uuid.UUID(v.claim_id) for v in contract.verdicts]
    if len(verdict_ids) != len(set(verdict_ids)):
        return _BlockGrounding(
            ok=False, verdicts=(), attempt=attempt, failure=f"block {block_index} got a duplicate verdict"
        )
    if set(verdict_ids) != set(distinct):
        return _BlockGrounding(
            ok=False,
            verdicts=(),
            attempt=attempt,
            failure=f"block {block_index} verdicts do not cover exactly its cited claims",
        )

    verdicts = tuple(
        ClaimVerdict(
            block_index=block_index,
            claim_id=uuid.UUID(v.claim_id),
            verdict=v.verdict.value,
            rationale=v.rationale,
            grounding_round=grounding_round,
        )
        for v in contract.verdicts
    )
    return _BlockGrounding(ok=True, verdicts=verdicts, attempt=attempt, failure=None)


def _ground_blocks(
    orchestrator: _Orchestrator,
    *,
    brief_date: datetime.date,
    section_kind: SectionKind,
    blocks: Sequence[DraftBlock],
    claims_by_id: dict[uuid.UUID, ClaimContext],
    grounding_round: int,
) -> tuple[tuple[ClaimVerdict, ...], tuple[GroundingAttempt, ...], str | None]:
    """Ground every block of a section for one round; stop closed at the first grounding failure."""

    verdicts: list[ClaimVerdict] = []
    attempts: list[GroundingAttempt] = []
    for block_index, block in enumerate(blocks):
        result = _ground_block(
            orchestrator,
            brief_date=brief_date,
            section_kind=section_kind,
            block_index=block_index,
            block=block,
            claims_by_id=claims_by_id,
            grounding_round=grounding_round,
        )
        if result.attempt is not None:
            attempts.append(result.attempt)
        if not result.ok:
            return tuple(verdicts), tuple(attempts), result.failure
        verdicts.extend(result.verdicts)
    return tuple(verdicts), tuple(attempts), None


# --------------------------------------------------------------------------------------
# Section results (builders)
# --------------------------------------------------------------------------------------


def _regeneration_feedback(
    unsupported: Sequence[ClaimVerdict], claims_by_id: dict[uuid.UUID, ClaimContext]
) -> str:
    """The exact failed-claim/verdict feedback fed into the one regeneration."""

    lines = [
        "The grounding check found unsupported claims in this section. Rewrite the section so "
        "every cited claim is fully supported by its evidence, or omit that claim. Cite only the "
        "claim_ids already provided; do not add new claims.",
    ]
    for verdict in unsupported:
        claim = claims_by_id.get(verdict.claim_id)
        claim_text = f" (claim: {claim.claim_text})" if claim else ""
        rationale = f" -- {verdict.rationale}" if verdict.rationale else ""
        lines.append(f"- claim {verdict.claim_id}: verdict {verdict.verdict}{rationale}{claim_text}")
    return "\n".join(lines)


def _copyright_findings(
    blocks: Sequence[DraftBlock], claims_by_id: dict[uuid.UUID, ClaimContext]
) -> tuple[CopyrightFinding, ...]:
    findings: list[CopyrightFinding] = []
    for block_index, block in enumerate(blocks):
        findings.extend(
            check_block_copyright(
                block_index=block_index,
                block_text=block.text,
                snippets=_snippets_for_block(block, claims_by_id),
            )
        )
    return tuple(findings)


def _blocked_section_result(
    section: DraftSection,
    *,
    verdicts: Sequence[ClaimVerdict],
    grounding_attempts: Sequence[GroundingAttempt],
    regeneration_attempts: Sequence[CompositionAttempt],
    regenerated: bool,
    failure: str,
) -> SectionGroundingResult:
    """A section whose grounding could not be verified: fail closed, block the gate, publish nothing."""

    return SectionGroundingResult(
        kind=section.kind,
        order=section.order,
        title=section.title,
        status=SectionGroundingStatus.FAILED,
        final_text=f"[grounding_failed] {failure}",
        final_blocks=(),
        verdicts=tuple(verdicts),
        copyright_findings=(),
        consistency_findings=(),
        composition_attempts=section.attempts,
        regeneration_attempts=tuple(regeneration_attempts),
        grounding_attempts=tuple(grounding_attempts),
        claims_before=len(_distinct_claim_ids(section.blocks)),
        claims_after=0,
        regenerated=regenerated,
        blocks_publication=True,
        transformations=(f"grounding could not be verified: {failure}",),
    )


def _finalize_section(
    section: DraftSection,
    *,
    candidate: DraftSection,
    candidate_verdicts: Sequence[ClaimVerdict],
    all_verdicts: Sequence[ClaimVerdict],
    grounding_attempts: Sequence[GroundingAttempt],
    regeneration_attempts: Sequence[CompositionAttempt],
    regenerated: bool,
    claims_by_id: dict[uuid.UUID, ClaimContext],
) -> SectionGroundingResult:
    """Trim still-unsupported blocks from the candidate, apply the 30% rule, then check copyright."""

    unsupported = {
        verdict.claim_id
        for verdict in candidate_verdicts
        if verdict.verdict == GroundingVerdict.UNSUPPORTED.value
    }
    before_ids = _distinct_claim_ids(candidate.blocks)
    before = len(before_ids)

    kept_blocks = tuple(
        block for block in candidate.blocks if not (set(block.claim_ids) & unsupported)
    )
    removed = before - len(_distinct_claim_ids(kept_blocks))

    transformations: list[str] = []
    if regenerated:
        transformations.append("regenerated the section once after an unsupported-claim verdict")
    for block_index, block in enumerate(candidate.blocks):
        offending = set(block.claim_ids) & unsupported
        if offending:
            ids = ", ".join(str(cid) for cid in sorted(offending, key=str))
            transformations.append(
                f"removed block {block_index}: still-unsupported claim(s) {ids}"
            )

    def _result(
        *, status: SectionGroundingStatus, final_text: str, final_blocks: tuple[DraftBlock, ...],
        copyright_findings: tuple[CopyrightFinding, ...], blocks_publication: bool,
    ) -> SectionGroundingResult:
        return SectionGroundingResult(
            kind=section.kind,
            order=section.order,
            title=section.title,
            status=status,
            final_text=final_text,
            final_blocks=final_blocks,
            verdicts=tuple(all_verdicts),
            copyright_findings=copyright_findings,
            consistency_findings=(),
            composition_attempts=section.attempts,
            regeneration_attempts=tuple(regeneration_attempts),
            grounding_attempts=tuple(grounding_attempts),
            claims_before=before,
            claims_after=len(_distinct_claim_ids(final_blocks)),
            regenerated=regenerated,
            blocks_publication=blocks_publication,
            transformations=tuple(transformations),
        )

    if before == 0:
        # The candidate produced no groundable prose (a regeneration that degraded or abstained).
        transformations.append("no groundable prose remained; section replaced with a data-quality note")
        return _result(
            status=SectionGroundingStatus.DATA_QUALITY_NOTE,
            final_text="[data_quality_note] this section could not be composed into groundable prose.",
            final_blocks=(),
            copyright_findings=(),
            blocks_publication=False,
        )

    if removed * _CLAIM_LOSS_DEN > _CLAIM_LOSS_NUM * before:
        transformations.append(
            f"{removed}/{before} distinct cited claims lost (> {MAX_CLAIM_LOSS_RATIO:.0%}); "
            "prose replaced with a data-quality note"
        )
        return _result(
            status=SectionGroundingStatus.DATA_QUALITY_NOTE,
            final_text=(
                f"[data_quality_note] {removed} of {before} cited claims in this section could not "
                f"be grounded and were removed, exceeding the {MAX_CLAIM_LOSS_RATIO:.0%} threshold; "
                "section prose withheld."
            ),
            final_blocks=(),
            copyright_findings=(),
            blocks_publication=False,
        )

    # Kept blocks ship: run the copyright gate on exactly the prose that would publish.
    copyright_findings = _copyright_findings(kept_blocks, claims_by_id)
    final_text = "\n\n".join(block.text for block in kept_blocks)
    if copyright_findings:
        return _result(
            status=SectionGroundingStatus.FAILED,
            final_text=final_text,
            final_blocks=kept_blocks,
            copyright_findings=copyright_findings,
            blocks_publication=True,
        )
    return _result(
        status=SectionGroundingStatus.PASSED,
        final_text=final_text,
        final_blocks=kept_blocks,
        copyright_findings=(),
        blocks_publication=False,
    )


def _ground_generated_section(
    orchestrator: _Orchestrator,
    *,
    inputs: BriefInputs,
    context: BriefContext,
    section: DraftSection,
    claims_by_id: dict[uuid.UUID, ClaimContext],
) -> SectionGroundingResult:
    """Ground one generated section, regenerate once on an unsupported claim, then finalize it."""

    brief_date = inputs.window.brief_date

    round1_verdicts, round1_attempts, failure = _ground_blocks(
        orchestrator,
        brief_date=brief_date,
        section_kind=section.kind,
        blocks=section.blocks,
        claims_by_id=claims_by_id,
        grounding_round=1,
    )
    if failure is not None:
        return _blocked_section_result(
            section,
            verdicts=round1_verdicts,
            grounding_attempts=round1_attempts,
            regeneration_attempts=(),
            regenerated=False,
            failure=failure,
        )

    unsupported_round1 = _distinct_unsupported(round1_verdicts)
    if not unsupported_round1:
        # No unsupported claim: no regeneration (an unverifiable claim never triggers one).
        return _finalize_section(
            section,
            candidate=section,
            candidate_verdicts=round1_verdicts,
            all_verdicts=round1_verdicts,
            grounding_attempts=round1_attempts,
            regeneration_attempts=(),
            regenerated=False,
            claims_by_id=claims_by_id,
        )

    # Exactly one regeneration down the existing T2 path, with the exact failed-claim feedback.
    regenerated_section = compose_section(
        orchestrator,
        inputs=inputs,
        context=context,
        material=section.material,
        feedback=_regeneration_feedback(unsupported_round1, claims_by_id),
    )

    round2_verdicts, round2_attempts, failure = _ground_blocks(
        orchestrator,
        brief_date=brief_date,
        section_kind=section.kind,
        blocks=regenerated_section.blocks,
        claims_by_id=claims_by_id,
        grounding_round=2,
    )
    grounding_attempts = (*round1_attempts, *round2_attempts)
    if failure is not None:
        return _blocked_section_result(
            section,
            verdicts=(*round1_verdicts, *round2_verdicts),
            grounding_attempts=grounding_attempts,
            regeneration_attempts=regenerated_section.attempts,
            regenerated=True,
            failure=failure,
        )

    return _finalize_section(
        section,
        candidate=regenerated_section,
        candidate_verdicts=round2_verdicts,
        all_verdicts=(*round1_verdicts, *round2_verdicts),
        grounding_attempts=grounding_attempts,
        regeneration_attempts=regenerated_section.attempts,
        regenerated=True,
        claims_by_id=claims_by_id,
    )


def _distinct_unsupported(verdicts: Sequence[ClaimVerdict]) -> tuple[ClaimVerdict, ...]:
    """The unsupported verdicts, one per claim id (first occurrence), for regeneration feedback."""

    seen: dict[uuid.UUID, ClaimVerdict] = {}
    for verdict in verdicts:
        if verdict.verdict == GroundingVerdict.UNSUPPORTED.value:
            seen.setdefault(verdict.claim_id, verdict)
    return tuple(seen.values())


def _passthrough_section(section: DraftSection) -> SectionGroundingResult:
    """A deterministic or composition-degraded section: no grounding call, no copyright check.

    A clean deterministic section (a table, a diff, the disclaimer) passes; a section that already
    degraded during composition ships as a data-quality note. The disclaimer is left untouched.
    """

    status = (
        SectionGroundingStatus.DATA_QUALITY_NOTE
        if section.is_degraded
        else SectionGroundingStatus.PASSED
    )
    return SectionGroundingResult(
        kind=section.kind,
        order=section.order,
        title=section.title,
        status=status,
        final_text=section.text,
        final_blocks=(),
        verdicts=(),
        copyright_findings=(),
        consistency_findings=(),
        composition_attempts=section.attempts,
        regeneration_attempts=(),
        grounding_attempts=(),
        claims_before=0,
        claims_after=0,
        regenerated=False,
        blocks_publication=False,
        transformations=(),
    )


# --------------------------------------------------------------------------------------
# Consistency post-check
# --------------------------------------------------------------------------------------


def _draft_reversals(draft: BriefDraft) -> tuple[Any, ...]:
    """Every day-over-day reversal in the brief's What Changed material."""

    return tuple(
        row
        for section in draft.sections
        if section.kind is SectionKind.WHAT_CHANGED
        for row in brief_reversals(section.material.change_rows)
    )


def _attach_consistency(
    results: Sequence[SectionGroundingResult],
    findings: Sequence[ConsistencyFinding],
) -> tuple[SectionGroundingResult, ...]:
    """Record unacknowledged-reversal findings on the What Changed section, and block the gate."""

    if not findings:
        return tuple(results)
    return tuple(
        replace(
            result,
            consistency_findings=tuple(findings),
            status=SectionGroundingStatus.FAILED,
            blocks_publication=True,
        )
        if result.kind is SectionKind.WHAT_CHANGED
        else result
        for result in results
    )


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def run_grounding_gate(
    orchestrator: _Orchestrator,
    *,
    inputs: BriefInputs,
    context: BriefContext,
    draft: BriefDraft,
) -> GroundingGateResult:
    """Ground, regenerate, trim, and check copyright/consistency for one composed brief.

    Returns an immutable verdict; it neither mutates the draft nor persists anything. Only sections
    that carry generated claim-tagged blocks make grounding calls -- quiet, deterministic, and
    degraded sections pass through untouched, and the disclaimer stays last.
    """

    claims_by_id = _claims_by_id(context)

    results = [
        _ground_generated_section(
            orchestrator, inputs=inputs, context=context, section=section, claims_by_id=claims_by_id
        )
        if section.is_generated
        else _passthrough_section(section)
        for section in draft.sections
    ]

    final_brief_text = "\n".join(result.final_text for result in results)
    consistency = check_reversal_acknowledgement(
        _draft_reversals(draft), final_brief_text=final_brief_text
    )
    results = list(_attach_consistency(results, consistency))

    outcome = (
        GateOutcome.BLOCKED
        if any(result.blocks_publication for result in results)
        else GateOutcome.PASS
    )
    return GroundingGateResult(
        brief_date=draft.brief_date, sections=tuple(results), outcome=outcome
    )


__all__ = [
    "GROUNDING_JOB_TYPE",
    "GROUNDING_RISK_LEVEL",
    "GROUNDING_TEMPERATURE",
    "GROUNDING_TIER",
    "MAX_CLAIM_LOSS_RATIO",
    "ClaimVerdict",
    "ConsistencyFinding",
    "CopyrightFinding",
    "GateOutcome",
    "GroundingAttempt",
    "GroundingGateResult",
    "SectionGroundingResult",
    "SectionGroundingStatus",
    "grounding_job",
    "run_grounding_gate",
]
