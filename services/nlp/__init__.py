"""NLP service (Stage P4): embeddings, deduplication, clustering, event features."""

from __future__ import annotations

from services.nlp.cluster_service import ClusterResult, cluster_unclustered_articles
from services.nlp.clustering import ClusterItem, cluster_by_similarity, exact_duplicate_groups
from services.nlp.embeddings import embed_unembedded_articles
from services.nlp.features import ArticleRecord, compute_event_features
from services.nlp.similarity import cosine_similarity

__all__ = [
    "ArticleRecord",
    "ClusterItem",
    "ClusterResult",
    "cluster_by_similarity",
    "cluster_unclustered_articles",
    "compute_event_features",
    "cosine_similarity",
    "embed_unembedded_articles",
    "exact_duplicate_groups",
]
