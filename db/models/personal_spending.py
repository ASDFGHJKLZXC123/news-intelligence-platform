"""Retained monetary obligations, independent of the workflow transaction.

Workflow locks use FOR NO KEY UPDATE, allowing these foreign-key checks from the
independent ledger connection while the workflow transaction remains open.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class PersonalSpendingState(Base):
    __tablename__ = "personal_spending_state"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    legacy_cutover_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PersonalLegacyUsage(Base):
    __tablename__ = "personal_legacy_usage"

    llm_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("llm_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    accounting_month: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    actual_usd: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    imported_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint("actual_usd IS NULL OR actual_usd >= 0", name="ck_personal_legacy_cost"),
    )


class PersonalPaidRequest(Base):
    __tablename__ = "personal_paid_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="RESTRICT"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    accounting_month: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    route: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    input_token_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    output_token_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    actual_usd: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(256))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    reserved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatch_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('reserved','dispatching','uncertain','reconciled','cancelled','non_billable')",
            name="ck_personal_paid_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_personal_paid_attempt"),
        CheckConstraint(
            "input_token_bound > 0 AND output_token_bound >= 0",
            name="ck_personal_paid_bounds",
        ),
        CheckConstraint(
            "reserved_usd >= 0 AND (actual_usd IS NULL OR actual_usd >= 0)",
            name="ck_personal_paid_cost",
        ),
        Index("ix_personal_paid_workspace_month", "workspace_id", "accounting_month"),
        Index("ix_personal_paid_run", "run_id"),
    )
