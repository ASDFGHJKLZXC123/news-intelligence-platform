"""Migration 0017 preserves legacy vectors and refuses a lossy rollback."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from tests.integration._stage6_db import (
    disposable_database,
    require_disposable_postgres,
    url_for,
)
from tests.integration.test_alembic_smoke import _run_owned_alembic

pytestmark = pytest.mark.integration

_EPISODE_ID = "71717171-7171-4171-8171-717171717171"
_VECTOR = "(SELECT ('[' || string_agg('0.1', ',') || ']')::vector FROM generate_series(1, 1536))"


@pytest.fixture
def legacy_episode_at_0016() -> Iterator[tuple[Engine, str]]:
    require_disposable_postgres()
    with disposable_database("nip_embedding_migration_") as database:
        database_url = url_for(database)
        engine = create_engine(database_url)
        try:
            _run_owned_alembic(database_url, "upgrade", "0016")
            with engine.begin() as connection:
                connection.execute(
                    text(f"""
                INSERT INTO historical_episodes
                    (id, name, episode_type, onset_date, onset_summary, onset_embedding,
                     model, model_version, version)
                VALUES
                    ('{_EPISODE_ID}', 'Legacy labelled episode', 'banking_stress',
                     '2008-09-15', 'Funding markets seize.', {_VECTOR},
                     'legacy-episode-model', 'legacy-provider-build', 7)
                """)
                )
            yield engine, database_url
        finally:
            # The owned disposable is deleted only after its engine closes. There is no
            # head downgrade, shared-table truncation, or source-record deletion here.
            engine.dispose()


def test_upgrade_copies_the_legacy_vector_without_relabelling_it(
    legacy_episode_at_0016: tuple[Engine, str],
) -> None:
    engine, database_url = legacy_episode_at_0016
    _run_owned_alembic(database_url, "upgrade", "0017")

    with engine.connect() as connection:
        copied = connection.execute(
            text(
                """
                SELECT embedding.model, embedding.model_version, embedding.episode_version,
                       embedding.dimension, embedding.input_sha256,
                       embedding.input_contract_version,
                       embedding.snapshot_manifest_sha256,
                       embedding.onset_embedding::text = episode.onset_embedding::text
                FROM historical_episode_embeddings embedding
                JOIN historical_episodes episode
                  ON episode.id = embedding.historical_episode_id
                WHERE episode.id = :episode_id
                """
            ),
            {"episode_id": _EPISODE_ID},
        ).one()
        assert copied == (
            "legacy-episode-model",
            "legacy-provider-build",
            7,
            1536,
            None,
            "legacy-unknown",
            None,
            True,
        )

        parent = connection.execute(
            text(
                """
                SELECT model, model_version, version
                FROM historical_episodes
                WHERE id = :episode_id
                """
            ),
            {"episode_id": _EPISODE_ID},
        ).one()
        assert parent == ("legacy-episode-model", "legacy-provider-build", 7)

        article_columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("article_embeddings")
        }
        event_columns = {
            column["name"]: column for column in inspect(connection).get_columns("event_embeddings")
        }
        assert article_columns["model"]["default"] is None
        assert article_columns["model_version"]["default"] is None
        assert event_columns["model"]["default"] is None
        assert event_columns["model_version"]["default"] is None


def test_downgrade_accepts_only_the_exact_legacy_copy(
    legacy_episode_at_0016: tuple[Engine, str],
) -> None:
    engine, database_url = legacy_episode_at_0016
    _run_owned_alembic(database_url, "upgrade", "0017")
    _run_owned_alembic(database_url, "downgrade", "0016")

    with engine.connect() as connection:
        assert "historical_episode_embeddings" not in inspect(connection).get_table_names()
        assert (
            connection.execute(
                text("SELECT model_version FROM historical_episodes WHERE id = :episode_id"),
                {"episode_id": _EPISODE_ID},
            ).scalar_one()
            == "legacy-provider-build"
        )


def test_downgrade_refuses_to_discard_a_snapshot_sidecar(
    legacy_episode_at_0016: tuple[Engine, str],
) -> None:
    engine, database_url = legacy_episode_at_0016
    _run_owned_alembic(database_url, "upgrade", "0017")
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                INSERT INTO historical_episode_embeddings
                    (historical_episode_id, model, model_version, episode_version, dimension,
                     onset_embedding, input_sha256, input_contract_version,
                     snapshot_manifest_sha256)
                VALUES
                    ('{_EPISODE_ID}', 'legacy-episode-model', 'nip-es1-20260729-0123456789abcdefabcd',
                     7, 1536, {_VECTOR}, :input_sha256,
                     'historical-episode-onset-text.v1', :manifest_sha256)
                """
            ),
            {"input_sha256": "a" * 64, "manifest_sha256": "b" * 64},
        )

    with pytest.raises(RuntimeError, match="refusing to downgrade 0017"):
        _run_owned_alembic(database_url, "downgrade", "0016")

    with engine.connect() as connection:
        assert "historical_episode_embeddings" in inspect(connection).get_table_names()
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0017"
        assert (
            connection.scalar(
                text(
                    """
                SELECT count(*) FROM historical_episode_embeddings
                WHERE model_version = 'nip-es1-20260729-0123456789abcdefabcd'
                """
                )
            )
            == 1
        )
