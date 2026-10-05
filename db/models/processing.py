"""Durable database-wide processing control, retaining the Phase 2 mode table."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class PersonalWriterMode(Base):
    """One mode and fencing generation for all supported processing entry points."""

    __tablename__ = "personal_writer_mode"
    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="legacy")
    generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="RESTRICT"), nullable=True
    )
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="RESTRICT"), nullable=True
    )
    active_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    ownership_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    queued_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    graceful_deadline_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hard_deadline_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    launcher_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    child_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    child_identity: Mapped[str | None] = mapped_column(String(256), nullable=True)
    child_history: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    unconfirmed_child: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint("singleton", name="ck_personal_writer_mode_singleton"),
        CheckConstraint("mode IN ('personal', 'legacy')", name="ck_personal_writer_mode_value"),
        CheckConstraint("generation >= 0", name="ck_personal_processing_generation"),
    )


ProcessingControl = PersonalWriterMode
