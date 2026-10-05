"""Immutable report lifecycle, version allocation, and grounded-section persistence (spec, ADR 0009).

The persistence half of Stage 6: once the grounding gate has ruled on a composed brief, this
module is what turns that ruling into durable rows and moves the report through its lifecycle.
It composes nothing and calls no model -- it consumes the immutable
:class:`~services.reports.grounding.GroundingGateResult` and writes it, or fails closed.

Four disciplines are enforced here rather than left to the caller:

* **The lifecycle is a closed state machine.** ``generating -> grounding_check -> published`` with
  a terminal ``failed``; every other transition is refused with a typed error and no mutation. A
  published report is immutable; the one post-publish write allowed is ``stale=false -> true``, and
  it touches nothing else.
* **Versions are allocated under a transaction-scoped subject lock.** A daily brief is global
  (``user_id IS NULL``), keyed by ``brief_date``; a first creation is version 1 and every rerun is
  ``max(version) + 1`` on a fresh row -- never an edit of a prior one. A PostgreSQL advisory xact
  lock on the subject serialises concurrent allocators, so two reruns cannot both read the same
  max and collide (the partial unique index is the backstop, not the mechanism).
* **Sections are written once, in order, from the gate result alone.** ``final_text`` becomes the
  body; the surviving claim-tagged blocks become the JSON ``blocks``; ``evidence_refs`` is the
  stable de-duplicated claim-id list derived *only* from those blocks (so a deterministic section
  fabricates no citation, and a historical episode id can never enter). Replacing or appending
  after a write is refused, not reconciled.
* **Publication is fail-closed.** A blocked gate can be retained only on a report bound for
  ``failed``; publishing verifies the gate passed, every section is publishable, and the fixed
  disclaimer is present, last, and verbatim before the status moves.

Nothing here commits or rolls back: the caller owns the transaction (its LLM audit rows and these
rows are one unit of work). Every method flushes so the caller sees its own rows, and every value
returned is a frozen, detached snapshot holding no ORM row and no session.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Integer, Text, and_, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models.core import (
    GROUNDING_STATUSES,
    REPORT_CONTENT_POLICIES,
    REPORT_STATUSES,
    Claim,
    ClaimEvidence,
    EventArticle,
    EvidenceItem,
    Report,
    ReportSection,
)
from db.models.personal import PERSONAL_REPORT_TYPE
from services.reports.context import ARTICLE_EVIDENCE_SOURCE_TYPE
from services.reports.grounding import (
    GateOutcome,
    GroundingGateResult,
    SectionGroundingResult,
    SectionGroundingStatus,
)
from services.reports.material import FINAL_DISCLAIMER, SectionKind
from services.reports.repository import DAILY_BRIEF_REPORT_TYPE
from services.reports.selection import PUBLISHED_STATUS


class ReportStatus(StrEnum):
    """The report lifecycle, in the exact vocabulary the ``reports`` model stores."""

    GENERATING = "generating"
    GROUNDING_CHECK = "grounding_check"
    PUBLISHED = "published"
    FAILED = "failed"


# The enum is the source of truth the DB check constraint mirrors; keep them from drifting.
assert tuple(status.value for status in ReportStatus) == REPORT_STATUSES

#: Terminal states: no transition leaves them. ``published`` is immutable content; ``failed`` is
#: the end of a generation that could not ship.
TERMINAL_STATUSES: frozenset[ReportStatus] = frozenset(
    {ReportStatus.PUBLISHED, ReportStatus.FAILED}
)

#: The only legal transitions. ``generating`` may advance to the gate or fail; ``grounding_check``
#: may publish or fail. There is no ``draft``, and nothing re-opens a terminal report.
LEGAL_TRANSITIONS: frozenset[tuple[ReportStatus, ReportStatus]] = frozenset(
    {
        (ReportStatus.GENERATING, ReportStatus.GROUNDING_CHECK),
        (ReportStatus.GENERATING, ReportStatus.FAILED),
        (ReportStatus.GROUNDING_CHECK, ReportStatus.PUBLISHED),
        (ReportStatus.GROUNDING_CHECK, ReportStatus.FAILED),
    }
)

#: The section grounding statuses a published report may carry. ``passed`` is fully grounded;
#: ``data_quality_note`` is a section honestly withheld -- both ship. ``failed``/``pending`` do not.
PUBLISHABLE_SECTION_STATUSES: frozenset[SectionGroundingStatus] = frozenset(
    {SectionGroundingStatus.PASSED, SectionGroundingStatus.DATA_QUALITY_NOTE}
)

#: A brief with more than this many versions or sections is a bug upstream, not a long brief; a
#: bounded read over-reads by one and raises :class:`ReportReadOverflowError` if it finds more,
#: rather than returning a silently truncated list that would imply completeness.
VERSION_LIST_LIMIT = 100
SECTION_LIST_LIMIT = 100

#: The dependency scan for stale-marking. One event lives in a ~24h window, so a handful of briefs
#: and one event report's versions can depend on it; reaching this bound means an upstream fault,
#: and it is surfaced (a silently truncated stale-marking leaves reports serving stale content).
STALE_SCAN_LIMIT = 5_000

#: Namespace for the version-allocation advisory lock (``R6`` -- reports, stage 6). Paired with a
#: per-subject int4 key so ``pg_advisory_xact_lock(ns, key)`` serialises allocators of one subject.
_ADVISORY_LOCK_NAMESPACE = 0x5236


# --------------------------------------------------------------------------------------
# Typed errors -- every one leaves the caller's transaction untouched
# --------------------------------------------------------------------------------------


class ReportLifecycleError(RuntimeError):
    """Base class for a lifecycle fault the caller must handle rather than retry blindly."""


class ReportNotFoundError(ReportLifecycleError):
    """A lifecycle operation named a report id that is not in this transaction."""


class IllegalTransitionError(ReportLifecycleError):
    """A status change outside :data:`LEGAL_TRANSITIONS` (including any move out of a terminal)."""


class ChangeReasonRequiredError(ReportLifecycleError):
    """A rerun (version > 1) was created without the nonblank ``change_reason`` the spec requires."""


class SectionValidationError(ReportLifecycleError):
    """A gate result whose sections cannot be persisted: bad order, blank title, or a stray ref."""


class SectionsAlreadyWrittenError(ReportLifecycleError):
    """Sections already exist for this report; a second write is refused, never reconciled."""


class CannotPublishError(ReportLifecycleError):
    """Publication refused: the gate blocked, a section is unpublishable, or the disclaimer is wrong."""


class ReportVersionConflictError(ReportLifecycleError):
    """The partial unique index rejected a version -- the advisory lock's backstop tripped."""


