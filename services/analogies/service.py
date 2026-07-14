"""The Stage 5 analogy pipeline for one event: retrieve, rerank, attach outcomes, persist.

One typed entry point (:func:`generate_event_analogies`) so the Celery task, and anything after
it, has exactly one thing to call. The order it runs in is the spec's look-ahead-bias rule spelled
out as control flow:

1. **Retrieve** (item 2): hard filters, vector search, threshold. Outcomes are read only for the
   episodes that already cleared it.
2. **Rerank** (item 3): the LLM sees the candidates' *onset* fields and nothing else. The outcomes
   retrieval loaded stay in a dictionary here and are never passed down.
3. **Attach**: the already-separated outcomes are joined back onto the episodes the rerank kept --
   only those -- and the conflicting-outcome distribution is recomputed over that final set. A
   distribution over the vector candidates the rerank *dropped* would be a base rate for a
   question nobody asked.
4. **Persist**: the final set replaces the event's durable rows in the caller's transaction.

Three answers are all successes, and the caller must be able to tell them apart from a failure:
an event whose type maps to no episode family, a vector search where nothing cleared 0.60, and a
rerank where the model found nothing structurally comparable. All three mean "no reliable
analogy", and all three still reconcile -- with an empty set -- so a previous run's analogies are
removed rather than left behind to be served as current.

Invalid input (unknown event, no embedding in the configured space, a malformed request) stays the
typed failure item 2 made it. A provider or contract failure propagates too. Neither is ever
laundered into "no analogy found", because the caller must be able to retry one and not the other.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from db.models.core import Event
from services.analogies.contracts import (
    DEFAULT_MIN_SIMILARITY,
    DEFAULT_TOP_K,
    NO_RELIABLE_ANALOGY,
    AnalogyRetrievalResult,
    EpisodeCandidate,
    HistoricalOutcome,
    MatchedAnalogy,
    OutcomeDistribution,
    ParentContext,
    RetrievalStatus,
)
from services.analogies.outcomes import summarize_outcomes
from services.analogies.persistence import (
    AnalogyReconciliation,
    build_analogy_rows,
    reconcile_event_analogies,
)
from services.analogies.rerank import RerankResult, RerankStatus, rerank_candidates
from services.analogies.retrieval import retrieve_analogies_for_event


class AnalogyStatus(StrEnum):
    """Every outcome a *successful* run can have. Failures raise instead."""

    MATCHED = "matched"
    NO_RELIABLE_ANALOGY = "no_reliable_analogy"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"


@dataclass(frozen=True)
class FinalAnalogy:
    """One analogy the whole pipeline stands behind, with its hindsight labelled as hindsight."""

    candidate: EpisodeCandidate
    #: Explicitly the *historical* episode's outcome. Never blended into the current event.
    outcome: HistoricalOutcome
    parent: ParentContext | None
    #: The contract's 0-100 structural score from the rerank.
    similarity_score: float
    explanation: str
    regime_caveats: tuple[str, ...]
    confidence: float

    @property
    def episode_id(self) -> uuid.UUID:
        return self.candidate.episode_id

    @property
    def vector_similarity(self) -> float:
        """Item 2's 0.0-1.0 cosine. A different scale from ``similarity_score``, kept apart."""
        return self.candidate.similarity


@dataclass(frozen=True)
class EventAnalogyResult:
    """The pipeline's answer for one event, and what it wrote."""

    event_id: uuid.UUID
    status: AnalogyStatus
    message: str
    matches: tuple[FinalAnalogy, ...] = ()
    #: Recomputed over the final selected set only, never over the dropped vector candidates.
    distribution: OutcomeDistribution | None = None
    reconciliation: AnalogyReconciliation = AnalogyReconciliation()
    llm_run_id: uuid.UUID | None = None
    trace_id: str | None = None
    tier: str | None = None
    cache_hit: bool = False
    #: Diagnostics: neighbours the vector query considered, and how many reached the reranker.
    considered_count: int = 0
    candidate_count: int = 0

    @property
    def matched(self) -> bool:
        return self.status is AnalogyStatus.MATCHED

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serializable summary. This is what a Celery result may carry.

        The status key is ``analogy_status``, not ``status``: a task merges this into the job
        contract's own payload, whose ``status`` means "did the job run" -- a very different
        question from "did this event turn out to have an analogy", and one no caller should have
        to disambiguate by position.
        """

        return {
            "event_id": str(self.event_id),
            "analogy_status": self.status.value,
            "message": self.message,
            "matched_episode_ids": [str(match.episode_id) for match in self.matches],
            "match_count": len(self.matches),
            "llm_run_id": None if self.llm_run_id is None else str(self.llm_run_id),
            "trace_id": self.trace_id,
            "tier": self.tier,
            "cache_hit": self.cache_hit,
            "considered_count": self.considered_count,
            "candidate_count": self.candidate_count,
            "analogies_inserted": self.reconciliation.inserted,
            "analogies_updated": self.reconciliation.updated,
            "analogies_removed": self.reconciliation.removed,
        }


def _outcomes_by_id(retrieval: AnalogyRetrievalResult) -> dict[uuid.UUID, HistoricalOutcome]:
    return {match.candidate.episode_id: match.outcome for match in retrieval.matches}


def _parents_by_episode(retrieval: AnalogyRetrievalResult) -> dict[uuid.UUID, ParentContext]:
    return {
        match.candidate.episode_id: match.parent
        for match in retrieval.matches
        if match.parent is not None
    }


def _parents_by_parent_id(retrieval: AnalogyRetrievalResult) -> dict[uuid.UUID, ParentContext]:
    """Keyed the way the prompt needs it: by the parent's own id."""

    return {
        match.parent.episode_id: match.parent
        for match in retrieval.matches
        if match.parent is not None
    }


