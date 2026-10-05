"""Historical rollback and Phase 3 retention on separately owned databases.

The shared test/application database is never migrated or rolled back by this module.
Historical reversible migrations are exercised only up to 0019; Phase 3's retained
records have their own explicit downgrade-refusal checks.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

from tests.integration._stage6_db import (
    disposable_database,
    require_disposable_postgres,
    url_for,
)

pytestmark = pytest.mark.integration


def _run_owned_alembic(database_url: str, action: str, revision: str) -> None:
    """Run against explicit disposable coordinates without mutating cached Settings.

    Migration env.py intentionally reads Settings and overrides Config's URL. A fresh
    subprocess receives the owned URL, so failures cannot leak a temporary environment
    or Settings-cache change into later tests in the parent process.
    """
    database = make_url(database_url).database
    assert database is not None and database.startswith(
        ("nip_alembic_smoke_", "nip_embedding_migration_")
    ), "migration smoke commands require an owned disposable database"
    assert action in {"upgrade", "downgrade"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", action, revision],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "APP_ENV": "test", "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(
            f"owned Alembic {action} {revision} failed\n{result.stdout[-2000:]}\n{result.stderr[-6000:]}"
        )


@pytest.fixture
def owned_migration_database() -> Iterator[tuple[Engine, str]]:
    require_disposable_postgres()
    with disposable_database("nip_alembic_smoke_") as database:
        database_url = url_for(database)
        engine = create_engine(database_url)
        try:
            yield engine, database_url
        finally:
            engine.dispose()


def _extension_installed(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = :name"),
                {"name": name},
            ).fetchone()
            is not None
        )


def test_upgrade_then_downgrade(owned_migration_database: tuple[Engine, str]) -> None:
    engine, database_url = owned_migration_database
    _run_owned_alembic(database_url, "upgrade", "0019")
    assert _extension_installed(engine, "vector"), "pgvector should exist after legacy upgrade"
    assert _extension_installed(engine, "postgis"), "PostGIS should exist after legacy upgrade"

    _run_owned_alembic(database_url, "downgrade", "base")
    assert not _extension_installed(engine, "vector"), "pgvector should be removed at base"
    assert not _extension_installed(engine, "postgis"), "PostGIS should be removed at base"

    # Prove that the reversible historical schema can bootstrap again. This disposable
    # never reaches Phase 3, and no shared database needs restoration after this test.
    _run_owned_alembic(database_url, "upgrade", "0019")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0019"


@pytest.mark.parametrize(
    "revision,previous,retained_table,message",
    [
        (
            "0021_personal_daily_bounds",
            "0020",
            "personal_feed_receipts",
            "Personal daily-use records are retained",
        ),
        (
            "0022_personal_spending",
            "0021_personal_daily_bounds",
            "personal_paid_requests",
            "Paid usage and unresolved reservations must be retained",
        ),
    ],
)
def test_phase3_downgrade_refuses_to_remove_retained_state(
    owned_migration_database: tuple[Engine, str],
    revision: str,
    previous: str,
    retained_table: str,
    message: str,
) -> None:
    engine, database_url = owned_migration_database
    _run_owned_alembic(database_url, "upgrade", revision)
    workspace_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO personal_workspaces (id, singleton, timezone, setup_provenance) "
                "VALUES (:id, true, 'America/Los_Angeles', jsonb_build_object('retained', true))"
            ),
            {"id": workspace_id},
        )
        if revision == "0022_personal_spending":
            connection.execute(
                text(
                    "INSERT INTO personal_spending_state (workspace_id, legacy_cutover_at) "
                    "VALUES (:id, CURRENT_TIMESTAMP)"
                ),
                {"id": workspace_id},
            )
        tables_before = set(inspect(connection).get_table_names())

    with pytest.raises(RuntimeError, match=message):
        _run_owned_alembic(database_url, "downgrade", previous)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == revision
        assert retained_table in tables_before == set(inspect(connection).get_table_names())
        assert connection.scalar(
            text("SELECT setup_provenance FROM personal_workspaces WHERE id=:id"),
            {"id": workspace_id},
        ) == {"retained": True}
        if revision == "0022_personal_spending":
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM personal_spending_state WHERE workspace_id=:id"),
                    {"id": workspace_id},
                )
                == 1
            )
