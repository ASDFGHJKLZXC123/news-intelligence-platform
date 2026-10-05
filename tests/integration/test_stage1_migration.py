"""Migration 0013 against a live database: data safety and reversibility.

The DDL-shape assertions live in tests/unit/test_stage1_schema.py. What can only be
checked against a real Postgres is that 0013 *runs*, that it carries pre-existing rows
into the new shape without losing them, and that it reverses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from tests.integration._stage6_db import disposable_database, require_disposable_postgres, url_for

pytestmark = pytest.mark.integration


def _migrate(engine: Engine, action: str, revision: str) -> None:
    """Use explicit subprocess settings; never migrate the shared runtime engine."""
    assert engine.url.database and engine.url.database.startswith("nip_stage1_migration_")
    environment = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": engine.url.render_as_string(hide_password=False),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", action, revision],
        env=environment,
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]


_VECTOR = "(SELECT ('[' || string_agg('0.1', ',') || ']')::vector FROM generate_series(1, 1536))"

# Rows in the pre-Stage-1 shape: a NULL embedding model_version, a resolved alert with no
# resolved_at, an 'acknowledged' alert, a 'draft' report, and a section whose evidence_refs
# is a JSONB array mixing a real claim id with junk.
_LEGACY_ROWS = f"""
INSERT INTO sources (id, name, feed_url, source_type, active) VALUES
 ('11111111-1111-1111-1111-111111111111', 'Legacy', 'https://x.test/f.xml', 'rss', true);
INSERT INTO articles (id, source_id, url, url_hash, title, content_hash) VALUES
 ('22222222-2222-2222-2222-222222222222', '11111111-1111-1111-1111-111111111111',
  'https://x.test/a', 'h1', 'Legacy article', 'c1');
INSERT INTO article_embeddings (article_id, model, model_version, dimension, embedding) VALUES
 ('22222222-2222-2222-2222-222222222222', 'text-embedding-3-small', NULL, 1536, {_VECTOR});
INSERT INTO users (id, email) VALUES
 ('33333333-3333-3333-3333-333333333333', 'legacy@test.example');
INSERT INTO alerts (id, user_id, title, message, severity, alert_type, status) VALUES
 ('44444444-4444-4444-4444-444444444444', '33333333-3333-3333-3333-333333333333',
  'Old resolved', 'm', 'high', 'risk_threshold', 'resolved'),
 ('55555555-5555-5555-5555-555555555555', '33333333-3333-3333-3333-333333333333',
  'Old acknowledged', 'm', 'critical', 'risk_threshold', 'acknowledged');
INSERT INTO reports (id, user_id, report_type, title, status) VALUES
 ('66666666-6666-6666-6666-666666666666', '33333333-3333-3333-3333-333333333333',
  'daily_brief', 'Legacy brief', 'draft');
INSERT INTO report_sections (id, report_id, section_order, title, body, evidence_refs) VALUES
 ('77777777-7777-7777-7777-777777777777', '66666666-6666-6666-6666-666666666666', 1,
  'Overview', 'body', '["88888888-8888-8888-8888-888888888888", "not-a-uuid"]'::jsonb);
"""


@pytest.fixture
def at_revision_0012():
    """A database holding pre-Stage-1 rows, parked one revision below 0013."""
    require_disposable_postgres()
    with disposable_database("nip_stage1_migration_") as database:
        engine = create_engine(url_for(database))
        try:
            # Bootstrap imports present-day ORM definitions. The historic 0013
            # downgrade recreates the actual 0012 shape (including nullable embedding
            # versions) before legacy rows are inserted. This disposable never
            # installs or downgrades a Phase 3 retention migration.
            _migrate(engine, "upgrade", "0013")
            _migrate(engine, "downgrade", "0012")
            with engine.begin() as conn:
                conn.execute(text(_LEGACY_ROWS))
            yield engine
        finally:
            engine.dispose()


def test_upgrade_carries_legacy_rows_into_the_stage1_shape(at_revision_0012: Engine) -> None:
    engine = at_revision_0012
    _migrate(engine, "upgrade", "0013")

    with engine.connect() as conn:
        # The embedding survives and is stamped with the deployed identity, so widening the
        # primary key to (article_id, model, model_version) orphans no vector.
        embeddings = conn.execute(text("SELECT model, model_version FROM article_embeddings")).all()
        assert embeddings == [("text-embedding-3-small", "current")]

        alerts = dict(
            conn.execute(text("SELECT title, state FROM alerts")).all()  # type: ignore[arg-type]
        )
        assert alerts["Old resolved"] == "resolved"
        # `acknowledged` was a UI concept, never a lifecycle state: the alert is still open.
        assert alerts["Old acknowledged"] == "open"
        # A resolved alert must carry the timestamp its CHECK constraint demands.
        resolved_at = conn.execute(
            text("SELECT resolved_at FROM alerts WHERE title = 'Old resolved'")
        ).scalar_one()
        assert resolved_at is not None
        assert "status" not in {c["name"] for c in inspect(conn).get_columns("alerts")}

        # No `draft` limbo in the report lifecycle.
        assert conn.execute(text("SELECT status FROM reports")).scalar_one() == "generating"

        # evidence_refs is now a typed claim-id list: the real claim survives, junk is dropped.
        claim_ids = conn.execute(text("SELECT evidence_refs FROM report_sections")).scalar_one()
        assert [str(value) for value in claim_ids] == ["88888888-8888-8888-8888-888888888888"]


def test_downgrade_reverses_stage1_and_keeps_one_embedding_per_article(
    at_revision_0012: Engine,
) -> None:
    engine = at_revision_0012
    _migrate(engine, "upgrade", "0013")

    with engine.begin() as conn:
        # A second embedding for the same article -- only legal under the composite key.
        conn.execute(
            text(
                "INSERT INTO article_embeddings (article_id, model, model_version, dimension,"
                " embedding) SELECT '22222222-2222-2222-2222-222222222222',"
                f" 'text-embedding-3-large', 'v2', 1536, {_VECTOR}"
            )
        )
        assert conn.execute(text("SELECT count(*) FROM article_embeddings")).scalar_one() == 2

    _migrate(engine, "downgrade", "0012")

    with engine.connect() as conn:
        columns = {c["name"] for c in inspect(conn).get_columns("alerts")}
        assert "status" in columns
        assert "state" not in columns

        stage1 = {
            "entity_aliases",
            "entity_redirects",
            "historical_episodes",
            "forecast_scenarios",
            "event_analogies",
            "risk_warnings",
        }
        assert not stage1 & set(inspect(conn).get_table_names())

        # Narrowing the key back to article_id keeps exactly one row -- the newest.
        rows = conn.execute(text("SELECT model FROM article_embeddings")).all()
        assert rows == [("text-embedding-3-large",)]
