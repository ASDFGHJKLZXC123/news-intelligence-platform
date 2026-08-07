"""intelligence contract schema

One Alembic pass for the Stage 1 contract changes (docs/implementation-order.md):

- ``article_embeddings``: composite ``(article_id, model, model_version)`` primary key,
  so re-embedding under a new model no longer overwrites the old vector (ADR 0004).
- New identity tables ``entity_aliases`` / ``entity_redirects`` (ADR 0005/0006).
- New ``historical_episodes`` with onset/outcome separation and an HNSW cosine index on
  the onset embedding (specs/historical-episode-schema.md).
- New LLM landing tables ``forecast_scenarios`` / ``event_analogies`` / ``risk_warnings``
  plus the numeric scale constraints (specs/llm-contracts-reconciliation.md).
- ``llm_runs``: tokens, params, status, error, prompt hash/template, trace (ADR 0008).
- ``alerts``: lifecycle, hysteresis, and notification columns (ADR 0010). ``state``
  replaces the overlapping ``status`` column.
- ``reports`` / ``report_sections``: versioning, staleness, and claim-level citations
  (specs/report-generation.md).

New tables are created from the ORM metadata, matching the convention set by migrations
0002-0012; alterations to existing tables are explicit ``op`` calls.

Convergent by design
--------------------
Migrations 0002/0009/0012 build their tables from *live* ORM metadata
(``model.__table__``), so a column this revision adds to ``llm_runs``, ``alerts``,
``reports``, ``report_sections``, or ``article_embeddings`` is already present on a
database created from scratch today -- the CREATE TABLE that first introduced the table
picks it up from the model. Migration 0003 works around this for the 0010 provenance
columns by rebuilding its tables minus the later columns (``_legacy_table``); 0002/0009/
0012 have no such projection.

Rather than rewrite three committed revisions, every operation here is guarded on the
database's actual state: add what is missing, drop what is present. The result converges
to the same schema whether it runs against a legacy database (which has the pre-Stage-1
shape) or a fresh one (which does not), and it stays reversible either way. Giving
0002/0009/0012 a ``_legacy_table`` projection is the proper cleanup and is recorded in
docs/worklogs/stage1-schema-migration.md as a follow-up for the Lead.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from db.models import STAGE1_INTELLIGENCE_TABLES
from db.models.core import (
    ACTIVE_ALERT_STATES,
    ALERT_STATES,
    GROUNDING_STATUSES,
    LLM_RUN_STATUSES,
    REPORT_STATUSES,
    RISK_LEVELS,
)

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [model.__table__ for model in STAGE1_INTELLIGENCE_TABLES]

# Historical literals, not imports from the live runtime settings. A future embedding snapshot
# must not cause a database upgrading through 0013 to relabel legacy vectors as that new space.
_LEGACY_EMBEDDING_MODEL = "text-embedding-3-small"
_LEGACY_EMBEDDING_MODEL_VERSION = "current"


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


# --------------------------------------------------------------------------------------
# State-guarded DDL (see "Convergent by design" above)
# --------------------------------------------------------------------------------------


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _checks(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {check["name"] for check in inspector.get_check_constraints(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _primary_key(table: str) -> list[str]:
    return list(sa.inspect(op.get_bind()).get_pk_constraint(table)["constrained_columns"])


def _column_type(table: str, column: str) -> str:
    for candidate in sa.inspect(op.get_bind()).get_columns(table):
        if candidate["name"] == column:
            return str(candidate["type"]).lower()
    return ""


def add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def drop_column(table: str, name: str) -> None:
    if name in _columns(table):
        op.drop_column(table, name)


def add_check(name: str, table: str, condition: str) -> None:
    if name not in _checks(table):
        op.create_check_constraint(name, table, condition)


def drop_check(name: str, table: str) -> None:
    if name in _checks(table):
        op.drop_constraint(name, table, type_="check")


def add_index(name: str, table: str, columns: list[str], **kwargs: object) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns, **kwargs)


def drop_index(name: str, table: str) -> None:
    if name in _indexes(table):
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    _upgrade_article_embeddings()
    _upgrade_llm_runs()
    _upgrade_alerts()
    _upgrade_reports()
    _upgrade_numeric_scales()

    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)

    _downgrade_numeric_scales()
    _downgrade_reports()
    _downgrade_alerts()
    _downgrade_llm_runs()
    _downgrade_article_embeddings()


# --------------------------------------------------------------------------------------
# article_embeddings: one row per (article, model, model_version)
# --------------------------------------------------------------------------------------


_EMBEDDING_KEY = ["article_id", "model", "model_version"]


def _upgrade_article_embeddings() -> None:
    if _primary_key("article_embeddings") == _EMBEDDING_KEY:
        return
    # Rows predating the embedding-identity contract carry a NULL model_version. Stamp the
    # deployed pair onto them before the column joins the key, so no vector is orphaned.
    op.execute(
        sa.text(
            "UPDATE article_embeddings SET model_version = :version WHERE model_version IS NULL"
        ).bindparams(version=_LEGACY_EMBEDDING_MODEL_VERSION)
    )
    op.alter_column(
        "article_embeddings",
        "model",
        existing_type=sa.String(length=128),
        existing_nullable=False,
        server_default=_LEGACY_EMBEDDING_MODEL,
    )
    op.alter_column(
        "article_embeddings",
        "model_version",
        existing_type=sa.String(length=64),
        existing_nullable=True,
        nullable=False,
        server_default=_LEGACY_EMBEDDING_MODEL_VERSION,
    )
    # article_id was the whole key, so it is unique -- widening it cannot collide.
    op.drop_constraint("article_embeddings_pkey", "article_embeddings", type_="primary")
    op.create_primary_key("article_embeddings_pkey", "article_embeddings", _EMBEDDING_KEY)


def _downgrade_article_embeddings() -> None:
    if _primary_key("article_embeddings") != _EMBEDDING_KEY:
        return
    # Narrowing the key needs one row per article; keep the newest embedding of each.
    op.execute(
        sa.text(
            """
            DELETE FROM article_embeddings a
            USING article_embeddings b
            WHERE a.article_id = b.article_id
              AND (a.created_at, a.model, a.model_version)
                < (b.created_at, b.model, b.model_version)
            """
        )
    )
    op.drop_constraint("article_embeddings_pkey", "article_embeddings", type_="primary")
    op.create_primary_key("article_embeddings_pkey", "article_embeddings", ["article_id"])
    op.alter_column(
        "article_embeddings",
        "model_version",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        "article_embeddings",
        "model",
        existing_type=sa.String(length=128),
        existing_nullable=False,
        server_default=None,
    )


# --------------------------------------------------------------------------------------
# llm_runs: cost, determinism, and trace observability (ADR 0008)
# --------------------------------------------------------------------------------------

_LLM_RUN_COLUMNS = (
    ("prompt_template_version", sa.String(length=64)),
    ("prompt_hash", sa.String(length=64)),
    ("model_params", JSONB()),
    ("temperature", sa.Numeric()),
    ("seed", sa.Integer()),
    ("error_message", sa.Text()),
    ("error_details", JSONB()),
    ("output_schema_name", sa.String(length=64)),
    ("no_finding_reason", sa.Text()),
    ("input_tokens", sa.Integer()),
    ("output_tokens", sa.Integer()),
    ("trace_id", sa.String(length=64)),
    ("started_at", sa.DateTime(timezone=True)),
    ("completed_at", sa.DateTime(timezone=True)),
)


_LLM_RUN_CHECKS = (
    ("ck_llm_runs_status", f"status IN ({_in(LLM_RUN_STATUSES)})"),
    ("ck_llm_runs_attempt_positive", "attempt >= 1"),
    ("ck_llm_runs_input_tokens_non_negative", "input_tokens IS NULL OR input_tokens >= 0"),
    ("ck_llm_runs_output_tokens_non_negative", "output_tokens IS NULL OR output_tokens >= 0"),
    ("ck_llm_runs_latency_ms_non_negative", "latency_ms IS NULL OR latency_ms >= 0"),
    ("ck_llm_runs_cost_usd_non_negative", "cost_usd IS NULL OR cost_usd >= 0"),
    (
        "ck_llm_runs_temperature_range",
        "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
    ),
)


def _upgrade_llm_runs() -> None:
    for name, type_ in _LLM_RUN_COLUMNS:
        add_column("llm_runs", sa.Column(name, type_, nullable=True))
    # Rows written before this migration all completed, or they would not exist.
    add_column(
        "llm_runs",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="succeeded"),
    )
    add_column("llm_runs", sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))

    for name, condition in _LLM_RUN_CHECKS:
        add_check(name, "llm_runs", condition)
    add_index("ix_llm_runs_trace_id", "llm_runs", ["trace_id"])
    add_index("ix_llm_runs_status_created", "llm_runs", ["status", "created_at"])


def _downgrade_llm_runs() -> None:
    drop_index("ix_llm_runs_status_created", "llm_runs")
    drop_index("ix_llm_runs_trace_id", "llm_runs")
    for name, _condition in reversed(_LLM_RUN_CHECKS):
        drop_check(name, "llm_runs")
    drop_column("llm_runs", "attempt")
    drop_column("llm_runs", "status")
    for name, _type in reversed(_LLM_RUN_COLUMNS):
        drop_column("llm_runs", name)


# --------------------------------------------------------------------------------------
# alerts: lifecycle, hysteresis, notification (ADR 0010)
# --------------------------------------------------------------------------------------

_ALERT_COLUMNS = (
    ("dedupe_key", sa.String(length=255), None),
    ("evidence_signal_ids", sa.ARRAY(sa.Uuid()), None),
    ("score_version", sa.String(length=16), "v1"),
    ("what_could_reduce_risk", JSONB(), None),
    ("news_driven", sa.Numeric(), None),
    ("last_evaluated_at", sa.DateTime(timezone=True), None),
    ("clear_band_since", sa.DateTime(timezone=True), None),
    ("cooldown_until", sa.DateTime(timezone=True), None),
    ("notified_at", sa.DateTime(timezone=True), None),
    ("all_clear_notified_at", sa.DateTime(timezone=True), None),
    ("resolved_at", sa.DateTime(timezone=True), None),
)


_ALERT_CHECKS = (
    ("ck_alerts_risk_score", "risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)"),
    ("ck_alerts_state", f"state IN ({_in(ALERT_STATES)})"),
    ("ck_alerts_severity", f"severity IN ({_in(RISK_LEVELS)})"),
    ("ck_alerts_news_driven", "news_driven IS NULL OR (news_driven >= 0 AND news_driven <= 1)"),
    ("ck_alerts_velocity_streak_non_negative", "velocity_streak >= 0"),
    ("ck_alerts_resolved_has_timestamp", "state <> 'resolved' OR resolved_at IS NOT NULL"),
    ("ck_alerts_superseded_has_target", "state <> 'superseded' OR superseded_by IS NOT NULL"),
    ("ck_alerts_no_self_supersede", "superseded_by IS NULL OR superseded_by <> id"),
)


def _upgrade_alerts() -> None:
    for name, type_, default in _ALERT_COLUMNS:
        add_column("alerts", sa.Column(name, type_, nullable=True, server_default=default))
    add_column(
        "alerts", sa.Column("state", sa.String(length=32), nullable=False, server_default="open")
    )
    add_column(
        "alerts", sa.Column("velocity_streak", sa.Integer(), nullable=False, server_default="0")
    )
    # Null-model gate: nothing has been backtested against baseline.py yet, so every
    # pre-existing alert is experimental by definition.
    add_column(
        "alerts", sa.Column("experimental", sa.Boolean(), nullable=False, server_default="true")
    )
    add_column(
        "alerts",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    add_column(
        "alerts",
        sa.Column(
            "superseded_by",
            sa.Uuid(),
            sa.ForeignKey("alerts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    if "status" in _columns("alerts"):
        # `state` supersedes `status`: carry the legacy value over, then drop the column.
        # `acknowledged` was a UI concept, not a lifecycle state -- those alerts are open.
        op.execute(
            sa.text(
                """
                UPDATE alerts
                SET state = CASE
                    WHEN status = 'resolved' THEN 'resolved'
                    WHEN status = 'superseded' THEN 'superseded'
                    ELSE 'open'
                END
                """
            )
        )
        drop_index("ix_alerts_user_status_created", "alerts")
        drop_index("ix_alerts_status", "alerts")
        op.drop_column("alerts", "status")

    # ck_alerts_resolved_has_timestamp demands a timestamp on every resolved alert; legacy
    # resolved rows never recorded one, so fall back to when the alert was raised.
    op.execute(
        sa.text(
            "UPDATE alerts SET resolved_at = created_at "
            "WHERE state = 'resolved' AND resolved_at IS NULL"
        )
    )
    # A legacy superseded alert names no successor, so it cannot satisfy
    # ck_alerts_superseded_has_target. Reopen it rather than drop the row.
    op.execute(
        sa.text(
            "UPDATE alerts SET state = 'open' WHERE state = 'superseded' AND superseded_by IS NULL"
        )
    )

    for name, condition in _ALERT_CHECKS:
        add_check(name, "alerts", condition)

    add_index("ix_alerts_state", "alerts", ["state"])
    add_index("ix_alerts_dedupe_key", "alerts", ["dedupe_key"])
    add_index("ix_alerts_superseded_by", "alerts", ["superseded_by"])
    add_index("ix_alerts_state_updated", "alerts", ["state", "updated_at"])
    add_index("ix_alerts_user_state_created", "alerts", ["user_id", "state", "created_at"])
    # At most one live alert per dedupe key: the same condition updates that row instead of
    # spawning a new one, which is what makes flapping structurally impossible.
    add_index(
        "uq_alerts_active_dedupe_key",
        "alerts",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text(
            f"dedupe_key IS NOT NULL AND state IN ({_in(ACTIVE_ALERT_STATES)})"
        ),
    )


def _downgrade_alerts() -> None:
    drop_index("uq_alerts_active_dedupe_key", "alerts")
    drop_index("ix_alerts_user_state_created", "alerts")
    drop_index("ix_alerts_state_updated", "alerts")
    drop_index("ix_alerts_superseded_by", "alerts")
    drop_index("ix_alerts_dedupe_key", "alerts")
    drop_index("ix_alerts_state", "alerts")
    for name, _condition in reversed(_ALERT_CHECKS):
        drop_check(name, "alerts")

    add_column(
        "alerts",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
    )
    op.execute(
        sa.text(
            "UPDATE alerts SET status = "
            "CASE WHEN state IN ('resolved', 'superseded') THEN state ELSE 'open' END"
        )
    )
    add_index("ix_alerts_status", "alerts", ["status"])
    add_index("ix_alerts_user_status_created", "alerts", ["user_id", "status", "created_at"])

    drop_column("alerts", "superseded_by")
    drop_column("alerts", "updated_at")
    drop_column("alerts", "experimental")
    drop_column("alerts", "velocity_streak")
    drop_column("alerts", "state")
    for name, _type, _default in reversed(_ALERT_COLUMNS):
        drop_column("alerts", name)


# --------------------------------------------------------------------------------------
# reports / report_sections: versioning and claim-level citations
# --------------------------------------------------------------------------------------


def _upgrade_reports() -> None:
    # The daily brief is global, not per-user (report-generation spec); the watchlist-scoped
    # report is deferred to Phase 6.
    op.alter_column(
        "reports", "user_id", existing_type=sa.Uuid(), existing_nullable=False, nullable=True
    )
    add_column("reports", sa.Column("brief_date", sa.Date(), nullable=True))
    add_column(
        "reports",
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="SET NULL"), nullable=True
        ),
    )
    add_column("reports", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    add_column("reports", sa.Column("change_reason", sa.Text(), nullable=True))
    add_column("reports", sa.Column("stale", sa.Boolean(), nullable=False, server_default="false"))
    add_column(
        "reports",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # No `draft` limbo: the lifecycle is generating -> grounding_check -> published (+failed).
    # Map every legacy status into it before the constraint lands.
    op.execute(
        sa.text(
            f"UPDATE reports SET status = 'generating' WHERE status NOT IN ({_in(REPORT_STATUSES)})"
        )
    )
    op.alter_column(
        "reports",
        "status",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        server_default="generating",
    )
    add_check("ck_reports_status", "reports", f"status IN ({_in(REPORT_STATUSES)})")
    add_check("ck_reports_version_positive", "reports", "version >= 1")
    add_check("ck_reports_brief_xor_event", "reports", "brief_date IS NULL OR event_id IS NULL")
    add_index(
        "ix_reports_report_type_status_created",
        "reports",
        ["report_type", "status", "created_at"],
    )
    add_index("ix_reports_brief_date", "reports", ["brief_date"])
    add_index("ix_reports_event_id", "reports", ["event_id"])
    # Regeneration adds a version; it never mutates a published report.
    add_index(
        "uq_reports_type_brief_date_version",
        "reports",
        ["report_type", "brief_date", "version"],
        unique=True,
        postgresql_where=sa.text("brief_date IS NOT NULL"),
    )
    add_index(
        "uq_reports_type_event_version",
        "reports",
        ["report_type", "event_id", "version"],
        unique=True,
        postgresql_where=sa.text("event_id IS NOT NULL"),
    )

    add_column("report_sections", sa.Column("blocks", JSONB(), nullable=True))
    add_column(
        "report_sections",
        sa.Column(
            "grounding_status", sa.String(length=32), nullable=False, server_default="pending"
        ),
    )
    add_check(
        "ck_report_sections_grounding_status",
        "report_sections",
        f"grounding_status IN ({_in(GROUNDING_STATUSES)})",
    )
    # evidence_refs becomes a typed list of claim IDs -- claim-level, not section-level,
    # citations. Legacy JSONB arrays of UUID strings convert; anything else was never a
    # claim reference and becomes NULL.
    #
    # Done as add-populate-swap rather than ALTER ... TYPE ... USING: unnesting the JSONB
    # array needs a subquery, and Postgres rejects subqueries in a USING transform just as
    # it does in a CHECK. A subquery in an UPDATE ... SET is fine.
    if "jsonb" in _column_type("report_sections", "evidence_refs"):
        op.add_column("report_sections", sa.Column("claim_ids", sa.ARRAY(sa.Uuid()), nullable=True))
        op.execute(
            sa.text(
                """
                UPDATE report_sections
                SET claim_ids = (
                    SELECT array_agg(element::uuid)
                    FROM jsonb_array_elements_text(evidence_refs) AS element
                    WHERE element ~*
                        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                )
                WHERE evidence_refs IS NOT NULL AND jsonb_typeof(evidence_refs) = 'array'
                """
            )
        )
        op.drop_column("report_sections", "evidence_refs")
        op.alter_column("report_sections", "claim_ids", new_column_name="evidence_refs")


def _downgrade_reports() -> None:
    if "jsonb" not in _column_type("report_sections", "evidence_refs"):
        op.execute(
            sa.text(
                """
                ALTER TABLE report_sections
                ALTER COLUMN evidence_refs TYPE jsonb
                USING (CASE WHEN evidence_refs IS NULL THEN NULL ELSE to_jsonb(evidence_refs) END)
                """
            )
        )
    drop_check("ck_report_sections_grounding_status", "report_sections")
    drop_column("report_sections", "grounding_status")
    drop_column("report_sections", "blocks")

    drop_index("uq_reports_type_event_version", "reports")
    drop_index("uq_reports_type_brief_date_version", "reports")
    drop_index("ix_reports_event_id", "reports")
    drop_index("ix_reports_brief_date", "reports")
    drop_index("ix_reports_report_type_status_created", "reports")
    drop_check("ck_reports_brief_xor_event", "reports")
    drop_check("ck_reports_version_positive", "reports")
    drop_check("ck_reports_status", "reports")
    op.alter_column(
        "reports",
        "status",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        server_default="draft",
    )
    op.execute(sa.text("UPDATE reports SET status = 'draft' WHERE status = 'generating'"))

    drop_column("reports", "updated_at")
    drop_column("reports", "stale")
    drop_column("reports", "change_reason")
    drop_column("reports", "version")
    drop_column("reports", "event_id")
    drop_column("reports", "brief_date")
    # user_id goes back to NOT NULL: a global report has no owner to restore, so drop those.
    op.execute(sa.text("DELETE FROM reports WHERE user_id IS NULL"))
    op.alter_column(
        "reports", "user_id", existing_type=sa.Uuid(), existing_nullable=True, nullable=False
    )


# --------------------------------------------------------------------------------------
# Numeric scales on the LLM destination tables (llm-contracts-reconciliation spec)
# --------------------------------------------------------------------------------------

_SCALE_CONSTRAINTS = (
    ("events", "ck_events_severity_score", "severity_score", 100),
    ("event_entities", "ck_event_entities_impact_score", "impact_score", 100),
    ("event_entities", "ck_event_entities_confidence_score", "confidence_score", 1),
    ("event_companies", "ck_event_companies_impact_score", "impact_score", 100),
    ("event_companies", "ck_event_companies_risk_score", "risk_score", 100),
    ("event_companies", "ck_event_companies_confidence_score", "confidence_score", 1),
    ("event_industries", "ck_event_industries_impact_score", "impact_score", 100),
    ("event_industries", "ck_event_industries_risk_score", "risk_score", 100),
    ("event_industries", "ck_event_industries_opportunity_score", "opportunity_score", 100),
)


def _upgrade_numeric_scales() -> None:
    for table, name, column, upper in _SCALE_CONSTRAINTS:
        add_check(name, table, f"{column} IS NULL OR ({column} >= 0 AND {column} <= {upper})")


def _downgrade_numeric_scales() -> None:
    for table, name, _column, _upper in reversed(_SCALE_CONSTRAINTS):
        drop_check(name, table)
