"""Database writer-mode fences shared by legacy and personal entry points."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Job
from services.personal.processing import lock_processing_control


class LegacyWriterModeConflict(RuntimeError):
    """A legacy writer attempted work after the personal writer was activated."""


LEGACY_DAILY_BRIEF_JOB_TYPE = "legacy_daily_brief"


def require_legacy_writer_mode(session: Session, *, lock: bool = True) -> None:
    """Hold the singleton fence for the caller's write transaction.

    Small non-SQLAlchemy doubles used by focused unit tests cannot acquire a database lock. The
    deployed ``SessionLocal`` boundary always returns a SQLAlchemy ``Session`` and is verified
    against PostgreSQL in the writer-mode integration tests.
    """

    if not isinstance(session, Session):
        return
    from packages.config.settings import get_settings

    if get_settings().personal_processing_mode != "legacy":
        raise LegacyWriterModeConflict(
            "legacy processing is unavailable while configured personal processing mode is selected"
        )
    try:
        mode = lock_processing_control(session, lock="exclusive" if lock else "read")
    except RuntimeError as exc:
        raise LegacyWriterModeConflict("legacy writer mode is not initialized") from exc
    if mode.mode != "legacy":
        raise LegacyWriterModeConflict(
            "legacy processing is unavailable while the personal writer mode is active"
        )
    if (
        mode.active_run_id is not None
        or mode.unconfirmed_child
        or any(not entry.get("exited", False) for entry in (mode.child_history or {}).values())
    ):
        raise LegacyWriterModeConflict(
            "a personal writer is queued, running, or awaiting confirmed child exit"
        )
    active_personal = session.execute(
        select(Job.id)
        .where(Job.job_type == "personal_daily", Job.state.in_(("queued", "running")))
        .limit(1)
    ).first()
    if active_personal is not None:
        raise LegacyWriterModeConflict("a personal writer is queued or running")


def require_legacy_maintenance_mode(session: Session) -> None:
    """Supported data maintenance runs only under the exclusive idle legacy fence."""
    require_legacy_writer_mode(session, lock=True)
    if not isinstance(session, Session):
        return
    active = session.scalar(select(Job.id).where(Job.state.in_(("queued", "running"))).limit(1))
    if active is not None:
        raise LegacyWriterModeConflict(
            "maintenance is unavailable while a writer is queued or running"
        )


def queue_legacy_daily_brief(
    session: Session,
    *,
    brief_date: datetime.date,
    task_id: uuid.UUID,
) -> Job:
    """Commit a visible legacy queue claim before the broker receives the task.

    Each manual request deliberately has its own identity because the legacy API is a regeneration
    action which may create a new report version.  The queued row is nevertheless durable, so
    personal setup cannot switch writer modes in the database-to-broker handoff window.
    """

    require_legacy_writer_mode(session, lock=True)
    job = Job(
        id=task_id,
        job_key=f"legacy-daily-brief:{brief_date.isoformat()}:{task_id}",
        job_type=LEGACY_DAILY_BRIEF_JOB_TYPE,
        state="queued",
        attempt=1,
        max_attempts=3,
        related_ids={
            "brief_date": brief_date.isoformat(),
            "celery_task_id": str(task_id),
        },
        safe_to_rerun=True,
    )
    session.add(job)
    session.flush()
    session.commit()
    return job


def mark_legacy_daily_brief_delivery_failed(session: Session, job_id: uuid.UUID) -> None:
    """Terminally release a manual queue claim when broker publication fails."""

    require_legacy_writer_mode(session, lock=True)
    job = session.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None or job.job_type != LEGACY_DAILY_BRIEF_JOB_TYPE:
        raise RuntimeError("legacy daily brief job is unavailable")
    if job.state == "queued":
        job.state = "failed"
        job.error = {
            "code": "broker_enqueue_failed",
            "message": "daily brief job could not be delivered to the worker",
            "retryable": False,
        }
    session.commit()


__all__ = [
    "LEGACY_DAILY_BRIEF_JOB_TYPE",
    "LegacyWriterModeConflict",
    "mark_legacy_daily_brief_delivery_failed",
    "queue_legacy_daily_brief",
    "require_legacy_writer_mode",
    "require_legacy_maintenance_mode",
]
