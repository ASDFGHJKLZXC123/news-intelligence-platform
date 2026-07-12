"""provider provenance columns

Adds consistent schema version, raw asset reference, and retrieval timestamp columns
to normalized provider-domain records without changing their existing natural keys.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVENANCE_TABLES = (
    "macro_series",
    "macro_observations",
    "sec_companies",
    "sec_filings",
    "sec_company_facts",
    "sanctions_lists",
    "sanctions_entities",
    "country_indicator_series",
    "country_indicator_observations",
    "humanitarian_reports",
    "geo_incidents",
    "energy_market_snapshots",
    "energy_disruption_events",
)

_TABLES_WITH_NEW_RETRIEVED_AT = tuple(
    table for table in _PROVENANCE_TABLES if table != "sanctions_lists"
)


def _fk_name(table_name: str) -> str:
    return f"fk_{table_name}_raw_asset"


def _raw_asset_index_name(table_name: str) -> str:
    return f"ix_{table_name}_raw_document_asset_id"


def upgrade() -> None:
    for table_name in _PROVENANCE_TABLES:
        op.add_column(
            table_name,
            sa.Column("schema_version", sa.String(length=32), nullable=False, server_default="v1"),
        )
        op.add_column(table_name, sa.Column("raw_document_asset_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            _fk_name(table_name),
            table_name,
            "raw_document_assets",
            ["raw_document_asset_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(_raw_asset_index_name(table_name), table_name, ["raw_document_asset_id"])

    for table_name in _TABLES_WITH_NEW_RETRIEVED_AT:
        op.add_column(
            table_name,
            sa.Column(
                "retrieved_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )


def downgrade() -> None:
    for table_name in reversed(_TABLES_WITH_NEW_RETRIEVED_AT):
        op.drop_column(table_name, "retrieved_at")

    for table_name in reversed(_PROVENANCE_TABLES):
        op.drop_index(_raw_asset_index_name(table_name), table_name=table_name)
        op.drop_constraint(_fk_name(table_name), table_name, type_="foreignkey")
        op.drop_column(table_name, "raw_document_asset_id")
        op.drop_column(table_name, "schema_version")
