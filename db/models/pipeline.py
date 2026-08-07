"""Pipeline-specific lifecycle detail attached one-to-one to the canonical Job row."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class PipelineRunDetail(Base):
    """Manual daily-pipeline metadata that does not belong in the generic Job contract.

    ``jobs.id`` is the deterministic process UUID and ``jobs.job_key`` is the deterministic
    process-date key. This one-to-one row adds the date itself, bounded delivery/run leases,
    the durable event fan-out scope, execution timestamps, and the complete terminal coordinator
    result. It intentionally has no error column: job failures live only in ``jobs.error``.
    """

    __tablename__ = "pipeline_run_details"
    __table_args__ = (
        # Explicit names keep metadata-created (unit test) and migrated (0018) databases
        # identical for name-based inspection, drops, and future autogenerate.
        UniqueConstraint("process_date", name="uq_pipeline_run_details_process_date"),
        UniqueConstraint("celery_task_id", name="uq_pipeline_run_details_celery_task_id"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    process_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    run_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    event_ids: Mapped[Any] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    queued_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["PipelineRunDetail"]
