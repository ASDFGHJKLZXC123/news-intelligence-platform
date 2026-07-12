"""Stage 2 schema smoke test against a live database.

Runs the migrations and asserts the core pipeline + canonical crisis-model tables exist
(with the pgvector HNSW index). Skipped when no PostgreSQL is reachable.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db.base import engine
from db.models import STAGE2_TABLES

pytestmark = pytest.mark.integration

_EXPECTED_TABLES = {model.__tablename__ for model in STAGE2_TABLES}


def test_core_schema_tables_exist(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing = _EXPECTED_TABLES - present
    assert not missing, f"missing tables after upgrade: {sorted(missing)}"

    index_names = {idx["name"] for idx in inspector.get_indexes("article_embeddings")}
    assert "ix_article_embeddings_embedding_hnsw" in index_names