class StaleScanOverflowError(ReportLifecycleError):
    """The stale-dependency scan hit its bound; the result would be silently incomplete."""


class ReportReadOverflowError(ReportLifecycleError):
    """A bounded read found more rows than its limit; a bare truncated list would imply completeness."""


class GeneratedByRunIdConflictError(ReportLifecycleError):
    """A second, *different* ``generated_by_run_id`` was attached to a report that already carries one."""


# --------------------------------------------------------------------------------------
# Immutable, detached results (no ORM row, no session)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportSnapshot:
    """A report row copied to primitives at one moment. Frozen and detached on purpose."""

    id: uuid.UUID
    user_id: uuid.UUID | None
    report_type: str
    brief_date: datetime.date | None
    event_id: uuid.UUID | None
    title: str
    status: str
    version: int
    change_reason: str | None
    stale: bool
    generated_by_run_id: uuid.UUID | None
    created_at: datetime.datetime | None
    updated_at: datetime.datetime | None
    # NULL identifies legacy/unclassified reports. Gate-G-closed readers accept only the
    # explicit descriptive-only marker; they never infer policy from titles or prose.
    content_policy: str | None = None


@dataclass(frozen=True)
class SectionBlock:
    """One persisted claim-tagged block: its text and its cited claim ids as strings (JSON shape)."""

    text: str
    claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReportSectionRow:
    """A section mapped from the gate result, before it is written. Pure: no Session, testable alone."""

    section_order: int
    title: str
    body: str
    #: The generated blocks that survived grounding; empty for deterministic/withheld sections.
    blocks: tuple[SectionBlock, ...]
    #: Claim ids cited by ``blocks``, de-duplicated in first-occurrence order. Typed UUIDs, and by
    #: construction a subset of the blocks' ids -- never a fabricated or historical-episode id.
    evidence_refs: tuple[uuid.UUID, ...]
    grounding_status: str


@dataclass(frozen=True)
class ReportSectionSnapshot:
    """A persisted section copied to primitives. Frozen and detached."""

    id: uuid.UUID
    report_id: uuid.UUID
    section_order: int
    title: str
    body: str
    blocks: tuple[SectionBlock, ...]
    evidence_refs: tuple[uuid.UUID, ...]
    grounding_status: str


# --------------------------------------------------------------------------------------
# Pure state machine and gate -> rows mapping
# --------------------------------------------------------------------------------------


def validate_transition(current: ReportStatus, target: ReportStatus) -> None:
    """Raise :class:`IllegalTransitionError` unless ``(current, target)`` is a legal transition."""

    if (current, target) not in LEGAL_TRANSITIONS:
        raise IllegalTransitionError(
            f"illegal report transition {current.value!r} -> {target.value!r}"
        )


def generated_by_run_id_for(gate_result: GroundingGateResult) -> uuid.UUID | None:
    """The report's ``generated_by_run_id``: the first LLM run behind the brief, or ``None``.

    Deterministic and documented: :attr:`GroundingGateResult.llm_run_ids` is ordered by section,
    then attempt, so its first entry is the earliest composition run of the first generated
    section. A quiet brief makes no LLM call, so the tuple is empty and the report is left ``None``.
    """

    run_ids = gate_result.llm_run_ids
    return run_ids[0] if run_ids else None


