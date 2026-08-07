"""The daily-brief Celery task: brief_date derivation, exact coordinator wiring, and dispositions.

No live broker, Redis, or Postgres. Everything the task reaches -- the Redis client, the
orchestrator factory, ``SessionLocal``, and the coordinator itself -- is patched, so a fake can
prove the contract: the coordinator owns every session (the task opens none), a blocked gate is a
completed non-raising attempt, an unexpected fault re-raises for Stage1Task, and the Redis client
is closed and the logging job context cleared on every path.
"""

from __future__ import annotations

import datetime
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from packages.config import metrics
from packages.config.logging import job_id_var
from services.reports import (
    DailyBriefGenerationError,
    DailyBriefGenerationResult,
    GateOutcome,
    window_for_date,
)
from services.reports.lifecycle import ReportStatus
from services.reports.window import BRIEF_TIMEZONE
from workers import report_tasks
from workers.celery_app import QUEUE_PIPELINE, Stage1Task, celery_app

ET = BRIEF_TIMEZONE
BRIEF_DATE = datetime.date(2026, 7, 14)
REPORT_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
EVENT_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
RUN_ID = uuid.UUID("88888888-8888-4888-8888-888888888888")


class FakeRedis:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeRedisCloseRaises:
    """A Redis client whose pool cleanup fails: proves ``close`` is best-effort, never fatal.

    Records that close was attempted, then raises -- the task must swallow the fault, leave the
    already-determined report/job disposition intact, and still clear the job context.
    """

    def __init__(self) -> None:
        self.close_attempts = 0

    def close(self) -> None:
        self.close_attempts += 1
        raise RuntimeError("redis pool close failed")


class FakeRedisCloseTimesOut:
    """A Redis client whose ``close`` is interrupted by the worker's soft time limit.

    Celery raises ``SoftTimeLimitExceeded`` in whatever frame is executing when the limit lands,
    including the cleanup one. That is not a pool-cleanup fault, so it must not be swallowed.
    """

    def __init__(self) -> None:
        self.close_attempts = 0

    def close(self) -> None:
        self.close_attempts += 1
        raise SoftTimeLimitExceeded


