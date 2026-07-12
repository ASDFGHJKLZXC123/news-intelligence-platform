"""Application workspace schema smoke test against a live migrated database."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db.base import engine
from db.models import APPLICATION_TABLES

pytestmark = pytest.mark.integration

_EXPECTED_TABLES = {model.__tablename__ for model in APPLICATION_TABLES}


def test_application_tables_constraints_and_indexes(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing = _EXPECTED_TABLES - present
    assert not missing, f"missing application tables after upgrade: {sorted(missing)}"

    constraints = {
        table: {constraint["name"] for constraint in inspector.get_unique_constraints(table)}
        for table in _EXPECTED_TABLES
    }
    assert "uq_watchlist_items_user_type_item" in constraints["watchlist_items"]
    assert "uq_alert_rules_user_rule_target" in constraints["alert_rules"]
    assert "uq_report_sections_report_order" in constraints["report_sections"]
    assert "uq_saved_searches_user_name" in constraints["saved_searches"]

    indexes = {
        table: {index["name"] for index in inspector.get_indexes(table)}
        for table in _EXPECTED_TABLES
    }
    assert "ix_users_email" in indexes["users"]
    assert "ix_watchlist_items_user_type" in indexes["watchlist_items"]
    assert "ix_alerts_user_status_created" in indexes["alerts"]
    assert "ix_reports_user_status_created" in indexes["reports"]