def _section_row(result: SectionGroundingResult) -> ReportSectionRow:
    """Map one gate section to a persistable row: body, blocks, and block-derived evidence refs."""

    blocks = tuple(
        SectionBlock(text=block.text, claim_ids=tuple(str(cid) for cid in block.claim_ids))
        for block in result.final_blocks
    )
    # evidence_refs derives ONLY from the final blocks -- de-duplicated, first-occurrence order,
    # typed. A deterministic/withheld section has no blocks and so gets no citations, and an id
    # that was never in a block (a historical episode id, a risk key) can never appear here.
    seen: dict[uuid.UUID, None] = {}
    for block in result.final_blocks:
        for cid in block.claim_ids:
            seen.setdefault(cid, None)
    return ReportSectionRow(
        section_order=result.order,
        title=result.title,
        body=result.final_text,
        blocks=blocks,
        evidence_refs=tuple(seen),
        grounding_status=result.status.value,
    )


def section_rows_from_gate(gate_result: GroundingGateResult) -> tuple[ReportSectionRow, ...]:
    """Map a gate result to ordered, validated section rows. Pure: no Session, no mutation.

    Validates before it maps: section orders are unique and strictly increasing, every title is
    nonblank, every status is a known grounding status, and every evidence ref is a UUID actually
    cited by one of the section's final blocks. A failure raises before anything is written.
    """

    rows = tuple(_section_row(result) for result in gate_result.sections)
    previous_order: int | None = None
    for row in rows:
        if row.section_order < 1:
            raise SectionValidationError(f"section order must be >= 1, got {row.section_order}")
        if previous_order is not None and row.section_order <= previous_order:
            raise SectionValidationError(
                f"section orders must be unique and increasing; {row.section_order} follows "
                f"{previous_order}"
            )
        previous_order = row.section_order
        if not row.title.strip():
            raise SectionValidationError(f"section {row.section_order} has a blank title")
        if row.grounding_status not in GROUNDING_STATUSES:
            raise SectionValidationError(
                f"section {row.section_order} has an unknown grounding status "
                f"{row.grounding_status!r}"
            )
        cited = {cid for block in row.blocks for cid in block.claim_ids}
        if any(str(ref) not in cited for ref in row.evidence_refs):
            raise SectionValidationError(
                f"section {row.section_order} has an evidence ref not present in a final block"
            )
    return rows


def _validate_disclaimer(gate_result: GroundingGateResult) -> None:
    """The fixed disclaimer must be present, exactly last, and verbatim (spec S23.2). Never altered."""

    sections = gate_result.sections
    if not sections:
        raise CannotPublishError("cannot publish a brief with no sections")
    disclaimers = [s for s in sections if s.kind is SectionKind.DISCLAIMER]
    if len(disclaimers) != 1:
        raise CannotPublishError(
            f"a brief must carry exactly one disclaimer, found {len(disclaimers)}"
        )
    last = sections[-1]
    if last.kind is not SectionKind.DISCLAIMER:
        raise CannotPublishError("the disclaimer must be the last section")
    if last.final_text != FINAL_DISCLAIMER:
        raise CannotPublishError("the disclaimer text does not match FINAL_DISCLAIMER verbatim")


def _assert_publishable(gate_result: GroundingGateResult) -> None:
    """Fail closed unless the gate passed and every section is publishable. No mutation."""

    if gate_result.outcome is not GateOutcome.PASS:
        raise CannotPublishError("cannot publish a report whose grounding gate did not pass")
    for result in gate_result.sections:
        if result.blocks_publication or SectionGroundingStatus(result.status) not in (
            PUBLISHABLE_SECTION_STATUSES
        ):
            raise CannotPublishError(
                f"section {result.order} is not publishable (status {result.status})"
            )
    _validate_disclaimer(gate_result)


def default_daily_brief_title(brief_date: datetime.date) -> str:
    """A deterministic, non-model title for a daily brief. The generator may override it."""

    return f"Daily Brief — {brief_date.isoformat()}"


