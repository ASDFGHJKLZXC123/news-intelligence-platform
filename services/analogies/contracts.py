"""Typed contracts for historical-episode retrieval (historical-episode spec, ADR 0004).

The onset/outcome separation the spec is built around is enforced here by the *shape* of the
types, not by discipline at the call site: :class:`EpisodeCandidate` -- the thing the vector
query produces and the thing item 3's reranker ranks -- has no field an outcome could be put in.
Hindsight lives in :class:`HistoricalOutcome`, which only exists inside a :class:`MatchedAnalogy`,
which can only be built for an episode that already passed the similarity threshold. A ranker
that never sees an outcome cannot be biased by one.

Invalid input raises (an unknown episode type, a malformed vector, a k of zero). A search that
ran correctly and found nothing good enough does not: it returns
:attr:`RetrievalStatus.NO_RELIABLE_ANALOGY`, which the spec calls "an explicit, allowed answer".
The two are never conflated, because a caller must be able to tell "I asked wrongly" from
"history has no analogy for this".
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from db.models.core import EMBEDDING_DIM
from packages.providers.base import ensure_finite_vector
from services.analogies.compatibility import normalize_tags, partition_episode_types

#: Spec defaults: "k = 5 candidates", "minimum similarity threshold (initial 0.60)".
DEFAULT_TOP_K = 5
DEFAULT_MIN_SIMILARITY = 0.60

#: A finite cap on k. The curated corpus is 80-120 episodes and the reranker's prompt is the real
#: consumer; asking for hundreds of neighbours is a bug, not a request.
MAX_TOP_K = 50

#: A finite cap on each optional filter, so one call cannot build an unbounded IN-list.
MAX_FILTER_VALUES = 32

#: The spec's exact wording for the below-threshold answer.
NO_RELIABLE_ANALOGY = "no reliable analogy"

#: Similarity is reported at this precision. It is not cosmetic: it makes the threshold boundary
#: decidable (a distance of 0.4 leaves 1 - 0.4 = 0.5999999999999999 in float64, which would
#: otherwise fall below an exactly-0.60 threshold) and makes tie ordering reproducible.
SIMILARITY_PRECISION = 6


class AnalogyRetrievalError(ValueError):
    """A retrieval call that cannot be answered as asked. Never a legitimate no-match."""


class AnalogyRequestError(AnalogyRetrievalError):
    """The request is invalid: bad vector, unknown episode type, out-of-range k/threshold."""


class EventNotFoundError(AnalogyRetrievalError):
    """No such event. Distinct from an event that simply has no analogy."""


class EventEmbeddingMissingError(AnalogyRetrievalError):
    """The event exists but carries no vector in the configured model space (ADR 0004)."""


class RetrievalStatus(StrEnum):
    """Every outcome a *successful* retrieval call can have."""

    MATCHED = "matched"
    #: Searched the right family and nothing cleared the threshold. The allowed answer.
    NO_RELIABLE_ANALOGY = "no_reliable_analogy"
    #: The event's type maps to no episode family, so nothing was searched. Abstaining here is
    #: the point: the alternative is ranking the whole of history against an unclassified event.
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"


@dataclass(frozen=True)
class AnalogyQuery:
    """One fully-specified, validated nearest-neighbour search of the episode corpus.

    Constructible without an ``Event`` row -- gold-set evaluation (item 4) supplies a vector it
    embedded itself and calls the retrieval core directly.
    """

    vector: tuple[float, ...]
    episode_types: frozenset[str]
    model: str
    model_version: str
    regime_tags: frozenset[str] = frozenset()
    #: ``None`` means "unfiltered". An explicitly empty set is a bug, not a filter that matches
    #: everything, so it raises rather than quietly returning nothing.
    geographies: frozenset[str] | None = None
    industries: frozenset[str] | None = None
    top_k: int = DEFAULT_TOP_K
    min_similarity: float = DEFAULT_MIN_SIMILARITY

    def __post_init__(self) -> None:
        object.__setattr__(self, "vector", _validated_vector(self.vector))
        object.__setattr__(self, "episode_types", _validated_episode_types(self.episode_types))
        object.__setattr__(self, "regime_tags", normalize_tags(self.regime_tags))
        object.__setattr__(self, "geographies", _validated_filter(self.geographies, "geographies"))
        object.__setattr__(self, "industries", _validated_filter(self.industries, "industries"))
        object.__setattr__(self, "top_k", _validated_top_k(self.top_k))
        object.__setattr__(self, "min_similarity", _validated_threshold(self.min_similarity))
        if not self.model or not self.model_version:
            msg = "a query must pin one embedding model space (ADR 0004)"
            raise AnalogyRequestError(msg)


@dataclass(frozen=True)
class EpisodeCandidate:
    """A ranked episode, as retrieval and the item-3 reranker are allowed to see it.

    Onset, structure, and provenance only. There is deliberately no outcome field: this is the
    record the vector query produces, and the spec forbids hindsight from reaching the ranker.
    """

    episode_id: uuid.UUID
    name: str
    episode_type: str
    onset_date: datetime.date
    peak_date: datetime.date | None
    end_date: datetime.date | None
    onset_summary: str
    onset_indicators: Any | None
    geography: str | None
    affected_industries: tuple[str, ...]
    regime_tags: tuple[str, ...]
    is_counterexample: bool
    source_refs: Any | None
    parent_episode_id: uuid.UUID | None
    #: Raw cosine similarity (``1 - cosine distance``) on the 0.0-1.0 scale, unrescaled.
    similarity: float
    regime_caveats_required: bool
    regime_caveat_reasons: tuple[str, ...]


@dataclass(frozen=True)
class HistoricalOutcome:
    """What became of an episode. Hindsight, labelled as such, attached only after matching.

    Item 3 must present these as *historical* outcomes and never blend them into the current
    event's description (historical-episode spec, "onset/outcome separation").
    """

    episode_id: uuid.UUID
    outcome_summary: str | None
    outcomes: tuple[str, ...]
    resolution_mechanism: str | None


@dataclass(frozen=True)
class ParentContext:
    """The parent arc a matched child episode sits inside. Context, never itself ranked."""

    episode_id: uuid.UUID
    name: str
    onset_summary: str


@dataclass(frozen=True)
class MatchedAnalogy:
    """A candidate that cleared the threshold, with its outcomes joined in afterwards."""

    candidate: EpisodeCandidate
    outcome: HistoricalOutcome
    parent: ParentContext | None = None


@dataclass(frozen=True)
class OutcomeTally:
    """How often one outcome tag occurred among the matched episodes."""

    outcome: str
    count: int
    #: ``count / matched_count``. Episodes carry multiple tags, so rates do not sum to 1.0.
    rate: float


@dataclass(frozen=True)
class OutcomeDistribution:
    """Conflicting analog outcomes as a distribution -- never averaged into one narrative.

    The denominator is always ``matched_count``: every episode that cleared the threshold,
    including the ones curation left untagged. Tagged-only denominators would quietly inflate
    every rate by hiding the episodes we know least about.
    """

    matched_count: int
    tallies: tuple[OutcomeTally, ...]
    untagged_count: int
    untagged_rate: float
    multi_tagged_count: int
    counterexample_count: int
    counterexample_rate: float


@dataclass(frozen=True)
class AnalogyRetrievalResult:
    """The retrieval core's answer, for item 3's reranker and item 4's evaluator."""

    status: RetrievalStatus
    message: str
    episode_types: tuple[str, ...]
    model: str
    model_version: str
    top_k: int
    min_similarity: float
    matches: tuple[MatchedAnalogy, ...] = ()
    distribution: OutcomeDistribution | None = None
    #: Neighbours the vector query returned before the threshold was applied, and the best
    #: similarity among them. Both are diagnostics for threshold recalibration (item 4); the
    #: below-threshold candidates themselves are never returned.
    considered_count: int = 0
    best_similarity: float | None = None

    @property
    def matched(self) -> bool:
        return self.status is RetrievalStatus.MATCHED


