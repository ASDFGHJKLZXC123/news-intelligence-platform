"""Company master schema smoke test against a live migrated database."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db.base import engine
from db.models import COMPANY_MASTER_TABLES

pytestmark = pytest.mark.integration

_EXPECTED_TABLES = {model.__tablename__ for model in COMPANY_MASTER_TABLES}


def test_company_master_schema_tables_constraints_and_indexes(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing = _EXPECTED_TABLES - present
    assert not missing, f"missing company master tables after upgrade: {sorted(missing)}"

    constraints = {
        table: {constraint["name"] for constraint in inspector.get_unique_constraints(table)}
        for table in _EXPECTED_TABLES
    }
    assert "uq_companies_entity_profile" in constraints["companies"]
    assert "uq_companies_primary_ticker_exchange" in constraints["companies"]
    assert "uq_company_aliases_company_alias_source" in constraints["company_aliases"]
    assert (
        "uq_company_identifiers_company_type_value_provider"
        in constraints["company_identifiers"]
    )
    assert "uq_securities_ticker_exchange_mic" in constraints["securities"]

    indexes = {
        table: {index["name"] for index in inspector.get_indexes(table)}
        for table in _EXPECTED_TABLES
    }
    assert "ix_companies_primary_ticker_exchange" in indexes["companies"]
    assert "ix_company_aliases_normalized_alias" in indexes["company_aliases"]
    assert "ix_company_identifiers_type_value" in indexes["company_identifiers"]
    assert "ix_securities_company_active" in indexes["securities"]

    company_fks = inspector.get_foreign_keys("companies")
    assert any(
        fk["referred_table"] == "entity_profiles"
        and fk["constrained_columns"] == ["entity_profile_id"]
        for fk in company_fks
    )
