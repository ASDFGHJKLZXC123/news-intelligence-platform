"""Celery task wiring the ADR 0009 daily-brief coordinator into a running worker.

One task lives here: :func:`run_daily_brief_generation`, registered under the exact Celery task
name ``generate_daily_brief`` (ADR 0009) and routed to the pipeline queue. Its Beat entry
(``celery_app.REPORT_BEAT_SCHEDULE``) fires it every calendar day at 05:30 ET via the DST-aware
:class:`~workers.celery_app.EasternDailyCrontab`.

Nothing is built, connected, or configured at import, matching :mod:`workers.analogy_tasks`: the
Redis client and the production orchestrator factory are constructed inside the task, so importing
this module opens no Redis socket and no database connection. ``from_url`` builds a lazy Redis
pool, and ``db.base.SessionLocal``'s engine only connects when the coordinator actually opens a
session.

## Session ownership: coordinator transactions plus an independent writer fence

The coordinator (:func:`services.reports.generation.generate_daily_brief`) owns its report SQL
sessions and two-transaction durability contract. This task also holds one independent
writer-mode transaction through the complete call. For a manually queued delivery, that same
fence transaction advances the durable Job row; scheduled calls have no manual row. The
orchestrator is built *for the coordinator's session*, via the accepted
:func:`~services.reports.generation.build_generation_orchestrator_factory` hook
(``commit_on_write=False``), so its LLM audit rows share the report's transaction -- the task
makes no direct LLM provider calls.

## Three outcomes, three job dispositions

* **Published** -- the grounding gate passed and the brief shipped: a succeeded job, a success
  metric, and a JSON-safe ``status="published"`` result.
* **Blocked** -- the grounding/copyright/consistency gate blocked the brief. This is an *expected*
  completed attempt: the coordinator has already durably recorded a ``failed`` report (retaining
  its audit-safe sections) and returned a result rather than raising, so this task does **not**
  raise or retry -- a Celery retry would only mint another version, not change the verdict. It is
  still not a published brief, so the job is a non-retryable failure
  (``Stage1Job.mark_failed(retryable=False)``) and a failure metric, with an accurate
  ``status="blocked"`` result. No bespoke retry exception is invented.
* **Errored** -- an unexpected :class:`~services.reports.generation.DailyBriefGenerationError` (or
  any other exception): a failure metric, a structured exception log, and a re-raise so
  :class:`~workers.celery_app.Stage1Task` retries with backoff. The coordinator has already
  durably marked the version ``failed``; the retry allocates ``version + 1``.

On every path the Redis client is closed (if it was opened) and the logging job context is
cleared. Closing is best-effort: a Redis pool-cleanup failure is logged and swallowed so it can
never mask the already-determined report/job disposition or trigger a spurious retry. The one
exception is a ``SoftTimeLimitExceeded`` raised *by* the close -- that is the worker being torn
down rather than a cleanup fault, so it propagates (the job context is still cleared).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from db.base import SessionLocal
from db.models import Job
from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.config.settings import get_settings
from packages.jobs import Stage1Job
from services.reports import (
    DailyBriefGenerationError,
    DailyBriefGenerationResult,
    build_generation_orchestrator_factory,
    generate_daily_brief,
)
from services.reports.window import BRIEF_TIMEZONE, window_for
from services.writer_mode import LEGACY_DAILY_BRIEF_JOB_TYPE, require_legacy_writer_mode
from workers.celery_app import QUEUE_PIPELINE, Stage1Task

logger = get_logger("workers.report_tasks")

#: The exact Celery task name required by ADR 0009 (and referenced by name in the Beat entry).
TASK_NAME = "generate_daily_brief"


def build_redis_client() -> Any:
    # Imported and constructed here, never at module import: `from_url` builds a lazy connection
    # pool, so no socket opens until the orchestrator actually reads the prompt cache. A separate,
    # real hook point -- swapped for a fake in tests via ``monkeypatch``.
    import redis

    return redis.Redis.from_url(get_settings().redis_url)


def _close(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()


def _scheduled_brief_date(now: datetime.datetime) -> datetime.date:
    """ADR 0009's cutoff derivation: the ET calendar date of the window ending at 05:30 ET.

    At or after 05:30 ET this is the ET date of ``now``; before 05:30 ET it is the previous ET
    date. Reuses :func:`services.reports.window.window_for` so the schedule and the coordinator
    agree on one cutoff rather than reimplementing it.
    """

    return window_for(now).brief_date


def _parse_explicit_brief_date(value: str | datetime.date) -> datetime.date:
    """Strictly coerce a manual/deterministic ``brief_date`` argument to a calendar date.

    Celery serializes task args as JSON, so the real explicit path is an ISO ``YYYY-MM-DD`` string;
    a direct ``datetime.date`` is also accepted for in-process callers. A ``datetime`` is refused
    (a brief is scoped to a calendar date, not an instant), and any non-date string raises.
    """

    if isinstance(value, datetime.datetime):
        msg = f"brief_date must be a calendar date (YYYY-MM-DD), not a datetime: {value!r}"
        raise TypeError(msg)
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.date.fromisoformat(value)
        except ValueError as exc:
            msg = f"brief_date must be an ISO date (YYYY-MM-DD); got {value!r}"
            raise ValueError(msg) from exc
        # Python 3.11+ ``date.fromisoformat`` also accepts non-canonical ISO 8601 spellings
        # (compact ``20260714`` and week dates ``2026-W29-2``, both of which parse to a valid
        # date). A brief_date is a stable identity key, so require the exact ``YYYY-MM-DD``
        # form: the accepted string must round-trip to itself.
        if parsed.isoformat() != value:
            msg = f"brief_date must be an ISO date (YYYY-MM-DD); got {value!r}"
            raise ValueError(msg)
        return parsed
    msg = f"brief_date must be an ISO date string or datetime.date; got {type(value).__name__}"
    raise TypeError(msg)


def _resolve_brief_date(brief_date: str | datetime.date | None) -> datetime.date:
    """The explicit date if one was given, else the ET-cutoff date for the wall clock now."""

    if brief_date is None:
        return _scheduled_brief_date(datetime.datetime.now(BRIEF_TIMEZONE))
    return _parse_explicit_brief_date(brief_date)


def _log_extra(result: DailyBriefGenerationResult) -> dict[str, Any]:
    """The result's headline facts as log extras (none collide with reserved LogRecord keys)."""

    return {
        "brief_date": result.brief_date.isoformat(),
        "report_id": str(result.report.id),
        "version": result.version,
        "published": result.published,
        "gate_outcome": result.gate_outcome.value,
        "selected_event_count": result.selected_event_count,
        "quiet_day": result.quiet_day,
        "section_count": result.section_count,
    }


