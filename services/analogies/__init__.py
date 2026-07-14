"""Historical episodes (Stage 5): retrieval, onset-only LLM rerank, and the durable analogy set.

Distinct from ``services.crisis_model.historical``, which is the standalone crisis model's
weighted-feature prior over hand-supplied cases. This package is the spec's analogy path: one
pgvector cosine search of the curated ``historical_episodes`` corpus, hard-filtered before it is
ranked, abstaining below 0.60, with outcomes joined in only after a match; then an LLM rerank that
sees the survivors' *onset* fields and nothing else, whose selections are reconciled into
``event_analogies`` as a set. :func:`generate_event_analogies` is the one entry point that runs
all of it for an event.
"""

from __future__ import annotations

from services.analogies.compatibility import (
    canonical_episode_type,
    episode_types_for_event_type,
)
from services.analogies.contracts import (
    DEFAULT_MIN_SIMILARITY,
    DEFAULT_TOP_K,
    MAX_TOP_K,
    NO_RELIABLE_ANALOGY,
    AnalogyQuery,
    AnalogyRequestError,
    AnalogyRetrievalError,
    AnalogyRetrievalResult,
    EpisodeCandidate,
    EventEmbeddingMissingError,
    EventNotFoundError,
    HistoricalOutcome,
    MatchedAnalogy,
    OutcomeDistribution,
    OutcomeTally,
    ParentContext,
    RetrievalStatus,
)
from services.analogies.corpus import (
    CorpusValidationError,
    EpisodeCorpus,
    GoldPair,
    GoldSet,
    load_corpus,
    load_corpus_and_gold,
    load_gold_set,
    quota_report,
)
from services.analogies.evaluation import EvaluationReport, evaluate_gold_set
from services.analogies.outcomes import summarize_outcomes
from services.analogies.persistence import (
    AnalogyPersistenceError,
    AnalogyReconciliation,
    EventAnalogyRow,
    build_analogy_rows,
    reconcile_event_analogies,
)
from services.analogies.rerank import (
    AnalogyRerankError,
    DuplicateEpisodeError,
    RegimeCaveatMissingError,
    RerankedCandidate,
    RerankResult,
    RerankStatus,
    UnknownEpisodeError,
    analogy_order_key,
    rerank_candidates,
)
from services.analogies.retrieval import (
    retrieve_analogies_for_event,
    retrieve_analogy_candidates,
)
from services.analogies.service import (
    AnalogyStatus,
    EventAnalogyResult,
    FinalAnalogy,
    generate_event_analogies,
)

__all__ = [
    "DEFAULT_MIN_SIMILARITY",
    "DEFAULT_TOP_K",
    "MAX_TOP_K",
    "NO_RELIABLE_ANALOGY",
    "AnalogyPersistenceError",
    "AnalogyQuery",
    "AnalogyReconciliation",
    "AnalogyRequestError",
    "AnalogyRerankError",
    "AnalogyRetrievalError",
    "AnalogyRetrievalResult",
    "AnalogyStatus",
    "CorpusValidationError",
    "DuplicateEpisodeError",
    "EpisodeCandidate",
    "EpisodeCorpus",
    "EvaluationReport",
    "EventAnalogyResult",
    "EventAnalogyRow",
    "EventEmbeddingMissingError",
    "EventNotFoundError",
    "FinalAnalogy",
    "GoldPair",
    "GoldSet",
    "HistoricalOutcome",
    "MatchedAnalogy",
    "OutcomeDistribution",
    "OutcomeTally",
    "ParentContext",
    "RegimeCaveatMissingError",
    "RerankResult",
    "RerankStatus",
    "RerankedCandidate",
    "RetrievalStatus",
    "UnknownEpisodeError",
    "analogy_order_key",
    "build_analogy_rows",
    "canonical_episode_type",
    "episode_types_for_event_type",
    "evaluate_gold_set",
    "generate_event_analogies",
    "load_corpus",
    "load_corpus_and_gold",
    "load_gold_set",
    "quota_report",
    "reconcile_event_analogies",
    "rerank_candidates",
    "retrieve_analogies_for_event",
    "retrieve_analogy_candidates",
    "summarize_outcomes",
]
