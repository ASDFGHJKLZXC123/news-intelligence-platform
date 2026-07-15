"""DB-free checks for the Stage 6 schema contract: `events.hotness_score` (migration 0016).

The DDL shape lives here; that migration 0016 actually *runs* and preserves rows is checked
against a live database in tests/integration/test_stage6_brief_selection.py.
"""

from __future__ import annotations

from sqlalchemy import Numeric
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from db.base import Base
from db.models import Event

_EVENTS = Base.metadata.tables["events"]


def _ddl() -> str:
    dialect = postgresql.dialect()
    statements = [str(CreateTable(_EVENTS).compile(dialect=dialect))]
    statements += [str(CreateIndex(index).compile(dialect=dialect)) for index in _EVENTS.indexes]
    return "\n".join(statements)


def test_hotness_score_column_exists_and_is_nullable() -> None:
    column = _EVENTS.columns["hotness_score"]
    # Nullable and never backfilled: events clustered before 0016 were never scored, and the
    # inputs that would reconstruct their hotness are not recoverable (see the migration).
    assert column.nullable is True
    assert column.server_default is None


def test_hotness_score_is_a_two_decimal_contract_score() -> None:
    # NUMERIC(5, 2), like every other persisted contract score since migration 0014, and
    # `asdecimal=False` so the score stays a plain float for the ranking arithmetic.
    column_type = _EVENTS.columns["hotness_score"].type
    assert isinstance(column_type, Numeric)
    assert (column_type.precision, column_type.scale) == (5, 2)
    assert column_type.asdecimal is False
    assert "NUMERIC(5, 2)" in _ddl()


def test_hotness_score_is_bounded_0_100_by_a_check_constraint() -> None:
    constraints = {c.name for c in _EVENTS.constraints if c.name}
    assert "ck_events_hotness_score" in constraints
    ddl = _ddl()
    assert "hotness_score >= 0 AND hotness_score <= 100" in ddl


def test_severity_score_survives_untouched_beside_it() -> None:
    # Hotness is an addition, not a rename: Stage 6 must not have quietly moved severity.
    assert "severity_score" in _EVENTS.columns
    assert {c.name for c in _EVENTS.constraints if c.name} >= {
        "ck_events_severity_score",
        "ck_events_hotness_score",
    }
    assert _EVENTS.columns["severity_score"].nullable is True


def test_the_brief_window_query_has_an_index_to_serve_it() -> None:
    # The brief's one event query ranges on `updated_at` (ADR 0009's predicate) and then
    # applies the hotness floor: leading range column, filter column second.
    indexes = {index.name: [c.name for c in index.columns] for index in _EVENTS.indexes}
    assert indexes.get("ix_events_updated_at_hotness") == ["updated_at", "hotness_score"]


def test_the_orm_model_exposes_hotness_score() -> None:
    assert Event.hotness_score is not None
    assert Event.__table__.columns["hotness_score"] is _EVENTS.columns["hotness_score"]
