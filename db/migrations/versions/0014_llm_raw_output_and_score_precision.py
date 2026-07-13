"""llm raw_output, contract score precision, and analogy scale reconciliation

Three changes that finish the LLM contract's persistence surface
(specs/llm-contracts-reconciliation.md):

- ``llm_runs.raw_output``: the provider payload exactly as parsed, kept beside the
  normalized ``output`` so a rescaled score stays auditable. Nullable, no default and no
  backfill -- runs that predate the column genuinely have no raw payload to record, and
  inventing one would forge audit evidence.
- The five persisted contract score destinations become ``NUMERIC(5, 2)``: two decimal
  places is the contract's resolution, and pinning it in the column stops an unconstrained
  ``NUMERIC`` from storing a score the contract cannot express.
- ``event_analogies.similarity_score`` moves from the 0-1 scale to the 0-100 scale shared
  by every other ``*_score``, rescaling the rows already written on the old scale.

Scale reconciliation and data safety
------------------------------------
Revision 0013 constrains ``similarity_score`` to 0-1, so on a database that actually ran
0013 every stored analogy value is on that old scale: it must be multiplied by 100 to mean
the same thing under the 0-100 constraint this revision installs. The multiply is folded
into the ``ALTER ... TYPE ... USING`` expression so it happens in full numeric precision
*before* the cast to ``NUMERIC(5, 2)`` -- scaling after the cast would round 0.855 to 0.86
and then store 86.00 instead of 85.50. Order matters too: the 0-1 CHECK is dropped first,
because the rescaled values violate it.

Convergent by design (see the 0013 docstring)
---------------------------------------------
Migration 0002 creates ``llm_runs`` and 0013 creates ``event_analogies`` from *live* ORM
metadata, so a database built from scratch today already carries ``raw_output``, already
has ``NUMERIC(5, 2)`` scores, and already has the 0-100 analogy CHECK before this revision
runs -- while a database that actually migrated through 0013 has none of them. Every
operation here is therefore guarded on the database's real state, and the rescale is keyed
on the CHECK that 0013 left behind: values are multiplied only where the column is still
proven to be on the 0-1 scale. A fresh database's analogy table is empty at this point
anyway, so the guard costs nothing and removes any chance of a 0-100 value being
misread as 0-1 and multiplied into nonsense.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ANALOGY_CHECK = "ck_event_analogies_similarity_score"

#: The persisted contract score destinations, and the CHECK guarding each. All are 0-100.
_SCORE_COLUMNS = (
    ("event_industries", "impact_score"),
    ("event_companies", "impact_score"),
    ("event_analogies", "similarity_score"),
    ("forecast_scenarios", "risk_score"),
    ("risk_warnings", "risk_score"),
)

#: NUMERIC(5, 2) holds 0.00-999.99: every 0-100 score, at the contract's two decimals.
_PRECISION, _SCALE = 5, 2


# --------------------------------------------------------------------------------------
# State guards
# --------------------------------------------------------------------------------------


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _checks(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {check["name"] for check in inspector.get_check_constraints(table)}


def _analogy_is_on_legacy_scale() -> bool:
    """True when similarity_score still carries 0013's 0-1 CHECK.

    That CHECK is the only trustworthy evidence of which scale the stored values are on.
    """
    for check in sa.inspect(op.get_bind()).get_check_constraints("event_analogies"):
        if check["name"] == _ANALOGY_CHECK:
            return "100" not in check["sqltext"]
    return False


def _retype(table: str, column: str, type_: str, using: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {type_} USING ({using})"))


# --------------------------------------------------------------------------------------
# upgrade
# --------------------------------------------------------------------------------------


def upgrade() -> None:
    if "raw_output" not in _columns("llm_runs"):
        op.add_column("llm_runs", sa.Column("raw_output", JSONB(), nullable=True))

    # The 0-1 CHECK forbids the rescaled values, so it goes before the values change.
    legacy_scale = _analogy_is_on_legacy_scale()
    if _ANALOGY_CHECK in _checks("event_analogies"):
        op.drop_constraint(_ANALOGY_CHECK, "event_analogies", type_="check")

    for table, column in _SCORE_COLUMNS:
        rescale = table == "event_analogies" and legacy_scale
        # Multiply inside the USING expression: full numeric precision first, cast second.
        using = f"{column} * 100" if rescale else column
        _retype(table, column, f"NUMERIC({_PRECISION}, {_SCALE})", using)

    op.create_check_constraint(
        _ANALOGY_CHECK,
        "event_analogies",
        "similarity_score >= 0 AND similarity_score <= 100",
    )


# --------------------------------------------------------------------------------------
# downgrade
# --------------------------------------------------------------------------------------


def downgrade() -> None:
    if _ANALOGY_CHECK in _checks("event_analogies"):
        op.drop_constraint(_ANALOGY_CHECK, "event_analogies", type_="check")

    for table, column in reversed(_SCORE_COLUMNS):
        # Back to an unconstrained NUMERIC. For the analogy score the divide rides along in
        # the USING expression, so 85.50 lands as 0.855 rather than being rounded to 0.86 by
        # the outgoing NUMERIC(5, 2) scale.
        using = f"{column} / 100" if table == "event_analogies" else column
        _retype(table, column, "NUMERIC", using)

    op.create_check_constraint(
        _ANALOGY_CHECK,
        "event_analogies",
        "similarity_score >= 0 AND similarity_score <= 1",
    )

    if "raw_output" in _columns("llm_runs"):
        op.drop_column("llm_runs", "raw_output")