def _subject_lock_key(subject_token: str) -> int:
    """A stable signed int4 for the advisory lock: same subject -> same key, deterministically.

    A hash collision would only over-serialise two unrelated subjects (still correct); it can never
    let two allocators of the *same* subject race, which is the only property the lock must hold.
    """

    digest = hashlib.blake2b(subject_token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big", signed=True)


def _canonical_blocks(raw: object) -> tuple[SectionBlock, ...]:
    """Canonicalise a persisted ``blocks`` JSON value into comparable :class:`SectionBlock` rows.

    PostgreSQL returns the JSONB column as ``None``, a list of dicts, or (if tampered) anything
    else. A withheld/deterministic section stores ``NULL`` and maps to no blocks. Any other shape --
    a non-list, a non-dict element, a missing/non-string ``text``, a non-list ``claim_ids`` -- is a
    malformed row and is failed closed with :class:`CannotPublishError`, never an unrelated
    ``KeyError`` or ``TypeError``. Claim ids are stringified so the comparison matches the
    string-shaped ids :func:`section_rows_from_gate` produces.
    """

    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise CannotPublishError("a persisted section's blocks are malformed (not a list)")
    blocks: list[SectionBlock] = []
    for block in raw:
        if not isinstance(block, dict):
            raise CannotPublishError("a persisted section block is malformed (not an object)")
        text = block.get("text")
        claim_ids = block.get("claim_ids", ())
        if not isinstance(text, str) or not isinstance(claim_ids, (list, tuple)):
            raise CannotPublishError("a persisted section block is malformed (bad text/claim_ids)")
        blocks.append(SectionBlock(text=text, claim_ids=tuple(str(cid) for cid in claim_ids)))
    return tuple(blocks)


def _canonical_evidence_refs(raw: object) -> tuple[uuid.UUID, ...]:
    """Canonicalise a persisted ``evidence_refs`` array into comparable typed UUIDs, fail-closed.

    ``NULL`` maps to no refs; a list of UUIDs (or their string form) maps to the typed tuple, in the
    stored order. Any other shape is a malformed row and is failed closed rather than raising an
    unrelated error.
    """

    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise CannotPublishError("a persisted section's evidence_refs are malformed (not a list)")
    refs: list[uuid.UUID] = []
    for ref in raw:
        if isinstance(ref, uuid.UUID):
            refs.append(ref)
        elif isinstance(ref, str):
            try:
                refs.append(uuid.UUID(ref))
            except ValueError as exc:
                raise CannotPublishError(
                    "a persisted section has an evidence ref that is not a UUID"
                ) from exc
        else:
            raise CannotPublishError(
                "a persisted section has an evidence ref of an unexpected type"
            )
    return tuple(refs)


# --------------------------------------------------------------------------------------
# Repository -- flushes, never commits or rolls back
# --------------------------------------------------------------------------------------


class ReportLifecycleRepository:
    """Durable report lifecycle over a caller-owned :class:`~sqlalchemy.orm.Session`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- creation and version allocation ------------------------------------------------

    def create_generating_daily_brief(
        self,
        *,
        brief_date: datetime.date,
        title: str,
        change_reason: str | None = None,
        generated_by_run_id: uuid.UUID | None = None,
        content_policy: str | None = None,
    ) -> ReportSnapshot:
        """Create the next version of a daily brief in ``generating``, allocating under a lock.

        A daily brief is global (``user_id`` NULL), keyed by ``brief_date``. The first version is
        1 and carries no ``change_reason`` (any supplied is dropped -- there is no prior to change
        from); every later version is ``max + 1`` and requires a nonblank ``change_reason``. The
        advisory xact lock serialises concurrent allocators of the same ``brief_date`` so the
        ``max(version)`` read cannot race -- including for the first row, which no
        ``SELECT ... FOR UPDATE`` could have locked. Dropping a supplied reason on version 1 (rather
        than rejecting it) is what lets two from-scratch reruns race cleanly to versions 1 and 2.
        """
        if content_policy is not None and content_policy not in REPORT_CONTENT_POLICIES:
            raise ValueError(f"unknown report content policy: {content_policy}")

        self._acquire_subject_lock(_daily_subject_token(brief_date))
        current_max = self._session.execute(
            select(func.max(Report.version)).where(
                Report.report_type == DAILY_BRIEF_REPORT_TYPE,
                Report.brief_date == brief_date,
            )
        ).scalar_one()
        version = (current_max or 0) + 1

        reason = self._resolve_change_reason(version, change_reason)
        report = Report(
            user_id=None,
            report_type=DAILY_BRIEF_REPORT_TYPE,
            brief_date=brief_date,
            event_id=None,
            title=title,
            status=ReportStatus.GENERATING.value,
            version=version,
            change_reason=reason,
            stale=False,
            generated_by_run_id=generated_by_run_id,
            content_policy=content_policy,
        )
        self._session.add(report)
        try:
            self._session.flush()
        except IntegrityError as exc:  # the advisory lock failed us; the unique index caught it
            raise ReportVersionConflictError(
                f"version {version} already exists for daily brief {brief_date.isoformat()}"
            ) from exc
        return _report_snapshot(report)

    def _acquire_subject_lock(self, subject_token: str) -> None:
        """Take the transaction-scoped advisory lock for one subject; released at commit/rollback."""

        # Both args cast to int4 so PostgreSQL resolves the two-argument overload unambiguously
        # (a bare bind could be taken as bigint, for which no two-arg overload exists).
        self._session.execute(
            select(
                func.pg_advisory_xact_lock(
                    cast(_ADVISORY_LOCK_NAMESPACE, Integer),
                    cast(_subject_lock_key(subject_token), Integer),
                )
            )
        )

    @staticmethod
    def _resolve_change_reason(version: int, change_reason: str | None) -> str | None:
        if version == 1:
            return (
                None  # the first version has no prior to change from; a supplied reason is dropped
            )
        if change_reason is None or not change_reason.strip():
            raise ChangeReasonRequiredError(
                f"version {version} is a rerun and requires a nonblank change_reason"
            )
        return change_reason

    # -- transitions --------------------------------------------------------------------

    def transition(self, report_id: uuid.UUID, target: ReportStatus) -> ReportSnapshot:
        """Move a report to ``target`` if legal. Publication has its own guarded path.

        ``target == published`` is refused here: publishing must go through :meth:`publish`, which
        enforces the gate/disclaimer preconditions. Every other legal transition is applied under a
        row lock so concurrent lifecycle writers serialise.
        """

        if target is ReportStatus.PUBLISHED:
            raise IllegalTransitionError(
                "reach 'published' through publish(), which enforces the publication gates"
            )
        report = self._locked_report(report_id)
        validate_transition(ReportStatus(report.status), target)
        report.status = target.value
        self._session.flush()
        return _report_snapshot(report)

    def attach_generated_by_run_id(self, report_id: uuid.UUID, run_id: uuid.UUID) -> ReportSnapshot:
        """Record which LLM run produced a report, once, while it is still pre-publish.

        A report is created ``generating`` before any model runs, so its ``generated_by_run_id`` is
        not known until the grounding gate has run; the generator attaches it afterwards, in the
        same transaction, before the report publishes. Deliberately narrow, and enforced under a row
        lock:

        * Legal only in ``generating`` or ``grounding_check``. A terminal report is immutable, so an
          attach against ``published``/``failed`` is refused (:class:`IllegalTransitionError`) -- a
          published report's provenance can never be rewritten.
        * A write only from ``None``. Re-attaching the *same* id is idempotent; attaching a
          *different* id over an existing one is refused
          (:class:`GeneratedByRunIdConflictError`), so a run id cannot be swapped after the fact.

        Flushes so the caller sees the write; never commits. A quiet brief made no run, so the
        generator simply does not call this.
        """

        report = self._locked_report(report_id)
        status = ReportStatus(report.status)
        if status in TERMINAL_STATUSES:
            raise IllegalTransitionError(
                f"cannot attach generated_by_run_id to a terminal report (status {status.value!r})"
            )
        existing = report.generated_by_run_id
        if existing is not None and existing != run_id:
            raise GeneratedByRunIdConflictError(
                f"report {report_id} already has generated_by_run_id {existing}; refusing to "
                f"overwrite it with {run_id}"
            )
        report.generated_by_run_id = run_id
        self._session.flush()
        return _report_snapshot(report)

    # -- section persistence ------------------------------------------------------------

    def persist_sections(
        self, report_id: uuid.UUID, gate_result: GroundingGateResult
    ) -> tuple[ReportSectionSnapshot, ...]:
        """Write a gate result's sections onto a report in ``grounding_check``, exactly once.

        The report and gate must agree on ``brief_date``; the report must be at the gate and hold no
        sections yet (a second write is refused, not reconciled). Blocked gates are allowed through
        here -- their audit-safe section outcomes are recorded -- but such a report can only be
        failed, never published.
        """

        report = self._locked_report(report_id)
        if report.status != ReportStatus.GROUNDING_CHECK.value:
            raise IllegalTransitionError(
                f"sections may only be written in 'grounding_check', not {report.status!r}"
            )
        if report.brief_date != gate_result.brief_date:
            raise SectionValidationError(
                f"report brief_date {report.brief_date} does not match gate "
                f"{gate_result.brief_date}"
            )
        existing = self._session.execute(
            select(func.count())
            .select_from(ReportSection)
            .where(ReportSection.report_id == report_id)
        ).scalar_one()
        if existing:
            raise SectionsAlreadyWrittenError(
                f"report {report_id} already has {existing} section(s); refusing to rewrite"
            )

        rows = section_rows_from_gate(gate_result)
        self._assert_evidence_refs_are_claims(rows)
        for row in rows:
            self._session.add(
                ReportSection(
                    report_id=report_id,
                    section_order=row.section_order,
                    title=row.title,
                    body=row.body,
                    blocks=(
                        [{"text": b.text, "claim_ids": list(b.claim_ids)} for b in row.blocks]
                        if row.blocks
                        else None
                    ),
                    evidence_refs=list(row.evidence_refs) if row.evidence_refs else None,
                    grounding_status=row.grounding_status,
                )
            )
        self._session.flush()
        return self.report_sections(report_id)

    def _assert_evidence_refs_are_claims(self, rows: tuple[ReportSectionRow, ...]) -> None:
        """Require every distinct evidence ref to be a real Claim id before any section is written.

        ``ReportSection.evidence_refs`` is ``ARRAY(UUID)`` with no foreign key, so a UUID that came
        off a block but is not a Claim -- a historical-episode id, say -- would otherwise persist
        silently. This queries the ``claims`` table for the distinct refs and refuses the whole
        write (``SectionValidationError``, no rows added, no status change) unless coverage is exact.
        A brief that cites nothing (quiet/deterministic) makes no query.
        """

        refs = {ref for row in rows for ref in row.evidence_refs}
        if not refs:
            return
        found = set(
            self._session.execute(select(Claim.id).where(Claim.id.in_(refs))).scalars().all()
        )
        missing = refs - found
        if missing:
            listed = ", ".join(sorted(str(ref) for ref in missing))
            raise SectionValidationError(
                f"{len(missing)} evidence ref(s) are not Claim ids and cannot be cited: {listed}"
            )

    def publish(self, report_id: uuid.UUID, gate_result: GroundingGateResult) -> ReportSnapshot:
        """Publish a grounded report, fail-closed. Content is never recomposed or altered here.

        Refuses unless the gate passed, every section is publishable, and the fixed disclaimer is
        present, last, and verbatim; and unless the persisted sections are exactly the gate's, in
        order. Only then does ``grounding_check -> published`` apply.
        """

        report = self._locked_report(report_id)
        validate_transition(ReportStatus(report.status), ReportStatus.PUBLISHED)
        if report.brief_date != gate_result.brief_date:
            raise SectionValidationError(
                f"report brief_date {report.brief_date} does not match gate "
                f"{gate_result.brief_date}"
            )
        _assert_publishable(gate_result)
        self._assert_sections_match(report_id, gate_result)
        report.status = ReportStatus.PUBLISHED.value
        self._session.flush()
        return _report_snapshot(report)

    def _assert_sections_match(
        self, report_id: uuid.UUID, gate_result: GroundingGateResult
    ) -> None:
        """Validate the *persisted* rows against the gate (spec: no recomposition), fail-closed.

        The persisted rows are the source of truth, not the gate argument: a report is publishable
        only if what is actually stored is exactly the gate's sections -- every field of every
        section, in order: ``section_order``, ``title``, ``body``, the canonical claim-tagged
        ``blocks`` (text and ordered claim ids), the stable ``evidence_refs``, and
        ``grounding_status``. Comparing only orders/statuses/the disclaimer body left the door open
        to publishing tampered title/body/blocks/refs or a different passing gate's content; this
        closes it. An extra persisted row beyond the mapped sections, or more rows than the read
        bound, is refused rather than truncated. Publishability of each row follows for free: the
        gate was already checked publishable, so an exact match is publishable too.
        """

        expected = section_rows_from_gate(gate_result)
        persisted = self._session.execute(
            select(
                ReportSection.section_order,
                ReportSection.title,
                ReportSection.body,
                ReportSection.blocks,
                ReportSection.evidence_refs,
                ReportSection.grounding_status,
            )
            .where(ReportSection.report_id == report_id)
            .order_by(ReportSection.section_order)
            .limit(
                SECTION_LIST_LIMIT + 1
            )  # over-read by one: an overflow must fail loud, not truncate
        ).all()
        if len(persisted) > SECTION_LIST_LIMIT:
            raise ReportReadOverflowError(
                f"report {report_id} has more than {SECTION_LIST_LIMIT} sections; refusing to "
                "publish against a truncated read"
            )
        if len(persisted) != len(expected):
            raise CannotPublishError(
                "persisted sections do not match the gate result being published"
            )
        for row, want in zip(persisted, expected, strict=True):
            stored = ReportSectionRow(
                section_order=row.section_order,
                title=row.title,
                body=row.body,
                blocks=_canonical_blocks(row.blocks),
                evidence_refs=_canonical_evidence_refs(row.evidence_refs),
                grounding_status=row.grounding_status,
            )
            if stored != want:
                raise CannotPublishError(
                    f"persisted section {row.section_order} does not match the gate result "
                    "being published"
                )

    # -- stale marking ------------------------------------------------------------------

    def mark_published_reports_stale_for_event(self, event_id: uuid.UUID) -> int:
        """Mark every published report that depends on ``event_id`` stale. Idempotent; deterministic.

        Two dependencies are marked: a report directly about the event (``Report.event_id``), and a
        published daily brief that cites a claim resolving -- through ``claim_evidence`` to an
        ``article``-typed evidence item, and through that item's ``source_id`` to one of the event's
        ``event_articles`` -- back to this event. Content, unpublished reports, and reports about
        other events are never touched. The count returned is the number of dependent published
        reports, so repeated calls report the same number and change nothing further.
        """

        ids = self._dependent_published_report_ids(event_id)
        if ids:
            reports = (
                self._session.execute(
                    select(Report).where(Report.id.in_(ids)).order_by(Report.id).with_for_update()
                )
                .scalars()
                .all()
            )
            for report in reports:
                if not report.stale:  # false -> true only; never rewrite an already-stale row
                    report.stale = True
            self._session.flush()
        return len(ids)

    def _dependent_published_report_ids(self, event_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        """The distinct published report ids that depend on ``event_id``, ordered. Bounded."""

        brief_stmt = (
            select(Report.id)
            .select_from(EventArticle)
            .join(
                EvidenceItem,
                and_(
                    EvidenceItem.source_type == ARTICLE_EVIDENCE_SOURCE_TYPE,
                    EvidenceItem.source_id == cast(EventArticle.article_id, Text),
                ),
            )
            .join(ClaimEvidence, ClaimEvidence.evidence_item_id == EvidenceItem.id)
            .join(ReportSection, ReportSection.evidence_refs.any(ClaimEvidence.claim_id))
            .join(Report, Report.id == ReportSection.report_id)
            .where(
                EventArticle.event_id == event_id,
                Report.report_type == DAILY_BRIEF_REPORT_TYPE,
                Report.status == PUBLISHED_STATUS,
            )
            .distinct()
            .order_by(Report.id)
            .limit(STALE_SCAN_LIMIT)
        )
        event_stmt = (
            select(Report.id)
            .where(
                Report.event_id == event_id,
                Report.status == PUBLISHED_STATUS,
                Report.report_type != PERSONAL_REPORT_TYPE,
            )
            .order_by(Report.id)
            .limit(STALE_SCAN_LIMIT)
        )
        brief_ids = self._session.execute(brief_stmt).scalars().all()
        event_ids = self._session.execute(event_stmt).scalars().all()
        if len(brief_ids) >= STALE_SCAN_LIMIT or len(event_ids) >= STALE_SCAN_LIMIT:
            raise StaleScanOverflowError(
                f"stale dependency scan for event {event_id} reached its {STALE_SCAN_LIMIT}-row "
                "bound; the result would be incomplete"
            )
        merged: dict[uuid.UUID, None] = {}
        for report_id in (*brief_ids, *event_ids):
            merged.setdefault(report_id, None)
        return tuple(sorted(merged))

    # -- reads (bounded, totally ordered, detached) -------------------------------------

    def get_report(self, report_id: uuid.UUID) -> ReportSnapshot | None:
        report = self._session.get(Report, report_id)
        return _report_snapshot(report) if report is not None else None

    def latest_published_daily_brief_by_date(
        self,
        brief_date: datetime.date,
        *,
        content_policy: str | None = None,
    ) -> ReportSnapshot | None:
        """The highest-version published brief for a date -- the one served by default.

        Ignores ``generating``/``grounding_check``/``failed`` versions; a stale published version is
        still returned and stays the served one until a *newer* version is published.
        """

        stmt = select(Report).where(
            Report.report_type == DAILY_BRIEF_REPORT_TYPE,
            Report.brief_date == brief_date,
            Report.status == PUBLISHED_STATUS,
        )
        if content_policy is not None:
            stmt = stmt.where(Report.content_policy == content_policy)
        stmt = stmt.order_by(Report.version.desc()).limit(1)
        report = self._session.execute(stmt).scalars().first()
        return _report_snapshot(report) if report is not None else None

    def latest_published_daily_brief(
        self,
        *,
        content_policy: str | None = None,
    ) -> ReportSnapshot | None:
        """The most recent published brief overall: newest ``brief_date``, then highest version."""

        stmt = select(Report).where(
            Report.report_type == DAILY_BRIEF_REPORT_TYPE,
            Report.status == PUBLISHED_STATUS,
        )
        if content_policy is not None:
            stmt = stmt.where(Report.content_policy == content_policy)
        stmt = stmt.order_by(Report.brief_date.desc(), Report.version.desc()).limit(1)
        report = self._session.execute(stmt).scalars().first()
        return _report_snapshot(report) if report is not None else None

    def daily_brief_version(
        self,
        brief_date: datetime.date,
        version: int,
        *,
        content_policy: str | None = None,
    ) -> ReportSnapshot | None:
        """One specific version of a brief, whatever its status."""

        stmt = select(Report).where(
            Report.report_type == DAILY_BRIEF_REPORT_TYPE,
            Report.brief_date == brief_date,
            Report.version == version,
        )
        if content_policy is not None:
            stmt = stmt.where(Report.content_policy == content_policy)
        stmt = stmt.limit(1)
        report = self._session.execute(stmt).scalars().first()
        return _report_snapshot(report) if report is not None else None

    def daily_brief_versions(
        self,
        brief_date: datetime.date,
        *,
        content_policy: str | None = None,
    ) -> tuple[ReportSnapshot, ...]:
        """Every version of a brief, newest first. Bounded, totally ordered, fail-loud on overflow.

        Over-reads by one and raises :class:`ReportReadOverflowError` if more than
        :data:`VERSION_LIST_LIMIT` versions exist, so a truncated list is never mistaken for the
        whole history. Exactly ``VERSION_LIST_LIMIT`` versions is returned in full, not refused.
        """

        stmt = select(Report).where(
            Report.report_type == DAILY_BRIEF_REPORT_TYPE,
            Report.brief_date == brief_date,
        )
        if content_policy is not None:
            stmt = stmt.where(Report.content_policy == content_policy)
        stmt = stmt.order_by(Report.version.desc()).limit(VERSION_LIST_LIMIT + 1)
        reports = self._session.execute(stmt).scalars().all()
        if len(reports) > VERSION_LIST_LIMIT:
            raise ReportReadOverflowError(
                f"daily brief {brief_date.isoformat()} has more than {VERSION_LIST_LIMIT} "
                "versions; refusing to return a truncated list"
            )
        return tuple(_report_snapshot(report) for report in reports)

    def report_sections(self, report_id: uuid.UUID) -> tuple[ReportSectionSnapshot, ...]:
        """A report's sections, in section order. Bounded, detached, fail-loud on overflow.

        Over-reads by one and raises :class:`ReportReadOverflowError` if more than
        :data:`SECTION_LIST_LIMIT` sections exist, rather than returning a silently truncated body.
        Exactly ``SECTION_LIST_LIMIT`` sections is returned in full, not refused.
        """

        stmt = (
            select(ReportSection)
            .where(ReportSection.report_id == report_id)
            .order_by(ReportSection.section_order)
            .limit(SECTION_LIST_LIMIT + 1)
        )
        sections = self._session.execute(stmt).scalars().all()
        if len(sections) > SECTION_LIST_LIMIT:
            raise ReportReadOverflowError(
                f"report {report_id} has more than {SECTION_LIST_LIMIT} sections; refusing to "
                "return a truncated list"
            )
        return tuple(_section_snapshot(section) for section in sections)

    # -- internals ----------------------------------------------------------------------

    def _locked_report(self, report_id: uuid.UUID) -> Report:
        report = self._session.get(Report, report_id, with_for_update=True)
        if report is None:
            raise ReportNotFoundError(f"no report {report_id} in this transaction")
        return report


# --------------------------------------------------------------------------------------
# Safe persistence sequence (usable by the generator item that follows)
# --------------------------------------------------------------------------------------


def persist_daily_brief(
    session: Session,
    gate_result: GroundingGateResult,
    *,
    title: str | None = None,
    change_reason: str | None = None,
    content_policy: str,
) -> ReportSnapshot:
    """Create, ground-check, persist, and publish-or-fail one daily brief from its gate result.

    ``content_policy`` is deliberately required: while Gate G is closed, a brief stamped
    ``prediction_backed.v1`` is withheld from ``/reports``, so the caller — not a silent
    default — must decide how each brief is classified (the production generator derives it
    from ``prediction_backed_outputs_enabled``).

    The one sequence the generator calls: allocate a ``generating`` version, advance it to
    ``grounding_check``, write the gate's sections, then publish if the gate passed or fail if it
    blocked. Nothing is recomposed. ``generated_by_run_id`` is taken from the gate's own run ids
    (:func:`generated_by_run_id_for`); a quiet brief leaves it ``None``. Flushes throughout; the
    caller owns the commit.
    """

    repo = ReportLifecycleRepository(session)
    report = repo.create_generating_daily_brief(
        brief_date=gate_result.brief_date,
        title=title or default_daily_brief_title(gate_result.brief_date),
        change_reason=change_reason,
        generated_by_run_id=generated_by_run_id_for(gate_result),
        content_policy=content_policy,
    )
    repo.transition(report.id, ReportStatus.GROUNDING_CHECK)
    repo.persist_sections(report.id, gate_result)
    if gate_result.outcome is GateOutcome.PASS:
        return repo.publish(report.id, gate_result)
    return repo.transition(report.id, ReportStatus.FAILED)


# --------------------------------------------------------------------------------------
# Snapshot builders and subject tokens (pure)
# --------------------------------------------------------------------------------------


def _daily_subject_token(brief_date: datetime.date) -> str:
    """The version-allocation subject for a daily brief: its type and date, nothing user-scoped."""

    return f"{DAILY_BRIEF_REPORT_TYPE}:{brief_date.isoformat()}"


def _report_snapshot(report: Report) -> ReportSnapshot:
    return ReportSnapshot(
        id=report.id,
        user_id=report.user_id,
        report_type=report.report_type,
        brief_date=report.brief_date,
        event_id=report.event_id,
        title=report.title,
        status=report.status,
        version=report.version,
        change_reason=report.change_reason,
        stale=report.stale,
        generated_by_run_id=report.generated_by_run_id,
        created_at=report.created_at,
        updated_at=report.updated_at,
        content_policy=report.content_policy,
    )


def _section_snapshot(section: ReportSection) -> ReportSectionSnapshot:
    raw_blocks = section.blocks or ()
    blocks = tuple(
        SectionBlock(
            text=block["text"],
            claim_ids=tuple(str(cid) for cid in block.get("claim_ids", ())),
        )
        for block in raw_blocks
    )
    return ReportSectionSnapshot(
        id=section.id,
        report_id=section.report_id,
        section_order=section.section_order,
        title=section.title,
        body=section.body,
        blocks=blocks,
        evidence_refs=tuple(section.evidence_refs or ()),
        grounding_status=section.grounding_status,
    )


__all__ = [
    "LEGAL_TRANSITIONS",
    "PUBLISHABLE_SECTION_STATUSES",
    "SECTION_LIST_LIMIT",
    "STALE_SCAN_LIMIT",
    "TERMINAL_STATUSES",
    "VERSION_LIST_LIMIT",
    "CannotPublishError",
    "ChangeReasonRequiredError",
    "GeneratedByRunIdConflictError",
    "IllegalTransitionError",
    "ReportLifecycleError",
    "ReportLifecycleRepository",
    "ReportNotFoundError",
    "ReportReadOverflowError",
    "ReportSectionRow",
    "ReportSectionSnapshot",
    "ReportSnapshot",
    "ReportStatus",
    "ReportVersionConflictError",
    "SectionBlock",
    "SectionsAlreadyWrittenError",
    "SectionValidationError",
    "StaleScanOverflowError",
    "default_daily_brief_title",
    "generated_by_run_id_for",
    "persist_daily_brief",
    "section_rows_from_gate",
    "validate_transition",
]
