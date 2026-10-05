"""SQLAlchemy durability for manual queueing and coordinator lifecycle transitions."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.base import SessionLocal
from db.models.core import Job
from db.models.pipeline import PipelineRunDetail
from packages.jobs import JobState
from services.pipeline.contracts import (
    PipelineIdentity,
    PipelineRunResult,
    PipelineState,
    StageStatus,
)
from services.pipeline.serialization import pipeline_run_result_from_dict
from services.writer_mode import require_legacy_writer_mode

PIPELINE_JOB_TYPE = "daily_intelligence_pipeline"
PIPELINE_MAX_ATTEMPTS = 3
# How long a claim survives without progress before another trigger may take the date over.
# The queue lease covers only the API-to-broker handoff, so it stays short: two minutes is long
# enough to rule out a slow publish and short enough to repair an API crash quickly.
# The running lease must outlast the worker's own hard kill, otherwise a healthy worker's date
# could be handed to a replacement while the original is still executing and still able to write.
# `workers.celery_app.PIPELINE_TASK_TIME_LIMIT` SIGKILLs the coordinator task at 30 minutes, so
# 35 leaves a five-minute margin for the post-limit terminal write. Raise this whenever that hard
# limit is raised; the unit tests assert the ordering so the two cannot drift apart silently.
PIPELINE_QUEUE_LEASE = datetime.timedelta(minutes=2)
PIPELINE_RUNNING_LEASE = datetime.timedelta(minutes=35)

SessionFactory = Callable[[], Session]
NowFactory = Callable[[], datetime.datetime]


class PipelineStoreError(RuntimeError):
    """The durable lifecycle rows are missing, inconsistent, or malformed."""


class PipelineAlreadyRunningError(PipelineStoreError):
    """A broker redelivery tried to claim a date whose worker still owns it."""


class PipelineStaleDeliveryError(PipelineStoreError):
    """A delayed broker message belongs to a delivery that has already been replaced."""


class PipelineRetryRequiredError(PipelineStoreError):
    """A terminal failed/partial date requires the explicit operator retry endpoint."""


class PipelineRetryConflictError(PipelineStoreError):
    """The requested retry is unsafe for the job's current state or attempt budget."""


@dataclass(frozen=True)
class PipelineJobSnapshot:
    """Detached primitives for an API or task response."""

    job_id: uuid.UUID
    job_key: str
    job_type: str
    process_date: datetime.date
    state: JobState
    attempt: int
    max_attempts: int
    safe_to_rerun: bool
    celery_task_id: str | None
    event_ids: tuple[uuid.UUID, ...]
    result: Mapping[str, Any] | None
    error: Mapping[str, Any] | None
    queued_at: datetime.datetime
    started_at: datetime.datetime | None
    completed_at: datetime.datetime | None
    lease_expires_at: datetime.datetime | None


@dataclass(frozen=True)
class PipelineQueueDecision:
    """Whether this call created/requeued work or found an idempotent duplicate."""

    snapshot: PipelineJobSnapshot
    should_enqueue: bool
    delivery_token: uuid.UUID | None

    @property
    def idempotent(self) -> bool:
        return not self.should_enqueue


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _state(value: str) -> JobState:
    try:
        return JobState(value)
    except ValueError as exc:
        raise PipelineStoreError(f"pipeline job has unsupported state {value!r}") from exc


def _error_payload(
    *,
    code: str,
    message: str,
    retryable: bool,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": dict(details or {}),
    }


def _pipeline_terminal_error(result: PipelineRunResult) -> dict[str, Any] | None:
    if result.state is PipelineState.SUCCEEDED:
        return None
    failed_stages = [
        {
            "stage": execution.stage.value,
            "status": execution.result.status.value,
            "failed_count": len(execution.result.failures),
            "blocked_by": [stage.value for stage in execution.blocked_by],
        }
        for execution in result.stages
        if execution.result.status is not StageStatus.SUCCEEDED
    ]
    if result.state is PipelineState.PARTIALLY_FAILED:
        return _error_payload(
            code="pipeline_partially_failed",
            message="daily pipeline completed with one or more degraded stages",
            retryable=True,
            details={"stages": failed_stages},
        )
    return _error_payload(
        code="pipeline_failed",
        message="daily pipeline could not produce the daily brief",
        retryable=True,
        details={"stages": failed_stages},
    )


