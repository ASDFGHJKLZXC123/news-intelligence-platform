"""company master tables

Creates the canonical company layer on top of entity profiles and provider identity
tables. These tables back company search, company detail pages, ticker/security lookup,
and source-independent company aliases.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-19
"""

from collections.abc import Sequence

from alembic import op

from db.models import COMPANY_MASTER_TABLES

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [model.__table__ for model in COMPANY_MASTER_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
