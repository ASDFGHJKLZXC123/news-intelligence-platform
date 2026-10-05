"""Add retained capture receipts and explicit personal enrichment transfers.

Revision ID: 0021_personal_daily_bounds
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_personal_daily_bounds"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("personal_captures", sa.Column("original_url", sa.Text(), nullable=True))
    op.add_column(
        "personal_captures",
        sa.Column("admission_charged", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("personal_captures", sa.Column("processing_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_personal_capture_processing_run",
        "personal_captures",
        "personal_runs",
        ["processing_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "personal_captures",
        sa.Column(
            "enrichment_state", sa.String(64), nullable=False, server_default="pending_admission"
        ),
    )
    op.create_unique_constraint(
        "uq_personal_capture_article", "personal_captures", ["workspace_id", "article_id"]
    )
    # Preserve historical identity/dates/content. Conservative states never give a failed
    # assisted run an implicit new paid attempt; raw terminal work can transfer explicitly.
    op.execute("""
        UPDATE personal_captures c SET original_url = c.canonical_url,
          processing_run_id = c.admitted_run_id,
          enrichment_state = CASE
            WHEN c.admitted_at IS NULL THEN 'pending_admission'
            WHEN EXISTS (SELECT 1 FROM event_articles ea WHERE ea.article_id=c.article_id)
              THEN 'grouped'
            WHEN r.state IN ('failed','partially_failed') THEN 'provider_failed'
            WHEN p.execution_profile='raw' AND r.state='succeeded' THEN 'disabled_by_profile'
            WHEN c.article_id <> ALL(r.enrichment_article_ids) AND r.state='succeeded'
              THEN 'deferred_enrichment_capacity'
            ELSE 'selected' END
        FROM personal_runs r JOIN personal_profile_revisions p ON p.id=r.profile_revision_id
        WHERE r.id=c.admitted_run_id
    """)
    op.create_table(
        "personal_feed_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("personal_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("personal_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id", sa.Uuid(), sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_personal_feed_receipts_run_id", "personal_feed_receipts", ["run_id"])
    op.create_table(
        "personal_enrichment_transfers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("personal_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "article_id",
            sa.Uuid(),
            sa.ForeignKey("articles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "from_run_id",
            sa.Uuid(),
            sa.ForeignKey("personal_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "to_run_id",
            sa.Uuid(),
            sa.ForeignKey("personal_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column(
            "transferred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("to_run_id", "article_id", name="uq_personal_transfer_run_article"),
        sa.CheckConstraint("from_run_id <> to_run_id", name="ck_personal_transfer_distinct_runs"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Personal daily-use records are retained. Disable personal processing to roll back "
        "application behavior; do not drop admission, receipt or transfer history."
    )
