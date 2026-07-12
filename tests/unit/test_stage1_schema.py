"""DB-free checks for the Stage 1 intelligence schema contract (migration 0013).

Sources of truth: docs/implementation-order.md (stage 1), ADR 0004/0008/0010, and the
specs for historical episodes, LLM contracts, report generation, and the API adapter.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import db.models as m
from db.base import Base

EXPECTED_STAGE1_INTELLIGENCE_TABLES = {
    "entity_aliases",
    "entity_redirects",
    "historical_episodes",
    "forecast_scenarios",
    "event_analogies",
    "risk_warnings",
}


def _table(name: str):
    return Base.metadata.tables[name]


def _constraints(name: str) -> set[str]:
    return {c.name for c in _table(name).constraints if c.name}


def _indexes(name: str) -> set[str]:
    return {index.name for index in _table(name).indexes}


def _ddl(name: str) -> str:
    table = _table(name)
    dialect = postgresql.dialect()
    statements = [str(CreateTable(table).compile(dialect=dialect))]
    statements += [str(CreateIndex(ix).compile(dialect=dialect)) for ix in table.indexes]
    return "\n".join(statements)


def test_stage1_intelligence_tables_registered() -> None:
    assert EXPECTED_STAGE1_INTELLIGENCE_TABLES <= set(Base.metadata.tables)
    assert {t.__tablename__ for t in m.STAGE1_INTELLIGENCE_TABLES} == (
        EXPECTED_STAGE1_INTELLIGENCE_TABLES
    )
    # Every Stage 1 model is exported by name.
    for model in m.STAGE1_INTELLIGENCE_TABLES:
        assert model.__name__ in m.__all__
        assert getattr(m, model.__name__) is model


@pytest.mark.parametrize("name", sorted(EXPECTED_STAGE1_INTELLIGENCE_TABLES))
def test_stage1_tables_compile_as_postgres_ddl(name: str) -> None:
    # Migration 0013 creates these from the ORM metadata, so a table that cannot compile
    # is a migration that cannot run.
    assert _ddl(name).strip()


# --------------------------------------------------------------------------------------
# Article embeddings: one vector per (article, model, model_version) -- ADR 0004
# --------------------------------------------------------------------------------------


def test_article_embeddings_composite_primary_key_and_defaults() -> None:
    table = _table("article_embeddings")
    assert {"article_id", "model", "model_version"} == {
        column.name for column in table.primary_key.columns
    }
    assert table.c.model.nullable is False
    assert table.c.model_version.nullable is False
    # Defaults exist so the migration can backfill pre-existing rows into the new key.
    assert str(table.c.model.server_default.arg) == m.EMBEDDING_MODEL
    assert str(table.c.model_version.server_default.arg) == m.EMBEDDING_MODEL_VERSION


# --------------------------------------------------------------------------------------
# Identity surface -- ADR 0005 / 0006
# --------------------------------------------------------------------------------------


def test_entity_aliases_contract() -> None:
    assert {c.name for c in _table("entity_aliases").columns} == {
        "id",
        "entity_id",
        "alias",
        "normalized_alias",
        "alias_type",
        "source",
        "valid_from",
        "valid_to",
        "created_at",
    }
    assert "uq_entity_aliases_entity_normalized_alias_source" in _constraints("entity_aliases")
    # Surface-form lookup is the linker's hot path.
    assert "ix_entity_aliases_normalized_alias" in _indexes("entity_aliases")


def test_entity_redirects_contract() -> None:
    assert {c.name for c in _table("entity_redirects").columns} == {
        "id",
        "old_entity_id",
        "new_entity_id",
        "reason",
        "effective_date",
        "created_at",
    }
    assert "uq_entity_redirects_old_new_effective_date" in _constraints("entity_redirects")
    assert "ck_entity_redirects_no_self_redirect" in _constraints("entity_redirects")


def test_identity_tables_declare_no_duplicate_index_names() -> None:
    # `index=True` already emits ix_<table>_<column>; re-declaring it in __table_args__
    # makes Postgres create the same index twice and the migration abort.
    for name in EXPECTED_STAGE1_INTELLIGENCE_TABLES:
        index_names = [index.name for index in _table(name).indexes]
        assert len(index_names) == len(set(index_names)), name


# --------------------------------------------------------------------------------------
# Historical episodes -- specs/historical-episode-schema.md
# --------------------------------------------------------------------------------------


def test_historical_episode_separates_onset_from_outcome() -> None:
    table = _table("historical_episodes")
    assert {c.name for c in table.columns} == {
        "id",
        "name",
        "episode_type",
        "onset_date",
        "peak_date",
        "end_date",
        "onset_summary",
        "onset_indicators",
        "onset_embedding",
        "model",
        "model_version",
        "outcome_summary",
        "outcomes",
        "resolution_mechanism",
        "geography",
        "affected_industries",
        "regime_tags",
        "parent_episode_id",
        "is_counterexample",
        "source_refs",
        "license_note",
        "version",
        "created_at",
        "updated_at",
        "review_due_at",
    }
    # Only onset fields are embedded: retrieval must never see an outcome (look-ahead bias).
    assert table.c.onset_embedding.nullable is False
    assert "outcome_embedding" not in table.c
    assert table.c.onset_summary.nullable is False


def test_historical_episode_onset_embedding_has_hnsw_cosine_index() -> None:
    ddl = _ddl("historical_episodes")
    assert "ix_historical_episodes_onset_embedding_hnsw" in _indexes("historical_episodes")
    assert "USING hnsw (onset_embedding vector_cosine_ops)" in ddl
    # Retrieval hard-filters before the vector scan.
    assert {
        "ix_historical_episodes_episode_type",
        "ix_historical_episodes_regime_tags",
        "ix_historical_episodes_affected_industries",
    } <= _indexes("historical_episodes")


def test_historical_episode_outcome_check_uses_array_containment_not_subquery() -> None:
    # Postgres rejects a subquery inside a CHECK constraint, so the enum guard on the
    # outcomes[] array has to be expressed as containment.
    ddl = _ddl("historical_episodes")
    assert "ck_historical_episodes_outcomes" in _constraints("historical_episodes")
    assert "SELECT" not in ddl.upper().split("CONSTRAINT CK_HISTORICAL_EPISODES_OUTCOMES")[1][:200]
    assert "outcomes <@ ARRAY[" in ddl
    for outcome in ("contained", "systemic_crisis", "failure", "regime_change"):
        assert f"'{outcome}'" in ddl


# --------------------------------------------------------------------------------------
# LLM landing tables -- specs/llm-contracts-reconciliation.md
# --------------------------------------------------------------------------------------


def test_forecast_scenarios_contract_and_scales() -> None:
    table = _table("forecast_scenarios")
    assert {c.name for c in table.columns} == {
        "id",
        "event_id",
        "llm_run_id",
        "scenario_set_id",
        "scenario_name",
        "probability",
        "risk_score",
        "severity",
        "horizon",
        "narrative",
        "assumptions",
        "triggers",
        "leading_indicators",
        "expected_impact",
        "invalidation_signals",
        "confidence",
        "evidence_refs",
        "created_at",
    }
    # A scenario set is the unit the MECE / sums-to-1.0 validator runs over.
    assert table.c.scenario_set_id.nullable is False
    assert "uq_forecast_scenarios_set_scenario_name" in _constraints("forecast_scenarios")
    assert {
        "ck_forecast_scenarios_probability",
        "ck_forecast_scenarios_risk_score",
        "ck_forecast_scenarios_severity",
        "ck_forecast_scenarios_horizon",
        "ck_forecast_scenarios_confidence",
    } <= _constraints("forecast_scenarios")

    ddl = _ddl("forecast_scenarios")
    # Scores are 0-100; probability and confidence are 0-1.
    assert "CHECK (risk_score >= 0 AND risk_score <= 100)" in ddl
    assert "CHECK (probability >= 0 AND probability <= 1)" in ddl
    assert "CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))" in ddl


def test_event_analogies_contract() -> None:
    table = _table("event_analogies")
    assert {c.name for c in table.columns} == {
        "id",
        "event_id",
        "historical_episode_id",
        "llm_run_id",
        "similarity_score",
        "rationale",
        "limitations",
        "shared_causes",
        "regime_caveats",
        "evidence_refs",
        "created_at",
    }
    # The analogy must point at a real curated episode, not a model-minted id.
    fks = {
        (fk.parent.name, fk.column.table.name)
        for fk in table.foreign_keys
    }
    assert ("historical_episode_id", "historical_episodes") in fks
    assert "uq_event_analogies_event_episode" in _constraints("event_analogies")
    assert "CHECK (similarity_score >= 0 AND similarity_score <= 1)" in _ddl("event_analogies")


def test_risk_warnings_contract() -> None:
    table = _table("risk_warnings")
    assert {c.name for c in table.columns} == {
        "id",
        "alert_id",
        "event_id",
        "llm_run_id",
        "risk_type",
        "risk_score",
        "probability",
        "horizon",
        "severity",
        "warning_message",
        "what_could_reduce_risk",
        "evidence_refs",
        "created_at",
    }
    ddl = _ddl("risk_warnings")
    assert "CHECK (risk_score >= 0 AND risk_score <= 100)" in ddl
    assert "CHECK (probability >= 0 AND probability <= 1)" in ddl
    assert "CHECK (severity IN ('low', 'medium', 'high', 'critical'))" in ddl


def test_existing_llm_destination_tables_gain_numeric_scale_checks() -> None:
    # "Columns are currently unconstrained Numeric" -- reconciliation spec, required DB
    # changes. Scores are 0-100; confidence is 0-1.
    assert "CHECK (severity_score IS NULL OR (severity_score >= 0 AND severity_score <= 100))" in (
        _ddl("events")
    )
    companies = _ddl("event_companies")
    assert "CHECK (impact_score IS NULL OR (impact_score >= 0 AND impact_score <= 100))" in companies
    assert (
        "CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1))"
        in companies
    )
    assert (
        "CHECK (opportunity_score IS NULL OR (opportunity_score >= 0 AND opportunity_score <= 100))"
        in _ddl("event_industries")
    )


# --------------------------------------------------------------------------------------
# llm_runs observability -- ADR 0008
# --------------------------------------------------------------------------------------


def test_llm_runs_records_tokens_params_status_and_trace() -> None:
    table = _table("llm_runs")
    assert {
        # envelope: schema_name / schema_version / prompt_template_version
        "output_schema_name",
        "output_schema_version",
        "prompt_template_version",
        "prompt_hash",
        # params
        "model_params",
        "temperature",
        "seed",
        # status + error
        "status",
        "attempt",
        "error_message",
        "error_details",
        "no_finding_reason",
        # cost / tokens / trace
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "latency_ms",
        "trace_id",
        "started_at",
        "completed_at",
    } <= {c.name for c in table.columns}
    assert table.c.status.nullable is False
    assert "ix_llm_runs_trace_id" in _indexes("llm_runs")

    ddl = _ddl("llm_runs")
    # A validation dead-letter is a first-class terminal status.
    assert "'validation_failed'" in ddl
    assert "CHECK (input_tokens IS NULL OR input_tokens >= 0)" in ddl
    assert "CHECK (cost_usd IS NULL OR cost_usd >= 0)" in ddl


# --------------------------------------------------------------------------------------
# Alert lifecycle, hysteresis, notification -- ADR 0010
# --------------------------------------------------------------------------------------


def test_alerts_carry_the_adr0010_lifecycle_columns() -> None:
    table = _table("alerts")
    assert {
        "state",
        "dedupe_key",
        "evidence_signal_ids",
        "score_version",
        "what_could_reduce_risk",
        "news_driven",
        "updated_at",
        "superseded_by",
        "resolved_at",
    } <= {c.name for c in table.columns}
    # `state` is the one lifecycle column: the overlapping legacy `status` is gone.
    assert "status" not in table.c
    assert table.c.state.nullable is False
    ddl = _ddl("alerts")
    assert (
        "CHECK (state IN ('open', 'escalated', 'downgraded', 'resolved', 'superseded'))" in ddl
    )


def test_alerts_carry_hysteresis_and_notification_state() -> None:
    columns = {c.name for c in _table("alerts").columns}
    # The 2-consecutive-runs velocity rule lives on the row, not in worker memory.
    assert "velocity_streak" in columns
    assert "last_evaluated_at" in columns
    # downgraded -> resolved after 7 days below the clear band; 24h re-fire cooldown.
    assert "clear_band_since" in columns
    assert "cooldown_until" in columns
    # Null-model gate, and the explicit all-clear that resolution must emit.
    assert "experimental" in columns
    assert "notified_at" in columns
    assert "all_clear_notified_at" in columns


def test_alerts_allow_only_one_active_alert_per_dedupe_key() -> None:
    # This is what makes flapping structurally impossible: while a key is live, the same
    # condition updates that row instead of creating a second one.
    ddl = _ddl("alerts")
    assert "uq_alerts_active_dedupe_key" in _indexes("alerts")
    assert "CREATE UNIQUE INDEX uq_alerts_active_dedupe_key ON alerts (dedupe_key)" in ddl
    assert "WHERE dedupe_key IS NOT NULL AND state IN ('open', 'escalated', 'downgraded')" in ddl


def test_alert_terminal_states_require_their_evidence() -> None:
    ddl = _ddl("alerts")
    assert "CHECK (state <> 'resolved' OR resolved_at IS NOT NULL)" in ddl
    assert "CHECK (state <> 'superseded' OR superseded_by IS NOT NULL)" in ddl
    # news-velocity contribution is a 0-1 share of the score.
    assert "CHECK (news_driven IS NULL OR (news_driven >= 0 AND news_driven <= 1))" in ddl


# --------------------------------------------------------------------------------------
# Reports -- specs/report-generation.md
# --------------------------------------------------------------------------------------


def test_reports_lifecycle_and_versioning() -> None:
    table = _table("reports")
    assert {
        "brief_date",
        "event_id",
        "version",
        "change_reason",
        "stale",
        "updated_at",
    } <= {c.name for c in table.columns}
    # The daily brief is global, not owned by a user.
    assert table.c.user_id.nullable is True
    ddl = _ddl("reports")
    # No `draft` limbo.
    assert "CHECK (status IN ('generating', 'grounding_check', 'published', 'failed'))" in ddl
    assert "'draft'" not in ddl
    assert "CHECK (version >= 1)" in ddl


def test_reports_are_unique_per_subject_and_version() -> None:
    # (report_type, brief_date) for daily briefs, (report_type, event_id) for event
    # reports -- with versions within each.
    ddl = _ddl("reports")
    assert {"uq_reports_type_brief_date_version", "uq_reports_type_event_version"} <= _indexes(
        "reports"
    )
    assert (
        "CREATE UNIQUE INDEX uq_reports_type_brief_date_version "
        "ON reports (report_type, brief_date, version) WHERE brief_date IS NOT NULL" in ddl
    )
    assert (
        "CREATE UNIQUE INDEX uq_reports_type_event_version "
        "ON reports (report_type, event_id, version) WHERE event_id IS NOT NULL" in ddl
    )
    # A report is a daily brief or an event report, never both.
    assert "CHECK (brief_date IS NULL OR event_id IS NULL)" in ddl


def test_report_sections_carry_claim_level_citations() -> None:
    table = _table("report_sections")
    assert {c.name for c in table.columns} == {
        "id",
        "report_id",
        "section_order",
        "title",
        "body",
        "blocks",
        "evidence_refs",
        "grounding_status",
    }
    # evidence_refs is a typed list of claim IDs, not free-form JSON: this is what makes
    # the Evidence Drawer resolvable claim-by-claim.
    ddl = _ddl("report_sections")
    assert "evidence_refs UUID[]" in ddl
    # The grounding gate records a verdict per section.
    assert (
        "CHECK (grounding_status IN ('pending', 'passed', 'failed', 'data_quality_note'))" in ddl
    )
