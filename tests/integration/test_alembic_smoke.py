"""Alembic upgrade/downgrade smoke test against a live database.

Proves a fresh database can bootstrap from migrations (creating the pgvector
extension) and roll back cleanly. Skipped when no PostgreSQL is reachable.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from db.base import engine

pytestmark = pytest.mark.integration


def _alembic_config() -> Config:
    cfg = Config("alembic.ini")
    # env.py sets the URL from Settings; this keeps the smoke test self-contained.
    return cfg


def _extension_installed(name: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = :name"),
                {"name": name},
            ).fetchone()
            is not None
        )


def test_upgrade_then_downgrade(require_postgres: None) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    assert _extension_installed("vector"), "pgvector extension should exist after upgrade"
    assert _extension_installed("postgis"), "PostGIS extension should exist after upgrade"

    command.downgrade(cfg, "base")
    assert not _extension_installed("vector"), "pgvector extension should be removed after downgrade"
    assert not _extension_installed("postgis"), "PostGIS extension should be removed after downgrade"

    # Restore head so the database is left in the expected bootstrapped state.
    command.upgrade(cfg, "head")