def _serialize(job: Stage1Job, result: DailyBriefGenerationResult) -> dict[str, Any]:
    """The task's JSON-safe return: the job identity/state plus the coordinator's frozen facts."""

    return {
        "status": "published" if result.published else "blocked",
        "job_id": job.job_id,
        "job_key": job.job_key,
        "state": job.state.value,
        "report_id": str(result.report.id),
        "brief_date": result.brief_date.isoformat(),
        "version": result.version,
        "selected_event_count": result.selected_event_count,
        "selected_event_ids": [str(event_id) for event_id in result.selected_event_ids],
        "quiet_day": result.quiet_day,
        "gate_outcome": result.gate_outcome.value,
        "published": result.published,
        "section_count": result.section_count,
        "change_reason": result.change_reason,
        "generated_by_run_id": (
            None if result.generated_by_run_id is None else str(result.generated_by_run_id)
        ),
    }


def _finish(job: Stage1Job, result: DailyBriefGenerationResult) -> dict[str, Any]:
    """Turn a completed coordinator result into a job disposition + JSON-safe return, never raising.

    A published brief is a succeeded job; a blocked one is a non-retryable failed job (an expected
    completed attempt that did not publish -- see the module docstring). Neither raises.
    """

    if result.published:
        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        logger.info("daily brief published", extra=_log_extra(result))
        return _serialize(completed, result)

    # Blocked gate: an expected, already-durable non-publish. Not a raise (nothing to retry), but
    # not a success either -- the brief did not ship as published, so the job records a
    # non-retryable failure and the failure metric, and the result says so.
    metrics.increment(metrics.JOB_FAILURES)
    completed = job.mark_failed(
        "grounding_gate_blocked",
        f"daily brief for {result.brief_date.isoformat()} was blocked by the "
        f"{result.gate_outcome.value} gate and not published",
        retryable=False,
    )
    logger.warning("daily brief blocked", extra=_log_extra(result))
    return _serialize(completed, result)


