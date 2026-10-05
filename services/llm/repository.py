"""Lightweight repository protocol and deterministic in-memory implementation."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models.core import Job, LLMRun


@runtime_checkable
class LLMRuntimeRepository(Protocol):
    def save_llm_run(self, run: LLMRun) -> None: ...

    def save_job(self, job: Job) -> None: ...

    def monthly_spend_usd(self, as_of: datetime.datetime | None = None) -> float: ...


@dataclass
class InMemoryLLMRuntimeRepository(LLMRuntimeRepository):
    """Simple persistence stub for deterministic tests and local development."""

    llm_runs: list[LLMRun] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)

    def save_llm_run(self, run: LLMRun) -> None:
        self.llm_runs.append(run)

    def save_job(self, job: Job) -> None:
        self.jobs.append(job)

    def monthly_spend_usd(self, as_of: datetime.datetime | None = None) -> float:
        reference = as_of or datetime.datetime.now(datetime.UTC)
        total = 0.0
        for run in self.llm_runs:
            if run.cost_usd is None:
                continue
            started = run.started_at or run.completed_at or run.created_at
            if started.year == reference.year and started.month == reference.month:
                total += float(run.cost_usd)
        return total

    def last_job_for_key(self, job_key: str) -> Job | None:
        for job in reversed(self.jobs):
            if job.job_key == job_key:
                return job
        return None


class SQLAlchemyLLMRuntimeRepository(LLMRuntimeRepository):
    """Durable repository over a caller-provided SQLAlchemy Session.

    With ``commit_on_write=True`` (the production default), each audit/job write is its
    own transaction and is committed before returning. Any database error rolls the
    Session back before it is re-raised, leaving the Session safe to reuse. Set it to
    false only when a caller deliberately owns a larger transaction; writes are then
    flushed but the caller must commit or roll back.
    """

    def __init__(self, session: Session, *, commit_on_write: bool = True) -> None:
        self._session = session
        self._commit_on_write = commit_on_write

    @property
    def commit_on_write(self) -> bool:
        """Whether orchestration writes end their own transaction."""

        return self._commit_on_write

    def _finish_write(self) -> None:
        try:
            if self._commit_on_write:
                self._session.commit()
            else:
                self._session.flush()
        except Exception:
            self._session.rollback()
            raise

    def save_llm_run(self, run: LLMRun) -> None:
        self._session.add(run)
        self._finish_write()

    def save_job(self, job: Job) -> None:
        self._session.merge(job)
        self._finish_write()

    def monthly_spend_usd(self, as_of: datetime.datetime | None = None) -> float:
        reference = as_of or datetime.datetime.now(datetime.UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=datetime.UTC)
        month_start = reference.astimezone(datetime.UTC).replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if month_start.month == 12:
            next_month = month_start.replace(year=month_start.year + 1, month=1)
        else:
            next_month = month_start.replace(month=month_start.month + 1)
        occurred_at = func.coalesce(LLMRun.started_at, LLMRun.completed_at, LLMRun.created_at)
        statement = select(func.coalesce(func.sum(LLMRun.cost_usd), 0)).where(
            occurred_at >= month_start,
            occurred_at < next_month,
        )
        return float(self._session.execute(statement).scalar_one())
