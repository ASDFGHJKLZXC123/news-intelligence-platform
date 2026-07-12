"""risk output tables

Creates time-series risk observations, company and industry rollups, and generated
daily intelligence summaries for database-backed dashboard and radar views.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-20
"""

from collections.abc import Sequence

from alembic import op

from db.models import RISK_OUTPUT_TABLES

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [model.__table__ for model in RISK_OUTPUT_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
