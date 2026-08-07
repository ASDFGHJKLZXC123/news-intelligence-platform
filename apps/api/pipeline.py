"""Authenticated manual trigger for the non-crisis daily intelligence pipeline."""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from packages.jobs import JobState
from services.pipeline import (
    current_pipeline_date,
    daily_pipeline_identity,
    validate_pipeline_date,
)
from services.pipeline.sqlalchemy_store import (
    PipelineJobSnapshot,
    PipelineQueueDecision,
    PipelineRetryConflictError,
    PipelineStaleDeliveryError,
    SQLAlchemyPipelineLifecycleStore,
)
from workers.celery_app import QUEUE_PIPELINE
from workers.pipeline_tasks import run_daily_pipeline_task

router = APIRouter(prefix="/api/v1/internal/jobs", tags=["jobs"])


class ProcessPipelineRequest(BaseModel):
    """The explicit brief/idempotency date; upstream stages reconcile outstanding work."""

    process_date: datetime.date


class ProcessPipelineResponse(BaseModel):
    job_id: uuid.UUID
    job_key: str
    process_date: datetime.date
    state: JobState
    attempt: int
    celery_task_id: str | None = None
    event_ids: list[uuid.UUID]
    lease_expires_at: datetime.datetime | None = None
    enqueued: bool
    idempotent: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def get_pipeline_lifecycle_store() -> SQLAlchemyPipelineLifecycleStore:
    return SQLAlchemyPipelineLifecycleStore()


PipelineStoreDep = Annotated[
    SQLAlchemyPipelineLifecycleStore,
    Depends(get_pipeline_lifecycle_store),
]


def _response(
    snapshot: PipelineJobSnapshot,
    *,
    enqueued: bool,
    idempotent: bool,
) -> ProcessPipelineResponse:
    return ProcessPipelineResponse(
        job_id=snapshot.job_id,
        job_key=snapshot.job_key,
        process_date=snapshot.process_date,
        state=snapshot.state,
        attempt=snapshot.attempt,
        celery_task_id=snapshot.celery_task_id,
        event_ids=list(snapshot.event_ids),
        lease_expires_at=snapshot.lease_expires_at,
        enqueued=enqueued,
        idempotent=idempotent,
        result=None if snapshot.result is None else dict(snapshot.result),
        error=None if snapshot.error is None else dict(snapshot.error),
    )


def enqueue_pipeline_task(
    process_date: datetime.date,
    delivery_token: uuid.UUID,
    celery_task_id: str,
):
    """Deliver one explicit date to the exact pipeline queue."""

    return run_daily_pipeline_task.apply_async(
        args=[process_date.isoformat(), str(delivery_token)],
        queue=QUEUE_PIPELINE,
        task_id=celery_task_id,
    )


def _validated_identity(process_date: datetime.date):
    identity = daily_pipeline_identity(process_date)
    try:
        validate_pipeline_date(identity, today=current_pipeline_date())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return identity


def _deliver(
    decision: PipelineQueueDecision,
    store: SQLAlchemyPipelineLifecycleStore,
) -> ProcessPipelineResponse:
    if not decision.should_enqueue:
        return _response(decision.snapshot, enqueued=False, idempotent=True)

    delivery_token = decision.delivery_token
    if delivery_token is None:
        raise RuntimeError("enqueue decision is missing its delivery token")
    identity = daily_pipeline_identity(decision.snapshot.process_date)
    celery_task_id = str(uuid.uuid4())
    try:
        snapshot = store.record_celery_task(identity, delivery_token, celery_task_id)
    except PipelineStaleDeliveryError:
        # A concurrent trigger rotated the delivery token between our queue decision and this
        # write. That trigger now owns broker publication; report the date as idempotently
        # active instead of surfacing an internal error.
        current = store.find_snapshot(identity)
        if current is None:
            raise
        return _response(current, enqueued=False, idempotent=True)
    try:
        enqueue_pipeline_task(identity.process_date, delivery_token, celery_task_id)
    except Exception as exc:
        store.mark_failed(
            identity,
            exc,
            code="broker_enqueue_failed",
            retryable=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pipeline job could not be delivered to the worker",
        ) from exc
    return _response(snapshot, enqueued=True, idempotent=False)


@router.post(
    "/process",
    response_model=ProcessPipelineResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def process_daily_pipeline(
    request: ProcessPipelineRequest,
    store: PipelineStoreDep,
) -> ProcessPipelineResponse:
    """Persist queued state before broker delivery and deduplicate active/succeeded dates."""

    identity = _validated_identity(request.process_date)
    return _deliver(store.queue(identity), store)


@router.get(
    "/process/{process_date}",
    response_model=ProcessPipelineResponse,
)
def get_daily_pipeline_status(
    process_date: datetime.date,
    store: PipelineStoreDep,
) -> ProcessPipelineResponse:
    """Inspect the current lease and complete prior terminal result without changing state."""

    identity = _validated_identity(process_date)
    snapshot = store.find_snapshot(identity)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="pipeline job not found",
        )
    return _response(snapshot, enqueued=False, idempotent=True)


@router.post(
    "/process/{process_date}/retry",
    response_model=ProcessPipelineResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_daily_pipeline(
    process_date: datetime.date,
    store: PipelineStoreDep,
) -> ProcessPipelineResponse:
    """Explicitly retry an inspected terminal fault or an expired active lease."""

    identity = _validated_identity(process_date)
    try:
        decision = store.retry(identity)
    except PipelineRetryConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return _deliver(decision, store)


__all__ = [
    "ProcessPipelineRequest",
    "ProcessPipelineResponse",
    "enqueue_pipeline_task",
    "get_daily_pipeline_status",
    "get_pipeline_lifecycle_store",
    "process_daily_pipeline",
    "retry_daily_pipeline",
    "router",
]
