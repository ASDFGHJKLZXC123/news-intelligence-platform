"""DB-free checks on the Stage 2 ORM metadata and canonical enums.

These assert the schema shape and the canonical contract without a live database
(the Alembic upgrade/downgrade smoke lives in tests/integration).
"""

from __future__ import annotations

import db.models as m
from db.base import Base
from db.models.enums import Horizon, RiskLevel, RiskType, SourceType, risk_level_for_score

EXPECTED_TABLES = {
    "sources",
    "articles",
    "article_embeddings",
    "events",
    "event_articles",
    "llm_runs",
    "jobs",
    "country_daily_risk_signals",
    "event_risk_features",
    "crisis_predictions",
    "crisis_prediction_evaluations",
}

EXPECTED_PROVIDER_TABLES = {
    "provider_runs",
    "source_cursors",
    "raw_ingestion_items",
    "macro_series",
    "macro_observations",
    "macro_signal_snapshots",
    "sec_companies",
    "sec_filings",
    "sec_company_facts",
}

EXPECTED_PROVIDER_EXPANSION_TABLES = {
    "sanctions_lists",
    "sanctions_entities",
    "sanctions_aliases",
    "sanctions_identifiers",
    "sanctions_matches",
    "entity_profiles",
    "entity_identifiers",
    "entity_relationships",
    "entity_resolution_runs",
    "country_indicator_series",
    "country_indicator_observations",
    "country_context_snapshots",
    "humanitarian_reports",
    "humanitarian_report_entities",
    "geo_incidents",
    "geo_incident_impacts",
    "energy_market_snapshots",
    "energy_disruption_events",
}

EXPECTED_COMPANY_MASTER_TABLES = {
    "companies",
    "company_aliases",
    "company_identifiers",
    "securities",
}

EXPECTED_INGESTION_OBSERVABILITY_TABLES = {
    "source_health_snapshots",
    "raw_document_assets",
}

EXPECTED_EVIDENCE_TABLES = {
    "evidence_items",
    "claims",
    "claim_evidence",
}

EXPECTED_EVENT_INTELLIGENCE_TABLES = {
    "event_embeddings",
    "event_entities",
    "event_companies",
    "event_industries",
    "event_timeline_items",
    "event_locations",
    "geocoding_cache",
}

PROVIDER_PROVENANCE_TABLES = {
    "macro_series",
    "macro_observations",
    "sec_companies",
    "sec_filings",
    "sec_company_facts",
    "sanctions_lists",
    "sanctions_entities",
    "country_indicator_series",
    "country_indicator_observations",
    "humanitarian_reports",
    "geo_incidents",
    "energy_market_snapshots",
    "energy_disruption_events",
}

EXPECTED_RISK_OUTPUT_TABLES = {
    "risk_score_observations",
    "company_risk_rollups",
    "industry_risk_rollups",
    "daily_intelligence_summaries",
}

EXPECTED_APPLICATION_TABLES = {
    "users",
    "watchlist_items",
    "alert_rules",
    "alerts",
    "reports",
    "report_sections",
    "saved_searches",
}


def test_all_stage2_tables_registered() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)
    assert len(m.STAGE2_TABLES) == len(EXPECTED_TABLES)
    assert {t.__tablename__ for t in m.STAGE2_TABLES} == EXPECTED_TABLES


def test_provider_data_tables_registered() -> None:
    assert EXPECTED_PROVIDER_TABLES <= set(Base.metadata.tables)
    assert len(m.PROVIDER_DATA_TABLES) == len(EXPECTED_PROVIDER_TABLES)
    assert {t.__tablename__ for t in m.PROVIDER_DATA_TABLES} == EXPECTED_PROVIDER_TABLES


