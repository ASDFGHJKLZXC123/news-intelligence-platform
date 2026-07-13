"""Migration 0014 against a live database: score precision, raw_output, analogy rescale.

The DDL-shape assertions live in tests/unit/test_stage1_schema.py. What can only be checked
against a real Postgres is that 0014 *runs*, that it carries analogy rows off the 0-1 scale
0013 gave them and onto the 0-100 scale without distorting them, and that it reverses.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from db.base import engine

pytestmark = pytest.mark.integration

_ALEMBIC = Config("alembic.ini")

_SCORE_COLUMNS = [
    ("event_industries", "impact_score"),
    ("event_companies", "impact_score"),
    ("event_analogies", "similarity_score"),
    ("forecast_scenarios", "risk_score"),
    ("risk_warnings", "risk_score"),
]

_VECTOR = "(SELECT ('[' || string_agg('0.1', ',') || ']')::vector FROM generate_series(1, 1536))"

#: 0.855 is the sentinel on purpose. Rescaling it correctly -- multiply at full precision,
#: *then* cast to NUMERIC(5, 2) -- yields 85.50. Casting first would round it to 0.86 and
#: store 86.00, so this single row is what distinguishes a correct upgrade from a lossy one.
_LEGACY_SIMILARITY = Decimal("0.855")
_RESCALED_SIMILARITY = Decimal("85.50")

_EVENT_ID = "a0000000-0000-0000-0000-0000000000e1"
_EPISODE_ID = "a0000000-0000-0000-0000-0000000000e2"

_LEGACY_ROWS = f"""
INSERT INTO events (id, title) VALUES
 ('{_EVENT_ID}', 'Sentinel event');
INSERT INTO historical_episodes
 (id, name, episode_type, onset_date, onset_summary, onset_embedding) VALUES
 ('{_EPISODE_ID}', 'Sentinel episode', 'banking_stress', '2008-09-15', 'onset', {_VECTOR});
INSERT INTO event_analogies
 (id, event_id, historical_episode_id, similarity_score, rationale) VALUES
 ('a0000000-0000-0000-0000-0000000000a1', '{_EVENT_ID}', '{_EPISODE_ID}',
  {_LEGACY_SIMILARITY}, 'sentinel');
"""


def _numeric_type(conn, table: str, column: str):
    for candidate in inspect(conn).get_columns(table):
        if candidate["name"] == column:
            return candidate["type"]
    raise AssertionError(f"{table}.{column} is missing")


def _analogy_check(conn) -> str:
    return conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'ck_event_analogies_similarity_score'"
        )
    ).scalar_one()


@pytest.fixture
def at_revision_0013(require_postgres: None):
    """A database holding an analogy row on 0013's 0-1 scale, parked one revision below 0014."""
    command.upgrade(_ALEMBIC, "head")
    command.downgrade(_ALEMBIC, "0013")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE events, historical_episodes RESTART IDENTITY CASCADE"))
        conn.execute(text(_LEGACY_ROWS))
    try:
        yield
    finally:
        command.upgrade(_ALEMBIC, "head")
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE events, historical_episodes RESTART IDENTITY CASCADE"))


def test_upgrade_rescales_legacy_analogies_and_pins_score_precision(
    at_revision_0013: None,
) -> None:
    with engine.connect() as conn:
        # Precondition: 0013 really does leave the row on the 0-1 scale.
        assert "<= (1)" in _analogy_check(conn)

    command.upgrade(_ALEMBIC, "0014")

    with engine.connect() as conn:
        # The rescale happens at full precision before the cast: 0.855 -> 85.50, not 86.00.
        stored = conn.execute(text("SELECT similarity_score FROM event_analogies")).scalar_one()
        assert stored == _RESCALED_SIMILARITY

        # ...and it now means the same thing every other *_score does.
        assert "<= (100)" in _analogy_check(conn)

        for table, column in _SCORE_COLUMNS:
            type_ = _numeric_type(conn, table, column)
            assert (type_.precision, type_.scale) == (5, 2), f"{table}.{column}"

        raw_output = _numeric_type(conn, "llm_runs", "raw_output")
        assert raw_output.__class__.__name__ == "JSONB"


def test_upgrade_leaves_no_analogy_stranded_above_its_new_ceiling(
    at_revision_0013: None,
) -> None:
    # The top of the old scale is the top of the new one; it must not overflow NUMERIC(5, 2)
    # or trip the 0-100 CHECK the same statement installs.
    with engine.begin() as conn:
        conn.execute(text("UPDATE event_analogies SET similarity_score = 1"))

    command.upgrade(_ALEMBIC, "0014")

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT similarity_score FROM event_analogies")
        ).scalar_one() == Decimal("100.00")


def test_downgrade_restores_the_legacy_scale_types_and_drops_raw_output(
    at_revision_0013: None,
) -> None:
    command.upgrade(_ALEMBIC, "0014")
    command.downgrade(_ALEMBIC, "0013")

    with engine.connect() as conn:
        # The divide rides along in the USING expression, so the row lands back on its exact
        # original value rather than being rounded by the outgoing NUMERIC(5, 2) scale.
        stored = conn.execute(text("SELECT similarity_score FROM event_analogies")).scalar_one()
        assert stored == _LEGACY_SIMILARITY
        assert "<= (1)" in _analogy_check(conn)

        # Precision alterations reverse to an unconstrained NUMERIC.
        for table, column in _SCORE_COLUMNS:
            type_ = _numeric_type(conn, table, column)
            assert (type_.precision, type_.scale) == (None, None), f"{table}.{column}"

        assert "raw_output" not in {c["name"] for c in inspect(conn).get_columns("llm_runs")}
