"""evidence and claim tables

Adds queryable evidence items, structured claims, and claim-to-evidence links. These
tables provide the storage foundation for evidence drawers, cited AI answers, and
claim-level traceability.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-20
"""

from collections.abc import Sequence

from alembic import op

from db.models import EVIDENCE_TABLES

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [model.__table__ for model in EVIDENCE_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