def _failure_extra(brief_date: datetime.date, cause: BaseException) -> dict[str, Any]:
    """Exception log extras: the brief date, plus the coordinator's structured failure fields."""

    extra: dict[str, Any] = {"brief_date": brief_date.isoformat()}
    if isinstance(cause, DailyBriefGenerationError):
        extra.update(
            report_id=str(cause.report_id),
            original_cause_type=cause.original_cause_type,
            durable_failure_marked=cause.durable_failure_marked,
        )
    return extra


def _begin_manual_job(
    session: Any, raw_job_id: str | None, brief_date: datetime.date
) -> Job | dict[str, Any] | None:
    """Lock a broker-backed manual queue claim or identify a duplicate terminal delivery."""

    if raw_job_id is None:
        return None
    try:
        job_id = uuid.UUID(raw_job_id)
    except ValueError as exc:
        raise ValueError("legacy_job_id must be a UUID") from exc
    job = session.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None or job.job_type != LEGACY_DAILY_BRIEF_JOB_TYPE:
        raise RuntimeError("manual daily brief job is unavailable")
    expected_key = f"legacy-daily-brief:{brief_date.isoformat()}:{job.id}"
    if (
        (job.related_ids or {}).get("brief_date") != brief_date.isoformat()
        or job.job_key != expected_key
    ):
        raise RuntimeError("manual daily brief delivery does not match its queued date")
    if job.state == "succeeded":
        return {
            "status": "duplicate_terminal_delivery",
            "job_id": str(job.id),
            "state": job.state,
            "brief_date": (job.related_ids or {}).get("brief_date"),
        }
    if job.state == "failed":
        retryable = isinstance(job.error, dict) and job.error.get("retryable") is True
        if not retryable or not job.safe_to_rerun or job.attempt >= job.max_attempts:
            return {
                "status": "duplicate_terminal_delivery",
                "job_id": str(job.id),
                "state": job.state,
                "brief_date": (job.related_ids or {}).get("brief_date"),
            }
        job.attempt += 1
        job.error = None
        job.state = "running"
    if job.state not in {"queued", "running"}:
        raise RuntimeError(f"manual daily brief job has invalid state {job.state!r}")
    job.state = "running"
    session.flush()
    return job


def _finish_manual_job(session: Any, job: Job | None, result: DailyBriefGenerationResult) -> None:
    if job is None:
        return
    job.state = "succeeded" if result.published else "failed"
    job.error = None
    if not result.published:
        job.safe_to_rerun = False
        job.error = {
            "code": "grounding_gate_blocked",
            "message": "daily brief generation completed without publishing",
            "retryable": False,
        }
    job.related_ids = {
        **(job.related_ids or {}),
        "report_id": str(result.report.id),
        "published": result.published,
    }
    session.commit()


def _fail_manual_job(session: Any, job: Job | None) -> None:
    if job is None:
        return
    job.state = "failed"
    job.error = {
        "code": "daily_brief_generation_failed",
        "message": "daily brief generation failed",
        "retryable": True,
    }
    session.commit()