def test_provider_expansion_tables_registered() -> None:
    assert EXPECTED_PROVIDER_EXPANSION_TABLES <= set(Base.metadata.tables)
    assert len(m.PROVIDER_EXPANSION_TABLES) == len(EXPECTED_PROVIDER_EXPANSION_TABLES)
    assert {
        t.__tablename__ for t in m.PROVIDER_EXPANSION_TABLES
    } == EXPECTED_PROVIDER_EXPANSION_TABLES


def test_company_master_tables_registered() -> None:
    assert EXPECTED_COMPANY_MASTER_TABLES <= set(Base.metadata.tables)
    assert len(m.COMPANY_MASTER_TABLES) == len(EXPECTED_COMPANY_MASTER_TABLES)
    assert {t.__tablename__ for t in m.COMPANY_MASTER_TABLES} == EXPECTED_COMPANY_MASTER_TABLES


def test_ingestion_observability_tables_registered() -> None:
    assert EXPECTED_INGESTION_OBSERVABILITY_TABLES <= set(Base.metadata.tables)
    assert len(m.INGESTION_OBSERVABILITY_TABLES) == len(EXPECTED_INGESTION_OBSERVABILITY_TABLES)
    assert {
        t.__tablename__ for t in m.INGESTION_OBSERVABILITY_TABLES
    } == EXPECTED_INGESTION_OBSERVABILITY_TABLES


def test_evidence_tables_registered() -> None:
    assert EXPECTED_EVIDENCE_TABLES <= set(Base.metadata.tables)
    assert len(m.EVIDENCE_TABLES) == len(EXPECTED_EVIDENCE_TABLES)
    assert {t.__tablename__ for t in m.EVIDENCE_TABLES} == EXPECTED_EVIDENCE_TABLES


def test_event_intelligence_tables_registered() -> None:
    assert EXPECTED_EVENT_INTELLIGENCE_TABLES <= set(Base.metadata.tables)
    assert len(m.EVENT_INTELLIGENCE_TABLES) == len(EXPECTED_EVENT_INTELLIGENCE_TABLES)
    assert {
        t.__tablename__ for t in m.EVENT_INTELLIGENCE_TABLES
    } == EXPECTED_EVENT_INTELLIGENCE_TABLES


def test_risk_output_tables_registered() -> None:
    assert EXPECTED_RISK_OUTPUT_TABLES <= set(Base.metadata.tables)
    assert len(m.RISK_OUTPUT_TABLES) == len(EXPECTED_RISK_OUTPUT_TABLES)
    assert {t.__tablename__ for t in m.RISK_OUTPUT_TABLES} == EXPECTED_RISK_OUTPUT_TABLES


def test_application_tables_registered() -> None:
    assert EXPECTED_APPLICATION_TABLES <= set(Base.metadata.tables)
    assert len(m.APPLICATION_TABLES) == len(EXPECTED_APPLICATION_TABLES)
    assert {t.__tablename__ for t in m.APPLICATION_TABLES} == EXPECTED_APPLICATION_TABLES


def test_provider_domain_tables_have_provenance_columns() -> None:
    for table_name in PROVIDER_PROVENANCE_TABLES:
        table = Base.metadata.tables[table_name]
        assert {"schema_version", "raw_document_asset_id", "retrieved_at"} <= {
            column.name for column in table.columns
        }
        assert f"ix_{table_name}_raw_document_asset_id" in {
            index.name for index in table.indexes
        }
        assert any(
            fk.column.table.name == "raw_document_assets"
            for fk in table.c.raw_document_asset_id.foreign_keys
        )


