"""enable postgis extension

PostGIS backs location-aware intelligence: event map queries, geospatial incident
joins, country/region filtering, and future geometry columns. pgvector remains enabled
by migration 0001.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS postgis")
