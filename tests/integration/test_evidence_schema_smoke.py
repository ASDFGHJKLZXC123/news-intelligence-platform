"""Evidence schema smoke test against a live migrated database."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db.base import engine
from db.models import EVIDENCE_TABLES

pytestmark = pytest.mark.integration

_EXPECTED_TABLES = {model.__tablename__ for model in EVIDENCE_TABLES}


def test_evidence_tables_constraints_indexes_and_fks(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing = _EXPECTED_TABLES - present
    assert not missing, f"missing evidence tables after upgrade: {sorted(missing)}"

    evidence_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("evidence_items")
    }
    assert "uq_evidence_items_source" in evidence_constraints

    evidence_indexes = {index["name"] for index in inspector.get_indexes("evidence_items")}
    assert "ix_evidence_items_source" in evidence_indexes
    assert "ix_evidence_items_publisher_published" in evidence_indexes

    claim_indexes = {index["name"] for index in inspector.get_indexes("claims")}
    assert "ix_claims_created_by_run_id" in claim_indexes

    claim_fks = inspector.get_foreign_keys("claims")
    assert any(
        fk["referred_table"] == "llm_runs"
        and fk["constrained_columns"] == ["created_by_run_id"]
        for fk in claim_fks
    )

    claim_evidence_fks = inspector.get_foreign_keys("claim_evidence")
    referred = {fk["referred_table"] for fk in claim_evidence_fks}
    assert {"claims", "evidence_items"} <= referred
