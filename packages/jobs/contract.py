"""Stage 1 in-memory job/idempotency contract.

This module defines the typed job shape and deterministic identity helpers that later
durable job tables will persist. It deliberately contains no database or product logic.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Self

from packages.providers.base import ensure_utc, freeze_value


class JobState(StrEnum):
    """Lifecycle states shared by future durable job rows and Celery tasks."""

    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIALLY_FAILED = "partially_failed"


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _to_canonical(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime.datetime):
        return ensure_utc(value).isoformat()
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = f"identity mapping keys must be str, got {type(key).__name__}"
                raise TypeError(msg)
            canonical[key] = _to_canonical(item)
        return canonical
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_to_canonical(item) for item in value]
    msg = f"unsupported identity value type: {type(value).__name__}"
    raise TypeError(msg)


def _canonical_identity(identity: Mapping[str, Any] | None) -> str:
    canonical = _to_canonical(identity or {})
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def stable_job_key(job_type: str, identity: Mapping[str, Any] | None = None) -> str:
    """Build a deterministic idempotency key from a job type and canonical identity."""
    canonical = json.dumps(
        {"identity": _to_canonical(identity or {}), "job_type": job_type},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{job_type}:{digest}"


def _job_id_for_key(job_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"news-intelligence-platform:{job_key}"))


@dataclass(frozen=True)
class JobError:
    """Structured error metadata without persistence or external side effects."""

    code: str
    message: str
    retryable: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", freeze_value(self.details))


@dataclass(frozen=True)
class Stage1Job:
    """Typed no-persistence job contract for idempotent, safe reruns."""

    job_type: str
    job_key: str
    job_id: str
    state: JobState = JobState.QUEUED
    created_at: datetime.datetime = field(default_factory=_utc_now)
    updated_at: datetime.datetime = field(default_factory=_utc_now)
    attempt: int = 1
    max_attempts: int = 3
    related_ids: tuple[str, ...] = field(default_factory=tuple)
    error: JobError | None = None
    safe_to_rerun: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))
        object.__setattr__(self, "related_ids", tuple(self.related_ids))
        if self.attempt < 1:
            msg = "attempt must be at least 1"
            raise ValueError(msg)
        if self.max_attempts < 1:
            msg = "max_attempts must be at least 1"
            raise ValueError(msg)
        if self.attempt > self.max_attempts:
            msg = "attempt cannot exceed max_attempts"
            raise ValueError(msg)

    @classmethod
    def create(
        cls,
        job_type: str,
        identity: Mapping[str, Any] | None = None,
        *,
        related_ids: Sequence[str] = (),
        max_attempts: int = 3,
        now: datetime.datetime | None = None,
    ) -> Self:
        """Create a deterministic Stage 1 job skeleton for the given identity."""
        timestamp = ensure_utc(now) if now else _utc_now()
        key_identity = {"identity": _canonical_identity(identity), "related_ids": list(related_ids)}
        job_key = stable_job_key(job_type, key_identity)
        return cls(
            job_type=job_type,
            job_key=job_key,
            job_id=_job_id_for_key(job_key),
            created_at=timestamp,
            updated_at=timestamp,
            max_attempts=max_attempts,
            related_ids=tuple(related_ids),
        )

    @property
    def idempotency_key(self) -> str:
        return self.job_key

    @property
    def can_retry(self) -> bool:
        error_allows_retry = self.error is None or self.error.retryable
        return (
            self.safe_to_rerun
            and error_allows_retry
            and self.state in {JobState.FAILED, JobState.RETRYING}
            and self.attempt < self.max_attempts
        )

    def mark_running(self, *, now: datetime.datetime | None = None) -> Self:
        timestamp = ensure_utc(now) if now else _utc_now()
        return replace(self, state=JobState.RUNNING, updated_at=timestamp, error=None)

    def mark_succeeded(self, *, now: datetime.datetime | None = None) -> Self:
        timestamp = ensure_utc(now) if now else _utc_now()
        return replace(self, state=JobState.SUCCEEDED, updated_at=timestamp, error=None)

    def mark_partially_failed(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        details: Mapping[str, Any] | None = None,
        now: datetime.datetime | None = None,
    ) -> Self:
        timestamp = ensure_utc(now) if now else _utc_now()
        return replace(
            self,
            state=JobState.PARTIALLY_FAILED,
            updated_at=timestamp,
            error=JobError(code=code, message=message, retryable=retryable, details=details or {}),
        )

    def mark_failed(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        details: Mapping[str, Any] | None = None,
        now: datetime.datetime | None = None,
    ) -> Self:
        timestamp = ensure_utc(now) if now else _utc_now()
        return replace(
            self,
            state=JobState.FAILED,
            updated_at=timestamp,
            error=JobError(code=code, message=message, retryable=retryable, details=details or {}),
        )

    def next_attempt(self, *, now: datetime.datetime | None = None) -> Self:
        if not self.can_retry:
            msg = "job has no retry attempts remaining"
            raise ValueError(msg)
        timestamp = ensure_utc(now) if now else _utc_now()
        return replace(
            self,
            state=JobState.RETRYING,
            updated_at=timestamp,
            attempt=self.attempt + 1,
            error=None,
        )
