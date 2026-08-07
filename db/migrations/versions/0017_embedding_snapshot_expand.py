"""Expand embedding persistence for immutable model snapshots.

The legacy embedding columns remain in place during this revision. Historical-episode vectors
are copied, without changing their model labels, into a sidecar keyed by episode, model,
model-version snapshot, and curated episode version. New code can therefore dual-write and read
the exact requested space while old code remains deployable during the expand/contract rollout.

Embedding identity defaults are removed from article and event embeddings. A missing identity is
an error at the writer boundary; allowing the database to silently supply one can relabel a vector
that was produced by a different hosted snapshot.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "historical_episode_embeddings"
_INDEX = "ix_historical_episode_embeddings_onset_embedding_hnsw"
_DIMENSION = 1536
_LEGACY_MODEL = "text-embedding-3-small"
_LEGACY_MODEL_VERSION = "current"
_LEGACY_INPUT_CONTRACT = "legacy-unknown"


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _indexes(table: str) -> set[str]:
    if not _has_table(table):
        return set()
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _create_sidecar() -> None:
    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column(
                "historical_episode_id",
                sa.Uuid(),
                sa.ForeignKey("historical_episodes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("model", sa.String(length=128), nullable=False),
            sa.Column("model_version", sa.String(length=64), nullable=False),
            sa.Column("episode_version", sa.Integer(), nullable=False),
            sa.Column("dimension", sa.Integer(), nullable=False),
            sa.Column("onset_embedding", Vector(_DIMENSION), nullable=False),
            sa.Column("input_sha256", sa.String(length=64), nullable=True),
            sa.Column("input_contract_version", sa.String(length=64), nullable=False),
            sa.Column("snapshot_manifest_sha256", sa.String(length=64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "episode_version >= 1",
                name="ck_historical_episode_embeddings_episode_version_positive",
            ),
            sa.CheckConstraint(
                f"dimension = {_DIMENSION}",
                name="ck_historical_episode_embeddings_dimension",
            ),
            sa.CheckConstraint(
                "input_contract_version <> ''",
                name="ck_historical_episode_embeddings_input_contract",
            ),
            sa.CheckConstraint(
                "input_sha256 IS NULL OR input_sha256 ~ '^[0-9a-f]{64}$'",
                name="ck_historical_episode_embeddings_input_sha256",
            ),
            sa.CheckConstraint(
                "snapshot_manifest_sha256 IS NULL OR snapshot_manifest_sha256 ~ '^[0-9a-f]{64}$'",
                name="ck_historical_episode_embeddings_snapshot_manifest_sha256",
            ),
            sa.PrimaryKeyConstraint(
                "historical_episode_id",
                "model",
                "model_version",
                "episode_version",
                name="historical_episode_embeddings_pkey",
            ),
        )
    if _INDEX not in _indexes(_TABLE):
        op.create_index(
            _INDEX,
            _TABLE,
            ["onset_embedding"],
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"onset_embedding": "vector_cosine_ops"},
        )


def _copy_legacy_vectors() -> None:
    # The source labels are data. In particular, ``current`` means "legacy, unverifiable hosted
    # alias"; it must never be rewritten to a newly captured snapshot id.
    op.execute(
        sa.text(
            f"""
            INSERT INTO {_TABLE}
                (historical_episode_id, model, model_version, episode_version, dimension,
                 onset_embedding, input_sha256, input_contract_version,
                 snapshot_manifest_sha256, created_at)
            SELECT id, model, model_version, version, :dimension,
                   onset_embedding, NULL, :input_contract, NULL, created_at
            FROM historical_episodes
            ON CONFLICT (historical_episode_id, model, model_version, episode_version)
            DO NOTHING
            """
        ).bindparams(dimension=_DIMENSION, input_contract=_LEGACY_INPUT_CONTRACT)
    )


def _remove_identity_defaults() -> None:
    op.alter_column(
        "article_embeddings",
        "model",
        existing_type=sa.String(length=128),
        existing_nullable=False,
        server_default=None,
    )
    op.alter_column(
        "article_embeddings",
        "model_version",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default=None,
    )
    op.alter_column(
        "event_embeddings",
        "model_version",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default=None,
    )


def upgrade() -> None:
    _create_sidecar()
    _copy_legacy_vectors()
    _remove_identity_defaults()


def _has_non_legacy_sidecar_rows() -> bool:
    if not _has_table(_TABLE):
        return False
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                f"""
                SELECT EXISTS (
                    SELECT 1
                    FROM {_TABLE} embedding
                    LEFT JOIN historical_episodes episode
                      ON episode.id = embedding.historical_episode_id
                    WHERE episode.id IS NULL
                       OR embedding.model IS DISTINCT FROM episode.model
                       OR embedding.model_version IS DISTINCT FROM episode.model_version
                       OR embedding.episode_version IS DISTINCT FROM episode.version
                       OR embedding.dimension IS DISTINCT FROM :dimension
                       OR embedding.onset_embedding::text
                            IS DISTINCT FROM episode.onset_embedding::text
                       OR embedding.input_sha256 IS NOT NULL
                       OR embedding.input_contract_version IS DISTINCT FROM :input_contract
                       OR embedding.snapshot_manifest_sha256 IS NOT NULL
                )
                """
            ).bindparams(dimension=_DIMENSION, input_contract=_LEGACY_INPUT_CONTRACT)
        )
        .scalar_one()
    )


def _restore_identity_defaults() -> None:
    op.alter_column(
        "article_embeddings",
        "model",
        existing_type=sa.String(length=128),
        existing_nullable=False,
        server_default=_LEGACY_MODEL,
    )
    op.alter_column(
        "article_embeddings",
        "model_version",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default=_LEGACY_MODEL_VERSION,
    )
    op.alter_column(
        "event_embeddings",
        "model_version",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default="",
    )


def downgrade() -> None:
    if _has_non_legacy_sidecar_rows():
        raise RuntimeError(
            "refusing to downgrade 0017: historical_episode_embeddings contains "
            "snapshot/revision rows that the legacy schema cannot preserve"
        )
    if _INDEX in _indexes(_TABLE):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _has_table(_TABLE):
        op.drop_table(_TABLE)
    _restore_identity_defaults()
