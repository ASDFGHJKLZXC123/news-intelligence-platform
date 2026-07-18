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
from services.nlp.clustering import (
    DEFAULT_CLUSTERING_THRESHOLD,
    ClusterItem,
    cluster_by_similarity,
    exact_duplicate_groups,
)
from services.nlp.embedding_text import (
    EMBEDDING_TOKEN_BUDGET,
    build_article_embedding_text,
    build_episode_onset_text,
    build_event_embedding_text,
)
from services.nlp.embeddings import (
    build_embedding_provider,
    embed_texts,
    embed_unembedded_articles,
    embed_unembedded_events,
    resolve_embedding_identity,
    validate_embedding_result,
)
from services.nlp.episodes import (
    EpisodeEmbedding,
    EpisodeRecord,
    embed_episode_onset,
    refresh_episode_embeddings,
    upsert_historical_episode,
)
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
    "DEFAULT_CLUSTERING_THRESHOLD",
    "DENIED_CUES",
    "EMBEDDING_TOKEN_BUDGET",
    "SPECULATIVE_CUES",
    "ArticleMentions",
    "ArticleRecord",
    "ArticleText",
    "AssertionStatus",
    "ClusterItem",
    "ClusterResult",
    "EntityLabel",
    "EntityMention",
    "EpisodeEmbedding",
    "EpisodeRecord",
    "MentionExtractionConfigurationError",
    "SentenceContext",
    "SpacyLanguage",
    "build_article_embedding_text",
    "build_embedding_provider",
    "build_episode_onset_text",
    "build_event_embedding_text",
    "classify_assertion",
    "cluster_by_similarity",
    "cluster_unclustered_articles",
    "compute_event_features",
    "cosine_similarity",
    "embed_episode_onset",
    "embed_texts",
    "embed_unembedded_articles",
    "embed_unembedded_events",
    "exact_duplicate_groups",
    "extract_mentions",
    "load_spacy_language",
    "refresh_episode_embeddings",
    "resolve_embedding_identity",
    "upsert_historical_episode",
    "validate_embedding_result",
]
