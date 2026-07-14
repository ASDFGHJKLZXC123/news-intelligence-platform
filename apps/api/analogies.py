"""Read API for an event's durable historical analogies (historical-episode spec, Stage 5).

A read, and only a read. The analogies this serves were decided by the Celery rerank task inside
its own transaction; this endpoint never calls an LLM, never embeds, and never writes, so a page
load cannot spend a token or race the worker that produced the rows.

The serialization carries the spec's separation onto the wire. Onset fields describe the episode
as a contemporary observer saw it; everything known only afterwards sits under an explicitly named
``historical_outcome`` object, so a consumer cannot mistake what happened to a 2008 bank for
something being asserted about today's event. The two similarity scales stay apart for the same
reason: ``similarity_score`` is the model's 0-100 structural judgement, ``vector_similarity`` is
the 0.0-1.0 embedding cosine, and neither is rescaled into the other.

An event with no durable rows is not an error. It is the spec's "no reliable analogy" -- the
honest answer, served with an explicit status rather than an empty list a caller might read as a
pipeline that has not run yet.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.base import get_session
from db.models.core import Event, EventAnalogy, HistoricalEpisode
from services.analogies.contracts import (
    MAX_TOP_K,
    NO_RELIABLE_ANALOGY,
    EpisodeCandidate,
    HistoricalOutcome,
    MatchedAnalogy,
    OutcomeDistribution,
)
from services.analogies.outcomes import summarize_outcomes
from services.analogies.rerank import analogy_order_key
from services.analogies.service import AnalogyStatus

router = APIRouter(tags=["analogies"])


class HistoricalOutcomePayload(BaseModel):
    """Hindsight, and named as such. Never merged into the current event's fields."""

    outcome_summary: str | None = None
    outcomes: list[str] = Field(default_factory=list)
    resolution_mechanism: str | None = None


class AnalogyEpisodePayload(BaseModel):
    """The episode as it looked at onset, plus its separately-labelled outcome."""

    episode_id: uuid.UUID
    name: str
    episode_type: str
    onset_date: datetime.date
    peak_date: datetime.date | None = None
    end_date: datetime.date | None = None
    onset_summary: str
    onset_indicators: Any | None = None
    geography: str | None = None
    affected_industries: list[str] = Field(default_factory=list)
    regime_tags: list[str] = Field(default_factory=list)
    is_counterexample: bool = False
    parent_episode_id: uuid.UUID | None = None
    historical_outcome: HistoricalOutcomePayload


class AnalogyItem(BaseModel):
    """One durable analogy: the structural verdict, its caveats, and the episode behind it."""

    #: The reranker's 0-100 structural score (``event_analogies.similarity_score``).
    similarity_score: float = Field(ge=0, le=100)
    #: Item 2's 0.0-1.0 embedding cosine. A different scale, never mixed with the score above.
    vector_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str
    regime_caveats: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    shared_causes: list[str] = Field(default_factory=list)
    llm_run_id: uuid.UUID | None = None
    episode: AnalogyEpisodePayload


class OutcomeTallyPayload(BaseModel):
    outcome: str
    count: int
    rate: float


class OutcomeDistributionPayload(BaseModel):
    """Conflicting analog outcomes as a distribution -- never averaged into one narrative."""

    matched_count: int
    tallies: list[OutcomeTallyPayload] = Field(default_factory=list)
    untagged_count: int = 0
    untagged_rate: float = 0.0
    multi_tagged_count: int = 0
    counterexample_count: int = 0
    counterexample_rate: float = 0.0


class EventAnalogiesResponse(BaseModel):
    event_id: uuid.UUID
    status: AnalogyStatus
    message: str
    items: list[AnalogyItem] = Field(default_factory=list)
    count: int = 0
    #: Computed over exactly the analogies served here.
    distribution: OutcomeDistributionPayload | None = None