def test_company_master_columns_match_plan() -> None:
    assert {c.name for c in Base.metadata.tables["companies"].columns} == {
        "id",
        "entity_profile_id",
        "display_name",
        "legal_name",
        "primary_ticker",
        "exchange",
        "country",
        "sector",
        "industry",
        "website",
        "logo_url",
        "logo_source",
        "active",
        "created_at",
        "updated_at",
    }
    assert {c.name for c in Base.metadata.tables["company_aliases"].columns} == {
        "id",
        "company_id",
        "alias",
        "normalized_alias",
        "source",
        "confidence_score",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["company_identifiers"].columns} == {
        "id",
        "company_id",
        "identifier_type",
        "identifier_value",
        "provider",
        "valid_from",
        "valid_to",
        "confidence_score",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["securities"].columns} == {
        "id",
        "company_id",
        "ticker",
        "exchange",
        "mic",
        "currency",
        "asset_type",
        "active",
        "created_at",
        "updated_at",
    }


def test_event_intelligence_core_columns_match_plan() -> None:
    assert {c.name for c in Base.metadata.tables["event_embeddings"].columns} == {
        "event_id",
        "model",
        "model_version",
        "dimension",
        "embedding",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["event_companies"].columns} == {
        "event_id",
        "company_id",
        "impact_direction",
        "impact_score",
        "risk_score",
        "exposure_explanation",
        "confidence_score",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["event_locations"].columns} == {
        "id",
        "event_id",
        "location_name",
        "country_code",
        "region",
        "latitude",
        "longitude",
        "geom",
        "location_type",
        "confidence_score",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["geocoding_cache"].columns} == {
        "id",
        "query",
        "normalized_query",
        "provider",
        "latitude",
        "longitude",
        "geom",
        "country_code",
        "admin1",
        "result_payload",
        "created_at",
    }


def test_evidence_columns_match_plan() -> None:
    assert {c.name for c in Base.metadata.tables["evidence_items"].columns} == {
        "id",
        "source_type",
        "source_id",
        "title",
        "publisher",
        "url",
        "published_at",
        "credibility_score",
        "raw_ref",
        "metadata",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["claims"].columns} == {
        "id",
        "claim_text",
        "claim_type",
        "confidence_score",
        "created_by_run_id",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["claim_evidence"].columns} == {
        "claim_id",
        "evidence_item_id",
        "support_type",
        "confidence_score",
        "created_at",
    }


def test_ingestion_observability_columns_match_plan() -> None:
    assert {c.name for c in Base.metadata.tables["source_health_snapshots"].columns} == {
        "id",
        "source_id",
        "provider",
        "checked_at",
        "status",
        "latency_ms",
        "error_rate",
        "items_fetched",
        "error",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["raw_document_assets"].columns} == {
        "id",
        "provider",
        "asset_type",
        "external_id",
        "storage_url",
        "content_type",
        "content_hash",
        "byte_size",
        "metadata",
        "created_at",
    }


def test_risk_output_columns_match_plan() -> None:
    assert {c.name for c in Base.metadata.tables["risk_score_observations"].columns} == {
        "id",
        "target_type",
        "target_id",
        "risk_type",
        "score",
        "level",
        "confidence_score",
        "as_of",
        "model_version",
        "evidence_refs",
        "driver_refs",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["company_risk_rollups"].columns} == {
        "id",
        "company_id",
        "as_of",
        "impact_score",
        "risk_score",
        "opportunity_score",
        "related_event_count",
        "top_driver",
        "confidence_score",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["industry_risk_rollups"].columns} == {
        "id",
        "industry_id",
        "as_of",
        "impact_score",
        "risk_score",
        "opportunity_score",
        "news_velocity_score",
        "related_event_count",
        "summary",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["daily_intelligence_summaries"].columns} == {
        "id",
        "summary_date",
        "overall_risk_level",
        "confidence_score",
        "summary",
        "key_points",
        "model_rating_prediction_id",
        "generated_by_run_id",
        "created_at",
    }


