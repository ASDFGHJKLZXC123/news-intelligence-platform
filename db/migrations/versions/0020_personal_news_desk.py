"""Add the personal news desk schema without rewriting legacy data.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A previous experimental/manual row with the future type has no sidecar to prove its
    # workspace/snapshot identity.  Refuse the upgrade rather than silently weakening its key.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM reports WHERE report_type = 'personal_daily_brief') THEN
            RAISE EXCEPTION 'cannot migrate unlinked personal_daily_brief rows; archive or link them explicitly first';
          END IF;
        END $$
        """
    )
    op.create_table(
        "personal_workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("singleton", sa.Boolean(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("active_profile_revision_id", sa.Uuid(), nullable=True),
        sa.Column("setup_provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("singleton", name="ck_personal_workspaces_singleton"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton"),
    )
    op.create_index("ix_personal_workspaces_owner_id", "personal_workspaces", ["owner_id"])

    op.create_table(
        "personal_profile_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("selected_source_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("include_phrases", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("exclude_phrases", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("execution_profile", sa.String(length=32), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_revision", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision >= 1", name="ck_personal_profiles_revision_positive"),
        sa.CheckConstraint(
            "execution_profile IN ('assisted', 'raw')",
            name="ck_personal_profiles_execution_profile",
        ),
        sa.CheckConstraint(
            "cardinality(include_phrases) <= 20 AND cardinality(exclude_phrases) <= 20",
            name="ck_personal_profiles_phrase_count",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["personal_workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "revision", name="uq_personal_profiles_revision"),
    )
    op.create_index(
        "ix_personal_profile_revisions_workspace_id", "personal_profile_revisions", ["workspace_id"]
    )
    op.create_foreign_key(
        "fk_personal_workspaces_active_profile_revision",
        "personal_workspaces",
        "personal_profile_revisions",
        ["active_profile_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "personal_writer_mode",
        sa.Column("singleton", sa.Boolean(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("singleton", name="ck_personal_writer_mode_singleton"),
        sa.CheckConstraint("mode IN ('personal', 'legacy')", name="ck_personal_writer_mode_value"),
        sa.PrimaryKeyConstraint("singleton"),
    )
    op.execute(
        """
        INSERT INTO personal_writer_mode (singleton, mode)
        VALUES (true, 'legacy')
        """
    )

    op.create_table(
        "personal_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("profile_revision_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("ownership_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("celery_task_id", sa.String(length=128), nullable=True),
        sa.Column("capture_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capture_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admitted_article_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("enrichment_article_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("event_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("scopes_frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stage_results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("terminal_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'partially_failed', 'failed')",
            name="ck_personal_runs_state",
        ),
        sa.CheckConstraint("attempt >= 1 AND max_attempts = 3", name="ck_personal_runs_attempts"),
        sa.CheckConstraint("execution_mode = 'personal'", name="ck_personal_runs_execution_mode"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["profile_revision_id"], ["personal_profile_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["personal_workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
        sa.UniqueConstraint("workspace_id", "local_date", name="uq_personal_runs_workspace_date"),
    )
    op.create_index("ix_personal_runs_workspace_id", "personal_runs", ["workspace_id"])
    op.create_index("ix_personal_runs_state", "personal_runs", ["state"])
    op.create_index(
        "ix_personal_runs_workspace_date", "personal_runs", ["workspace_id", "local_date"]
    )

    op.create_table(
        "personal_captures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("rss_summary", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("article_id", sa.Uuid(), nullable=True),
        sa.Column("admitted_run_id", sa.Uuid(), nullable=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["admitted_run_id"], ["personal_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["personal_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["personal_workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "source_id", "url_hash", name="uq_personal_capture_run_source_url"
        ),
        sa.UniqueConstraint(
            "workspace_id", "url_hash", name="uq_personal_capture_workspace_url"
        ),
    )
    op.create_index("ix_personal_captures_run_id", "personal_captures", ["run_id"])
    op.create_index(
        "ix_personal_captures_pending",
        "personal_captures",
        ["workspace_id", "admitted_at", "captured_at"],
    )

    op.create_table(
        "personal_run_event_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("qualifying_article_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("source_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ranking_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision >= 1", name="ck_personal_observation_revision_positive"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["personal_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "event_id", "revision", name="uq_personal_observation_revision"
        ),
    )
    op.create_index(
        "ix_personal_run_event_observations_run_id", "personal_run_event_observations", ["run_id"]
    )

    op.create_table(
        "personal_brief_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("profile_revision_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_event_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("selected_event_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("input_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("model_route", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_contract", sa.String(length=64), nullable=False),
        sa.Column(
            "prepared_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["profile_revision_id"], ["personal_profile_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["personal_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["personal_workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_foreign_key(
        "fk_personal_runs_snapshot_id",
        "personal_runs",
        "personal_brief_snapshots",
        ["snapshot_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "personal_report_links",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("brief_date", sa.Date(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version >= 1", name="ck_personal_report_version_positive"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["personal_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["personal_brief_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["personal_workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("report_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "brief_date",
            "version",
            name="uq_personal_reports_workspace_date_version",
        ),
    )
    op.create_index(
        "ix_personal_reports_workspace_date",
        "personal_report_links",
        ["workspace_id", "brief_date", "version"],
    )

    op.create_table(
        "personal_article_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("retained_title", sa.Text(), nullable=False),
        sa.Column("retained_summary", sa.Text(), nullable=True),
        sa.Column("retained_url", sa.Text(), nullable=False),
        sa.Column("retained_publisher", sa.Text(), nullable=True),
        sa.Column("retained_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["personal_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "article_id", name="uq_personal_article_revision_run_article"
        ),
    )

    op.create_table(
        "personal_claim_preparations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("article_revision_id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_item_id", sa.Uuid(), nullable=True),
        sa.Column("normalized_claim_key", sa.String(length=64), nullable=False),
        sa.Column("source_field", sa.String(length=16), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("exact_excerpt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "span_start >= 0 AND span_end >= span_start", name="ck_personal_claim_span"
        ),
        sa.CheckConstraint(
            "status IN ('supported', 'abstained', 'failed')",
            name="ck_personal_claim_preparation_status",
        ),
        sa.CheckConstraint(
            "source_field IN ('title', 'summary')", name="ck_personal_claim_source_field"
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["article_revision_id"], ["personal_article_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["evidence_item_id"], ["evidence_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["personal_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "article_revision_id",
            "normalized_claim_key",
            name="uq_personal_claim_preparation_identity",
        ),
    )
    op.create_index(
        "ix_personal_claim_preparations_run_status",
        "personal_claim_preparations",
        ["run_id", "status"],
    )

    op.drop_index("uq_reports_type_brief_date_version", table_name="reports")
    op.create_index(
        "uq_reports_type_brief_date_version",
        "reports",
        ["report_type", "brief_date", "version"],
        unique=True,
        postgresql_where=sa.text(
            "brief_date IS NOT NULL AND report_type <> 'personal_daily_brief'"
        ),
    )
    op.execute(
        """
        CREATE FUNCTION validate_personal_report_links() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM reports r
            LEFT JOIN personal_report_links l ON l.report_id = r.id
            WHERE r.report_type = 'personal_daily_brief'
              AND (l.report_id IS NULL OR r.brief_date IS DISTINCT FROM l.brief_date
                   OR r.version IS DISTINCT FROM l.version)
          ) OR EXISTS (
            SELECT 1
            FROM personal_report_links l
            JOIN reports r ON r.id = l.report_id
            JOIN personal_runs pr ON pr.id = l.run_id
            JOIN personal_brief_snapshots bs ON bs.id = l.snapshot_id
            WHERE r.report_type <> 'personal_daily_brief'
               OR r.brief_date IS DISTINCT FROM l.brief_date
               OR r.version IS DISTINCT FROM l.version
               OR pr.workspace_id IS DISTINCT FROM l.workspace_id
               OR pr.local_date IS DISTINCT FROM l.brief_date
               OR pr.snapshot_id IS DISTINCT FROM l.snapshot_id
               OR bs.workspace_id IS DISTINCT FROM l.workspace_id
               OR bs.run_id IS DISTINCT FROM l.run_id
               OR bs.profile_revision_id IS DISTINCT FROM pr.profile_revision_id
          ) OR EXISTS (
            SELECT 1 FROM personal_report_links l
            LEFT JOIN reports r ON r.id = l.report_id
            WHERE r.id IS NULL
          ) THEN
            RAISE EXCEPTION 'personal report/link/workspace/run/snapshot identity mismatch';
          END IF;
          RETURN NULL;
        END $$
        """
    )
    for table in (
        "reports",
        "personal_report_links",
        "personal_runs",
        "personal_brief_snapshots",
    ):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER ck_{table}_personal_report_identity
            AFTER INSERT OR UPDATE OR DELETE ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_personal_report_links()
            """
        )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM personal_workspaces)
             OR EXISTS (SELECT 1 FROM personal_profile_revisions)
             OR EXISTS (SELECT 1 FROM personal_runs)
             OR EXISTS (SELECT 1 FROM personal_captures)
             OR EXISTS (SELECT 1 FROM personal_run_event_observations)
             OR EXISTS (SELECT 1 FROM personal_brief_snapshots)
             OR EXISTS (SELECT 1 FROM personal_report_links)
             OR EXISTS (SELECT 1 FROM personal_article_revisions)
             OR EXISTS (SELECT 1 FROM personal_claim_preparations)
             OR EXISTS (SELECT 1 FROM reports WHERE report_type = 'personal_daily_brief')
             THEN
            RAISE EXCEPTION 'refusing to downgrade nonempty personal news desk data; archive it with an explicit migration first';
          END IF;
        END $$
        """
    )
    for table in (
        "reports",
        "personal_report_links",
        "personal_runs",
        "personal_brief_snapshots",
    ):
        op.execute(f"DROP TRIGGER ck_{table}_personal_report_identity ON {table}")
    op.execute("DROP FUNCTION validate_personal_report_links()")
    op.drop_index("uq_reports_type_brief_date_version", table_name="reports")
    op.create_index(
        "uq_reports_type_brief_date_version",
        "reports",
        ["report_type", "brief_date", "version"],
        unique=True,
        postgresql_where=sa.text("brief_date IS NOT NULL"),
    )
    op.drop_table("personal_claim_preparations")
    op.drop_table("personal_article_revisions")
    op.drop_index("ix_personal_reports_workspace_date", table_name="personal_report_links")
    op.drop_table("personal_report_links")
    op.drop_constraint("fk_personal_runs_snapshot_id", "personal_runs", type_="foreignkey")
    op.drop_table("personal_brief_snapshots")
    op.drop_index(
        "ix_personal_run_event_observations_run_id", table_name="personal_run_event_observations"
    )
    op.drop_table("personal_run_event_observations")
    op.drop_index("ix_personal_captures_pending", table_name="personal_captures")
    op.drop_index("ix_personal_captures_run_id", table_name="personal_captures")
    op.drop_table("personal_captures")
    op.drop_index("ix_personal_runs_workspace_date", table_name="personal_runs")
    op.drop_index("ix_personal_runs_state", table_name="personal_runs")
    op.drop_index("ix_personal_runs_workspace_id", table_name="personal_runs")
    op.drop_table("personal_runs")
    op.drop_table("personal_writer_mode")
    op.drop_constraint(
        "fk_personal_workspaces_active_profile_revision", "personal_workspaces", type_="foreignkey"
    )
    op.drop_index(
        "ix_personal_profile_revisions_workspace_id", table_name="personal_profile_revisions"
    )
    op.drop_table("personal_profile_revisions")
    op.drop_index("ix_personal_workspaces_owner_id", table_name="personal_workspaces")
    op.drop_table("personal_workspaces")
