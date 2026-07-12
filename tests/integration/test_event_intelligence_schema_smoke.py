"""Event intelligence schema smoke test against a live migrated database."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db.base import engine
from db.models import EVENT_INTELLIGENCE_TABLES

pytestmark = pytest.mark.integration

_EXPECTED_TABLES = {model.__tablename__ for model in EVENT_INTELLIGENCE_TABLES}


def test_event_intelligence_tables_indexes_and_fks(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing = _EXPECTED_TABLES - present
    assert not missing, f"missing event intelligence tables after upgrade: {sorted(missing)}"

    event_embedding_indexes = {
        index["name"] for index in inspector.get_indexes("event_embeddings")
    }
    assert "ix_event_embeddings_embedding_hnsw" in event_embedding_indexes

    event_location_indexes = {
        index["name"] for index in inspector.get_indexes("event_locations")
    }
    assert "ix_event_locations_geom_gist" in event_location_indexes
    assert "ix_event_locations_country_region" in event_location_indexes

    geocoding_indexes = {index["name"] for index in inspector.get_indexes("geocoding_cache")}
    assert "ix_geocoding_cache_geom_gist" in geocoding_indexes
    assert "ix_geocoding_cache_normalized_query" in geocoding_indexes

    geocoding_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("geocoding_cache")
    }
    assert "uq_geocoding_cache_provider_query" in geocoding_constraints

    event_company_fks = inspector.get_foreign_keys("event_companies")
    referred = {fk["referred_table"] for fk in event_company_fks}
    assert {"events", "companies"} <= referred

    event_entity_fks = inspector.get_foreign_keys("event_entities")
    referred = {fk["referred_table"] for fk in event_entity_fks}
    assert {"events", "entity_profiles"} <= referred
