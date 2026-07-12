"""No-op Celery tasks that prove worker and beat wiring.

Stage 1 deliberately contains no product logic. ``noop`` proves a worker can import
the app and execute a queued task; ``scheduled_heartbeat`` proves Celery Beat can fire
a scheduled task. Both emit structured logs and update the job metrics counters so the
job contract is observable from day one.
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.jobs import Stage1Job
from workers.celery_app import Stage1Task

logger = get_logger("workers.tasks")


def _payload_identity(payload: dict[str, Any] | None) -> dict[str, Any]:
    return {"payload": payload or {}}


@shared_task(name="workers.tasks.noop", base=Stage1Task)
def noop(payload: dict[str, Any] | None = None) -> dict[str, str]:
    """Execute a trivial unit of work and report success via metrics/logs."""
    job = Stage1Job.create("workers.tasks.noop", _payload_identity(payload)).mark_running()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)
    try:
        logger.info("noop task executed", extra={"payload_keys": sorted((payload or {}).keys())})
        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        return {
            "status": "ok",
            "job_id": completed.job_id,
            "job_key": completed.job_key,
            "state": completed.state.value,
        }
    except Exception:
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("noop task failed")
        raise
    finally:
        set_job_id(None)


@shared_task(name="workers.tasks.scheduled_heartbeat", base=Stage1Task)
def scheduled_heartbeat() -> dict[str, str]:
    """Beat-scheduled no-op proving the scheduler fires without product side effects."""
    job = Stage1Job.create(
        "workers.tasks.scheduled_heartbeat", {"schedule": "noop-heartbeat"}
    ).mark_running()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)
    try:
        logger.info("scheduled heartbeat fired")
        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        return {
            "status": "ok",
            "job_id": completed.job_id,
            "job_key": completed.job_key,
            "state": completed.state.value,
        }
    finally:
        set_job_id(None)
