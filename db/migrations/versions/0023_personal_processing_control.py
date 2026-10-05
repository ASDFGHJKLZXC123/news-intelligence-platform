"""Expand the existing processing mode into durable global ownership.

Revision ID: 0023_personal_processing_control
Revises: 0022_personal_spending
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023_personal_processing_control"
down_revision = "0022_personal_spending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retain the selected mode and all earlier rows; no activation occurs here.
    op.add_column(
        "personal_writer_mode",
        sa.Column("generation", sa.BigInteger(), server_default="0", nullable=False),
    )
    for name in (
        "workspace_id",
        "active_run_id",
        "delivery_token",
        "ownership_token",
        "launcher_id",
    ):
        op.add_column("personal_writer_mode", sa.Column(name, sa.Uuid(), nullable=True))
    op.add_column("personal_writer_mode", sa.Column("active_attempt", sa.Integer(), nullable=True))
    op.add_column("personal_writer_mode", sa.Column("child_pid", sa.Integer(), nullable=True))
    op.add_column(
        "personal_writer_mode", sa.Column("child_identity", sa.String(256), nullable=True)
    )
    op.add_column(
        "personal_writer_mode",
        sa.Column("child_history", JSONB(), server_default="{}", nullable=False),
    )
    op.add_column(
        "personal_writer_mode",
        sa.Column("unconfirmed_child", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    for name in (
        "queued_at",
        "started_at",
        "graceful_deadline_at",
        "hard_deadline_at",
        "lease_expires_at",
    ):
        op.add_column(
            "personal_writer_mode", sa.Column(name, sa.DateTime(timezone=True), nullable=True)
        )
    op.create_foreign_key(
        "fk_personal_processing_workspace",
        "personal_writer_mode",
        "personal_workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_personal_processing_active_run",
        "personal_writer_mode",
        "personal_runs",
        ["active_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_personal_processing_generation", "personal_writer_mode", "generation >= 0"
    )
    op.add_column(
        "personal_runs",
        sa.Column("delivery_transport", sa.String(32), server_default="celery", nullable=False),
    )
    op.add_column(
        "personal_runs",
        sa.Column("fencing_generation", sa.BigInteger(), server_default="0", nullable=False),
    )
    for name in ("queued_at", "started_at", "graceful_deadline_at", "hard_deadline_at"):
        op.add_column("personal_runs", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    # If exactly one old personal writer is active, retain its original lease and infer
    # its fixed instants. Ambiguous old owners remain untouched and block new starts.
    op.execute("""
        UPDATE personal_runs SET fencing_generation = 1,
            queued_at = CASE WHEN state='queued' THEN lease_expires_at - interval '2 minutes' END,
            started_at = CASE WHEN state='running' THEN lease_expires_at - interval '35 minutes' END,
            graceful_deadline_at = CASE WHEN state='running' THEN lease_expires_at - interval '10 minutes' END,
            hard_deadline_at = CASE WHEN state='running' THEN lease_expires_at - interval '5 minutes' END
        WHERE state IN ('queued','running')
          AND (SELECT count(*) FROM personal_runs WHERE state IN ('queued','running')) = 1
    """)
    op.execute("""
        UPDATE personal_writer_mode AS c SET generation=1, workspace_id=r.workspace_id,
            active_run_id=r.id, active_attempt=r.attempt,
            delivery_token=CASE WHEN r.state='queued' THEN r.ownership_token END,
            ownership_token=CASE WHEN r.state='running' THEN r.ownership_token END,
            queued_at=r.queued_at, started_at=r.started_at,
            graceful_deadline_at=r.graceful_deadline_at, hard_deadline_at=r.hard_deadline_at,
            lease_expires_at=r.lease_expires_at
        FROM personal_runs AS r WHERE r.fencing_generation=1 AND r.state IN ('queued','running')
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Processing ownership and history must be retained; select the previous supported runtime mode instead."
    )