def test_application_columns_match_plan() -> None:
    assert {c.name for c in Base.metadata.tables["users"].columns} == {
        "id",
        "email",
        "display_name",
        "role",
        "created_at",
        "updated_at",
    }
    assert {c.name for c in Base.metadata.tables["watchlist_items"].columns} == {
        "id",
        "user_id",
        "item_type",
        "item_id",
        "label",
        "metadata",
        "alert_enabled",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["alert_rules"].columns} == {
        "id",
        "user_id",
        "rule_type",
        "target_type",
        "target_id",
        "threshold",
        "enabled",
        "created_at",
        "updated_at",
    }
    # `state` is the single lifecycle column (ADR 0010); the legacy `status` column is gone.
    assert {c.name for c in Base.metadata.tables["alerts"].columns} == {
        "id",
        "user_id",
        "alert_rule_id",
        "title",
        "message",
        "severity",
        "peak_severity",
        "state",
        "risk_score",
        "alert_type",
        "dedupe_key",
        "evidence_signal_ids",
        "score_version",
        "what_could_reduce_risk",
        "news_driven",
        "velocity_streak",
        "last_evaluated_at",
        "clear_band_since",
        "cooldown_until",
        "experimental",
        "notified_at",
        "all_clear_notified_at",
        "updated_at",
        "superseded_by",
        "resolved_at",
        "related_event_id",
        "related_company_id",
        "related_industry_id",
        "evidence_refs",
        "created_at",
    }
    assert {c.name for c in Base.metadata.tables["reports"].columns} == {
        "id",
        "user_id",
        "report_type",
        "brief_date",
        "event_id",
        "title",
        "status",
        "version",
        "change_reason",
        "stale",
        "confidence_score",
        "generated_by_run_id",
        "created_at",
        "updated_at",
    }
    assert {c.name for c in Base.metadata.tables["report_sections"].columns} == {
        "id",
        "report_id",
        "section_order",
        "title",
        "body",
        "blocks",
        "evidence_refs",
        "grounding_status",
    }
    assert {c.name for c in Base.metadata.tables["saved_searches"].columns} == {
        "id",
        "user_id",
        "name",
        "query",
        "filters",
        "created_at",
    }


def test_event_intelligence_indexes_and_primary_keys() -> None:
    event_embeddings = Base.metadata.tables["event_embeddings"]
    assert {"event_id", "model", "model_version"} == {
        column.name for column in event_embeddings.primary_key.columns
    }
    assert "ix_event_embeddings_embedding_hnsw" in {
        index.name for index in event_embeddings.indexes
    }

    event_locations = Base.metadata.tables["event_locations"]
    assert {
        "ix_event_locations_geom_gist",
        "ix_event_locations_country_region",
        "ix_event_locations_event_id",
        "ix_event_locations_country_code",
        "ix_event_locations_region",
    } <= {index.name for index in event_locations.indexes}

    geocoding_cache = Base.metadata.tables["geocoding_cache"]
    assert "uq_geocoding_cache_provider_query" in {
        constraint.name for constraint in geocoding_cache.constraints
    }
    assert {
        "ix_geocoding_cache_normalized_query",
        "ix_geocoding_cache_geom_gist",
    } <= {index.name for index in geocoding_cache.indexes}


def test_evidence_constraints_and_indexes() -> None:
    evidence_items = Base.metadata.tables["evidence_items"]
    assert "uq_evidence_items_source" in {
        constraint.name for constraint in evidence_items.constraints
    }
    assert {
        "ix_evidence_items_source",
        "ix_evidence_items_publisher_published",
        "ix_evidence_items_published_at",
    } <= {index.name for index in evidence_items.indexes}

    claims = Base.metadata.tables["claims"]
    assert {
        "ix_claims_claim_type",
        "ix_claims_created_by_run_id",
    } <= {index.name for index in claims.indexes}

    claim_evidence = Base.metadata.tables["claim_evidence"]
    assert {"claim_id", "evidence_item_id", "support_type"} == {
        column.name for column in claim_evidence.primary_key.columns
    }
    assert {
        "ix_claim_evidence_evidence_item",
        "ix_claim_evidence_support_type",
    } <= {index.name for index in claim_evidence.indexes}


