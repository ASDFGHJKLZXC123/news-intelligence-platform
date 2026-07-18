"""DB-free unit tests for similarity, clustering, and event features."""

from __future__ import annotations

import datetime
import inspect
import uuid
from unittest.mock import Mock

import pytest
from sqlalchemy.dialects import postgresql

from db.models import EMBEDDING_DIM, Article, ArticleEmbedding
from packages.providers.base import EmbeddingResult
from services.nlp import (
    DEFAULT_CLUSTERING_THRESHOLD,
    ArticleRecord,
    ClusterItem,
    cluster_by_similarity,
    compute_event_features,
    cosine_similarity,
    exact_duplicate_groups,
)
from services.nlp.cluster_service import ClusterResult, cluster_unclustered_articles
from services.nlp.clustering import ClusteringThresholdError
from services.nlp.embeddings import embed_unembedded_articles
from services.nlp.features import event_severity_score, source_diversity_score

_T = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)


def test_cosine_similarity_bounds() -> None:
    assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == 1.0
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == 0.0
    assert cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == -1.0
    assert cosine_similarity((0.0, 0.0), (1.0, 1.0)) == 0.0


def test_cluster_groups_similar_and_separates_dissimilar() -> None:
    items = [
        ClusterItem("a", (1.0, 0.0, 0.0)),
        ClusterItem("b", (0.99, 0.01, 0.0)),  # ~ a
        ClusterItem("c", (0.0, 0.0, 1.0)),  # orthogonal
    ]
    clusters = cluster_by_similarity(items, threshold=0.8)
    assert [0, 1] in clusters
    assert [2] in clusters
    assert len(clusters) == 2


def test_default_clustering_threshold_is_080_and_both_functions_use_it() -> None:
    # A refactor of the live default, not a value change: still 0.80, shared by both entry points.
    assert DEFAULT_CLUSTERING_THRESHOLD == 0.80
    assert (
        inspect.signature(cluster_by_similarity).parameters["threshold"].default
        is DEFAULT_CLUSTERING_THRESHOLD
    )
    assert (
        inspect.signature(cluster_unclustered_articles).parameters["threshold"].default
        is DEFAULT_CLUSTERING_THRESHOLD
    )


@pytest.mark.parametrize(
    "bad", [True, False, float("nan"), float("inf"), float("-inf"), -0.1, 1.1, "0.8", None]
)
def test_cluster_by_similarity_rejects_invalid_thresholds(bad: object) -> None:
    items = [ClusterItem("a", (1.0, 0.0)), ClusterItem("b", (1.0, 0.0))]
    with pytest.raises(ClusteringThresholdError):
        cluster_by_similarity(items, threshold=bad)  # type: ignore[arg-type]


def test_invalid_threshold_is_rejected_before_any_pair_is_scored() -> None:
    # Unequal-length vectors make cosine_similarity raise a *plain* ValueError; a bad threshold must
    # be caught first, so an invalid cutoff surfaces as ClusteringThresholdError, never reaching cosine.
    mismatched = [ClusterItem("a", (1.0,)), ClusterItem("b", (1.0, 1.0))]
    with pytest.raises(ClusteringThresholdError):
        cluster_by_similarity(mismatched, threshold=1.5)


def test_cluster_links_at_exact_threshold_and_not_just_below() -> None:
    # cosine == 1.0 exactly links at threshold 1.0 (inclusive >=); a pair just below 1.0 does not.
    parallel = [ClusterItem("a", (1.0, 0.0)), ClusterItem("b", (1.0, 0.0))]
    assert cluster_by_similarity(parallel, threshold=1.0) == [[0, 1]]
    near = [ClusterItem("a", (1.0, 0.0)), ClusterItem("b", (1.0, 0.001))]
    assert cluster_by_similarity(near, threshold=1.0) == [[0], [1]]


def test_single_link_clustering_is_transitive() -> None:
    items = [
        ClusterItem("a", (1.0, 0.0, 0.0)),
        ClusterItem("b", (1.0, 1.0, 0.0)),  # cos(a,b) == cos(b,c) == 1/sqrt(2) ~ 0.707
        ClusterItem("c", (0.0, 1.0, 0.0)),  # cos(a,c) == 0
    ]
    # A links B, B links C, A does not link C directly -> single-link merges all three.
    assert cluster_by_similarity(items, threshold=0.7) == [[0, 1, 2]]


def test_exact_duplicate_groups_by_content_hash() -> None:
    items = [
        ClusterItem("a", (1.0,), content_hash="h1"),
        ClusterItem("b", (1.0,), content_hash="h1"),
        ClusterItem("c", (1.0,), content_hash="h2"),
    ]
    dups = exact_duplicate_groups(items)
    assert dups == {"h1": ["a", "b"]}


def test_severity_and_diversity_are_monotonic() -> None:
    assert event_severity_score(10, 5) > event_severity_score(2, 1)
    assert event_severity_score(5, 3) >= event_severity_score(5, 1)
    assert source_diversity_score(4, 2) == 0.5
    assert source_diversity_score(0, 0) == 0.0


def test_compute_event_features_aggregates_cluster() -> None:
    records = [
        ArticleRecord("a1", "s1", _T, source_authority=0.8),
        ArticleRecord("a2", "s2", _T + datetime.timedelta(hours=1)),
        ArticleRecord("a3", "s1", _T + datetime.timedelta(hours=2)),
    ]
    features = compute_event_features(records)
    assert features["article_count"] == 3
    assert features["source_count"] == 2
    assert features["first_seen_at"] == _T
    assert features["last_seen_at"] == _T + datetime.timedelta(hours=2)
    assert 0.0 <= features["confidence_score"] <= 1.0
    assert features["evidence_keys"] == ["a1", "a2", "a3"]
    assert features["source_authority_score"] == 0.8


def test_cluster_query_pins_one_model_version_instead_of_selecting_newest() -> None:
    session = Mock()
    session.execute.return_value.all.return_value = []

    result = cluster_unclustered_articles(
        session,
        embedding_model="text-embedding-3-small",
        embedding_model_version="current",
    )

    assert result == ClusterResult(0, 0, 0)
    statement = session.execute.call_args.args[0]
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "article_embeddings.model = 'text-embedding-3-small'" in sql
    assert "article_embeddings.model_version = 'current'" in sql
    assert "'next'" not in sql
    assert "DISTINCT ON" not in sql


def test_embedding_write_persists_provider_model_identity() -> None:
    article = Article(id=uuid.uuid4(), title="Article", summary=None)
    provider = Mock(model_name="configured-model", model_version="v2")
    provider.embed.return_value = [
        EmbeddingResult(
            vector=(0.0,) * EMBEDDING_DIM,
            provider_name="provider",
            model_name="configured-model",
            model_version="v2",
            dimension=EMBEDDING_DIM,
            model_run_id="run-1",
        )
    ]
    session = Mock()
    session.scalars.return_value.all.return_value = [article]

    assert embed_unembedded_articles(session, provider) == 1

    embedding = session.add.call_args.args[0]
    assert isinstance(embedding, ArticleEmbedding)
    assert (embedding.model, embedding.model_version) == ("configured-model", "v2")
    statement = session.scalars.call_args.args[0]
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "article_embeddings.model = 'configured-model'" in sql
    assert "article_embeddings.model_version = 'v2'" in sql
