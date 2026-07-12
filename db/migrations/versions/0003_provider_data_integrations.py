"""provider data integration storage

Creates storage tables for external provider run auditing, resumable source cursors,
raw retained payloads, macroeconomic series/observations/signals, and SEC company,
filing, and company-fact data. Built from a frozen ORM table list so the migration
stays aligned with the models without re-creating prior Stage 2 tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from db.models import PROVIDER_DATA_TABLES

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PHASE6_PROVENANCE_COLUMNS = {"schema_version", "raw_document_asset_id", "retrieved_at"}
_LEGACY_METADATA = sa.MetaData()


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
    """Return the 0003 table shape before later provenance columns were added."""

    cloned = sa.Table(
        table.name,
        _LEGACY_METADATA,
        *(
            _copy_column_without_implicit_indexes(column)
            for column in table.columns
            if column.name not in _PHASE6_PROVENANCE_COLUMNS
        ),
    )

    for constraint in table.constraints:
        if not isinstance(constraint, sa.UniqueConstraint):
            continue
        column_names = [column.name for column in constraint.columns]
        if any(name in _PHASE6_PROVENANCE_COLUMNS for name in column_names):
            continue
        cloned.append_constraint(
            sa.UniqueConstraint(
                *(cloned.c[name] for name in column_names),
                name=constraint.name,
            )
        )

    for index in table.indexes:
        column_names = [column.name for column in index.columns]
        if any(name in _PHASE6_PROVENANCE_COLUMNS for name in column_names):
            continue
        sa.Index(
            index.name,
            *(cloned.c[name] for name in column_names),
            unique=index.unique,
        )

    return cloned


_TABLES = [_legacy_table(model.__table__) for model in PROVIDER_DATA_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
