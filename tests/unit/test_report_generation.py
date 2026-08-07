"""Unit tests for the Stage 6 daily-brief generation coordinator.

DB-free. The coordinator's *orchestration* -- the two-transaction shape, the exact pipeline order,
the durable start committing before any model work, the single main commit, the publish-or-fail
decision, and the unexpected-failure cleanup -- is tested against a shared in-memory lifecycle fake
(one report "row" surviving across sessions, as a committed durable start would) and the real
composition/grounding path driven by the whitelist-validating :class:`GateOrchestrator`. The
lifecycle repository's own SQL is proven elsewhere; here we prove the coordinator drives it correctly.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

import pytest

from db.models.core import (
    REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY,
    REPORT_CONTENT_POLICY_PREDICTION_BACKED,
)
from services.llm.orchestrator import LLMOrchestratorError
from services.reports.contracts import (
    LinkedRisk,
    PriorBriefContext,
    PriorBriefSection,
    RiskProvenance,
)
from services.reports.generation import (
    DEFAULT_CHANGE_REASON,
    DailyBriefGenerationError,
    DailyBriefGenerationResult,
    build_generation_orchestrator_factory,
    generate_daily_brief,
)
from services.reports.grounding import GateOutcome
from services.reports.lifecycle import (
    ChangeReasonRequiredError,
    GeneratedByRunIdConflictError,
    IllegalTransitionError,
    ReportSnapshot,
    ReportStatus,
    section_rows_from_gate,
    validate_transition,
)
from services.reports.prompts import COMPOSITION_SCHEMA
from services.reports.window import window_for_date
from tests.unit._report_composition_fixtures import (
    abstain,
    alert_change,
    brief_context,
    brief_inputs,
    claim,
    single_event_brief,
)
from tests.unit._report_grounding_fixtures import GateOrchestrator, per_claim_blocks

BRIEF_DATE = datetime.date(2026, 7, 14)
WINDOW = window_for_date(BRIEF_DATE)


# --------------------------------------------------------------------------------------
# Fakes: a recorder-threaded session, orchestrator, and a shared durable lifecycle "row"
# --------------------------------------------------------------------------------------


class FakeSession:
    """A caller-owned session that records its commit/rollback/close on a shared recorder."""

    def __init__(self, recorder: list[str], index: int) -> None:
        self.recorder = recorder
        self.index = index
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def commit(self) -> None:
        self.commit_count += 1
        self.recorder.append(f"s{self.index}:commit")

    def rollback(self) -> None:
        self.rollback_count += 1
        self.recorder.append(f"s{self.index}:rollback")

    def close(self) -> None:
        self.close_count += 1


class FakeSessionFactory:
    def __init__(self, recorder: list[str]) -> None:
        self.recorder = recorder
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession(self.recorder, len(self.sessions))
        self.sessions.append(session)
        return session


class ProgrammableSession(FakeSession):
    """A FakeSession whose rollback/close may be scripted to raise -- after recording the attempt.

    ``super()`` runs first in both, so the attempt is counted (and, for rollback, recorded) *before*
    any injected fault: a test can prove a close/rollback was genuinely attempted even when it raised.
    """

    def __init__(self, recorder, index, *, rollback_error=None, close_error=None) -> None:  # noqa: ANN001
        super().__init__(recorder, index)
        self._rollback_error = rollback_error
        self._close_error = close_error

    def rollback(self) -> None:
        super().rollback()
        if self._rollback_error is not None:
            raise self._rollback_error

    def close(self) -> None:
        super().close()
        if self._close_error is not None:
            raise self._close_error


class ProgrammableSessionFactory:
    """Yields sessions per call, scriptable to model acquisition/cleanup faults after a durable start.

    A call index in ``raise_on`` raises that exception instead of returning a session (a failed
    ``session_factory()`` acquisition); a call index in ``specs`` returns a :class:`ProgrammableSession`
    whose rollback/close is scripted to raise. Calls are 0=durable start, 1=main, 2=failure cleanup.
    """

    def __init__(self, recorder: list[str], *, raise_on=None, specs=None) -> None:  # noqa: ANN001
        self.recorder = recorder
        self.sessions: list[ProgrammableSession] = []
        self._raise_on = raise_on or {}
        self._specs = specs or {}
        self.call_count = 0

    def __call__(self) -> ProgrammableSession:
        index = self.call_count
        self.call_count += 1
        if index in self._raise_on:
            raise self._raise_on[index]
        session = ProgrammableSession(
            self.recorder, len(self.sessions), **self._specs.get(index, {})
        )
        self.sessions.append(session)
        return session


class RecordingOrchestrator:
    """Wraps the real gate orchestrator and logs a marker per compose/ground call."""

    def __init__(
        self,
        inner: GateOrchestrator,
        recorder: list[str],
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.inner = inner
        self.recorder = recorder
        self.close_count = 0
        self.close_error = close_error

    def run(self, request):  # noqa: ANN001, ANN201 - narrow test seam
        self.recorder.append(
            "compose" if request.requested_schema == COMPOSITION_SCHEMA else "ground"
        )
        return self.inner.run(request)

    def close(self) -> None:
        self.close_count += 1
        self.recorder.append("orchestrator:close")
        if self.close_error is not None:
            raise self.close_error


class RecordingOrchestratorFactory:
    """Records which session each orchestrator was built for (must be the main session)."""

    def __init__(self, orchestrator: RecordingOrchestrator) -> None:
        self.orchestrator = orchestrator
        self.sessions_seen: list[object] = []

    def __call__(self, session: object) -> RecordingOrchestrator:
        self.sessions_seen.append(session)
        return self.orchestrator


class _Row:
    def __init__(
        self,
        row_id,
        brief_date,
        version,
        change_reason,
        content_policy,
    ) -> None:  # noqa: ANN001
        self.id = row_id
        self.brief_date = brief_date
        self.version = version
        self.change_reason = change_reason
        self.status = ReportStatus.GENERATING.value
        self.generated_by_run_id: uuid.UUID | None = None
        self.content_policy = content_policy


class FakeLifecycleRepo:
    """One shared, in-memory model of the report rows -- durable across sessions, flush-only.

    Returned for every session by ``__call__`` (a committed durable start means every later session
    sees the same row). It mirrors the real state machine's guards closely enough to prove the
    coordinator's control flow.
    """

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, _Row] = {}
        self.calls: list[tuple] = []
        self.fail_transition_to_failed = False
        self.persisted_rows: tuple[Any, ...] = ()
        #: The per-run event recorder, so lifecycle ops interleave with compose/ground/commit.
        self.recorder: list[str] | None = None

    def __call__(self, session: object) -> FakeLifecycleRepo:
        return self

    def _mark(self, marker: str) -> None:
        if self.recorder is not None:
            self.recorder.append(marker)

    def _snapshot(self, row: _Row) -> ReportSnapshot:
        return ReportSnapshot(
            id=row.id,
            user_id=None,
            report_type="daily_brief",
            brief_date=row.brief_date,
            event_id=None,
            title="t",
            status=row.status,
            version=row.version,
            change_reason=row.change_reason,
            stale=False,
            generated_by_run_id=row.generated_by_run_id,
            created_at=None,
            updated_at=None,
            content_policy=row.content_policy,
        )

    def create_generating_daily_brief(
        self,
        *,
        brief_date,
        title,
        change_reason=None,
        generated_by_run_id=None,
        content_policy=None,
    ) -> ReportSnapshot:  # noqa: ANN001
        existing = [r for r in self.rows.values() if r.brief_date == brief_date]
        version = max((r.version for r in existing), default=0) + 1
        if version == 1:
            reason = None  # v1 drops any supplied reason
        else:
            if change_reason is None or not change_reason.strip():
                raise ChangeReasonRequiredError("rerun requires a nonblank change_reason")
            reason = change_reason
        row = _Row(uuid.uuid4(), brief_date, version, reason, content_policy)
        row.generated_by_run_id = generated_by_run_id
        self.rows[row.id] = row
        self.calls.append(("create", version))
        self._mark("create")
        return self._snapshot(row)

    def transition(self, report_id, target) -> ReportSnapshot:  # noqa: ANN001
        row = self.rows[report_id]
        validate_transition(ReportStatus(row.status), target)
        if target is ReportStatus.FAILED and self.fail_transition_to_failed:
            raise RuntimeError("row lock failed during failure cleanup")
        row.status = target.value
        self.calls.append(("transition", target.value))
        self._mark(f"transition:{target.value}")
        return self._snapshot(row)

    def persist_sections(self, report_id, gate):  # noqa: ANN001, ANN201
        row = self.rows[report_id]
        if row.status != ReportStatus.GROUNDING_CHECK.value:
            raise IllegalTransitionError("sections only in grounding_check")
        rows = section_rows_from_gate(gate)
        self.persisted_rows = rows
        self.calls.append(("persist_sections", len(rows)))
        self._mark("persist_sections")
        return rows

    def attach_generated_by_run_id(self, report_id, run_id) -> ReportSnapshot:  # noqa: ANN001
        row = self.rows[report_id]
        if ReportStatus(row.status) in {ReportStatus.PUBLISHED, ReportStatus.FAILED}:
            raise IllegalTransitionError("cannot attach to a terminal report")
        if row.generated_by_run_id is not None and row.generated_by_run_id != run_id:
            raise GeneratedByRunIdConflictError("already has a different run id")
        row.generated_by_run_id = run_id
        self.calls.append(("attach", run_id))
        self._mark("attach")
        return self._snapshot(row)

    def publish(self, report_id, gate) -> ReportSnapshot:  # noqa: ANN001
        row = self.rows[report_id]
        validate_transition(ReportStatus(row.status), ReportStatus.PUBLISHED)
        row.status = ReportStatus.PUBLISHED.value
        self.calls.append(("publish",))
        self._mark("publish")
        return self._snapshot(row)

    def get_report(self, report_id) -> ReportSnapshot | None:  # noqa: ANN001
        row = self.rows.get(report_id)
        return self._snapshot(row) if row is not None else None

    # test helpers
    def only_row(self) -> _Row:
        (row,) = self.rows.values()
        return row

    def op_names(self) -> list[str]:
        return [call[0] for call in self.calls]


class _Harness:
    """The wired-up fakes and the coordinator result, for concise assertions."""

    def __init__(
        self,
        *,
        inputs,
        context,
        orch=None,
        lifecycle=None,
        change_reason=None,
        inputs_builder=None,
        context_builder=None,
        session_factory_builder=None,
        prediction_backed_outputs_enabled=False,
        orchestrator_close_error=None,
    ) -> None:  # noqa: ANN001
        self.recorder: list[str] = []
        self.windows_seen: list[object] = []
        self.gate_orch = orch or GateOrchestrator()
        self.rec_orch = RecordingOrchestrator(
            self.gate_orch,
            self.recorder,
            close_error=orchestrator_close_error,
        )
        self.orch_factory = RecordingOrchestratorFactory(self.rec_orch)
        # ``session_factory_builder`` (recorder -> factory) lets a test inject a session factory that
        # models acquisition/rollback/close faults; the default is the plain recording factory.
        self.session_factory = (
            session_factory_builder(self.recorder)
            if session_factory_builder is not None
            else FakeSessionFactory(self.recorder)
        )
        self.lifecycle = lifecycle or FakeLifecycleRepo()
        self.lifecycle.recorder = self.recorder  # this run's ops interleave in this run's recorder

        def _inputs(session, window):  # noqa: ANN001
            self.windows_seen.append(window)
            return inputs

        self.result: DailyBriefGenerationResult | None = None
        self.error: DailyBriefGenerationError | None = None
        try:
            self.result = generate_daily_brief(
                BRIEF_DATE,
                session_factory=self.session_factory,
                orchestrator_factory=self.orch_factory,
                change_reason=change_reason,
                inputs_builder=inputs_builder or _inputs,
                context_builder=context_builder or (lambda session, i: context),
                lifecycle_repository_factory=self.lifecycle,
                prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
            )
        except DailyBriefGenerationError as exc:
            self.error = exc

    def first(self, marker: str) -> int:
        return self.recorder.index(marker)

    def all_indices(self, marker: str) -> list[int]:
        return [i for i, event in enumerate(self.recorder) if event == marker]

    def assert_all_sessions_closed(self) -> None:
        assert self.session_factory.sessions, "no session was ever opened"
        for session in self.session_factory.sessions:
            assert session.close_count >= 1, f"session {session.index} was not closed"


def _busy_brief(**kwargs):
    inputs, context, _material = single_event_brief(claims=(claim(),), **kwargs)
    return inputs, context


def test_default_generation_sanitizes_injected_prediction_inputs_and_stamps_policy() -> None:
    inputs, context, _material = single_event_brief(
        claims=(claim(),),
        alert_changes=(alert_change(),),
        forecasts_present=True,
    )
    prior = PriorBriefContext(
        report_id=uuid.uuid4(),
        brief_date=BRIEF_DATE - datetime.timedelta(days=1),
        version=1,
        sections=(
            PriorBriefSection(
                section_order=1,
                title="Executive Summary",
                body="LEGACY_PREDICTION_PROBABILITY_91_PERCENT",
                claim_ids=(),
            ),
        ),
    )
    predictive_event = dataclasses.replace(
        inputs.top_events[0],
        max_linked_risk=LinkedRisk(
            score=99.0,
            provenance=RiskProvenance.EVENT_INDUSTRY,
        ),
        ranking_score=99.0,
    )
    inputs = dataclasses.replace(
        inputs,
        top_events=(predictive_event,),
        prior_brief=prior,
    )

    h = _Harness(inputs=inputs, context=context)

    assert h.result is not None and h.result.published is True
    assert h.lifecycle.only_row().content_policy == REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
    assert {row.title for row in h.lifecycle.persisted_rows}.isdisjoint({"Forecasts", "Alerts"})
    persisted_text = "\n".join(row.body for row in h.lifecycle.persisted_rows)
    assert "LEGACY_PREDICTION_PROBABILITY_91_PERCENT" not in persisted_text
    prompts = "\n".join(request.prompt for request in h.gate_orch.composition_requests)
    assert "Bank-run risk" not in prompts
    assert "LEGACY_PREDICTION_PROBABILITY_91_PERCENT" not in prompts
    assert "base_case" not in prompts
    assert "event_industry" not in prompts
    assert '"max_linked_risk_score": 99.0' not in prompts
    assert '"risk_provenance": "none"' in prompts


def test_explicit_open_generation_preserves_diagnostic_outputs_and_stamps_policy() -> None:
    inputs, context, _material = single_event_brief(
        claims=(claim(),),
        alert_changes=(alert_change(),),
        forecasts_present=True,
    )

    h = _Harness(
        inputs=inputs,
        context=context,
        prediction_backed_outputs_enabled=True,
    )

    assert h.result is not None and h.result.published is True
    assert h.lifecycle.only_row().content_policy == REPORT_CONTENT_POLICY_PREDICTION_BACKED
    assert {"Forecasts", "Alerts"} <= {row.title for row in h.lifecycle.persisted_rows}


# --------------------------------------------------------------------------------------
# Window and the durable start
# --------------------------------------------------------------------------------------


def test_derives_the_exact_adr_0009_window_from_brief_date() -> None:
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)
    assert h.windows_seen == [WINDOW]  # window_for_date, never a rolling now-window
    assert h.result.window == WINDOW
    assert h.result.window.brief_date == BRIEF_DATE


def test_durable_start_commits_before_any_model_work() -> None:
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)
    # The row is created and its start transaction committed before the first composition call.
    assert h.lifecycle.op_names()[0] == "create"
    assert "s0:commit" in h.recorder  # the start session (index 0) committed on its own
    assert h.first("s0:commit") < h.first("compose")
    # The start session committed exactly once and closed.
    start = h.session_factory.sessions[0]
    assert start.commit_count == 1 and start.close_count == 1


# --------------------------------------------------------------------------------------
# Exact pipeline order and dependencies
# --------------------------------------------------------------------------------------


def test_busy_pass_runs_the_pipeline_in_the_exact_order_and_publishes() -> None:
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)

    assert h.result.published is True
    assert h.result.gate_outcome is GateOutcome.PASS
    assert h.lifecycle.only_row().status == ReportStatus.PUBLISHED.value

    # Lifecycle op order: create -> to grounding_check -> persist -> attach -> publish.
    assert h.lifecycle.op_names() == [
        "create",
        "transition",
        "persist_sections",
        "attach",
        "publish",
    ]
    # And relative to the model calls: all composition before the grounding_check transition, all
    # grounding after it, persistence after grounding, and the single main commit last.
    to_grounding = h.first("transition:grounding_check")
    assert all(i < to_grounding for i in h.all_indices("compose"))
    assert all(i > to_grounding for i in h.all_indices("ground"))
    assert max(h.all_indices("ground")) < h.first("persist_sections")
    assert h.first("persist_sections") < h.first("attach") < h.first("publish")
    assert h.recorder[-1] == "s1:commit"  # main transaction commits exactly once, at the very end


def test_transition_to_grounding_check_happens_after_composition() -> None:
    # Distinct assertion: the report advances to grounding_check only after the draft is composed.
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)
    assert h.recorder.count("compose") >= 1
    assert h.first("compose") < h.first("transition:grounding_check")


# --------------------------------------------------------------------------------------
# Main-transaction commit boundary and LLM audit sharing
# --------------------------------------------------------------------------------------


def test_the_main_transaction_commits_exactly_once() -> None:
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)
    main = h.session_factory.sessions[1]
    assert main.commit_count == 1
    assert main.rollback_count == 0


def test_the_orchestrator_is_built_for_the_main_session_so_audit_rows_share_it() -> None:
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)
    # Exactly one orchestrator, built for the main session -- never a provider called directly.
    assert h.orch_factory.sessions_seen == [h.session_factory.sessions[1]]
    # Every model call went through that injected orchestrator.
    assert h.gate_orch.composition_requests and h.gate_orch.grounding_requests


def test_every_opened_session_is_closed_on_the_success_path() -> None:
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)
    assert len(h.session_factory.sessions) == 2  # start + main
    assert h.rec_orch.close_count == 1
    assert h.first("ground") < h.first("orchestrator:close") < h.first("s1:commit")
    h.assert_all_sessions_closed()


# --------------------------------------------------------------------------------------
# Quiet day
# --------------------------------------------------------------------------------------


def test_quiet_day_publishes_with_no_llm_call_and_no_run_id() -> None:
    inputs = brief_inputs(top_events=())
    context = brief_context()
    h = _Harness(inputs=inputs, context=context)

    assert h.result.quiet_day is True
    assert h.result.published is True
    assert h.result.gate_outcome is GateOutcome.PASS
    assert h.result.selected_event_count == 0
    assert h.result.selected_event_ids == ()
    # No generated LLM calls, no attachment, run id stays None.
    assert "compose" not in h.recorder and "ground" not in h.recorder
    assert "attach" not in h.lifecycle.op_names()
    assert h.result.generated_by_run_id is None
    assert h.rec_orch.close_count == 1


# --------------------------------------------------------------------------------------
# generated_by attachment rules at the coordinator level
# --------------------------------------------------------------------------------------


def test_busy_pass_attaches_the_first_run_id_while_pre_publish() -> None:
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)
    attach_calls = [c for c in h.lifecycle.calls if c[0] == "attach"]
    assert len(attach_calls) == 1
    run_id = attach_calls[0][1]
    assert run_id is not None
    assert h.result.generated_by_run_id == run_id
    # Attachment landed before publication, on the same durable row.
    assert h.first("attach") < h.first("publish")


# --------------------------------------------------------------------------------------
# BLOCKED gate is an expected, non-raising, durable failure
# --------------------------------------------------------------------------------------


def test_blocked_gate_fails_the_report_without_raising_and_retains_sections() -> None:
    # A grounding call that fails closed blocks the gate; that is an expected completed generation.
    inputs, context = _busy_brief()
    orch = GateOrchestrator(ground=lambda request: LLMOrchestratorError("grounding unavailable"))
    h = _Harness(inputs=inputs, context=context, orch=orch)

    assert h.error is None  # a blocked gate never raises a retry exception
    assert h.result.gate_outcome is GateOutcome.BLOCKED
    assert h.result.published is False
    assert h.lifecycle.only_row().status == ReportStatus.FAILED.value
    # Audit-safe sections were persisted, and the report ended failed, not published.
    assert h.result.section_count >= 1
    assert h.lifecycle.op_names() == [
        "create",
        "transition",  # -> grounding_check
        "persist_sections",
        "attach",  # composition ran, so a run id exists even on a blocked brief
        "transition",  # -> failed
    ]
    assert ("publish",) not in h.lifecycle.calls
    h.assert_all_sessions_closed()


# --------------------------------------------------------------------------------------
# Composition degradation flows through (not an exception)
# --------------------------------------------------------------------------------------


def test_a_section_degrading_during_composition_still_publishes() -> None:
    def compose(request):  # noqa: ANN001, ANN202
        if request.context["section_kind"] == "top_event":
            return abstain("nothing citable to say")  # degrade this one section
        return per_claim_blocks(request)

    inputs, context = _busy_brief()
    orch = GateOrchestrator(compose=compose)
    h = _Harness(inputs=inputs, context=context, orch=orch)

    assert h.error is None  # a degraded section is not an exception
    assert h.result.published is True
    assert h.result.gate_outcome is GateOutcome.PASS


# --------------------------------------------------------------------------------------
# Rerun: a new version, a new row, and the prior published version untouched
# --------------------------------------------------------------------------------------


def test_a_rerun_creates_version_2_with_a_reason_and_leaves_version_1_unchanged() -> None:
    lifecycle = FakeLifecycleRepo()
    inputs, context = _busy_brief()

    first = _Harness(inputs=inputs, context=context, lifecycle=lifecycle)
    assert first.result.version == 1
    assert first.result.change_reason is None  # v1 stores no reason (the default is dropped)
    v1_id = lifecycle.only_row().id
    v1_snapshot = lifecycle.get_report(v1_id)

    second = _Harness(
        inputs=inputs,
        context=context,
        lifecycle=lifecycle,
        change_reason="reprocessed upstream event",
    )
    assert second.result.version == 2
    assert second.result.change_reason == "reprocessed upstream event"
    # Two distinct rows now exist; the prior published version is byte-for-byte unchanged.
    assert len(lifecycle.rows) == 2
    assert lifecycle.get_report(v1_id) == v1_snapshot
    assert v1_snapshot.status == ReportStatus.PUBLISHED.value


def test_a_scheduled_run_uses_a_nonblank_default_change_reason_for_reruns() -> None:
    # The coordinator always passes a nonblank reason; version 1 drops it, a rerun keeps the default.
    lifecycle = FakeLifecycleRepo()
    inputs, context = _busy_brief()
    _Harness(inputs=inputs, context=context, lifecycle=lifecycle)  # v1
    second = _Harness(inputs=inputs, context=context, lifecycle=lifecycle)  # v2, no caller reason
    assert second.result.version == 2
    assert second.result.change_reason == DEFAULT_CHANGE_REASON


# --------------------------------------------------------------------------------------
# Unexpected failure: rollback, then the SAME version marked failed in a clean transaction
# --------------------------------------------------------------------------------------


def test_an_unexpected_fault_rolls_back_and_marks_the_same_version_failed() -> None:
    inputs, _context = _busy_brief()

    def boom(session, i):  # noqa: ANN001, ANN202
        raise RuntimeError("context load exploded")

    h = _Harness(inputs=inputs, context=None, context_builder=boom)

    assert h.result is None
    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.brief_date == BRIEF_DATE
    assert h.error.original_cause_type == "RuntimeError"
    assert "context load exploded" in h.error.original_cause_text
    assert h.error.durable_failure_marked is True
    assert h.error.cleanup_error is None

    # No second version was created just to record the failure; the same row is now failed.
    assert len(h.lifecycle.rows) == 1
    row = h.lifecycle.only_row()
    assert row.status == ReportStatus.FAILED.value
    assert h.error.report_id == row.id
    # The main transaction rolled back and never committed; no sections were persisted.
    main = h.session_factory.sessions[1]
    assert main.rollback_count == 1 and main.commit_count == 0
    assert "persist_sections" not in h.lifecycle.op_names()
    # start + main + a clean failure session, all closed.
    assert len(h.session_factory.sessions) == 3
    assert h.rec_orch.close_count == 1
    h.assert_all_sessions_closed()


def test_model_failure_and_orchestrator_close_failure_are_both_preserved() -> None:
    inputs, _context = _busy_brief()
    model_error = RuntimeError("context load exploded")
    close_error = RuntimeError("provider client close exploded")

    def boom(_session, _inputs):  # noqa: ANN001, ANN202
        raise model_error

    h = _Harness(
        inputs=inputs,
        context=None,
        context_builder=boom,
        orchestrator_close_error=close_error,
    )

    assert isinstance(h.error, DailyBriefGenerationError)
    assert isinstance(h.error.cause, ExceptionGroup)
    assert list(h.error.cause.exceptions) == [model_error, close_error]
    assert h.rec_orch.close_count == 1
    main = h.session_factory.sessions[1]
    assert (main.commit_count, main.rollback_count) == (0, 1)
    assert h.lifecycle.only_row().status == ReportStatus.FAILED.value


def test_failure_cleanup_marks_failed_in_a_fresh_transaction_that_commits() -> None:
    inputs, _context = _busy_brief()

    def boom(session, i):  # noqa: ANN001, ANN202
        raise ValueError("selection blew up")

    h = _Harness(inputs=inputs, context=None, context_builder=boom)
    failure_session = h.session_factory.sessions[2]
    assert failure_session.commit_count == 1  # the failure marker is durably committed
    assert failure_session.rollback_count == 0


def test_a_cleanup_failure_is_surfaced_with_both_causes() -> None:
    inputs, _context = _busy_brief()
    lifecycle = FakeLifecycleRepo()
    lifecycle.fail_transition_to_failed = True  # the failure-marking transition itself fails

    def boom(session, i):  # noqa: ANN001, ANN202
        raise RuntimeError("original mid-pipeline fault")

    h = _Harness(inputs=inputs, context=None, context_builder=boom, lifecycle=lifecycle)

    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.durable_failure_marked is False
    assert h.error.cleanup_error is not None
    # Both causes are visible in the error message -- fail loud, swallow neither.
    assert "original mid-pipeline fault" in str(h.error)
    assert "row lock failed during failure cleanup" in str(h.error)
    # Even a failed cleanup closes every session it opened.
    h.assert_all_sessions_closed()


# --------------------------------------------------------------------------------------
# The returned result is immutable and holds no ORM object or session
# --------------------------------------------------------------------------------------


def test_the_result_is_frozen_and_holds_only_primitives() -> None:
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)
    result = h.result

    assert isinstance(result, DailyBriefGenerationResult)
    assert isinstance(result.report, ReportSnapshot)
    assert dataclasses.is_dataclass(result) and dataclasses.is_dataclass(result.report)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.published = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.report.status = "tampered"  # type: ignore[misc]
    # Useful primitive facts, no session/ORM handles.
    assert result.selected_event_ids == (inputs.top_events[0].event_id,)
    assert result.selected_event_count == 1
    assert result.section_count >= 2
    assert result.report.brief_date == BRIEF_DATE


def test_the_result_reports_selected_event_ids_and_counts() -> None:
    inputs, context = _busy_brief()
    h = _Harness(inputs=inputs, context=context)
    assert h.result.selected_event_count == 1
    assert len(h.result.selected_event_ids) == 1
    assert h.result.quiet_day is False


# --------------------------------------------------------------------------------------
# The production orchestrator factory: audit rows share the transaction (commit_on_write=False)
# --------------------------------------------------------------------------------------


def test_production_orchestrator_factory_builds_flush_only_for_the_session(monkeypatch) -> None:  # noqa: ANN001
    # The factory contract (clause 10): the orchestrator's LLM audit rows must share the
    # coordinator's transaction, so it is built ``commit_on_write=False`` for the given session.
    import services.llm.runtime as runtime

    captured: dict = {}

    def fake_build(*, settings, session, redis_client, commit_on_write=True):  # noqa: ANN001, ANN202
        captured.update(
            settings=settings,
            session=session,
            redis_client=redis_client,
            commit_on_write=commit_on_write,
        )
        return "orchestrator-sentinel"

    monkeypatch.setattr(runtime, "build_production_orchestrator", fake_build)

    settings, redis_client, session = object(), object(), object()
    factory = build_generation_orchestrator_factory(settings=settings, redis_client=redis_client)
    orchestrator = factory(session)

    assert orchestrator == "orchestrator-sentinel"
    assert captured["commit_on_write"] is False  # flush-only: audit rows share the main transaction
    assert captured["session"] is session
    assert captured["settings"] is settings
    assert captured["redis_client"] is redis_client


# --------------------------------------------------------------------------------------
# Post-durable faults never escape raw: acquisition, rollback, and close failures on every
# path end in a typed DailyBriefGenerationError, with all causes preserved and no version split.
# --------------------------------------------------------------------------------------


def test_main_session_acquisition_failure_marks_the_same_version_failed() -> None:
    # The 2nd session_factory() call (the main session) raises after the durable start. The clean
    # failure path opens a later session and marks the SAME row failed -- never a second version.
    inputs, context = _busy_brief()
    boom = ConnectionError("main session pool exhausted")
    h = _Harness(
        inputs=inputs,
        context=context,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(rec, raise_on={1: boom}),
    )

    assert h.result is None
    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.cause is boom
    assert h.error.original_cause_type == "ConnectionError"
    assert h.error.durable_failure_marked is True  # a later failure session marked it
    assert h.error.cleanup_error is None
    assert len(h.lifecycle.rows) == 1  # no second version created to record the failure
    row = h.lifecycle.only_row()
    assert row.status == ReportStatus.FAILED.value
    assert h.error.report_id == row.id
    # start (call 0) + failed main acquisition (call 1, no session) + failure cleanup (call 2).
    assert h.session_factory.call_count == 3
    assert len(h.session_factory.sessions) == 2  # only the two that actually opened
    assert "compose" not in h.recorder  # _run_main never entered
    h.assert_all_sessions_closed()


def test_failure_session_acquisition_failure_surfaces_original_cause_and_acquisition_error() -> (
    None
):
    # The 3rd session_factory() call (the failure cleanup session) raises. The original pipeline
    # cause is preserved, the acquisition exception becomes cleanup_error, and marked stays False.
    inputs, _context = _busy_brief()
    acquire_boom = ConnectionError("failure session pool exhausted")

    def boom(session, i):  # noqa: ANN001, ANN202
        raise RuntimeError("mid-pipeline fault")

    h = _Harness(
        inputs=inputs,
        context=None,
        context_builder=boom,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(
            rec, raise_on={2: acquire_boom}
        ),
    )

    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.original_cause_type == "RuntimeError"
    assert "mid-pipeline fault" in h.error.original_cause_text
    assert h.error.durable_failure_marked is False  # no failure session ever opened
    assert h.error.cleanup_error is acquire_boom
    # The durable row could not be marked and is left generating -- never rewritten.
    assert h.lifecycle.only_row().status == ReportStatus.GENERATING.value
    main = h.session_factory.sessions[1]
    assert main.rollback_count == 1 and main.close_count >= 1  # main rolled back+closed first
    assert h.session_factory.call_count == 3
    h.assert_all_sessions_closed()


def test_main_rollback_failure_still_marks_failed_and_surfaces_the_rollback_error() -> None:
    inputs, _context = _busy_brief()
    rollback_boom = RuntimeError("main rollback failed")

    def boom(session, i):  # noqa: ANN001, ANN202
        raise ValueError("mid-pipeline fault")

    h = _Harness(
        inputs=inputs,
        context=None,
        context_builder=boom,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(
            rec, specs={1: {"rollback_error": rollback_boom}}
        ),
    )

    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.original_cause_type == "ValueError"
    assert h.error.durable_failure_marked is True  # the failure marker still committed
    assert h.error.cleanup_error is rollback_boom
    assert h.lifecycle.only_row().status == ReportStatus.FAILED.value
    main = h.session_factory.sessions[1]
    assert (
        main.rollback_count == 1 and main.close_count >= 1
    )  # close attempted after rollback raised
    h.assert_all_sessions_closed()


def test_main_close_failure_during_cleanup_still_marks_failed() -> None:
    inputs, _context = _busy_brief()
    close_boom = RuntimeError("main close failed")

    def boom(session, i):  # noqa: ANN001, ANN202
        raise ValueError("mid-pipeline fault")

    h = _Harness(
        inputs=inputs,
        context=None,
        context_builder=boom,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(
            rec, specs={1: {"close_error": close_boom}}
        ),
    )

    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.durable_failure_marked is True
    assert h.error.cleanup_error is close_boom
    assert h.lifecycle.only_row().status == ReportStatus.FAILED.value
    main = h.session_factory.sessions[1]
    assert main.rollback_count == 1 and main.close_count >= 1
    failure = h.session_factory.sessions[2]
    assert failure.commit_count == 1  # a fresh session committed the failed marker
    h.assert_all_sessions_closed()


def test_main_rollback_and_close_both_failing_are_aggregated_deterministically() -> None:
    # Two coinciding cleanup faults must both survive, in occurrence order, as an ExceptionGroup.
    inputs, _context = _busy_brief()
    rollback_boom = RuntimeError("main rollback failed")
    close_boom = RuntimeError("main close failed")

    def boom(session, i):  # noqa: ANN001, ANN202
        raise ValueError("mid-pipeline fault")

    h = _Harness(
        inputs=inputs,
        context=None,
        context_builder=boom,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(
            rec, specs={1: {"rollback_error": rollback_boom, "close_error": close_boom}}
        ),
    )

    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.durable_failure_marked is True  # the same row was still marked failed
    group = h.error.cleanup_error
    assert isinstance(group, ExceptionGroup)
    assert list(group.exceptions) == [rollback_boom, close_boom]  # both, in order, none discarded
    assert h.lifecycle.only_row().status == ReportStatus.FAILED.value
    h.assert_all_sessions_closed()


def test_failure_transaction_rollback_failure_preserves_every_cause() -> None:
    # The failure transition raises AND the failure session's rollback raises: original cause plus
    # both cleanup faults are preserved; the failure close is still attempted after the rollback.
    inputs, _context = _busy_brief()
    lifecycle = FakeLifecycleRepo()
    lifecycle.fail_transition_to_failed = True
    failure_rollback_boom = RuntimeError("failure rollback failed")

    def boom(session, i):  # noqa: ANN001, ANN202
        raise RuntimeError("mid-pipeline fault")

    h = _Harness(
        inputs=inputs,
        context=None,
        context_builder=boom,
        lifecycle=lifecycle,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(
            rec, specs={2: {"rollback_error": failure_rollback_boom}}
        ),
    )

    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.durable_failure_marked is False  # the failed-state commit never happened
    assert "mid-pipeline fault" in h.error.original_cause_text
    group = h.error.cleanup_error
    assert isinstance(group, ExceptionGroup)
    assert [str(e) for e in group.exceptions] == [
        "row lock failed during failure cleanup",  # the transition fault
        "failure rollback failed",  # then the failure-session rollback fault
    ]
    failure = h.session_factory.sessions[2]
    assert (
        failure.rollback_count == 1 and failure.close_count >= 1
    )  # close attempted despite rollback
    h.assert_all_sessions_closed()


def test_failure_transaction_close_failure_is_surfaced_after_a_committed_marker() -> None:
    inputs, _context = _busy_brief()
    failure_close_boom = RuntimeError("failure close failed")

    def boom(session, i):  # noqa: ANN001, ANN202
        raise RuntimeError("mid-pipeline fault")

    h = _Harness(
        inputs=inputs,
        context=None,
        context_builder=boom,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(
            rec, specs={2: {"close_error": failure_close_boom}}
        ),
    )

    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.durable_failure_marked is True  # the marker committed before the close raised
    assert h.error.cleanup_error is failure_close_boom
    assert h.lifecycle.only_row().status == ReportStatus.FAILED.value
    failure = h.session_factory.sessions[2]
    assert failure.commit_count == 1 and failure.close_count >= 1
    h.assert_all_sessions_closed()


def test_close_after_a_successful_main_commit_routes_through_the_typed_path_without_rewriting() -> (
    None
):
    # A close failure after the report is durably PUBLISHED is a post-durable fault: it raises typed,
    # the reload finds the row terminal and never rewrites it, and marked reflects published != failed.
    inputs, context = _busy_brief()
    main_close_boom = RuntimeError("main close failed post-commit")

    h = _Harness(
        inputs=inputs,
        context=context,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(
            rec, specs={1: {"close_error": main_close_boom}}
        ),
    )

    assert h.result is None
    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.cause is main_close_boom
    row = h.lifecycle.only_row()
    assert row.status == ReportStatus.PUBLISHED.value  # published, and never rewritten
    assert h.error.durable_failure_marked is False  # a published (not failed) report reads unmarked
    assert h.error.cleanup_error is None
    main = h.session_factory.sessions[1]
    assert main.commit_count == 1 and main.close_count >= 1  # one main commit; close attempted
    assert ("transition", ReportStatus.FAILED.value) not in h.lifecycle.calls  # no failed rewrite
    h.assert_all_sessions_closed()


def test_close_after_a_blocked_gate_commit_finds_the_row_already_failed_and_never_rewrites_it() -> (
    None
):
    # A blocked gate durably FAILS the report inside the single main commit (an expected outcome, no
    # raise). A close failure right after is a post-durable fault routed through the typed path: the
    # reload finds the row already terminal `failed`, so it is never rewritten and marked stays True.
    inputs, context = _busy_brief()
    main_close_boom = RuntimeError("main close failed after a blocked-gate commit")
    orch = GateOrchestrator(ground=lambda request: LLMOrchestratorError("grounding unavailable"))

    h = _Harness(
        inputs=inputs,
        context=context,
        orch=orch,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(
            rec, specs={1: {"close_error": main_close_boom}}
        ),
    )

    assert h.result is None
    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.cause is main_close_boom  # the close fault, not the blocked gate, is the cause
    assert h.error.durable_failure_marked is True  # cleanup reloaded an already-terminal FAILED row
    assert h.error.cleanup_error is None
    # The same row is still failed and was never rewritten by cleanup: exactly one FAILED transition
    # (the blocked gate's, inside _run_main), and the failure session committed nothing.
    assert len(h.lifecycle.rows) == 1  # no second report/version was created
    row = h.lifecycle.only_row()
    assert row.status == ReportStatus.FAILED.value
    assert h.error.report_id == row.id
    assert h.lifecycle.op_names() == [
        "create",
        "transition",  # -> grounding_check
        "persist_sections",
        "attach",  # composition ran, so a run id exists even on a blocked brief
        "transition",  # -> failed, inside the one main commit (cleanup adds no further op)
    ]
    assert h.lifecycle.calls.count(("transition", ReportStatus.FAILED.value)) == 1  # not re-marked
    assert ("publish",) not in h.lifecycle.calls
    main = h.session_factory.sessions[1]
    assert main.commit_count == 1 and main.close_count >= 1  # one main commit; close attempted
    failure = h.session_factory.sessions[2]
    assert (
        failure.commit_count == 0 and failure.close_count >= 1
    )  # reload only, nothing re-committed
    assert h.session_factory.call_count == 3  # durable start + main + failure cleanup
    h.assert_all_sessions_closed()


def test_close_after_the_durable_start_commit_marks_the_same_row_failed() -> None:
    # A close failure right after the durable-start commit is a post-durable fault: the same
    # generating row is marked failed through the typed path; the main pipeline never runs.
    inputs, context = _busy_brief()
    start_close_boom = RuntimeError("durable-start close failed post-commit")

    h = _Harness(
        inputs=inputs,
        context=context,
        session_factory_builder=lambda rec: ProgrammableSessionFactory(
            rec, specs={0: {"close_error": start_close_boom}}
        ),
    )

    assert h.result is None
    assert isinstance(h.error, DailyBriefGenerationError)
    assert h.error.cause is start_close_boom
    assert h.error.durable_failure_marked is True
    assert h.error.cleanup_error is None
    row = h.lifecycle.only_row()
    assert row.status == ReportStatus.FAILED.value
    assert h.error.report_id == row.id
    assert "compose" not in h.recorder  # _run_main was never entered
    assert h.session_factory.call_count == 2  # durable start (call 0) + failure cleanup (call 1)
    start = h.session_factory.sessions[0]
    assert (
        start.commit_count == 1 and start.close_count >= 1
    )  # committed durable, then close raised
    h.assert_all_sessions_closed()
