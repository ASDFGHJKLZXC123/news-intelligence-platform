"""ingestion observability tables

Adds durable source-health snapshots and raw document asset metadata. These support
Admin/source health APIs, replayable provider ingestion, and large document retention
without storing bulky assets directly in PostgreSQL.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-20
"""

from collections.abc import Sequence

from alembic import op

from db.models import INGESTION_OBSERVABILITY_TABLES

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [model.__table__ for model in INGESTION_OBSERVABILITY_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