class SessionFactorySpy:
    """Stands in for ``SessionLocal``: the task must pass it through, never call it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("the worker must not open a session; the coordinator owns sessions")


def _result(
    *,
    published: bool = True,
    gate: GateOutcome = GateOutcome.PASS,
    quiet_day: bool = False,
    version: int = 1,
    section_count: int = 6,
    event_ids: tuple[uuid.UUID, ...] = (EVENT_ID,),
    change_reason: str | None = "scheduled daily brief generation",
    run_id: uuid.UUID | None = RUN_ID,
) -> DailyBriefGenerationResult:
    from services.reports.lifecycle import ReportSnapshot

    status = ReportStatus.PUBLISHED.value if published else ReportStatus.FAILED.value
    report = ReportSnapshot(
        id=REPORT_ID,
        user_id=None,
        report_type="daily_brief",
        brief_date=BRIEF_DATE,
        event_id=None,
        title="Daily Brief",
        status=status,
        version=version,
        change_reason=change_reason,
        stale=False,
        generated_by_run_id=run_id,
        created_at=None,
        updated_at=None,
    )
    return DailyBriefGenerationResult(
        report=report,
        brief_date=BRIEF_DATE,
        window=window_for_date(BRIEF_DATE),
        selected_event_ids=tuple(event_ids),
        selected_event_count=len(event_ids),
        quiet_day=quiet_day,
        gate_outcome=gate,
        published=published,
        section_count=section_count,
        version=version,
        change_reason=change_reason,
        generated_by_run_id=run_id,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: DailyBriefGenerationResult | None = None,
    raises: BaseException | None = None,
    redis_client: Any | None = None,
) -> dict[str, Any]:
    redis_client = FakeRedis() if redis_client is None else redis_client
    session_factory = SessionFactorySpy()
    orchestrator_factory = object()  # a sentinel the coordinator would receive
    state: dict[str, Any] = {
        "redis": redis_client,
        "session_factory": session_factory,
        "orchestrator_factory": orchestrator_factory,
        "factory_kwargs": {},
        "calls": [],
    }

    def _generate(brief_date: Any, **kwargs: Any) -> DailyBriefGenerationResult:
        state["calls"].append({"brief_date": brief_date, **kwargs})
        if raises is not None:
            raise raises
        return result if result is not None else _result()

    def _build_factory(**kwargs: Any) -> Any:
        state["factory_kwargs"] = kwargs
        return orchestrator_factory

    monkeypatch.setattr(report_tasks, "build_redis_client", lambda: redis_client)
    monkeypatch.setattr(report_tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(report_tasks, "build_generation_orchestrator_factory", _build_factory)
    monkeypatch.setattr(report_tasks, "generate_daily_brief", _generate)
    metrics.reset()
    job_id_var.set(None)
    return state


# --- registration ----------------------------------------------------------------------
def test_task_registered_under_exact_name_on_pipeline_queue() -> None:
    task = celery_app.tasks["generate_daily_brief"]
    assert isinstance(task, Stage1Task)
    assert task.queue == QUEUE_PIPELINE
    assert report_tasks.TASK_NAME == "generate_daily_brief"


# --- brief_date derivation and validation ----------------------------------------------
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Before 05:30 ET -> the previous ET calendar date.
        (datetime.datetime(2026, 7, 14, 5, 29, tzinfo=ET), datetime.date(2026, 7, 13)),
        # Exactly at 05:30 ET (inclusive cutoff) -> that ET date.
        (datetime.datetime(2026, 7, 14, 5, 30, tzinfo=ET), datetime.date(2026, 7, 14)),
        # After 05:30 ET -> that ET date.
        (datetime.datetime(2026, 7, 14, 6, 0, tzinfo=ET), datetime.date(2026, 7, 14)),
    ],
)
def test_scheduled_brief_date_respects_the_0530_cutoff(
    moment: datetime.datetime, expected: datetime.date
) -> None:
    assert report_tasks._scheduled_brief_date(moment) == expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Spring-forward day: after 05:30 EDT -> that date.
        (datetime.datetime(2026, 3, 8, 6, 0, tzinfo=ET), datetime.date(2026, 3, 8)),
        # Fall-back day: before 05:30 EST -> the previous date.
        (datetime.datetime(2026, 11, 1, 5, 0, tzinfo=ET), datetime.date(2026, 10, 31)),
    ],
)
def test_scheduled_brief_date_on_dst_dates(
    moment: datetime.datetime, expected: datetime.date
) -> None:
    assert report_tasks._scheduled_brief_date(moment) == expected


def test_explicit_iso_date_is_parsed() -> None:
    assert report_tasks._parse_explicit_brief_date("2026-07-14") == datetime.date(2026, 7, 14)
    assert report_tasks._parse_explicit_brief_date(datetime.date(2026, 7, 14)) == BRIEF_DATE


def test_explicit_brief_date_is_validated_strictly() -> None:
    with pytest.raises(ValueError, match="ISO date"):
        report_tasks._parse_explicit_brief_date("2026-07-32")
    with pytest.raises(ValueError, match="ISO date"):
        report_tasks._parse_explicit_brief_date("not-a-date")
    with pytest.raises(TypeError, match="not a datetime"):
        report_tasks._parse_explicit_brief_date(datetime.datetime(2026, 7, 14, 5, 30, tzinfo=ET))


def test_noncanonical_iso_forms_are_rejected() -> None:
    # Python 3.11+ ``date.fromisoformat`` also accepts these ISO 8601 spellings -- both parse to a
    # valid 2026-07-14 -- but they are not the canonical ``YYYY-MM-DD`` the brief_date identity key
    # requires, so the strict parser must reject them rather than silently normalize.
    with pytest.raises(ValueError, match="ISO date"):
        report_tasks._parse_explicit_brief_date("20260714")  # compact basic format
    with pytest.raises(ValueError, match="ISO date"):
        report_tasks._parse_explicit_brief_date("2026-W29-2")  # ISO week date


# --- exact coordinator wiring ----------------------------------------------------------
def test_it_passes_an_explicit_date_and_the_coordinator_owns_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install(monkeypatch)

    report_tasks.run_daily_brief_generation("2026-07-14")

    call = state["calls"][0]
    assert call["brief_date"] == BRIEF_DATE  # an explicit datetime.date, not a now-window
    assert call["session_factory"] is state["session_factory"]  # SessionLocal handed through...
    assert state["session_factory"].calls == 0  # ...and never called by the worker
    # The orchestrator is built for the coordinator's session via the accepted factory hook, with
    # the task's Redis client -- the worker makes no direct LLM provider call.
    assert call["orchestrator_factory"] is state["orchestrator_factory"]
    assert state["factory_kwargs"]["redis_client"] is state["redis"]
    assert "settings" in state["factory_kwargs"]
    assert call["prediction_backed_outputs_enabled"] is False


def test_worker_threads_an_explicit_open_gate_to_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install(monkeypatch)
    monkeypatch.setattr(
        report_tasks,
        "get_settings",
        lambda: SimpleNamespace(crisis_prediction_reads_enabled=True),
    )

    report_tasks.run_daily_brief_generation("2026-07-14")

    assert state["calls"][0]["prediction_backed_outputs_enabled"] is True


def test_a_none_argument_derives_the_date_from_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install(monkeypatch)
    monkeypatch.setattr(
        report_tasks, "_scheduled_brief_date", lambda now: datetime.date(2026, 2, 2)
    )

    report_tasks.run_daily_brief_generation()

    assert state["calls"][0]["brief_date"] == datetime.date(2026, 2, 2)


# --- dispositions: published, quiet, blocked, errored ----------------------------------
def test_a_published_brief_is_a_succeeded_job_with_a_serializable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install(monkeypatch)

    payload = report_tasks.run_daily_brief_generation("2026-07-14")

    assert payload["status"] == "published"
    assert payload["state"] == "succeeded"
    assert payload["published"] is True
    assert payload["report_id"] == str(REPORT_ID)
    assert payload["brief_date"] == "2026-07-14"
    assert payload["version"] == 1
    assert payload["gate_outcome"] == GateOutcome.PASS.value
    assert payload["selected_event_count"] == 1
    assert payload["selected_event_ids"] == [str(EVENT_ID)]
    assert payload["quiet_day"] is False
    assert payload["section_count"] == 6
    assert payload["change_reason"] == "scheduled daily brief generation"
    assert payload["generated_by_run_id"] == str(RUN_ID)
    assert payload["job_key"].startswith("generate_daily_brief:")
    assert json.loads(json.dumps(payload)) == payload

    assert metrics.get(metrics.JOB_SUCCESSES) == 1
    assert metrics.get(metrics.JOB_FAILURES) == 0
    assert state["redis"].closed is True
    assert job_id_var.get() is None


def test_a_quiet_day_brief_still_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        result=_result(quiet_day=True, event_ids=(), run_id=None),
    )

    payload = report_tasks.run_daily_brief_generation("2026-07-14")

    assert payload["status"] == "published"
    assert payload["state"] == "succeeded"
    assert payload["quiet_day"] is True
    assert payload["selected_event_ids"] == []
    assert payload["generated_by_run_id"] is None
    assert metrics.get(metrics.JOB_SUCCESSES) == 1


def test_a_blocked_gate_does_not_raise_and_is_a_nonretryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install(
        monkeypatch,
        result=_result(published=False, gate=GateOutcome.BLOCKED, version=2),
    )

    payload = report_tasks.run_daily_brief_generation("2026-07-14")

    # An expected completed attempt: no raise, no retry. Still not a published brief.
    assert payload["status"] == "blocked"
    assert payload["state"] == "failed"
    assert payload["published"] is False
    assert payload["gate_outcome"] == GateOutcome.BLOCKED.value
    assert payload["report_id"] == str(REPORT_ID)  # the durable report is named, semantically true
    assert payload["version"] == 2
    assert json.loads(json.dumps(payload)) == payload

    assert metrics.get(metrics.JOB_FAILURES) == 1
    assert metrics.get(metrics.JOB_SUCCESSES) == 0
    assert state["redis"].closed is True
    assert job_id_var.get() is None


def test_an_unexpected_generation_error_reraises_and_records_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = DailyBriefGenerationError(
        report_id=REPORT_ID,
        brief_date=BRIEF_DATE,
        cause=RuntimeError("provider exhausted"),
        durable_failure_marked=True,
    )
    state = _install(monkeypatch, raises=error)

    with pytest.raises(DailyBriefGenerationError):
        report_tasks.run_daily_brief_generation("2026-07-14")

    assert metrics.get(metrics.JOB_FAILURES) == 1
    assert metrics.get(metrics.JOB_SUCCESSES) == 0
    assert state["redis"].closed is True  # closed even on the raising path
    assert job_id_var.get() is None  # job context cleared even on the raising path


def test_an_arbitrary_exception_also_reraises_and_records_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install(monkeypatch, raises=ValueError("unexpected"))

    with pytest.raises(ValueError, match="unexpected"):
        report_tasks.run_daily_brief_generation("2026-07-14")

    assert metrics.get(metrics.JOB_FAILURES) == 1
    assert state["redis"].closed is True
    assert job_id_var.get() is None


def test_the_same_brief_date_produces_a_stable_job_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)

    first = report_tasks.run_daily_brief_generation("2026-07-14")
    second = report_tasks.run_daily_brief_generation("2026-07-14")
    other = report_tasks.run_daily_brief_generation("2026-07-15")

    # One brief per (report_type, brief_date): a rerun of the same date is the same job.
    assert first["job_key"] == second["job_key"]
    assert first["job_id"] == second["job_id"]
    assert other["job_key"] != first["job_key"]


# --- Redis pool cleanup is best-effort: a close fault never changes the disposition -----
def test_a_redis_close_failure_does_not_mask_a_published_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedisCloseRaises()
    _install(monkeypatch, redis_client=redis_client)

    # No raise: the close fault is swallowed and the published disposition stands unchanged.
    payload = report_tasks.run_daily_brief_generation("2026-07-14")

    assert redis_client.close_attempts == 1  # close was attempted...
    assert payload["status"] == "published"  # ...but did not change the result
    assert payload["state"] == "succeeded"
    assert payload["published"] is True
    assert metrics.get(metrics.JOB_SUCCESSES) == 1  # metrics stay semantically correct...
    assert metrics.get(metrics.JOB_FAILURES) == 0  # ...no contradictory failure for the cleanup
    assert job_id_var.get() is None  # job context cleared despite the close fault


def test_a_redis_close_failure_does_not_mask_a_blocked_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedisCloseRaises()
    _install(
        monkeypatch,
        result=_result(published=False, gate=GateOutcome.BLOCKED, version=2),
        redis_client=redis_client,
    )

    payload = report_tasks.run_daily_brief_generation("2026-07-14")

    assert redis_client.close_attempts == 1
    assert payload["status"] == "blocked"  # the blocked disposition is unchanged
    assert payload["state"] == "failed"
    assert payload["published"] is False
    assert payload["version"] == 2
    assert metrics.get(metrics.JOB_FAILURES) == 1  # the one expected non-retryable failure...
    assert metrics.get(metrics.JOB_SUCCESSES) == 0  # ...and no spurious success from cleanup
    assert job_id_var.get() is None


def test_a_redis_close_failure_does_not_mask_the_original_generation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedisCloseRaises()
    _install(monkeypatch, raises=ValueError("provider exhausted"), redis_client=redis_client)

    # The generation fault is what propagates to Stage1Task -- not the RuntimeError from close.
    with pytest.raises(ValueError, match="provider exhausted") as excinfo:
        report_tasks.run_daily_brief_generation("2026-07-14")

    assert not isinstance(excinfo.value, RuntimeError)  # the close fault did not replace it
    assert redis_client.close_attempts == 1  # close was still attempted on the raising path
    assert metrics.get(metrics.JOB_FAILURES) == 1  # one failure for the generation error...
    assert metrics.get(metrics.JOB_SUCCESSES) == 0  # ...not an extra one for the cleanup
    assert job_id_var.get() is None  # job context cleared even when both faults occur


# --- A soft time limit is a teardown signal, not a cleanup fault ------------------------
def test_a_soft_time_limit_during_redis_close_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedisCloseTimesOut()
    _install(monkeypatch, redis_client=redis_client)

    # Swallowing this would return a published payload from a worker that is out of time, and,
    # run as a daily-pipeline stage, would hide the timeout from the coordinator's unwind.
    with pytest.raises(SoftTimeLimitExceeded):
        report_tasks.run_daily_brief_generation("2026-07-14")

    assert redis_client.close_attempts == 1
    assert metrics.get(metrics.JOB_SUCCESSES) == 1  # the brief itself did publish before the limit
    assert metrics.get(metrics.JOB_FAILURES) == 0  # no invented failure for the interrupted close
    assert job_id_var.get() is None  # the job context is still cleared on the way out