def test_company_master_identity_constraints_and_indexes() -> None:
    expected_constraints = {
        "companies": {
            "uq_companies_entity_profile",
            "uq_companies_primary_ticker_exchange",
        },
        "company_aliases": {"uq_company_aliases_company_alias_source"},
        "company_identifiers": {"uq_company_identifiers_company_type_value_provider"},
        "securities": {"uq_securities_ticker_exchange_mic"},
    }
    for table_name, constraints in expected_constraints.items():
        table = Base.metadata.tables[table_name]
        assert constraints <= {constraint.name for constraint in table.constraints}

    expected_indexes = {
        "companies": {
            "ix_companies_primary_ticker_exchange",
            "ix_companies_country_industry",
            "ix_companies_active",
        },
        "company_aliases": {"ix_company_aliases_normalized_alias"},
        "company_identifiers": {"ix_company_identifiers_type_value"},
        "securities": {
            "ix_securities_company_active",
            "ix_securities_ticker_exchange",
        },
    }
    for table_name, indexes in expected_indexes.items():
        table = Base.metadata.tables[table_name]
        assert indexes <= {index.name for index in table.indexes}


def test_ingestion_observability_constraints_and_indexes() -> None:
    raw_assets = Base.metadata.tables["raw_document_assets"]
    assert "uq_raw_document_assets_provider_type_external_hash" in {
        constraint.name for constraint in raw_assets.constraints
    }
    assert {
        "ix_raw_document_assets_provider_type",
        "ix_raw_document_assets_provider",
        "ix_raw_document_assets_asset_type",
        "ix_raw_document_assets_content_hash",
    } <= {index.name for index in raw_assets.indexes}

    source_health = Base.metadata.tables["source_health_snapshots"]
    assert {
        "ix_source_health_provider_checked",
        "ix_source_health_status_checked",
        "ix_source_health_snapshots_source_id",
        "ix_source_health_snapshots_provider",
        "ix_source_health_snapshots_checked_at",
        "ix_source_health_snapshots_status",
    } <= {index.name for index in source_health.indexes}


def test_risk_output_constraints_and_indexes() -> None:
    expected_constraints = {
        "risk_score_observations": {
            "uq_risk_score_observations_target_type_risk_as_of_model",
        },
        "company_risk_rollups": {"uq_company_risk_rollups_company_as_of"},
        "industry_risk_rollups": {"uq_industry_risk_rollups_industry_as_of"},
    }
    for table_name, constraints in expected_constraints.items():
        table = Base.metadata.tables[table_name]
        assert constraints <= {constraint.name for constraint in table.constraints}

    expected_indexes = {
        "risk_score_observations": {
            "ix_risk_score_observations_target_type",
            "ix_risk_score_observations_risk_type",
            "ix_risk_score_observations_level",
            "ix_risk_score_observations_target_as_of",
            "ix_risk_score_observations_risk_as_of",
        },
        "company_risk_rollups": {
            "ix_company_risk_rollups_company_id",
            "ix_company_risk_rollups_risk_score",
            "ix_company_risk_rollups_company_as_of",
        },
        "industry_risk_rollups": {
            "ix_industry_risk_rollups_risk_score",
            "ix_industry_risk_rollups_industry_as_of",
        },
        "daily_intelligence_summaries": {
            "ix_daily_intelligence_summaries_summary_date",
            "ix_daily_intelligence_summaries_overall_risk_level",
            "ix_daily_intelligence_summaries_model_rating_prediction_id",
            "ix_daily_intelligence_summaries_generated_by_run_id",
        },
    }
    for table_name, indexes in expected_indexes.items():
        table = Base.metadata.tables[table_name]
        assert indexes <= {index.name for index in table.indexes}


