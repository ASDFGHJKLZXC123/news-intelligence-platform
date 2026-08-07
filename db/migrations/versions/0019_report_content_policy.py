"""Add a durable Gate-G content policy marker to reports.

Legacy reports remain NULL and therefore fail closed. New daily-brief generations stamp
whether their full composition path allowed prediction-backed inputs.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "reports"
_CONSTRAINT = "ck_reports_content_policy"


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _checks() -> set[str]:
    return {check["name"] for check in sa.inspect(op.get_bind()).get_check_constraints(_TABLE)}


def upgrade() -> None:
    # Older migrations create reports from live ORM metadata. On a fresh database the
    # current model can therefore have already supplied both objects before 0019 runs.
    if "content_policy" not in _columns():
        op.add_column(
            _TABLE,
            sa.Column("content_policy", sa.String(length=32), nullable=True),
        )
    if _CONSTRAINT not in _checks():
        op.create_check_constraint(
            _CONSTRAINT,
            _TABLE,
            "content_policy IS NULL OR "
            "content_policy IN ('descriptive_only.v1', 'prediction_backed.v1')",
        )


def downgrade() -> None:
    if _CONSTRAINT in _checks():
        op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    if "content_policy" in _columns():
        op.drop_column(_TABLE, "content_policy")
