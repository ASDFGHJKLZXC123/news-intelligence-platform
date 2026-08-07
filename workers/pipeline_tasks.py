"""Manual-only Celery boundary for the non-crisis daily pipeline coordinator."""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from services.pipeline import (
    DailyPipelineCoordinator,
    PipelineState,
    current_pipeline_date,
    daily_pipeline_identity,
    validate_pipeline_date,
)
from services.pipeline.sqlalchemy_store import (
    PipelineAlreadyRunningError,
    PipelineRetryConflictError,
    PipelineRetryRequiredError,
    PipelineStaleDeliveryError,
    SQLAlchemyPipelineLifecycleStore,
)
from workers.celery_app import (
    PIPELINE_TASK_SOFT_TIME_LIMIT,
    PIPELINE_TASK_TIME_LIMIT,
    QUEUE_PIPELINE,
    Stage1Task,
)
from workers.pipeline_stages import ProductionPipelineStages

TASK_NAME = "workers.pipeline_tasks.run_daily_pipeline"

#: Exception types that mean "this worker process is being torn down", not "this stage failed".
#: Celery raises ``SoftTimeLimitExceeded`` in whatever frame is executing when the soft limit
#: lands, and in a real run that is almost always deep inside a task body (an HTTP fetch, an LLM
#: call, a database write) rather than in the coordinator between two stages. So the *same* set
#: has to be declared to both layers that would otherwise absorb it -- the per-item adapter in
#: ``workers.pipeline_stages`` and the per-stage coordinator -- from this one definition; naming
#: it in only one of them leaves the other free to swallow the signal.
PIPELINE_FATAL_EXCEPTIONS: tuple[type[BaseException], ...] = (SoftTimeLimitExceeded,)

logger = get_logger("workers.pipeline_tasks")


def build_lifecycle_store(
    delivery_token: str | None = None,
) -> SQLAlchemyPipelineLifecycleStore:
    """Production hook replaced by an in-memory fake in focused task tests."""

    return SQLAlchemyPipelineLifecycleStore(
        delivery_token=None if delivery_token is None else uuid.UUID(delivery_token)
    )


def build_stage_runners(store: SQLAlchemyPipelineLifecycleStore):
    """Production stage adapter hook replaced by deterministic runners in tests."""

    return ProductionPipelineStages(
        event_scope_store=store,
        fatal_exceptions=PIPELINE_FATAL_EXCEPTIONS,
    ).runners()


@shared_task(
    name=TASK_NAME,
    base=Stage1Task,
    queue=QUEUE_PIPELINE,
    # This task alone runs the whole seven-stage graph in-process; the fleet-wide 120s/180s
    # limits would kill it mid-stage. See workers.celery_app for the lease relationship.
    soft_time_limit=PIPELINE_TASK_SOFT_TIME_LIMIT,
    time_limit=PIPELINE_TASK_TIME_LIMIT,
)
def run_daily_pipeline_task(
    process_date: str,
    delivery_token: str | None = None,
) -> dict[str, Any]:
    """Run one explicit date; never derive a date from wall-clock time and never self-schedule."""

    identity = daily_pipeline_identity(process_date)
    validate_pipeline_date(identity, today=current_pipeline_date())
    set_job_id(str(identity.process_id))
    metrics.increment(metrics.JOB_STARTS)
    store = build_lifecycle_store(delivery_token)
    try:
        result = DailyPipelineCoordinator(
            stage_runners=build_stage_runners(store),
            lifecycle_store=store,
            # The coordinator records every other stage exception as a stage failure and moves on,
            # which would spend the graceful window before the hard kill starting the *next* stage
            # instead of unwinding. Naming these fatal makes the signal propagate to the generic
            # handler below, which durably marks the date failed (retryable) exactly once. The
            # stage adapter built above is given the identical set, because that is where the
            # limit actually lands; both are required for the propagation to be real.
            fatal_exceptions=PIPELINE_FATAL_EXCEPTIONS,
        ).run(identity.process_date)
        if result.state is PipelineState.SUCCEEDED:
            metrics.increment(metrics.JOB_SUCCESSES)
        else:
            metrics.increment(metrics.JOB_FAILURES)
        snapshot = store.get_snapshot(identity)
        logger.info(
            "daily pipeline completed",
            extra={
                "process_date": process_date,
                "pipeline_state": result.state.value,
                "idempotent": result.idempotent_skip,
            },
        )
        return {
            "status": result.state.value,
            "state": snapshot.state.value,
            "job_id": str(snapshot.job_id),
            "job_key": snapshot.job_key,
            "process_date": identity.process_date.isoformat(),
            "event_ids": [str(event_id) for event_id in snapshot.event_ids],
            "celery_task_id": snapshot.celery_task_id,
            "idempotent": result.idempotent_skip,
            "result": result.to_dict(),
        }
    except PipelineStaleDeliveryError:
        # A bounded queue/running recovery replaced this broker message. Acknowledge it as a
        # harmless no-op so Stage1Task does not autoretry stale work against the new owner.
        metrics.increment(metrics.JOB_SUCCESSES)
        logger.info(
            "ignored superseded daily pipeline delivery",
            extra={"process_date": process_date},
        )
        return {
            "status": "superseded",
            "process_date": identity.process_date.isoformat(),
            "idempotent": True,
        }
    except PipelineRetryRequiredError:
        # A worker can commit a terminal partial/failure and die before Celery acknowledges the
        # message. Redelivery must expose that terminal result, not silently start another attempt.
        snapshot = store.get_snapshot(identity)
        metrics.increment(metrics.JOB_SUCCESSES)
        return {
            "status": snapshot.state.value,
            "state": snapshot.state.value,
            "job_id": str(snapshot.job_id),
            "job_key": snapshot.job_key,
            "process_date": identity.process_date.isoformat(),
            "event_ids": [str(event_id) for event_id in snapshot.event_ids],
            "celery_task_id": snapshot.celery_task_id,
            "idempotent": True,
            "result": None if snapshot.result is None else dict(snapshot.result),
        }
    except PipelineAlreadyRunningError:
        # The existing worker owns this date. Do not overwrite its RUNNING row with a failure;
        # Stage1Task's broker retry can observe the eventual terminal result idempotently.
        metrics.increment(metrics.JOB_FAILURES)
        logger.warning(
            "refused concurrent daily pipeline execution",
            extra={"process_date": process_date},
        )
        raise
    except PipelineRetryConflictError:
        # The store already committed a deliberate terminal marker (e.g. the non-retryable
        # ``pipeline_running_lease_exhausted`` result). Falling through to the generic handler
        # would call ``mark_failed`` and overwrite that marker with a retryable
        # ``pipeline_execution_error``; re-raise without touching the row instead.
        metrics.increment(metrics.JOB_FAILURES)
        logger.warning(
            "refused daily pipeline attempt with no retry budget remaining",
            extra={"process_date": process_date},
        )
        raise
    except Exception as exc:
        metrics.increment(metrics.JOB_FAILURES)
        try:
            store.mark_failed(identity, exc)
        except Exception:
            logger.exception(
                "could not persist daily pipeline task failure",
                extra={"process_date": process_date},
            )
        logger.exception(
            "daily pipeline task failed",
            extra={"process_date": process_date},
        )
        raise
    finally:
        set_job_id(None)


__all__ = [
    "PIPELINE_FATAL_EXCEPTIONS",
    "TASK_NAME",
    "build_lifecycle_store",
    "build_stage_runners",
    "run_daily_pipeline_task",
]