@shared_task(name=TASK_NAME, base=Stage1Task, queue=QUEUE_PIPELINE)
def run_daily_brief_generation(
    brief_date: str | None = None, legacy_job_id: str | None = None
) -> dict[str, Any]:
    """Generate, ground, and publish-or-fail the daily brief for one ``brief_date`` (ADR 0009).

    ``brief_date`` is normally ``None`` -- the Beat entry fires with no args and the ET-cutoff date
    is derived here at run time; a manual/deterministic call may pass an explicit ISO ``YYYY-MM-DD``
    string. Either way an explicit :class:`datetime.date` is handed to the coordinator, never a
    rolling now-window.
    """

    resolved = _resolve_brief_date(brief_date)
    # The brief_date *is* this job's identity: one brief per (report_type='daily_brief', brief_date)
    # (ADR 0009), so two scheduled runs for the same ET date share one stable job_key/job_id and are
    # recognizable as reruns of the same brief.
    job = Stage1Job.create(TASK_NAME, {"brief_date": resolved.isoformat()}).mark_running()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)

    redis_client: Any = None
    fence_session: Any = None
    durable_job: Job | None = None
    try:
        fence_session = SessionLocal()
        # Keep this independent read transaction open through every coordinator transaction, so
        # personal setup cannot switch writer modes between report stages or provider calls.
        require_legacy_writer_mode(fence_session, lock=True)
        job_or_terminal = _begin_manual_job(fence_session, legacy_job_id, resolved)
        if isinstance(job_or_terminal, dict):
            return job_or_terminal
        durable_job = job_or_terminal
        redis_client = build_redis_client()
        settings = get_settings()
        orchestrator_factory = build_generation_orchestrator_factory(
            settings=settings, redis_client=redis_client
        )
        # The coordinator owns the session: `SessionLocal` is handed in as the factory and this
        # task opens/commits nothing itself. A blocked gate comes back as a result (not an
        # exception) and is dispositioned by `_finish`; only an unexpected fault raises below.
        result = generate_daily_brief(
            resolved,
            session_factory=SessionLocal,
            orchestrator_factory=orchestrator_factory,
            prediction_backed_outputs_enabled=settings.crisis_prediction_reads_enabled,
        )
        _finish_manual_job(fence_session, durable_job, result)
        return _finish(job, result)
    except Exception as cause:  # noqa: BLE001 -- failure metric + structured log, then re-raise
        # DailyBriefGenerationError or anything else: the coordinator already durably marked the
        # version `failed`, so a Stage1Task retry safely allocates version + 1.
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("daily brief generation failed", extra=_failure_extra(resolved, cause))
        if fence_session is not None:
            try:
                _fail_manual_job(fence_session, durable_job)
            except Exception:  # noqa: BLE001 -- retain original task failure
                fence_session.rollback()
                logger.exception(
                    "failed to persist manual daily brief job failure",
                    extra={"brief_date": resolved.isoformat()},
                )
        raise
    finally:
        # Closing the Redis pool is best-effort cleanup, never part of the report/job disposition.
        # If ``close`` raises it must not mask the return value (a published/blocked result) or the
        # original generation exception, must not make Stage1Task retry an already-terminal report,
        # and must not add a contradictory failure metric: log it and swallow it. The job context is
        # then cleared unconditionally so no job_id leaks into a later task on this worker thread.
        try:
            if redis_client is not None:
                try:
                    _close(redis_client)
                except SoftTimeLimitExceeded:
                    # Not a pool-cleanup fault: the worker is being torn down, and Celery raises
                    # this in whatever frame happens to be executing -- including this one.
                    # Swallowing it would let the task return normally and burn the graceful
                    # window before the hard kill; run as a daily-pipeline stage it would also
                    # hide the timeout from the coordinator whose unwind durably fails the date.
                    raise
                except Exception:  # noqa: BLE001 -- cleanup fault is non-fatal: logged, not raised
                    logger.warning(
                        "failed to close redis client after daily brief generation",
                        exc_info=True,
                        extra={"brief_date": resolved.isoformat()},
                    )
        finally:
            try:
                if fence_session is not None:
                    fence_session.rollback()
            finally:
                if fence_session is not None:
                    fence_session.close()
                # Still unconditional: the re-raise above must not leak this thread's job context.
                set_job_id(None)