class SQLAlchemyPipelineLifecycleStore:
    """Own generic Job + pipeline sidecar transactions.

    Every public operation opens and closes one short transaction.  Row locks serialize state
    transitions for an existing deterministic job id; the job-id/process-date unique constraints
    close the initial-insert race.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory = SessionLocal,
        now: NowFactory = _utc_now,
        delivery_token: uuid.UUID | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now
        self._delivery_token = delivery_token
        self._active_run_token: uuid.UUID | None = None

    @property
    def active_run_token(self) -> uuid.UUID | None:
        """The RUNNING-attempt lease this store holds, for propagation to in-run stage writes."""

        return self._active_run_token

    def queue(self, identity: PipelineIdentity) -> PipelineQueueDecision:
        """Create work or recover an abandoned queue lease.

        Terminal failed/partial rows are deliberately inspectable here and are never requeued.
        An operator must call :meth:`retry` after reviewing their stored result.  A queued row is
        redelivered only after its bounded handoff lease expires, which repairs an API-process
        crash between the database commit and broker publication without duplicating a healthy
        delivery.
        """

        session = self._session_factory()
        try:
            require_legacy_writer_mode(session, lock=True)
            job, detail = self._load(session, identity, for_update=True)
            timestamp = self._now()
            if job is None:
                job, detail = self._new_rows(identity, timestamp, state=JobState.QUEUED)
                self._prepare_queued(
                    job,
                    detail,
                    timestamp,
                    increment_attempt=False,
                )
                session.add(job)
                session.add(detail)
                session.flush()
                session.commit()
                self._delivery_token = detail.delivery_token
                return PipelineQueueDecision(
                    snapshot=self._snapshot(job, detail),
                    should_enqueue=True,
                    delivery_token=detail.delivery_token,
                )

            assert detail is not None
            state = _state(job.state)
            if state is JobState.QUEUED and self._lease_expired(detail, timestamp):
                if job.attempt >= job.max_attempts:
                    self._expire_abandoned_job(
                        job,
                        detail,
                        timestamp,
                        code="pipeline_queue_lease_exhausted",
                        message="pipeline broker-delivery lease expired with no attempts remaining",
                    )
                    session.flush()
                    session.commit()
                    return PipelineQueueDecision(
                        snapshot=self._snapshot(job, detail),
                        should_enqueue=False,
                        delivery_token=None,
                    )
                self._prepare_queued(
                    job,
                    detail,
                    timestamp,
                    increment_attempt=True,
                )
                session.flush()
                session.commit()
                self._delivery_token = detail.delivery_token
                return PipelineQueueDecision(
                    snapshot=self._snapshot(job, detail),
                    should_enqueue=True,
                    delivery_token=detail.delivery_token,
                )
            if state in {
                JobState.QUEUED,
                JobState.RUNNING,
                JobState.SUCCEEDED,
                JobState.FAILED,
                JobState.PARTIALLY_FAILED,
            }:
                session.commit()
                return PipelineQueueDecision(
                    snapshot=self._snapshot(job, detail),
                    should_enqueue=False,
                    delivery_token=None,
                )
            raise PipelineStoreError(f"pipeline job cannot be queued from state {state.value!r}")
        except IntegrityError:
            session.rollback()
            return self._queue_after_insert_conflict(identity)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _queue_after_insert_conflict(
        self,
        identity: PipelineIdentity,
    ) -> PipelineQueueDecision:
        """Resolve only a deterministic first-queue race; surface every unrelated integrity fault."""

        session = self._session_factory()
        try:
            require_legacy_writer_mode(session, lock=True)
            job, detail = self._load(session, identity, for_update=True)
            if job is None or detail is None:
                raise PipelineStoreError(
                    "pipeline queue insert conflicted but no matching deterministic job exists"
                )
            session.commit()
            return PipelineQueueDecision(
                snapshot=self._snapshot(job, detail),
                should_enqueue=False,
                delivery_token=None,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def retry(self, identity: PipelineIdentity) -> PipelineQueueDecision:
        """Explicitly requeue an inspected terminal fault or take over an expired active lease."""

        session = self._session_factory()
        try:
            require_legacy_writer_mode(session, lock=True)
            job, detail = self._require_rows(session, identity, for_update=True)
            timestamp = self._now()
            state = _state(job.state)
            retryable_state = state in {JobState.FAILED, JobState.PARTIALLY_FAILED}
            abandoned_active = state in {JobState.QUEUED, JobState.RUNNING} and self._lease_expired(
                detail,
                timestamp,
            )
            if not retryable_state and not abandoned_active:
                raise PipelineRetryConflictError(
                    f"pipeline job in state {state.value!r} is not eligible for retry"
                )
            if not job.safe_to_rerun:
                raise PipelineRetryConflictError("pipeline job is not safe to rerun")
            if (
                retryable_state
                and isinstance(job.error, Mapping)
                and job.error.get("retryable") is False
            ):
                raise PipelineRetryConflictError("pipeline job failure is marked non-retryable")
            if job.attempt >= job.max_attempts:
                raise PipelineRetryConflictError("pipeline job has no retry attempts remaining")

            self._prepare_queued(
                job,
                detail,
                timestamp,
                increment_attempt=True,
            )
            session.flush()
            session.commit()
            self._delivery_token = detail.delivery_token
            return PipelineQueueDecision(
                snapshot=self._snapshot(job, detail),
                should_enqueue=True,
                delivery_token=detail.delivery_token,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def record_celery_task(
        self,
        identity: PipelineIdentity,
        delivery_token: uuid.UUID,
        celery_task_id: str,
    ) -> PipelineJobSnapshot:
        if not isinstance(celery_task_id, str) or not celery_task_id.strip():
            raise ValueError("celery_task_id must be a nonblank string")
        session = self._session_factory()
        try:
            require_legacy_writer_mode(session, lock=True)
            job, detail = self._require_rows(session, identity, for_update=True)
            if detail.delivery_token != delivery_token:
                raise PipelineStaleDeliveryError(
                    "pipeline delivery was replaced before its Celery task id was recorded"
                )
            if detail.celery_task_id not in {None, celery_task_id}:
                raise PipelineStoreError("pipeline job already has a different Celery task id")
            detail.celery_task_id = celery_task_id
            detail.updated_at = self._now()
            session.flush()
            session.commit()
            return self._snapshot(job, detail)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def begin_or_get_succeeded(
        self,
        identity: PipelineIdentity,
    ) -> PipelineRunResult | None:
        """Implement the coordinator protocol and durably enter ``running``."""

        session = self._session_factory()
        try:
            require_legacy_writer_mode(session, lock=True)
            job, detail = self._load(session, identity, for_update=True)
            timestamp = self._now()
            if job is None:
                job, detail = self._new_rows(identity, timestamp, state=JobState.RUNNING)
                delivery_token = self._delivery_token or uuid.uuid4()
                run_token = uuid.uuid4()
                detail.delivery_token = delivery_token
                detail.run_token = run_token
                detail.lease_expires_at = timestamp + PIPELINE_RUNNING_LEASE
                detail.started_at = timestamp
                session.add(job)
                session.add(detail)
                session.flush()
                session.commit()
                self._delivery_token = delivery_token
                self._active_run_token = run_token
                return None

            assert detail is not None
            state = _state(job.state)
            if state is JobState.SUCCEEDED:
                if not isinstance(detail.result, Mapping):
                    raise PipelineStoreError("succeeded pipeline job has no valid terminal result")
                result = pipeline_run_result_from_dict(detail.result)
                if result.identity != identity:
                    raise PipelineStoreError(
                        "stored terminal result belongs to a different pipeline identity"
                    )
                session.commit()
                return result

            if (
                self._delivery_token is None
                or detail.delivery_token is None
                or self._delivery_token != detail.delivery_token
            ):
                raise PipelineStaleDeliveryError(
                    "pipeline broker delivery token no longer owns this date"
                )

            if state is JobState.RUNNING:
                if not self._lease_expired(detail, timestamp):
                    raise PipelineAlreadyRunningError(
                        f"daily pipeline for {identity.process_date.isoformat()} is already running"
                    )
                if not job.safe_to_rerun or job.attempt >= job.max_attempts:
                    self._expire_abandoned_job(
                        job,
                        detail,
                        timestamp,
                        code="pipeline_running_lease_exhausted",
                        message="pipeline worker lease expired with no attempts remaining",
                    )
                    session.flush()
                    session.commit()
                    raise PipelineRetryConflictError(
                        "pipeline worker lease expired with no retry attempts remaining"
                    )
                job.attempt += 1
            if state in {JobState.FAILED, JobState.PARTIALLY_FAILED}:
                raise PipelineRetryRequiredError(
                    "pipeline terminal fault must be explicitly requeued after inspection"
                )
            if state is not JobState.QUEUED and state is not JobState.RUNNING:
                raise PipelineStoreError(f"pipeline worker cannot claim state {state.value!r}")

            run_token = uuid.uuid4()
            job.state = JobState.RUNNING.value
            job.updated_at = timestamp
            detail.run_token = run_token
            detail.lease_expires_at = timestamp + PIPELINE_RUNNING_LEASE
            detail.started_at = timestamp
            detail.completed_at = None
            detail.updated_at = timestamp
            session.flush()
            session.commit()
            self._active_run_token = run_token
            return None
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def save_terminal(self, result: PipelineRunResult) -> None:
        """Persist the full result sidecar and mirror only disposition/error into Job."""

        session = self._session_factory()
        try:
            require_legacy_writer_mode(session, lock=True)
            job, detail = self._require_rows(
                session,
                result.identity,
                for_update=True,
            )
            if (
                _state(job.state) is not JobState.RUNNING
                or self._active_run_token is None
                or detail.run_token != self._active_run_token
            ):
                raise PipelineStaleDeliveryError(
                    "pipeline worker no longer owns the terminal-write lease"
                )
            timestamp = self._now()
            job.state = JobState(result.state.value).value
            job.error = _pipeline_terminal_error(result)
            job.updated_at = timestamp
            detail.result = result.to_dict()
            detail.run_token = None
            detail.lease_expires_at = None
            detail.completed_at = timestamp
            detail.updated_at = timestamp
            session.flush()
            session.commit()
            self._active_run_token = None
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_failed(
        self,
        identity: PipelineIdentity,
        error: BaseException,
        *,
        code: str = "pipeline_execution_error",
        retryable: bool = True,
    ) -> PipelineJobSnapshot:
        """Durably record an exception that occurred outside a coordinator terminal result."""

        session = self._session_factory()
        try:
            require_legacy_writer_mode(session, lock=True)
            job, detail = self._load(session, identity, for_update=True)
            timestamp = self._now()
            if job is None:
                job, detail = self._new_rows(identity, timestamp, state=JobState.FAILED)
                session.add(job)
                session.add(detail)
            assert detail is not None
            state = _state(job.state)
            if state is JobState.SUCCEEDED or (
                state in {JobState.FAILED, JobState.PARTIALLY_FAILED}
                and isinstance(detail.result, Mapping)
            ):
                # A late broker/backend error must never overwrite a complete coordinator result
                # that this or another worker already committed.
                session.commit()
                return self._snapshot(job, detail)
            if self._active_run_token is not None and detail.run_token != self._active_run_token:
                raise PipelineStaleDeliveryError(
                    "pipeline worker no longer owns the failure-write lease"
                )
            if (
                self._active_run_token is None
                and self._delivery_token is not None
                and detail.delivery_token != self._delivery_token
            ):
                raise PipelineStaleDeliveryError(
                    "pipeline delivery no longer owns the failure-write lease"
                )
            message = str(error).strip() or f"{type(error).__name__} without an error message"
            job.state = JobState.FAILED.value
            job.error = _error_payload(
                code=code,
                message=message,
                retryable=retryable,
                details={"error_type": type(error).__name__},
            )
            job.updated_at = timestamp
            detail.run_token = None
            detail.lease_expires_at = None
            detail.completed_at = timestamp
            detail.updated_at = timestamp
            session.flush()
            session.commit()
            self._active_run_token = None
            return self._snapshot(job, detail)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def add_event_ids(
        self,
        identity: PipelineIdentity,
        event_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Durably union clustering output into the date scope before enrichment fan-out."""

        normalized = self._normalize_event_ids(event_ids)
        session = self._session_factory()
        try:
            require_legacy_writer_mode(session, lock=True)
            job, detail = self._require_rows(session, identity, for_update=True)
            if (
                _state(job.state) is not JobState.RUNNING
                or self._active_run_token is None
                or detail.run_token != self._active_run_token
            ):
                raise PipelineStaleDeliveryError(
                    "pipeline worker no longer owns the event-scope write lease"
                )
            combined = self._normalize_event_ids((*self._detail_event_ids(detail), *normalized))
            detail.event_ids = list(combined)
            detail.updated_at = self._now()
            session.flush()
            session.commit()
            return combined
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def event_ids(self, identity: PipelineIdentity) -> tuple[str, ...]:
        """Return the durable event scope accumulated for this process date."""

        session = self._session_factory()
        try:
            _job, detail = self._require_rows(session, identity, for_update=False)
            return self._detail_event_ids(detail)
        finally:
            session.close()

    def get_snapshot(self, identity: PipelineIdentity) -> PipelineJobSnapshot:
        session = self._session_factory()
        try:
            job, detail = self._require_rows(session, identity, for_update=False)
            return self._snapshot(job, detail)
        finally:
            session.close()

    def find_snapshot(self, identity: PipelineIdentity) -> PipelineJobSnapshot | None:
        """Read a status snapshot without turning an unknown date into a store fault."""

        session = self._session_factory()
        try:
            job, detail = self._load(session, identity, for_update=False)
            if job is None or detail is None:
                return None
            return self._snapshot(job, detail)
        finally:
            session.close()

    @staticmethod
    def _lease_expired(
        detail: PipelineRunDetail,
        timestamp: datetime.datetime,
    ) -> bool:
        expires_at = detail.lease_expires_at
        return expires_at is None or expires_at <= timestamp

    @staticmethod
    def _normalize_event_ids(event_ids: tuple[str, ...]) -> tuple[str, ...]:
        normalized: set[str] = set()
        for raw in event_ids:
            try:
                normalized.add(str(uuid.UUID(str(raw))))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError(f"invalid pipeline event id: {raw!r}") from exc
        return tuple(sorted(normalized))

    @classmethod
    def _detail_event_ids(cls, detail: PipelineRunDetail) -> tuple[str, ...]:
        raw = detail.event_ids
        if raw is None:
            return ()
        if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
            raise PipelineStoreError("pipeline event_ids must be a JSON array of UUID strings")
        try:
            return cls._normalize_event_ids(tuple(raw))
        except ValueError as exc:
            raise PipelineStoreError("pipeline event_ids contain an invalid UUID") from exc

    @staticmethod
    def _prepare_queued(
        job: Job,
        detail: PipelineRunDetail,
        timestamp: datetime.datetime,
        *,
        increment_attempt: bool,
    ) -> None:
        if increment_attempt:
            job.attempt += 1
        job.state = JobState.QUEUED.value
        job.updated_at = timestamp
        detail.delivery_token = uuid.uuid4()
        detail.run_token = None
        detail.lease_expires_at = timestamp + PIPELINE_QUEUE_LEASE
        detail.celery_task_id = None
        detail.queued_at = timestamp
        detail.started_at = None
        detail.completed_at = None
        detail.updated_at = timestamp

    @staticmethod
    def _expire_abandoned_job(
        job: Job,
        detail: PipelineRunDetail,
        timestamp: datetime.datetime,
        *,
        code: str,
        message: str,
    ) -> None:
        job.state = JobState.FAILED.value
        job.error = _error_payload(
            code=code,
            message=message,
            retryable=False,
            details={},
        )
        job.updated_at = timestamp
        detail.run_token = None
        detail.lease_expires_at = None
        detail.completed_at = timestamp
        detail.updated_at = timestamp

    @staticmethod
    def _new_rows(
        identity: PipelineIdentity,
        timestamp: datetime.datetime,
        *,
        state: JobState,
    ) -> tuple[Job, PipelineRunDetail]:
        job = Job(
            id=identity.process_id,
            job_key=identity.process_key,
            job_type=PIPELINE_JOB_TYPE,
            state=state.value,
            attempt=1,
            max_attempts=PIPELINE_MAX_ATTEMPTS,
            related_ids=None,
            error=None,
            safe_to_rerun=True,
            created_at=timestamp,
            updated_at=timestamp,
        )
        detail = PipelineRunDetail(
            job_id=identity.process_id,
            process_date=identity.process_date,
            celery_task_id=None,
            delivery_token=None,
            run_token=None,
            lease_expires_at=None,
            event_ids=[],
            result=None,
            queued_at=timestamp,
            started_at=None,
            completed_at=None,
            created_at=timestamp,
            updated_at=timestamp,
        )
        return job, detail

    @staticmethod
    def _load(
        session: Session,
        identity: PipelineIdentity,
        *,
        for_update: bool,
    ) -> tuple[Job | None, PipelineRunDetail | None]:
        job = session.get(Job, identity.process_id, with_for_update=for_update)
        detail = session.get(
            PipelineRunDetail,
            identity.process_id,
            with_for_update=for_update,
        )
        if job is None:
            if detail is not None:
                raise PipelineStoreError("pipeline detail exists without its Job")
            return None, None
        if (
            job.job_key != identity.process_key
            or job.job_type != PIPELINE_JOB_TYPE
            or detail is None
            or detail.process_date != identity.process_date
        ):
            raise PipelineStoreError("deterministic pipeline Job/detail identity is inconsistent")
        return job, detail

    @classmethod
    def _require_rows(
        cls,
        session: Session,
        identity: PipelineIdentity,
        *,
        for_update: bool,
    ) -> tuple[Job, PipelineRunDetail]:
        job, detail = cls._load(session, identity, for_update=for_update)
        if job is None or detail is None:
            raise PipelineStoreError("pipeline Job/detail rows do not exist")
        return job, detail

    @staticmethod
    def _snapshot(job: Job, detail: PipelineRunDetail) -> PipelineJobSnapshot:
        result = dict(detail.result) if isinstance(detail.result, Mapping) else None
        error = dict(job.error) if isinstance(job.error, Mapping) else None
        return PipelineJobSnapshot(
            job_id=job.id,
            job_key=job.job_key,
            job_type=job.job_type,
            process_date=detail.process_date,
            state=_state(job.state),
            attempt=job.attempt,
            max_attempts=job.max_attempts,
            safe_to_rerun=job.safe_to_rerun,
            celery_task_id=detail.celery_task_id,
            event_ids=tuple(
                uuid.UUID(value)
                for value in SQLAlchemyPipelineLifecycleStore._detail_event_ids(detail)
            ),
            result=result,
            error=error,
            queued_at=detail.queued_at,
            started_at=detail.started_at,
            completed_at=detail.completed_at,
            lease_expires_at=detail.lease_expires_at,
        )


__all__ = [
    "PIPELINE_JOB_TYPE",
    "PIPELINE_MAX_ATTEMPTS",
    "PIPELINE_QUEUE_LEASE",
    "PIPELINE_RUNNING_LEASE",
    "PipelineJobSnapshot",
    "PipelineAlreadyRunningError",
    "PipelineQueueDecision",
    "PipelineRetryConflictError",
    "PipelineRetryRequiredError",
    "PipelineStaleDeliveryError",
    "PipelineStoreError",
    "SQLAlchemyPipelineLifecycleStore",
]