def _validated_vector(vector: Iterable[float]) -> tuple[float, ...]:
    try:
        values = ensure_finite_vector(vector)
    except (TypeError, ValueError) as exc:
        # A NaN/Inf reaching the query side is as fatal as one reaching the corpus side: cosine
        # distance against it is NaN, `NaN >= min_similarity` is False, and the search would
        # abstain forever while looking like an honest "no reliable analogy". Checked before the
        # all-zeros rule below, which an all-NaN vector would otherwise sail past (NaN is truthy).
        msg = f"query vector is unusable: {exc}"
        raise AnalogyRequestError(msg) from exc
    if len(values) != EMBEDDING_DIM:
        msg = f"query vector dimension {len(values)} != EMBEDDING_DIM {EMBEDDING_DIM}"
        raise AnalogyRequestError(msg)
    if not any(values):
        # Cosine distance against a zero vector is undefined; pgvector returns NaN and every
        # episode would tie.
        msg = "query vector is all zeros; cosine similarity is undefined"
        raise AnalogyRequestError(msg)
    return values


def _validated_episode_types(values: Iterable[str]) -> frozenset[str]:
    known, unknown = partition_episode_types(values)
    if unknown:
        msg = f"unknown episode types: {', '.join(unknown)}"
        raise AnalogyRequestError(msg)
    if not known:
        msg = "a query must name at least one episode type; retrieval never searches all history"
        raise AnalogyRequestError(msg)
    return known


def _validated_filter(values: Iterable[str] | None, name: str) -> frozenset[str] | None:
    if values is None:
        return None
    cleaned = frozenset(value.strip().lower() for value in values if value and value.strip())
    if not cleaned:
        msg = f"{name} filter was supplied but empty; pass None to leave it unfiltered"
        raise AnalogyRequestError(msg)
    if len(cleaned) > MAX_FILTER_VALUES:
        msg = f"{name} filter holds {len(cleaned)} values; the cap is {MAX_FILTER_VALUES}"
        raise AnalogyRequestError(msg)
    return cleaned


def _validated_top_k(top_k: int) -> int:
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= MAX_TOP_K:
        msg = f"top_k must be an integer in 1..{MAX_TOP_K}, got {top_k!r}"
        raise AnalogyRequestError(msg)
    return top_k


def _validated_threshold(min_similarity: float) -> float:
    value = float(min_similarity)
    # Stricter than cosine's mathematical -1..1: an analogy less similar than an unrelated
    # episode is never usable, so a negative floor could only ever admit noise.
    if not 0.0 <= value <= 1.0:
        msg = f"min_similarity must be a cosine similarity in 0.0..1.0, got {min_similarity!r}"
        raise AnalogyRequestError(msg)
    return value


__all__ = [
    "DEFAULT_MIN_SIMILARITY",
    "DEFAULT_TOP_K",
    "MAX_FILTER_VALUES",
    "MAX_TOP_K",
    "NO_RELIABLE_ANALOGY",
    "SIMILARITY_PRECISION",
    "AnalogyQuery",
    "AnalogyRequestError",
    "AnalogyRetrievalError",
    "AnalogyRetrievalResult",
    "EpisodeCandidate",
    "EventEmbeddingMissingError",
    "EventNotFoundError",
    "HistoricalOutcome",
    "MatchedAnalogy",
    "OutcomeDistribution",
    "OutcomeTally",
    "ParentContext",
    "RetrievalStatus",
]
