"""event intelligence tables

Adds event embeddings, entity/company/industry exposure links, timeline items,
PostGIS-backed event locations, and a geocoding cache.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-20
"""

from collections.abc import Sequence

from alembic import op

from db.models import EVENT_INTELLIGENCE_TABLES

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [model.__table__ for model in EVENT_INTELLIGENCE_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
