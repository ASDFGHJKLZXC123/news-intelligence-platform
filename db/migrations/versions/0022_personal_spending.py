"""Retain paid dispatch reservations and legacy application usage.

Revision ID: 0022_personal_spending
Revises: 0021_personal_daily_bounds
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_personal_spending"
down_revision = "0021_personal_daily_bounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_spending_state",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("legacy_cutover_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["personal_workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_table(
        "personal_legacy_usage",
        sa.Column("llm_run_id", sa.Uuid(), nullable=False),
        sa.Column("accounting_month", sa.Date(), nullable=False),
        sa.Column("actual_usd", sa.Numeric(28, 12), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("actual_usd IS NULL OR actual_usd >= 0", name="ck_personal_legacy_cost"),
        sa.ForeignKeyConstraint(["llm_run_id"], ["llm_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("llm_run_id"),
    )
    op.create_index(
        "ix_personal_legacy_usage_accounting_month", "personal_legacy_usage", ["accounting_month"]
    )
    op.create_table(
        "personal_paid_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("accounting_month", sa.Date(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("route", postgresql.JSONB(), nullable=False),
        sa.Column("input_token_bound", sa.Integer(), nullable=False),
        sa.Column("output_token_bound", sa.Integer(), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(28, 12), nullable=False),
        sa.Column("actual_usd", sa.Numeric(28, 12), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("provider_request_id", sa.String(256), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatch_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('reserved','dispatching','uncertain','reconciled','cancelled','non_billable')",
            name="ck_personal_paid_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_personal_paid_attempt"),
        sa.CheckConstraint(
            "input_token_bound > 0 AND output_token_bound >= 0", name="ck_personal_paid_bounds"
        ),
        sa.CheckConstraint(
            "reserved_usd >= 0 AND (actual_usd IS NULL OR actual_usd >= 0)",
            name="ck_personal_paid_cost",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["personal_workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["personal_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_personal_paid_workspace_month",
        "personal_paid_requests",
        ["workspace_id", "accounting_month"],
    )
    op.create_index("ix_personal_paid_run", "personal_paid_requests", ["run_id"])
    # Preserve all known records and explicit unknowns once at upgrade. ROUND UP to
    # the storage quantum so pre-existing sub-quantum costs never become free credit.
    op.execute("""
        INSERT INTO personal_legacy_usage (llm_run_id, accounting_month, actual_usd, evidence)
        SELECT id,
               date_trunc('month', COALESCE(started_at, completed_at, created_at)
                                     AT TIME ZONE 'UTC')::date,
               CASE WHEN cost_usd > 0 THEN ceil(cost_usd * 1000000000000) / 1000000000000
                    WHEN input_refs @> jsonb_build_object('cache_hit', true)
                      OR (status = 'failed' AND error_message = 'token bucket limit reached'
                          AND input_tokens = 0 AND output_tokens = 0 AND latency_ms = 0)
                      OR (provider = 'offline-personal-fixture'
                          AND model LIKE 'offline-personal-fixture:%')
                    THEN 0 ELSE NULL END,
               jsonb_build_object('kind', 'legacy_llm_run', 'classification', 'positive_cost_or_proven_no_dispatch')
        FROM llm_runs
    """)
    op.execute("""
        INSERT INTO personal_spending_state (workspace_id, legacy_cutover_at)
        SELECT id, clock_timestamp() FROM personal_workspaces
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Paid usage and unresolved reservations must be retained; disable processing instead of dropping the ledger."
    )
