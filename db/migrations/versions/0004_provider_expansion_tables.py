"""provider expansion storage tables

Creates compact storage for sanctions, entity identity, country indicators,
humanitarian reports, geospatial incidents, and energy market context. ACLED and
OpenBB-specific storage are intentionally excluded from this expansion migration.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from db.models import PROVIDER_EXPANSION_TABLES

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PHASE6_PROVENANCE_COLUMNS = {"schema_version", "raw_document_asset_id", "retrieved_at"}
_LEGACY_METADATA = sa.MetaData()


def _phase6_columns_for(table_name: str) -> set[str]:
    columns = set(_PHASE6_PROVENANCE_COLUMNS)
    if table_name == "sanctions_lists":
        columns.remove("retrieved_at")
    return columns


def _copy_column_without_implicit_indexes(column: sa.Column) -> sa.Column:
    return sa.Column(
        column.name,
        column.type,
        *(
            sa.ForeignKey(
                fk.target_fullname,
                ondelete=fk.ondelete,
                onupdate=fk.onupdate,
            )
            for fk in column.foreign_keys
        ),
        primary_key=column.primary_key,
        nullable=column.nullable,
        server_default=column.server_default,
    )


def _legacy_table(table: sa.Table) -> sa.Table:
    """Return the 0004 table shape before later provenance columns were added."""

    skipped_columns = _phase6_columns_for(table.name)
    cloned = sa.Table(
        table.name,
        _LEGACY_METADATA,
        *(
            _copy_column_without_implicit_indexes(column)
            for column in table.columns
            if column.name not in skipped_columns
        ),
    )

    for constraint in table.constraints:
        if not isinstance(constraint, sa.UniqueConstraint):
            continue
        column_names = [column.name for column in constraint.columns]
        if any(name in skipped_columns for name in column_names):
            continue
        cloned.append_constraint(
            sa.UniqueConstraint(
                *(cloned.c[name] for name in column_names),
                name=constraint.name,
            )
        )

    for index in table.indexes:
        column_names = [column.name for column in index.columns]
        if any(name in skipped_columns for name in column_names):
            continue
        sa.Index(
            index.name,
            *(cloned.c[name] for name in column_names),
            unique=index.unique,
        )

    return cloned


_TABLES = [_legacy_table(model.__table__) for model in PROVIDER_EXPANSION_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