def test_application_constraints_and_indexes() -> None:
    expected_constraints = {
        "watchlist_items": {"uq_watchlist_items_user_type_item"},
        "alert_rules": {"uq_alert_rules_user_rule_target"},
        "report_sections": {"uq_report_sections_report_order"},
        "saved_searches": {"uq_saved_searches_user_name"},
    }
    for table_name, constraints in expected_constraints.items():
        table = Base.metadata.tables[table_name]
        assert constraints <= {constraint.name for constraint in table.constraints}

    assert Base.metadata.tables["users"].c.email.unique is True

    expected_indexes = {
        "users": {"ix_users_email"},
        "watchlist_items": {"ix_watchlist_items_user_id", "ix_watchlist_items_user_type"},
        "alert_rules": {
            "ix_alert_rules_user_id",
            "ix_alert_rules_user_enabled",
            "ix_alert_rules_target",
        },
        "alerts": {
            "ix_alerts_user_id",
            "ix_alerts_alert_rule_id",
            "ix_alerts_related_event_id",
            "ix_alerts_related_company_id",
            "ix_alerts_related_industry_id",
            "ix_alerts_state",
            "ix_alerts_user_state_created",
            "ix_alerts_severity_created",
        },
        "reports": {
            "ix_reports_user_id",
            "ix_reports_report_type",
            "ix_reports_status",
            "ix_reports_generated_by_run_id",
            "ix_reports_user_status_created",
        },
        "report_sections": {"ix_report_sections_report_id"},
        "saved_searches": {"ix_saved_searches_user_id", "ix_saved_searches_user_created"},
    }
    for table_name, indexes in expected_indexes.items():
        table = Base.metadata.tables[table_name]
        assert indexes <= {index.name for index in table.indexes}


def test_provider_storage_idempotency_keys_are_unique() -> None:
    assert Base.metadata.tables["provider_runs"].c.run_key.unique is True
    assert Base.metadata.tables["raw_ingestion_items"].c.idempotency_key.unique is True
    assert Base.metadata.tables["sec_companies"].c.cik.unique is True
    assert Base.metadata.tables["sec_filings"].c.accession_number.unique is True


def test_provider_expansion_storage_identity_constraints() -> None:
    expected_constraints = {
        "sanctions_lists": "uq_sanctions_lists_provider_code",
        "sanctions_entities": "uq_sanctions_entities_provider_list_uid",
        "entity_resolution_runs": None,
        "country_indicator_series": "uq_country_indicator_series_provider_indicator",
        "country_indicator_observations": "uq_country_indicator_observations_series_country_date",
        "country_context_snapshots": "uq_country_context_snapshots_country_date",
        "humanitarian_reports": "uq_humanitarian_reports_provider_id",
        "geo_incidents": "uq_geo_incidents_provider_id",
        "energy_market_snapshots": "uq_energy_market_snapshots_provider_region_commodity_date",
        "energy_disruption_events": "uq_energy_disruption_events_provider_id",
    }
    for table_name, constraint_name in expected_constraints.items():
        table = Base.metadata.tables[table_name]
        if constraint_name is None:
            assert table.c.run_key.unique is True
            continue
        assert constraint_name in {constraint.name for constraint in table.constraints}


def test_provider_expansion_metadata_columns_keep_database_name() -> None:
    assert "metadata" in Base.metadata.tables["entity_profiles"].c
    assert "metadata" in Base.metadata.tables["country_indicator_series"].c
    assert "metadata" in Base.metadata.tables["energy_market_snapshots"].c


def test_macro_observations_have_series_date_identity() -> None:
    constraints = {
        constraint.name
        for constraint in Base.metadata.tables["macro_observations"].constraints
    }
    assert "uq_macro_observations_series_date_realtime" in constraints
    assert "ix_macro_observations_series_date" in {
        index.name for index in Base.metadata.tables["macro_observations"].indexes
    }


