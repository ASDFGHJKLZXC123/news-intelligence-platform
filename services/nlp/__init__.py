"""NLP service (Stage P4): embeddings, deduplication, clustering, event features.

ADR 0005 stage 1 (mention extraction plus assertion cues) also lives here; it is
deliberately LLM-free and returns typed results rather than persisting anything.
"""

from __future__ import annotations

from services.nlp.assertions import (
    DENIED_CUES,
    SPECULATIVE_CUES,
    AssertionStatus,
    classify_assertion,
)
from services.nlp.cluster_service import ClusterResult, cluster_unclustered_articles
from services.nlp.clustering import ClusterItem, cluster_by_similarity, exact_duplicate_groups
from services.nlp.embeddings import embed_unembedded_articles
from services.nlp.features import ArticleRecord, compute_event_features
from services.nlp.mentions import (
    ArticleMentions,
    ArticleText,
    EntityLabel,
    EntityMention,
    MentionExtractionConfigurationError,
    SentenceContext,
    SpacyLanguage,
    extract_mentions,
    load_spacy_language,
)
from services.nlp.similarity import cosine_similarity

__all__ = [
    "DENIED_CUES",
    "SPECULATIVE_CUES",
    "ArticleMentions",
    "ArticleRecord",
    "ArticleText",
    "AssertionStatus",
    "ClusterItem",
    "ClusterResult",
    "EntityLabel",
    "EntityMention",
    "MentionExtractionConfigurationError",
    "SentenceContext",
    "SpacyLanguage",
    "classify_assertion",
    "cluster_by_similarity",
    "cluster_unclustered_articles",
    "compute_event_features",
    "cosine_similarity",
    "embed_unembedded_articles",
    "exact_duplicate_groups",
    "extract_mentions",
    "load_spacy_language",
]
