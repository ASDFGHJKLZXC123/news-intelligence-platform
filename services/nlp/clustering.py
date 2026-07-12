"""Deterministic deduplication and clustering (pure; unit-tested without a database).

``exact_duplicate_groups`` finds byte-identical content by hash. ``cluster_by_similarity``
groups articles into events with single-link union-find over cosine similarity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from services.nlp.similarity import cosine_similarity


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


def cluster_by_similarity(items: Sequence[ClusterItem], threshold: float = 0.8) -> list[list[int]]:
    """Single-link cluster item indices where cosine similarity >= ``threshold``.

    Returns a deterministic list of index groups (each sorted; groups ordered by first index).
    """
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