def test_event_risk_features_matches_canonical_contract() -> None:
    cols = {c.name for c in Base.metadata.tables["event_risk_features"].columns}
    assert cols == {
        "id",
        "event_id",
        "country",
        "region",
        "risk_type",
        "event_type",
        "event_subtype",
        "mechanism",
        "severity_score",
        "novelty_score",
        "velocity_zscore",
        "source_diversity_score",
        "source_authority_score",
        "official_confirmation",
        "rumor_risk_score",
        "market_relevance_score",
        "macro_relevance_score",
        "affected_industries",
        "affected_companies",
        "observed_at",
        "evidence_article_ids",
        "confidence_score",
        "created_at",
    }


def test_idempotency_keys_are_unique() -> None:
    assert Base.metadata.tables["articles"].c.url_hash.unique is True
    assert Base.metadata.tables["jobs"].c.job_key.unique is True


def test_canonical_enum_values() -> None:
    assert {e.value for e in SourceType} == {
        "rss",
        "news_api",
        "official",
        "filing",
        "macro_indicator",
        "market_data",
        "banking_data",
        "historical_case",
    }
    assert {e.value for e in RiskType} == {
        "banking",
        "currency",
        "sovereign",
        "recession",
        "market_liquidity",
        "geopolitical_supply_chain",
        "company",
    }
    assert [e.value for e in Horizon] == ["0_6m", "6_12m", "12_18m", "within_18m"]


def test_risk_level_bands() -> None:
    assert risk_level_for_score(0) is RiskLevel.LOW
    assert risk_level_for_score(30) is RiskLevel.LOW
    assert risk_level_for_score(31) is RiskLevel.MEDIUM
    assert risk_level_for_score(55) is RiskLevel.MEDIUM
    assert risk_level_for_score(56) is RiskLevel.HIGH
    assert risk_level_for_score(75) is RiskLevel.HIGH
    assert risk_level_for_score(76) is RiskLevel.CRITICAL
    assert risk_level_for_score(100) is RiskLevel.CRITICAL


def test_crisis_prediction_required_columns_not_null() -> None:
    table = Base.metadata.tables["crisis_predictions"]
    for col in (
        "target_type",
        "target_id",
        "risk_type",
        "as_of_date",
        "probability_0_6m",
        "probability_6_12m",
        "probability_12_18m",
        "probability_within_18m",
        "risk_score",
        "risk_level",
        "confidence_score",
    ):
        assert table.c[col].nullable is False


def test_crisis_predictions_matches_canonical_contract() -> None:
    cols = {c.name for c in Base.metadata.tables["crisis_predictions"].columns}
    assert cols == {
        "id",
        "target_type",
        "target_id",
        "risk_type",
        "as_of_date",
        "probability_0_6m",
        "probability_6_12m",
        "probability_12_18m",
        "probability_within_18m",
        "risk_score",
        "risk_level",
        "confidence_score",
        "model_versions",
        "top_drivers",
        "historical_analogies",
        "evidence_refs",
        "what_could_escalate",
        "what_could_reduce_risk",
        "created_at",
    }


def test_crisis_prediction_evaluations_matches_canonical_contract() -> None:
    cols = {c.name for c in Base.metadata.tables["crisis_prediction_evaluations"].columns}
    assert cols == {
        "id",
        "prediction_id",
        "evaluation_date",
        "horizon",
        "actual_outcome",
        "actual_start_date",
        "brier_score",
        "log_loss",
        "false_positive",
        "false_negative",
        "lead_time_days",
        "evaluator_notes",
        "created_at",
    }
    table = Base.metadata.tables["crisis_prediction_evaluations"]
    assert table.c.prediction_id.nullable is False
    assert table.c.evaluation_date.nullable is False
    assert table.c.horizon.nullable is False


def test_embedding_has_hnsw_index_and_fixed_dim() -> None:
    table = Base.metadata.tables["article_embeddings"]
    assert "ix_article_embeddings_embedding_hnsw" in {ix.name for ix in table.indexes}
    assert m.EMBEDDING_DIM == 1536