#: Item 2's 0.0-1.0 cosine as SQL sees it. It rides in a JSONB payload rather than a column, so the
#: ORDER BY has to reach into it. A missing key and a JSON null both coalesce to 0.0 -- exactly what
#: :func:`_vector_similarity` returns for them in Python, so the database's order and the final
#: in-process sort cannot disagree about where a row belongs.
_VECTOR_SIMILARITY_SQL = func.coalesce(
    EventAnalogy.evidence_refs["vector_similarity"].as_float(), 0.0
)


class AnalogyReadRepository:
    """SQLAlchemy read model over the durable ``event_analogies`` set. No LLM, no embeddings."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def event_exists(self, event_id: uuid.UUID) -> bool:
        return self.session.get(Event, event_id) is not None

    def list_analogies(
        self, event_id: uuid.UUID, *, limit: int
    ) -> list[tuple[EventAnalogy, HistoricalEpisode]]:
        """The event's durable rows, in the one ordering, cut by ``limit`` only after it.

        The full tie-break has to be *in the SQL*, not only in the sort that follows. LIMIT is
        applied to whatever order the database produced, so an ORDER BY that stopped at the score
        would let the database break a tie by id and hand back the wrong row: two analogies tied at
        88.0, one with a 0.95 vector prior and one with 0.61, `limit=1`, and the row that wins is
        whichever has the smaller id. Re-sorting the truncated result afterwards cannot recover the
        candidate that was never fetched.
        """
        statement = (
            select(EventAnalogy, HistoricalEpisode)
            .join(HistoricalEpisode, EventAnalogy.historical_episode_id == HistoricalEpisode.id)
            .where(EventAnalogy.event_id == event_id)
            .order_by(
                EventAnalogy.similarity_score.desc(),
                _VECTOR_SIMILARITY_SQL.desc(),
                EventAnalogy.historical_episode_id.asc(),
            )
            .limit(limit)
        )
        return [(analogy, episode) for analogy, episode in self.session.execute(statement).all()]


def get_analogy_repository(
    session: Annotated[Session, Depends(get_session)],
) -> AnalogyReadRepository:
    return AnalogyReadRepository(session)


RepositoryDep = Annotated[AnalogyReadRepository, Depends(get_analogy_repository)]


def _list(values: Any) -> list[Any]:
    return list(values) if values else []


def _vector_similarity(analogy: EventAnalogy) -> float | None:
    refs = analogy.evidence_refs
    if not isinstance(refs, dict):
        return None
    value = refs.get("vector_similarity")
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _outcome(episode: HistoricalEpisode) -> HistoricalOutcomePayload:
    return HistoricalOutcomePayload(
        outcome_summary=episode.outcome_summary,
        outcomes=_list(episode.outcomes),
        resolution_mechanism=episode.resolution_mechanism,
    )


def _item(analogy: EventAnalogy, episode: HistoricalEpisode) -> AnalogyItem:
    return AnalogyItem(
        similarity_score=float(analogy.similarity_score),
        vector_similarity=_vector_similarity(analogy),
        rationale=analogy.rationale,
        regime_caveats=_list(analogy.regime_caveats),
        limitations=_list(analogy.limitations),
        shared_causes=_list(analogy.shared_causes),
        llm_run_id=analogy.llm_run_id,
        episode=AnalogyEpisodePayload(
            episode_id=episode.id,
            name=episode.name,
            episode_type=episode.episode_type,
            onset_date=episode.onset_date,
            peak_date=episode.peak_date,
            end_date=episode.end_date,
            onset_summary=episode.onset_summary,
            onset_indicators=episode.onset_indicators,
            geography=episode.geography,
            affected_industries=_list(episode.affected_industries),
            regime_tags=_list(episode.regime_tags),
            is_counterexample=bool(episode.is_counterexample),
            parent_episode_id=episode.parent_episode_id,
            historical_outcome=_outcome(episode),
        ),
    )


def _distribution(
    rows: list[tuple[EventAnalogy, HistoricalEpisode]],
) -> OutcomeDistributionPayload:
    """The base rate over the served set, through the same counter the pipeline used."""

    matches = [
        MatchedAnalogy(
            candidate=EpisodeCandidate(
                episode_id=episode.id,
                name=episode.name,
                episode_type=episode.episode_type,
                onset_date=episode.onset_date,
                peak_date=episode.peak_date,
                end_date=episode.end_date,
                onset_summary=episode.onset_summary,
                onset_indicators=episode.onset_indicators,
                geography=episode.geography,
                affected_industries=tuple(_list(episode.affected_industries)),
                regime_tags=tuple(_list(episode.regime_tags)),
                is_counterexample=bool(episode.is_counterexample),
                source_refs=episode.source_refs,
                parent_episode_id=episode.parent_episode_id,
                similarity=_vector_similarity(analogy) or 0.0,
                regime_caveats_required=bool(analogy.regime_caveats),
                regime_caveat_reasons=tuple(_list(analogy.limitations)),
            ),
            outcome=HistoricalOutcome(
                episode_id=episode.id,
                outcome_summary=episode.outcome_summary,
                outcomes=tuple(_list(episode.outcomes)),
                resolution_mechanism=episode.resolution_mechanism,
            ),
        )
        for analogy, episode in rows
    ]
    return _distribution_payload(summarize_outcomes(matches))


def _distribution_payload(distribution: OutcomeDistribution) -> OutcomeDistributionPayload:
    return OutcomeDistributionPayload(
        matched_count=distribution.matched_count,
        tallies=[
            OutcomeTallyPayload(outcome=tally.outcome, count=tally.count, rate=tally.rate)
            for tally in distribution.tallies
        ],
        untagged_count=distribution.untagged_count,
        untagged_rate=distribution.untagged_rate,
        multi_tagged_count=distribution.multi_tagged_count,
        counterexample_count=distribution.counterexample_count,
        counterexample_rate=distribution.counterexample_rate,
    )


@router.get("/api/v1/events/{event_id}/analogies", response_model=EventAnalogiesResponse)
def get_event_analogies(
    event_id: uuid.UUID,
    repo: RepositoryDep,
    limit: int = Query(default=MAX_TOP_K, ge=1, le=MAX_TOP_K),
) -> EventAnalogiesResponse:
    """Serve the analogies the rerank task durably selected for this event."""

    if not repo.event_exists(event_id):
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")

    rows = repo.list_analogies(event_id, limit=limit)
    if not rows:
        # An event nothing matched, and an event nobody has reranked yet, look the same from here.
        # Both honestly have no reliable analogy to serve, so both say so rather than 404.
        return EventAnalogiesResponse(
            event_id=event_id,
            status=AnalogyStatus.NO_RELIABLE_ANALOGY,
            message=NO_RELIABLE_ANALOGY,
        )

    # The SQL already applied this exact order (that is what makes the LIMIT cut in the right
    # place). Re-applying it here through the reranker's own key is what keeps the two definitions
    # from drifting apart, and it is where a row carrying a non-numeric `vector_similarity` gets
    # treated as 0.0 rather than trusted.
    rows.sort(
        key=lambda row: analogy_order_key(
            similarity_score=float(row[0].similarity_score),
            vector_similarity=_vector_similarity(row[0]) or 0.0,
            episode_id=row[1].id,
        )
    )
    return EventAnalogiesResponse(
        event_id=event_id,
        status=AnalogyStatus.MATCHED,
        message=f"{len(rows)} durable historical analogy(ies)",
        items=[_item(analogy, episode) for analogy, episode in rows],
        count=len(rows),
        distribution=_distribution(rows),
    )


__all__ = [
    "AnalogyReadRepository",
    "EventAnalogiesResponse",
    "get_analogy_repository",
    "get_event_analogies",
    "router",
]
