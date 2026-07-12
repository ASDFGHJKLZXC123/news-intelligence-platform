"""pgvector smoke test: enable the extension, insert a vector, and query it.

Marked ``integration`` and skipped automatically when no PostgreSQL is reachable, so
the default unit run stays network-free.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from db.base import engine

pytestmark = pytest.mark.integration


def test_pgvector_insert_and_query(require_postgres: None) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("DROP TABLE IF EXISTS _vector_smoke"))
        conn.execute(text("CREATE TABLE _vector_smoke (id int PRIMARY KEY, embedding vector(3))"))
        conn.execute(text("INSERT INTO _vector_smoke (id, embedding) VALUES (1, '[1,2,3]')"))
        # Nearest-neighbour query using the L2 distance operator.
        row = conn.execute(
            text("SELECT id FROM _vector_smoke ORDER BY embedding <-> '[1,2,3]' LIMIT 1")
        ).fetchone()
        assert row is not None and row[0] == 1
        conn.execute(text("DROP TABLE _vector_smoke"))