def _attach_outcomes(
    rerank: RerankResult,
    outcomes: Mapping[uuid.UUID, HistoricalOutcome],
    parents: Mapping[uuid.UUID, ParentContext],
) -> tuple[FinalAnalogy, ...]:
    """Join hindsight back on -- for the final selections only, and labelled as historical."""

    return tuple(
        FinalAnalogy(
            candidate=selection.candidate,
            outcome=outcomes.get(
                selection.episode_id,
                HistoricalOutcome(
                    episode_id=selection.episode_id,
                    outcome_summary=None,
                    outcomes=(),
                    resolution_mechanism=None,
                ),
            ),
            parent=parents.get(selection.episode_id),
            similarity_score=selection.similarity_score,
            explanation=selection.explanation,
            regime_caveats=selection.regime_caveats,
            confidence=selection.confidence,
        )
        for selection in rerank.selections
    )


def final_distribution(matches: Iterable[FinalAnalogy]) -> OutcomeDistribution:
    """The conflicting-outcome distribution over the *final* set, via the item-2 counter."""

    return summarize_outcomes(
        [
            MatchedAnalogy(candidate=match.candidate, outcome=match.outcome, parent=match.parent)
            for match in matches
        ]
    )


def _no_match(
    session: Session,
    event_id: uuid.UUID,
    *,
    status: AnalogyStatus,
    message: str,
    rerank: RerankResult | None = None,
    considered_count: int = 0,
    candidate_count: int = 0,
) -> EventAnalogyResult:
    """The allowed answer -- and it still clears the event, so nothing stale is served."""

    reconciliation = reconcile_event_analogies(session, event_id, ())
    return EventAnalogyResult(
        event_id=event_id,
        status=status,
        message=message,
        reconciliation=reconciliation,
        llm_run_id=None if rerank is None else rerank.llm_run_id,
        trace_id=None if rerank is None else rerank.trace_id,
        tier=None if rerank is None else rerank.tier,
        cache_hit=False if rerank is None else rerank.cache_hit,
        considered_count=considered_count,
        candidate_count=candidate_count,
    )


def generate_event_analogies(
    session: Session,
    event_id: uuid.UUID,
    *,
    orchestrator: Any,
    regime_tags: Iterable[str] | None = None,
    geographies: Iterable[str] | None = None,
    industries: Iterable[str] | None = None,
    episode_types: Iterable[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
) -> EventAnalogyResult:
    """Produce, persist, and return one event's historical analogies. Never commits."""

    current_regime_tags = tuple(regime_tags or ())
    retrieval = retrieve_analogies_for_event(
        session,
        event_id,
        regime_tags=current_regime_tags,
        geographies=geographies,
        industries=industries,
        episode_types=episode_types,
        top_k=top_k,
        min_similarity=min_similarity,
        embedding_model=embedding_model,
        embedding_model_version=embedding_model_version,
    )

    if retrieval.status is not RetrievalStatus.MATCHED:
        # Nothing cleared the threshold, or the event's type maps to no family. Either way there
        # is nothing to rerank, so no LLM is called and no whitelist is needed.
        return _no_match(
            session,
            event_id,
            status=AnalogyStatus(retrieval.status.value),
            message=retrieval.message,
            considered_count=retrieval.considered_count,
        )

    event = session.get(Event, event_id)
    if event is None:  # unreachable: retrieval already raised for a missing event
        msg = f"event {event_id} disappeared between retrieval and rerank"
        raise LookupError(msg)

    candidates = [match.candidate for match in retrieval.matches]
    rerank = rerank_candidates(
        orchestrator,
        event=event,
        candidates=candidates,
        parents=_parents_by_parent_id(retrieval),
        current_regime_tags=current_regime_tags,
    )

    if rerank.status is RerankStatus.NO_RELIABLE_ANALOGY:
        return _no_match(
            session,
            event_id,
            status=AnalogyStatus.NO_RELIABLE_ANALOGY,
            message=f"{NO_RELIABLE_ANALOGY}: {rerank.message}",
            rerank=rerank,
            considered_count=retrieval.considered_count,
            candidate_count=len(candidates),
        )

    matches = _attach_outcomes(rerank, _outcomes_by_id(retrieval), _parents_by_episode(retrieval))
    reconciliation = reconcile_event_analogies(
        session,
        event_id,
        build_analogy_rows(
            rerank.selections,
            llm_run_id=rerank.llm_run_id,
            trace_id=rerank.trace_id,
            model=retrieval.model,
            model_version=retrieval.model_version,
            current_regime_tags=current_regime_tags,
        ),
    )
    return EventAnalogyResult(
        event_id=event_id,
        status=AnalogyStatus.MATCHED,
        message=rerank.message,
        matches=matches,
        distribution=final_distribution(matches),
        reconciliation=reconciliation,
        llm_run_id=rerank.llm_run_id,
        trace_id=rerank.trace_id,
        tier=rerank.tier,
        cache_hit=rerank.cache_hit,
        considered_count=retrieval.considered_count,
        candidate_count=len(candidates),
    )


__all__ = [
    "AnalogyStatus",
    "EventAnalogyResult",
    "FinalAnalogy",
    "final_distribution",
    "generate_event_analogies",
]
