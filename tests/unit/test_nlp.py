"""DB-free unit tests for similarity, clustering, and event features."""

from __future__ import annotations

import datetime

from services.nlp import (
    ArticleRecord,
    ClusterItem,
    cluster_by_similarity,
    compute_event_features,
    cosine_similarity,
    exact_duplicate_groups,
)
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
