"""application tables

Creates persisted workspace tables for users, watchlists, alert rules, alerts,
reports, report sections, and saved searches.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-20
"""

from collections.abc import Sequence

from alembic import op

from db.models import APPLICATION_TABLES

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [model.__table__ for model in APPLICATION_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
