"""Unit tests for the Stage 6 report lifecycle: transitions, versioning, mapping, publication.

DB-free. The pure state machine and the gate -> rows mapping are tested directly; the repository's
control flow (version allocation, duplicate-write refusal, stale marking, and the fact that it
never commits or rolls back) is tested against a recording fake session that captures the SQL it
issues without a Postgres. The join/lock *execution* is proven in the disposable-Postgres
integration test; here we assert the SQL *shape*.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from db.models.core import Report, ReportSection
from services.reports.composition import CompositionAttempt, DraftBlock, compose_brief
from services.reports.grounding import (
    GateOutcome,
    GroundingGateResult,
    SectionGroundingResult,
    SectionGroundingStatus,
    run_grounding_gate,
)
from services.reports.lifecycle import (
    LEGAL_TRANSITIONS,
    SECTION_LIST_LIMIT,
    VERSION_LIST_LIMIT,
    CannotPublishError,
    ChangeReasonRequiredError,
    GeneratedByRunIdConflictError,
    IllegalTransitionError,
    ReportLifecycleRepository,
    ReportReadOverflowError,
    ReportStatus,
    SectionsAlreadyWrittenError,
    SectionValidationError,
    _assert_publishable,
    default_daily_brief_title,
    generated_by_run_id_for,
    persist_daily_brief,
    section_rows_from_gate,
    validate_transition,
)
from services.reports.material import FINAL_DISCLAIMER, SectionKind, build_brief_material
from tests.unit._report_grounding_fixtures import (
    CLAIM_ID,
    CLAIM_ID_2,
    EPISODE_ID,
    GateOrchestrator,
    brief_context,
    brief_inputs,
    claim,
    single_event_brief,
)

BRIEF_DATE = datetime.date(2026, 7, 14)


# --------------------------------------------------------------------------------------
# Gate-result builders
# --------------------------------------------------------------------------------------


def _section(
    kind: SectionKind,
    order: int,
    title: str,
    *,
    status: SectionGroundingStatus = SectionGroundingStatus.PASSED,
    blocks: tuple[tuple[str, tuple[uuid.UUID, ...]], ...] = (),
    final_text: str | None = None,
    blocks_publication: bool = False,
) -> SectionGroundingResult:
    final_blocks = tuple(DraftBlock(text=text, claim_ids=cids) for text, cids in blocks)
    text = final_text if final_text is not None else "\n\n".join(b.text for b in final_blocks)
    return SectionGroundingResult(
        kind=kind,
        order=order,
        title=title,
        status=status,
        final_text=text,
        final_blocks=final_blocks,
        verdicts=(),
        copyright_findings=(),
        consistency_findings=(),
        composition_attempts=(),
        regeneration_attempts=(),
        grounding_attempts=(),
        claims_before=0,
        claims_after=0,
        regenerated=False,
        blocks_publication=blocks_publication,
        transformations=(),
    )


def _disclaimer(order: int, *, text: str = FINAL_DISCLAIMER) -> SectionGroundingResult:
    return _section(SectionKind.DISCLAIMER, order, "Disclaimer", final_text=text)


def _gate(*sections: SectionGroundingResult, outcome: GateOutcome = GateOutcome.PASS) -> GroundingGateResult:
    return GroundingGateResult(brief_date=BRIEF_DATE, sections=sections, outcome=outcome)


def _real_gate(**kwargs) -> GroundingGateResult:
    """A gate result from the real composition + grounding path (healthy, all-supported)."""
    orch = GateOrchestrator()
    inputs, context, material = single_event_brief(claims=(claim(),), **kwargs)
    draft = compose_brief(orch, inputs=inputs, context=context, material=material)
    return run_grounding_gate(orch, inputs=inputs, context=context, draft=draft)


def _quiet_gate() -> GroundingGateResult:
    """A zero-event quiet brief: no generated section, no LLM run, a valid PASS."""
    orch = GateOrchestrator()
    inputs = brief_inputs(top_events=())
    context = brief_context()
    material = build_brief_material(inputs, context)
    draft = compose_brief(orch, inputs=inputs, context=context, material=material)
    return run_grounding_gate(orch, inputs=inputs, context=context, draft=draft)


def _comp_attempt(run_id: uuid.UUID | None) -> CompositionAttempt:
    return CompositionAttempt(
        attempt=1,
        prompt_name="p",
        prompt_version="1",
        prompt_template_version="1",
        schema_name="s",
        schema_version="1",
        requested_tier="T2",
        actual_tier="T2",
        tier_degraded=False,
        route_degradation_reasons=(),
        degraded_provider=None,
        mode="realtime",
        queue="essential",
        cache_hit=False,
        trace_id="t",
        llm_run_id=run_id,
        word_count=10,
        within_budget=True,
    )


# --------------------------------------------------------------------------------------
# Transition matrix, terminal states, published immutability
# --------------------------------------------------------------------------------------


def test_transition_matrix_allows_exactly_the_legal_pairs() -> None:
    for current in ReportStatus:
        for target in ReportStatus:
            if (current, target) in LEGAL_TRANSITIONS:
                validate_transition(current, target)  # does not raise
            else:
                with pytest.raises(IllegalTransitionError):
                    validate_transition(current, target)


def test_the_legal_set_is_exactly_the_spec() -> None:
    assert LEGAL_TRANSITIONS == {
        (ReportStatus.GENERATING, ReportStatus.GROUNDING_CHECK),
        (ReportStatus.GENERATING, ReportStatus.FAILED),
        (ReportStatus.GROUNDING_CHECK, ReportStatus.PUBLISHED),
        (ReportStatus.GROUNDING_CHECK, ReportStatus.FAILED),
    }


@pytest.mark.parametrize("terminal", [ReportStatus.PUBLISHED, ReportStatus.FAILED])
def test_terminal_states_have_no_outgoing_transition(terminal: ReportStatus) -> None:
    for target in ReportStatus:
        with pytest.raises(IllegalTransitionError):
            validate_transition(terminal, target)


def test_no_draft_state_exists() -> None:
    assert {s.value for s in ReportStatus} == {
        "generating",
        "grounding_check",
        "published",
        "failed",
    }


# --------------------------------------------------------------------------------------
# Gate -> section-row mapping
# --------------------------------------------------------------------------------------


def test_mapping_carries_body_blocks_and_status_in_section_order() -> None:
    gate = _gate(
        _section(
            SectionKind.EXECUTIVE_SUMMARY,
            1,
            "Executive Summary",
            blocks=((("Rates held."), (CLAIM_ID,)),),
        ),
        _section(SectionKind.RISK_RADAR, 2, "Risk Radar", final_text="a table"),
        _disclaimer(3),
    )
    rows = section_rows_from_gate(gate)

    assert [r.section_order for r in rows] == [1, 2, 3]
    exec_row = rows[0]
    assert exec_row.body == "Rates held."
    assert exec_row.blocks[0].text == "Rates held."
    assert exec_row.blocks[0].claim_ids == (str(CLAIM_ID),)  # claim ids serialised as strings
    assert exec_row.evidence_refs == (CLAIM_ID,)  # typed UUIDs
    assert exec_row.grounding_status == "passed"

    # A deterministic section fabricates no citation.
    radar_row = rows[1]
    assert radar_row.blocks == ()
    assert radar_row.evidence_refs == ()
    assert radar_row.body == "a table"


def test_evidence_refs_are_stable_and_de_duplicated_first_occurrence() -> None:
    gate = _gate(
        _section(
            SectionKind.TOP_EVENT,
            1,
            "Event",
            blocks=(
                ("block one", (CLAIM_ID, CLAIM_ID_2)),
                ("block two", (CLAIM_ID_2, CLAIM_ID)),
            ),
        ),
        _disclaimer(2),
    )
    rows = section_rows_from_gate(gate)
    assert rows[0].evidence_refs == (CLAIM_ID, CLAIM_ID_2)


def test_evidence_refs_never_include_a_non_block_id_such_as_an_episode() -> None:
    # The section's blocks cite only claim ids; a historical episode id is never in a block, so it
    # cannot reach evidence_refs, which is derived from the blocks alone.
    gate = _gate(
        _section(
            SectionKind.HISTORICAL_PARALLELS,
            1,
            "Historical Parallels",
            blocks=(("parallel", (CLAIM_ID,)),),
        ),
        _disclaimer(2),
    )
    rows = section_rows_from_gate(gate)
    assert rows[0].evidence_refs == (CLAIM_ID,)
    assert EPISODE_ID not in rows[0].evidence_refs


def test_mapping_rejects_non_increasing_orders() -> None:
    gate = _gate(
        _section(SectionKind.EXECUTIVE_SUMMARY, 1, "Exec", blocks=(("x", (CLAIM_ID,)),)),
        _section(SectionKind.TOP_EVENT, 1, "Dup order", blocks=(("y", (CLAIM_ID,)),)),
        _disclaimer(3),
    )
    with pytest.raises(SectionValidationError):
        section_rows_from_gate(gate)


def test_mapping_rejects_a_blank_title() -> None:
    gate = _gate(_section(SectionKind.RISK_RADAR, 1, "   ", final_text="t"), _disclaimer(2))
    with pytest.raises(SectionValidationError):
        section_rows_from_gate(gate)


def test_a_real_gate_maps_cleanly_with_a_disclaimer_last() -> None:
    gate = _real_gate()
    rows = section_rows_from_gate(gate)
    assert rows[-1].body == FINAL_DISCLAIMER
    assert [r.section_order for r in rows] == sorted(r.section_order for r in rows)
    assert gate.sections[-1].kind is SectionKind.DISCLAIMER


# --------------------------------------------------------------------------------------
# Publication gate (pure): blocked, disclaimer, publishable statuses
# --------------------------------------------------------------------------------------


def test_a_passing_real_gate_is_publishable() -> None:
    _assert_publishable(_real_gate())  # does not raise


def test_a_blocked_gate_is_never_publishable() -> None:
    gate = _gate(
        _section(
            SectionKind.EXECUTIVE_SUMMARY,
            1,
            "Exec",
            status=SectionGroundingStatus.FAILED,
            final_text="[grounding_failed] ...",
            blocks_publication=True,
        ),
        _disclaimer(2),
        outcome=GateOutcome.BLOCKED,
    )
    with pytest.raises(CannotPublishError):
        _assert_publishable(gate)


def test_publish_refused_when_a_section_status_is_not_publishable() -> None:
    gate = _gate(
        _section(SectionKind.EXECUTIVE_SUMMARY, 1, "Exec", status=SectionGroundingStatus.PENDING),
        _disclaimer(2),
        outcome=GateOutcome.PASS,
    )
    with pytest.raises(CannotPublishError):
        _assert_publishable(gate)


def test_data_quality_note_sections_are_publishable() -> None:
    gate = _gate(
        _section(
            SectionKind.RISK_RADAR,
            1,
            "Risk Radar",
            status=SectionGroundingStatus.DATA_QUALITY_NOTE,
            final_text="[data_quality_note] withheld",
        ),
        _disclaimer(2),
    )
    _assert_publishable(gate)  # does not raise


def test_disclaimer_must_be_present_last_and_verbatim() -> None:
    # Missing (last section is not the disclaimer).
    with pytest.raises(CannotPublishError):
        _assert_publishable(_gate(_section(SectionKind.RISK_RADAR, 1, "R", final_text="t")))
    # Altered text.
    with pytest.raises(CannotPublishError):
        _assert_publishable(
            _gate(
                _section(SectionKind.RISK_RADAR, 1, "R", final_text="t"),
                _disclaimer(2, text=FINAL_DISCLAIMER + " Buy stocks."),
            )
        )
    # Not last.
    with pytest.raises(CannotPublishError):
        _assert_publishable(
            _gate(_disclaimer(1), _section(SectionKind.RISK_RADAR, 2, "R", final_text="t"))
        )


# --------------------------------------------------------------------------------------
# generated_by_run_id choice and quiet brief
# --------------------------------------------------------------------------------------


def test_generated_by_run_id_is_the_first_run_in_section_then_attempt_order() -> None:
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    gate = _gate(
        SectionGroundingResult(
            kind=SectionKind.EXECUTIVE_SUMMARY,
            order=1,
            title="Exec",
            status=SectionGroundingStatus.PASSED,
            final_text="x",
            final_blocks=(DraftBlock(text="x", claim_ids=(CLAIM_ID,)),),
            verdicts=(),
            copyright_findings=(),
            consistency_findings=(),
            composition_attempts=(_comp_attempt(run_a),),
            regeneration_attempts=(),
            grounding_attempts=(),
            claims_before=1,
            claims_after=1,
            regenerated=False,
            blocks_publication=False,
            transformations=(),
        ),
        SectionGroundingResult(
            kind=SectionKind.TOP_EVENT,
            order=2,
            title="Event",
            status=SectionGroundingStatus.PASSED,
            final_text="y",
            final_blocks=(DraftBlock(text="y", claim_ids=(CLAIM_ID,)),),
            verdicts=(),
            copyright_findings=(),
            consistency_findings=(),
            composition_attempts=(_comp_attempt(run_b),),
            regeneration_attempts=(),
            grounding_attempts=(),
            claims_before=1,
            claims_after=1,
            regenerated=False,
            blocks_publication=False,
            transformations=(),
        ),
        _disclaimer(3),
    )
    assert generated_by_run_id_for(gate) == run_a


def test_a_quiet_brief_leaves_generated_by_run_id_none_and_passes() -> None:
    gate = _quiet_gate()
    assert generated_by_run_id_for(gate) is None
    assert gate.outcome is GateOutcome.PASS
    rows = section_rows_from_gate(gate)
    assert all(r.evidence_refs == () for r in rows)  # nothing citable, nothing fabricated


def test_default_title_is_deterministic_and_non_model() -> None:
    assert default_daily_brief_title(BRIEF_DATE) == "Daily Brief — 2026-07-14"


# --------------------------------------------------------------------------------------
# Recording fake session: version allocation, duplicate write, stale, never-commit
# --------------------------------------------------------------------------------------


class _Result:
    def __init__(self, *, value=None, rows: list | None = None) -> None:
        self._value = value
        self._rows = rows or []

    def scalar_one(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class RecordingSession:
    """A fake Session that captures issued statements and refuses to commit or roll back."""

    def __init__(self, *, get_result=None, results: list[_Result] | None = None) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self.added: list = []
        self.executed: list = []
        self._get_result = get_result
        self._results = list(results or [])

    def execute(self, statement):
        self.executed.append(statement)
        if "pg_advisory_xact_lock" in str(statement):
            return _Result()  # lock result is ignored; does not consume the queue
        return self._results.pop(0) if self._results else _Result()

    def get(self, model, ident, with_for_update=False):
        return self._get_result

    def add(self, obj) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:  # pragma: no cover - asserted never to run
        self.commits += 1

    def rollback(self) -> None:  # pragma: no cover - asserted never to run
        self.rollbacks += 1


def _assert_never_committed(session: RecordingSession) -> None:
    assert session.commits == 0
    assert session.rollbacks == 0


def test_first_version_is_one_with_no_change_reason() -> None:
    session = RecordingSession(results=[_Result(value=None)])
    snap = ReportLifecycleRepository(session).create_generating_daily_brief(
        brief_date=BRIEF_DATE, title="t"
    )
    assert snap.version == 1
    assert snap.change_reason is None
    assert snap.status == "generating"
    assert snap.user_id is None
    assert session.flushes == 1
    _assert_never_committed(session)


def test_first_version_drops_any_supplied_change_reason() -> None:
    # A supplied reason on version 1 is dropped, not rejected -- there is no prior to change from,
    # and this is what lets two concurrent from-scratch reruns settle to versions 1 and 2.
    session = RecordingSession(results=[_Result(value=None)])
    snap = ReportLifecycleRepository(session).create_generating_daily_brief(
        brief_date=BRIEF_DATE, title="t", change_reason="raced rerun"
    )
    assert snap.version == 1
    assert snap.change_reason is None
    _assert_never_committed(session)


def test_a_rerun_requires_a_nonblank_change_reason() -> None:
    session = RecordingSession(results=[_Result(value=1)])
    with pytest.raises(ChangeReasonRequiredError):
        ReportLifecycleRepository(session).create_generating_daily_brief(
            brief_date=BRIEF_DATE, title="t", change_reason="   "
        )
    _assert_never_committed(session)


def test_a_rerun_allocates_max_plus_one_and_persists_the_reason() -> None:
    session = RecordingSession(results=[_Result(value=3)])
    snap = ReportLifecycleRepository(session).create_generating_daily_brief(
        brief_date=BRIEF_DATE, title="t", change_reason="reprocessed upstream event"
    )
    assert snap.version == 4
    assert snap.change_reason == "reprocessed upstream event"
    _assert_never_committed(session)


def test_transition_validates_and_mutates_without_committing() -> None:
    report = Report(id=uuid.uuid4(), status="generating")
    session = RecordingSession(get_result=report)
    snap = ReportLifecycleRepository(session).transition(report.id, ReportStatus.GROUNDING_CHECK)
    assert snap.status == "grounding_check"
    assert report.status == "grounding_check"
    assert session.flushes == 1
    _assert_never_committed(session)


def test_an_illegal_transition_leaves_the_row_untouched() -> None:
    report = Report(id=uuid.uuid4(), status="published")
    session = RecordingSession(get_result=report)
    with pytest.raises(IllegalTransitionError):
        ReportLifecycleRepository(session).transition(report.id, ReportStatus.GROUNDING_CHECK)
    assert report.status == "published"  # no partial mutation
    assert session.flushes == 0
    _assert_never_committed(session)


def test_transition_refuses_to_publish_directly() -> None:
    report = Report(id=uuid.uuid4(), status="grounding_check")
    session = RecordingSession(get_result=report)
    with pytest.raises(IllegalTransitionError):
        ReportLifecycleRepository(session).transition(report.id, ReportStatus.PUBLISHED)
    assert report.status == "grounding_check"
    _assert_never_committed(session)


def test_a_second_section_write_is_refused() -> None:
    report = Report(id=uuid.uuid4(), status="grounding_check", brief_date=BRIEF_DATE)
    session = RecordingSession(get_result=report, results=[_Result(value=1)])  # one section exists
    with pytest.raises(SectionsAlreadyWrittenError):
        ReportLifecycleRepository(session).persist_sections(report.id, _real_gate())
    assert session.added == []  # nothing written
    _assert_never_committed(session)


def test_persist_sections_rejects_a_brief_date_mismatch() -> None:
    report = Report(id=uuid.uuid4(), status="grounding_check", brief_date=datetime.date(2000, 1, 1))
    session = RecordingSession(get_result=report, results=[_Result(value=0)])
    with pytest.raises(SectionValidationError):
        ReportLifecycleRepository(session).persist_sections(report.id, _real_gate())
    _assert_never_committed(session)


def test_stale_marking_is_idempotent_and_only_flips_false_to_true() -> None:
    fresh = Report(id=uuid.uuid4(), status="published", stale=False)
    already = Report(id=uuid.uuid4(), status="published", stale=True)
    event_id = uuid.uuid4()
    session = RecordingSession(
        results=[
            _Result(rows=[fresh.id]),  # daily-brief dependency scan
            _Result(rows=[already.id]),  # direct event-report scan
            _Result(rows=[fresh, already]),  # the locked load
        ]
    )
    count = ReportLifecycleRepository(session).mark_published_reports_stale_for_event(event_id)
    assert count == 2  # deterministic: both dependents
    assert fresh.stale is True
    assert already.stale is True
    _assert_never_committed(session)


# --------------------------------------------------------------------------------------
# SQL shape: the advisory lock and the dependency join
# --------------------------------------------------------------------------------------


def _compiled(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_version_allocation_takes_a_transaction_scoped_advisory_lock() -> None:
    session = RecordingSession(results=[_Result(value=None)])
    ReportLifecycleRepository(session).create_generating_daily_brief(brief_date=BRIEF_DATE, title="t")
    lock_stmts = [s for s in session.executed if "pg_advisory_xact_lock" in str(s)]
    assert len(lock_stmts) == 1
    assert "pg_advisory_xact_lock" in _compiled(lock_stmts[0])


def test_default_reads_filter_to_published_and_order_by_version_desc() -> None:
    session = RecordingSession()
    ReportLifecycleRepository(session).latest_published_daily_brief_by_date(BRIEF_DATE)
    sql = _compiled(session.executed[-1])
    assert "'published'" in sql
    assert "'daily_brief'" in sql
    assert "version DESC" in sql


def test_stale_dependency_join_resolves_claims_through_article_evidence() -> None:
    session = RecordingSession(results=[_Result(rows=[]), _Result(rows=[])])
    ReportLifecycleRepository(session).mark_published_reports_stale_for_event(uuid.uuid4())
    join_sql = _compiled(session.executed[0])
    assert "event_articles" in join_sql
    assert "evidence_items" in join_sql
    assert "claim_evidence" in join_sql
    assert "report_sections" in join_sql
    assert "'article'" in join_sql  # exact Stage 6 evidence source type
    assert "'published'" in join_sql
    assert "'daily_brief'" in join_sql
    assert "ANY" in join_sql  # claim_id = ANY(evidence_refs)
    assert "CAST" in join_sql.upper()  # article_id cast to text


# --------------------------------------------------------------------------------------
# Safe persistence sequence branches (against the recording session)
# --------------------------------------------------------------------------------------


def test_persist_daily_brief_fails_a_blocked_gate_and_never_publishes() -> None:
    """A blocked gate drives create -> grounding_check -> persist -> failed; never published."""
    # The fixture row stands in for the report the create step allocates; every get() returns it,
    # so the sequence's transitions land on it. Executes consumed in order: create max-version,
    # then the persist section-count probe.
    report = Report(id=uuid.uuid4(), status="generating", brief_date=BRIEF_DATE)
    session = RecordingSession(get_result=report, results=[_Result(value=None), _Result(value=0)])
    blocked = _gate(
        _section(
            SectionKind.EXECUTIVE_SUMMARY,
            1,
            "Exec",
            status=SectionGroundingStatus.FAILED,
            final_text="[grounding_failed]",
            blocks_publication=True,
        ),
        _disclaimer(2),
        outcome=GateOutcome.BLOCKED,
    )
    snap = persist_daily_brief(session, blocked)
    assert snap.status == "failed"
    assert report.status == "failed"
    _assert_never_committed(session)


# --------------------------------------------------------------------------------------
# Finding 2: evidence refs must be real Claim ids before any section is written
# --------------------------------------------------------------------------------------


def test_persist_sections_rejects_a_non_claim_evidence_ref_before_any_write() -> None:
    # An episode id injected into a final block reaches evidence_refs (it *is* in a block), but it is
    # not a Claim. The claim-coverage query returns only the real claim, so the whole write is
    # refused before a single ReportSection is added or the status is touched.
    report = Report(id=uuid.uuid4(), status="grounding_check", brief_date=BRIEF_DATE)
    session = RecordingSession(
        get_result=report,
        results=[
            _Result(value=0),  # section-count probe: none yet
            _Result(rows=[CLAIM_ID]),  # claim-coverage query: only CLAIM_ID is a real Claim
        ],
    )
    gate = _gate(
        _section(
            SectionKind.HISTORICAL_PARALLELS,
            1,
            "Historical Parallels",
            blocks=(("parallel", (CLAIM_ID, EPISODE_ID)),),
        ),
        _disclaimer(2),
    )
    with pytest.raises(SectionValidationError):
        ReportLifecycleRepository(session).persist_sections(report.id, gate)
    assert not any(isinstance(o, ReportSection) for o in session.added)  # no pending Section writes
    assert report.status == "grounding_check"  # no status mutation
    _assert_never_committed(session)


def test_persist_sections_writes_when_every_ref_is_a_real_claim() -> None:
    report = Report(id=uuid.uuid4(), status="grounding_check", brief_date=BRIEF_DATE)
    session = RecordingSession(
        get_result=report,
        results=[
            _Result(value=0),  # section-count probe
            _Result(rows=[CLAIM_ID]),  # claim-coverage query: CLAIM_ID is a real Claim
            _Result(rows=[]),  # bounded report_sections read-back
        ],
    )
    gate = _gate(
        _section(SectionKind.EXECUTIVE_SUMMARY, 1, "Exec", blocks=(("x", (CLAIM_ID,)),)),
        _disclaimer(2),
    )
    ReportLifecycleRepository(session).persist_sections(report.id, gate)
    written = [o for o in session.added if isinstance(o, ReportSection)]
    assert [s.section_order for s in written] == [1, 2]
    assert session.flushes == 1
    _assert_never_committed(session)


def test_a_quiet_brief_persists_without_a_claim_query() -> None:
    # No citable ids means no claim query at all (the results queue is left untouched by persist).
    report = Report(id=uuid.uuid4(), status="grounding_check", brief_date=BRIEF_DATE)
    session = RecordingSession(
        get_result=report,
        results=[
            _Result(value=0),  # section-count probe
            _Result(rows=[]),  # bounded report_sections read-back (claim query is skipped)
        ],
    )
    gate = _gate(_section(SectionKind.RISK_RADAR, 1, "Risk Radar", final_text="a table"), _disclaimer(2))
    ReportLifecycleRepository(session).persist_sections(report.id, gate)
    claim_queries = [s for s in session.executed if "claims" in str(s).lower()]
    assert claim_queries == []  # empty-ref brief makes no claim query
    _assert_never_committed(session)


# --------------------------------------------------------------------------------------
# Finding 1: publication verifies every persisted section field against the gate
# --------------------------------------------------------------------------------------

_MATCH_GATE = _gate(
    _section(SectionKind.EXECUTIVE_SUMMARY, 1, "Executive Summary", blocks=(("Rates held.", (CLAIM_ID,)),)),
    _section(SectionKind.RISK_RADAR, 2, "Risk Radar", final_text="a table"),
    _disclaimer(3),
)


def _persisted_rows(gate: GroundingGateResult) -> list[SimpleNamespace]:
    """The rows exactly as ``persist_sections`` would store them, as queryable row namespaces."""
    return [
        SimpleNamespace(
            section_order=r.section_order,
            title=r.title,
            body=r.body,
            blocks=[{"text": b.text, "claim_ids": list(b.claim_ids)} for b in r.blocks] or None,
            evidence_refs=list(r.evidence_refs) or None,
            grounding_status=r.grounding_status,
        )
        for r in section_rows_from_gate(gate)
    ]


def _match(rows: list[SimpleNamespace], gate: GroundingGateResult = _MATCH_GATE) -> None:
    session = RecordingSession(results=[_Result(rows=rows)])
    ReportLifecycleRepository(session)._assert_sections_match(uuid.uuid4(), gate)


def test_exact_persisted_match_is_publishable() -> None:
    _match(_persisted_rows(_MATCH_GATE))  # does not raise


def test_publish_refused_when_a_persisted_title_is_altered() -> None:
    rows = _persisted_rows(_MATCH_GATE)
    rows[0].title = "Tampered Title"
    with pytest.raises(CannotPublishError):
        _match(rows)


def test_publish_refused_when_a_non_disclaimer_body_is_altered() -> None:
    # The old check compared only the *disclaimer* body; a tampered exec-summary body slipped past.
    rows = _persisted_rows(_MATCH_GATE)
    rows[0].body = "Rates were slashed."
    with pytest.raises(CannotPublishError):
        _match(rows)


def test_publish_refused_when_persisted_blocks_are_altered() -> None:
    rows = _persisted_rows(_MATCH_GATE)
    rows[0].blocks = [{"text": "Rates held.", "claim_ids": [str(CLAIM_ID_2)]}]  # different claim id
    with pytest.raises(CannotPublishError):
        _match(rows)


def test_publish_refused_when_persisted_evidence_refs_are_altered() -> None:
    rows = _persisted_rows(_MATCH_GATE)
    rows[0].evidence_refs = [CLAIM_ID_2]  # a ref the gate never cited
    with pytest.raises(CannotPublishError):
        _match(rows)


def test_publish_refused_when_a_persisted_status_differs_from_the_gate() -> None:
    # data_quality_note is itself publishable, but it is not what the gate says; a full-field
    # comparison must still refuse it (the old publishable-only check let it through).
    rows = _persisted_rows(_MATCH_GATE)
    rows[0].grounding_status = "data_quality_note"
    with pytest.raises(CannotPublishError):
        _match(rows)


def test_publish_refused_on_an_extra_persisted_row_beyond_the_gate() -> None:
    rows = _persisted_rows(_MATCH_GATE)
    rows.append(
        SimpleNamespace(
            section_order=4, title="Smuggled", body="extra", blocks=None,
            evidence_refs=None, grounding_status="passed",
        )
    )
    with pytest.raises(CannotPublishError):
        _match(rows)


def test_publish_refused_when_persisted_sections_are_missing() -> None:
    rows = _persisted_rows(_MATCH_GATE)[:-1]  # drop the disclaimer
    with pytest.raises(CannotPublishError):
        _match(rows)


@pytest.mark.parametrize(
    "bad_blocks",
    [
        [{"claim_ids": [str(CLAIM_ID)]}],  # missing "text"
        [{"text": "Rates held.", "claim_ids": "not-a-list"}],  # claim_ids wrong shape
        ["not-a-dict"],  # element is not an object
        "not-a-list",  # blocks column is not a list
        [{"text": 123, "claim_ids": []}],  # text is not a string
    ],
)
def test_publish_fails_closed_on_a_malformed_persisted_block(bad_blocks: object) -> None:
    # A tampered/corrupt blocks value fails closed with CannotPublishError, never an unrelated
    # KeyError/TypeError leaking out of the comparison.
    rows = _persisted_rows(_MATCH_GATE)
    rows[0].blocks = bad_blocks
    with pytest.raises(CannotPublishError):
        _match(rows)


def test_assert_sections_match_over_reads_and_refuses_an_overflow() -> None:
    over = [
        SimpleNamespace(
            section_order=i, title="t", body="b", blocks=None,
            evidence_refs=None, grounding_status="passed",
        )
        for i in range(SECTION_LIST_LIMIT + 1)
    ]
    session = RecordingSession(results=[_Result(rows=over)])
    with pytest.raises(ReportReadOverflowError):
        ReportLifecycleRepository(session)._assert_sections_match(uuid.uuid4(), _MATCH_GATE)
    assert f"LIMIT {SECTION_LIST_LIMIT + 1}" in _compiled(session.executed[-1])


# --------------------------------------------------------------------------------------
# Finding 3: bounded reads over-read by one and fail loud past the bound
# --------------------------------------------------------------------------------------


def test_bounded_reads_over_read_by_one_for_a_truncation_signal() -> None:
    session = RecordingSession(results=[_Result(rows=[])])
    ReportLifecycleRepository(session).daily_brief_versions(BRIEF_DATE)
    assert f"LIMIT {VERSION_LIST_LIMIT + 1}" in _compiled(session.executed[-1])

    session2 = RecordingSession(results=[_Result(rows=[])])
    ReportLifecycleRepository(session2).report_sections(uuid.uuid4())
    assert f"LIMIT {SECTION_LIST_LIMIT + 1}" in _compiled(session2.executed[-1])


def test_daily_brief_versions_returns_the_limit_but_raises_past_it() -> None:
    at_limit = [Report(id=uuid.uuid4(), version=i) for i in range(VERSION_LIST_LIMIT)]
    session = RecordingSession(results=[_Result(rows=at_limit)])
    assert len(ReportLifecycleRepository(session).daily_brief_versions(BRIEF_DATE)) == VERSION_LIST_LIMIT

    over = [Report(id=uuid.uuid4(), version=i) for i in range(VERSION_LIST_LIMIT + 1)]
    session2 = RecordingSession(results=[_Result(rows=over)])
    with pytest.raises(ReportReadOverflowError):
        ReportLifecycleRepository(session2).daily_brief_versions(BRIEF_DATE)


def _min_section(order: int) -> ReportSection:
    return ReportSection(
        id=uuid.uuid4(),
        report_id=uuid.uuid4(),
        section_order=order,
        title="t",
        body="b",
        blocks=None,
        evidence_refs=None,
        grounding_status="passed",
    )


def test_report_sections_returns_the_limit_but_raises_past_it() -> None:
    at_limit = [_min_section(i) for i in range(SECTION_LIST_LIMIT)]
    session = RecordingSession(results=[_Result(rows=at_limit)])
    assert len(ReportLifecycleRepository(session).report_sections(uuid.uuid4())) == SECTION_LIST_LIMIT

    over = [_min_section(i) for i in range(SECTION_LIST_LIMIT + 1)]
    session2 = RecordingSession(results=[_Result(rows=over)])
    with pytest.raises(ReportReadOverflowError):
        ReportLifecycleRepository(session2).report_sections(uuid.uuid4())


# --------------------------------------------------------------------------------------
# attach_generated_by_run_id: pre-publish only, from None, never over a different value
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["generating", "grounding_check"])
def test_attach_sets_the_run_id_from_none_and_flushes_without_committing(status: str) -> None:
    run_id = uuid.uuid4()
    report = Report(id=uuid.uuid4(), status=status, generated_by_run_id=None)
    session = RecordingSession(get_result=report)
    snap = ReportLifecycleRepository(session).attach_generated_by_run_id(report.id, run_id)
    assert snap.generated_by_run_id == run_id
    assert report.generated_by_run_id == run_id
    assert session.flushes == 1
    _assert_never_committed(session)


def test_attach_is_idempotent_for_the_same_run_id() -> None:
    run_id = uuid.uuid4()
    report = Report(id=uuid.uuid4(), status="grounding_check", generated_by_run_id=run_id)
    session = RecordingSession(get_result=report)
    snap = ReportLifecycleRepository(session).attach_generated_by_run_id(report.id, run_id)
    assert snap.generated_by_run_id == run_id  # same value: allowed, not a conflict
    _assert_never_committed(session)


def test_attach_refuses_to_overwrite_a_different_existing_run_id() -> None:
    existing, other = uuid.uuid4(), uuid.uuid4()
    report = Report(id=uuid.uuid4(), status="grounding_check", generated_by_run_id=existing)
    session = RecordingSession(get_result=report)
    with pytest.raises(GeneratedByRunIdConflictError):
        ReportLifecycleRepository(session).attach_generated_by_run_id(report.id, other)
    assert report.generated_by_run_id == existing  # unchanged
    assert session.flushes == 0
    _assert_never_committed(session)


@pytest.mark.parametrize("status", ["published", "failed"])
def test_attach_refuses_a_terminal_report(status: str) -> None:
    report = Report(id=uuid.uuid4(), status=status, generated_by_run_id=None)
    session = RecordingSession(get_result=report)
    with pytest.raises(IllegalTransitionError):
        ReportLifecycleRepository(session).attach_generated_by_run_id(report.id, uuid.uuid4())
    assert report.generated_by_run_id is None  # terminal report never rewritten
    assert session.flushes == 0
    _assert_never_committed(session)
