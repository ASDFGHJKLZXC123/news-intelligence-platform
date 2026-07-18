"""Deterministic deduplication and clustering (pure; unit-tested without a database).

``exact_duplicate_groups`` finds byte-identical content by hash. ``cluster_by_similarity``
groups articles into events with single-link union-find over cosine similarity.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from services.nlp.similarity import cosine_similarity

#: The live single-link cosine clustering default. ADR 0004 names 0.82 as an *unvalidated*
#: roadmap target; the running system uses 0.80 and keeps it until a labeled same-event /
#: different-event article-pair dataset is curated (Stage 9, `stage9-validation.v1`).
DEFAULT_CLUSTERING_THRESHOLD = 0.80


class ClusteringThresholdError(ValueError):
    """A clustering threshold is not a real number in ``[0, 1]``. Raised before any pair is scored."""


def _validate_threshold(threshold: float) -> float:
    """Reject a threshold that is not a finite real number in ``[0, 1]``, before scoring any pair.

    ``bool`` is rejected explicitly (it is an ``int`` subclass but never a meaningful cutoff), as are
    ``NaN``/``Infinity`` and any value outside the closed unit interval cosine similarity can reach.
    """
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ClusteringThresholdError(
            f"threshold must be a real number in [0, 1], got {threshold!r}"
        )
    if not math.isfinite(threshold):
        raise ClusteringThresholdError(f"threshold must be finite, not {threshold!r}")
    if not 0.0 <= threshold <= 1.0:
        raise ClusteringThresholdError(f"threshold must lie in [0, 1], got {threshold!r}")
    return threshold


@dataclass(frozen=True)
class ClusterItem:
    """One article's clustering inputs."""

    key: str
    vector: tuple[float, ...]
    content_hash: str | None = None


def exact_duplicate_groups(items: Sequence[ClusterItem]) -> dict[str, list[str]]:
    """Return ``{content_hash: [keys]}`` for hashes shared by more than one item."""
    groups: dict[str, list[str]] = {}
    for item in items:
        if item.content_hash:
            groups.setdefault(item.content_hash, []).append(item.key)
    return {digest: keys for digest, keys in groups.items() if len(keys) > 1}


def cluster_by_similarity(
    items: Sequence[ClusterItem], threshold: float = DEFAULT_CLUSTERING_THRESHOLD
) -> list[list[int]]:
    """Single-link cluster item indices where cosine similarity >= ``threshold``.

    Returns a deterministic list of index groups (each sorted; groups ordered by first index).
    The threshold is validated up front (:class:`ClusteringThresholdError`), before any pair is
    scored, so a malformed cutoff never silently reaches ``cosine_similarity``.
    """
    _validate_threshold(threshold)
    n = len(items)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[max(root_i, root_j)] = min(root_i, root_j)

    for i in range(n):
        for j in range(i + 1, n):
            if cosine_similarity(items[i].vector, items[j].vector) >= threshold:
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    return [sorted(group) for _, group in sorted(clusters.items())]
