"""Risk output schema smoke test against a live migrated database."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db.base import engine
from db.models import RISK_OUTPUT_TABLES

pytestmark = pytest.mark.integration

_EXPECTED_TABLES = {model.__tablename__ for model in RISK_OUTPUT_TABLES}


def test_risk_output_tables_constraints_and_indexes(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing = _EXPECTED_TABLES - present
    assert not missing, f"missing risk output tables after upgrade: {sorted(missing)}"

    constraints = {
        table: {constraint["name"] for constraint in inspector.get_unique_constraints(table)}
        for table in _EXPECTED_TABLES
    }
    assert (
        "uq_risk_score_observations_target_type_risk_as_of_model"
        in constraints["risk_score_observations"]
    )
    assert "uq_company_risk_rollups_company_as_of" in constraints["company_risk_rollups"]
    assert "uq_industry_risk_rollups_industry_as_of" in constraints["industry_risk_rollups"]

    indexes = {
        table: {index["name"] for index in inspector.get_indexes(table)}
        for table in _EXPECTED_TABLES
    }
    assert "ix_risk_score_observations_target_as_of" in indexes["risk_score_observations"]
    assert "ix_company_risk_rollups_company_as_of" in indexes["company_risk_rollups"]
    assert "ix_industry_risk_rollups_industry_as_of" in indexes["industry_risk_rollups"]
    assert "ix_daily_intelligence_summaries_summary_date" in indexes[
        "daily_intelligence_summaries"
    ]
