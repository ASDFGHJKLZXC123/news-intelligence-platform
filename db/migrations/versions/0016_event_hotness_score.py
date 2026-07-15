"""events.hotness_score

The daily brief ranks its Top Events by ``0.6 x hotness_score + 0.4 x max_linked_risk_score``
and qualifies nothing below a hotness of 40 (report-generation spec, "Content selection"),
but ``events`` carries only ``severity_score``. The two are not the same measurement:
severity reads volume and source diversity and answers how *big* an event is; hotness also
reads how fast the coverage arrived and how authoritative the outlets carrying it are, and
answers how *newsworthy* it is right now. Ranking the brief on severity would be a silent
substitution of one for the other -- a slow story of great size would outrank the story
actually breaking this morning -- so hotness becomes a column in its own right, scored by
``services.nlp.features.event_hotness_score`` as clusters are formed.

Nullable, and deliberately not backfilled
-----------------------------------------
Hotness is a function of the *coverage timing* that produced a cluster. The events written
before this revision were never scored on it, and the inputs that would reconstruct it are
not recoverable for them: an event's per-article publication spread is what velocity is
computed from, and articles can be re-clustered, re-fetched, or absent. Deriving a number
from what happens to survive would be inventing the measurement rather than recovering it,
and an invented 40 is the difference between an event appearing in the brief and not. So
historical rows stay NULL, and the selection code excludes a NULL hotness outright rather
than guessing -- which is exactly the spec's rule read literally ("events below hotness 40
never qualify"), applied to an event whose hotness is not merely low but unknown.

Convergent by design (see the 0013/0014 docstrings)
---------------------------------------------------
Migration 0002 creates ``events`` from *live* ORM metadata, so a database built from scratch
today already has ``hotness_score``, its CHECK, and its index before this revision runs,
while a database that actually migrated through 0015 has none of them. Every operation is
therefore guarded on the database's real state.

The index
---------
``events`` carries no index at all today, and the brief's one event query filters a range on
``updated_at`` (ADR 0009's window predicate) and then the hotness floor. ``(updated_at,
hotness_score)`` serves exactly that: leading range column, filter column second.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "events"
_COLUMN = "hotness_score"
_CHECK = "ck_events_hotness_score"
_INDEX = "ix_events_updated_at_hotness"

#: NUMERIC(5, 2), matching every other persisted contract score since migration 0014.
_PRECISION, _SCALE = 5, 2


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _checks() -> set[str]:
    return {check["name"] for check in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)}


def _indexes() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    if _COLUMN not in _columns():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Numeric(_PRECISION, _SCALE), nullable=True))
    if _CHECK not in _checks():
        op.create_check_constraint(
            _CHECK,
            _TABLE,
            f"{_COLUMN} IS NULL OR ({_COLUMN} >= 0 AND {_COLUMN} <= 100)",
        )
    if _INDEX not in _indexes():
        op.create_index(_INDEX, _TABLE, ["updated_at", _COLUMN])


def downgrade() -> None:
    if _INDEX in _indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    if _CHECK in _checks():
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    if _COLUMN in _columns():
        op.drop_column(_TABLE, _COLUMN)
