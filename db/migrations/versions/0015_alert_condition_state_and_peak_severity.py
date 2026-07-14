"""alert condition state and alert peak severity

The two pieces of ADR 0010's alert lifecycle that the Stage 1 schema cannot express. Both
are additive: no existing column, constraint, or index changes meaning, so every alerts
consumer written before this revision keeps working unchanged.

``alert_condition_states``
--------------------------
A velocity condition (``z > 2.5``) may not open an alert until it has held for two
consecutive runs, and ADR 0010 requires the streak to survive a worker restart. The first
qualifying run therefore needs a durable home, and no ``alerts`` row can be it: the three
active states (``open``/``escalated``/``downgraded``) are precisely the states in which an
alert is *live* -- the alerts API serves them, the platform budget counts them -- so a
not-yet-fired condition parked in one is a premature alert; and the two terminal states are
false statements about the lifecycle (``resolved`` claims an all-clear and arms the 24h
cooldown, ``superseded`` demands a successor). This table is that home and nothing more:
one row per ``dedupe_key``, invisible to every alerts query, deleted the moment the
condition opens an alert -- after which ``alerts.velocity_streak`` owns the streak, exactly
as the ADR specifies.

``alerts.peak_severity``
------------------------
``severity`` follows the score, and an alert can only resolve once it has decayed to Low.
A resolved row therefore always reads ``severity = 'low'``, which makes ADR 0010's cooldown
rule -- "a resolved key cannot re-fire for 24h unless the new severity is higher" --
vacuous: an alert enters at Medium or above, which is higher than Low every single time, so
nothing would ever be suppressed. ``peak_severity`` records the strongest severity the alert
ever held, giving the cooldown a comparison with meaning. Nullable, and not backfilled: the
alerts that predate this column never recorded their peak, and inventing one would be a
guess. The service treats a NULL peak as the row's ``severity``.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from db.models.core import RISK_LEVELS

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONDITION_STATES = "alert_condition_states"
_PEAK_CHECK = "ck_alerts_peak_severity"
_STREAK_CHECK = "ck_alert_condition_states_velocity_streak_non_negative"
_DEDUPE_UNIQUE = "uq_alert_condition_states_dedupe_key"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _checks(table: str) -> set[str]:
    return {check["name"] for check in sa.inspect(op.get_bind()).get_check_constraints(table)}


def upgrade() -> None:
    if not _has_table(_CONDITION_STATES):
        op.create_table(
            _CONDITION_STATES,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("velocity_streak", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("dedupe_key", name=_DEDUPE_UNIQUE),
            sa.CheckConstraint("velocity_streak >= 0", name=_STREAK_CHECK),
        )

    if "peak_severity" not in _columns("alerts"):
        op.add_column("alerts", sa.Column("peak_severity", sa.String(length=32), nullable=True))
    if _PEAK_CHECK not in _checks("alerts"):
        levels = ", ".join(f"'{level}'" for level in RISK_LEVELS)
        op.create_check_constraint(
            _PEAK_CHECK, "alerts", f"peak_severity IS NULL OR peak_severity IN ({levels})"
        )


def downgrade() -> None:
    if _PEAK_CHECK in _checks("alerts"):
        op.drop_constraint(_PEAK_CHECK, "alerts", type_="check")
    if "peak_severity" in _columns("alerts"):
        op.drop_column("alerts", "peak_severity")
    if _has_table(_CONDITION_STATES):
        op.drop_table(_CONDITION_STATES)
