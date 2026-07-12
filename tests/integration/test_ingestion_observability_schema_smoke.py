"""Ingestion observability schema smoke test against a live migrated database."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db.base import engine
from db.models import INGESTION_OBSERVABILITY_TABLES

pytestmark = pytest.mark.integration

_EXPECTED_TABLES = {model.__tablename__ for model in INGESTION_OBSERVABILITY_TABLES}


def test_ingestion_observability_tables_constraints_and_indexes(
    require_postgres: None,
) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing = _EXPECTED_TABLES - present
    assert not missing, f"missing ingestion observability tables after upgrade: {sorted(missing)}"

    raw_asset_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("raw_document_assets")
    }
    assert "uq_raw_document_assets_provider_type_external_hash" in raw_asset_constraints

    source_health_indexes = {
        index["name"] for index in inspector.get_indexes("source_health_snapshots")
    }
    assert "ix_source_health_provider_checked" in source_health_indexes
    assert "ix_source_health_status_checked" in source_health_indexes

    raw_asset_indexes = {index["name"] for index in inspector.get_indexes("raw_document_assets")}
    assert "ix_raw_document_assets_provider_type" in raw_asset_indexes
    assert "ix_raw_document_assets_content_hash" in raw_asset_indexes

    source_health_fks = inspector.get_foreign_keys("source_health_snapshots")
    assert any(
        fk["referred_table"] == "sources"
        and fk["constrained_columns"] == ["source_id"]
        for fk in source_health_fks
    )
