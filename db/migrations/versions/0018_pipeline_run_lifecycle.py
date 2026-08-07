"""Add durable lifecycle details for the manual daily pipeline.

The generic ``jobs`` row owns deterministic identity, state, attempts, and errors.  This
one-to-one sidecar owns only pipeline-specific facts: process date, broker/run leases, durable
event fan-out scope, timestamps, and the complete terminal coordinator result.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "pipeline_run_details"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("process_date", sa.Date(), nullable=False),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("delivery_token", sa.Uuid(), nullable=True),
        sa.Column("run_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "event_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "process_date",
            name="uq_pipeline_run_details_process_date",
        ),
        sa.UniqueConstraint(
            "celery_task_id",
            name="uq_pipeline_run_details_celery_task_id",
        ),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
