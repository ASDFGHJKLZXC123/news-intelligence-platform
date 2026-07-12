"""Provider provenance schema smoke test against a live migrated database."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db.base import engine

pytestmark = pytest.mark.integration

_PROVENANCE_TABLES = {
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


def test_provider_tables_have_provenance_columns_and_raw_asset_fk(
    require_postgres: None,
) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    inspector = inspect(engine)
    for table_name in _PROVENANCE_TABLES:
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert {"schema_version", "raw_document_asset_id", "retrieved_at"} <= columns

        indexes = {index["name"] for index in inspector.get_indexes(table_name)}
        assert f"ix_{table_name}_raw_document_asset_id" in indexes

        fks = inspector.get_foreign_keys(table_name)
        assert any(
            fk["referred_table"] == "raw_document_assets"
            and fk["constrained_columns"] == ["raw_document_asset_id"]
            for fk in fks
        )
