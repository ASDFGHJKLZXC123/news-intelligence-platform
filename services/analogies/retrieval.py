"""Database-backed historical-episode retrieval (historical-episode spec, ADR 0004).

The query architecture *is* the look-ahead-bias control, so it is worth being explicit about the
shape of it. Retrieval is two statements, in this order and never fused:

1. **Rank.** Hard filters (episode family, the pinned model space, leaf granularity, the optional
   geography/industry filters) are applied first, and only then are the survivors ordered by
   pgvector's cosine distance. This statement selects onset, filter, and structural columns and
   nothing else -- ``outcome_summary``, ``outcomes``, ``resolution_mechanism`` are not in its
   projection, are not in its predicates, and are not in its ORDER BY. Outcome text is not merely
   ignored during ranking; it is not fetched, so it *cannot* rank anything.
2. **Attach.** Only for the episodes that then cleared the similarity threshold does a second
   statement read the outcomes, and a third the parent arc's context. Hindsight enters the
   process exactly once matching is already decided.

Regime is not in step 1's WHERE clause, and that is the spec's rule rather than an oversight: an
episode from another regime is still an analogy, it is an analogy that needs caveats. Excluding
it would hide it; admitting it silently would launder it. It comes back flagged.

The corpus is a curated 80-120 rows, which is why the tie-break sort keys ride along in SQL: at
this size a deterministic total order is worth more than a plan that keeps the HNSW index for the
final sort. There is no live embedding call anywhere in this module -- the current event's vector
is read from ``event_embeddings``, and the gold-set evaluator hands its own vector to
:func:`retrieve_analogy_candidates`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Select, and_, func, literal, select
from sqlalchemy.orm import Session, aliased

from db.models.core import (
    Event,
    EventEmbedding,
    HistoricalEpisode,
    HistoricalEpisodeEmbedding,
)
from services.analogies.compatibility import (
    episode_types_for_event_type,
    partition_episode_types,
    regime_caveats,
)
from services.analogies.contracts import (
    DEFAULT_MIN_SIMILARITY,
    DEFAULT_TOP_K,
    NO_RELIABLE_ANALOGY,
    SIMILARITY_PRECISION,
    AnalogyQuery,
    AnalogyRequestError,
    AnalogyRetrievalResult,
    EpisodeCandidate,
    EventEmbeddingMissingError,
    EventNotFoundError,
    HistoricalOutcome,
    MatchedAnalogy,
    ParentContext,
    RetrievalStatus,
)
from services.analogies.outcomes import summarize_outcomes
from services.nlp.embeddings import resolve_embedding_identity

#: The ranking projection. Onset, filter, and structural fields -- the columns item 3 reranks on.
#: Outcome columns are absent by construction; see the module docstring.
_RANKING_COLUMNS = (
    HistoricalEpisode.id,
    HistoricalEpisode.name,
    HistoricalEpisode.episode_type,
    HistoricalEpisode.onset_date,
    HistoricalEpisode.peak_date,
    HistoricalEpisode.end_date,
    HistoricalEpisode.onset_summary,
    HistoricalEpisode.onset_indicators,
    HistoricalEpisode.geography,
    HistoricalEpisode.affected_industries,
    HistoricalEpisode.regime_tags,
    HistoricalEpisode.is_counterexample,
    HistoricalEpisode.source_refs,
    HistoricalEpisode.parent_episode_id,
)


def _ranking_statement(query: AnalogyQuery) -> Select[Any]:
    """Hard filters, then nearest neighbours by cosine distance. Onset columns only."""
    child = aliased(HistoricalEpisode)
    # Matching runs at leaf granularity (spec, "Boundary rule"): an episode that parents another
    # row is an arc, not an event-sized comparable. Standalone and child rows both stay.
    is_parent = select(child.id).where(child.parent_episode_id == HistoricalEpisode.id).exists()
    distance = HistoricalEpisodeEmbedding.onset_embedding.cosine_distance(list(query.vector)).label(
        "distance"
    )

    statement = (
        select(*_RANKING_COLUMNS, distance)
        .join(
            HistoricalEpisodeEmbedding,
            and_(
                HistoricalEpisodeEmbedding.historical_episode_id == HistoricalEpisode.id,
                HistoricalEpisodeEmbedding.model == query.model,
                HistoricalEpisodeEmbedding.model_version == query.model_version,
                HistoricalEpisodeEmbedding.episode_version == HistoricalEpisode.version,
            ),
        )
        .where(
            HistoricalEpisode.episode_type.in_(sorted(query.episode_types)),
            ~is_parent,
        )
        .order_by(
            distance.asc(),
            HistoricalEpisode.onset_date.asc(),
            HistoricalEpisode.id.asc(),
        )
        .limit(query.top_k)
    )
    if query.geographies:
        statement = statement.where(
            func.lower(HistoricalEpisode.geography).in_(sorted(query.geographies))
        )
    if query.industries:
        # ``column_valued``, not ``table_valued``: the latter renders ``unnest(...) AS anon`` with
        # no derived column list, so the ``anon.industry`` it then references does not exist and
        # PostgreSQL rejects the statement outright. ``column_valued`` names the alias itself
        # (``unnest(...) AS industry``), which is how a one-column set-returning function is
        # referenced. Compiling the SQL is not enough to catch this -- only executing it is.
        industry = func.unnest(HistoricalEpisode.affected_industries).column_valued("industry")
        overlaps = (
            select(literal(1))
            .where(func.lower(industry).in_(sorted(query.industries)))
            .correlate(HistoricalEpisode)
            .exists()
        )
        statement = statement.where(overlaps)
    return statement


def _outcome_statement(episode_ids: Sequence[uuid.UUID]) -> Select[Any]:
    """The hindsight, read only for episodes that already matched."""
    return select(
        HistoricalEpisode.id,
        HistoricalEpisode.outcome_summary,
        HistoricalEpisode.outcomes,
        HistoricalEpisode.resolution_mechanism,
    ).where(HistoricalEpisode.id.in_(sorted(episode_ids)))


def _parent_statement(episode_ids: Sequence[uuid.UUID]) -> Select[Any]:
    """The parent arc as context. Onset text only -- a parent is not ranked, and not a spoiler."""
    return select(
        HistoricalEpisode.id,
        HistoricalEpisode.name,
        HistoricalEpisode.onset_summary,
    ).where(HistoricalEpisode.id.in_(sorted(episode_ids)))


def _tuple(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(values) if values else ()


def _candidate(row: Any, regime_tags: frozenset[str]) -> EpisodeCandidate:
    """Build one candidate, scoring and regime-gating it. ``1 - cosine distance``, unrescaled."""
    similarity = round(1.0 - float(row.distance), SIMILARITY_PRECISION)
    required, reasons = regime_caveats(regime_tags, row.regime_tags)
    return EpisodeCandidate(
        episode_id=row.id,
        name=row.name,
        episode_type=row.episode_type,
        onset_date=row.onset_date,
        peak_date=row.peak_date,
        end_date=row.end_date,
        onset_summary=row.onset_summary,
        onset_indicators=row.onset_indicators,
        geography=row.geography,
        affected_industries=_tuple(row.affected_industries),
        regime_tags=_tuple(row.regime_tags),
        is_counterexample=bool(row.is_counterexample),
        source_refs=row.source_refs,
        parent_episode_id=row.parent_episode_id,
        similarity=similarity,
        regime_caveats_required=required,
        regime_caveat_reasons=reasons,
    )


def _load_outcomes(
    session: Session, episode_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, HistoricalOutcome]:
    rows = session.execute(_outcome_statement(episode_ids)).all()
    return {
        row.id: HistoricalOutcome(
            episode_id=row.id,
            outcome_summary=row.outcome_summary,
            outcomes=_tuple(row.outcomes),
            resolution_mechanism=row.resolution_mechanism,
        )
        for row in rows
    }


def _load_parents(
    session: Session, episode_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ParentContext]:
    if not episode_ids:
        return {}
    rows = session.execute(_parent_statement(episode_ids)).all()
    return {
        row.id: ParentContext(episode_id=row.id, name=row.name, onset_summary=row.onset_summary)
        for row in rows
    }


def _narrowed_families(
    requested: Iterable[str], allowed: frozenset[str], event_type: str | None
) -> frozenset[str]:
    """Validate an explicit ``episode_types`` override: it may narrow the search, never widen it.

    The override exists so a caller can search *fewer* families than the event's type admits. It is
    not a way to search a different one. Compatibility is the rule the whole module is built on --
    an event is only ever matched against its own family -- and a caller free to name any family at
    will would walk straight around it: ranking a bank run against pandemics, or reviving the search
    for an event type that deliberately maps to nothing at all. Both would return the nearest
    vectors in an unrelated corpus and present them as history's verdict.

    So an incompatible request is a caller bug and raises. It is emphatically not a no-match: the
    caller must be able to tell "I asked wrongly" from "history has no analogy for this".
    """
    known, unknown = partition_episode_types(requested)
    if unknown:
        msg = f"unknown episode types: {', '.join(unknown)}"
        raise AnalogyRequestError(msg)
    if not known:
        msg = "episode_types was supplied but empty; pass None to use the event's own family"
        raise AnalogyRequestError(msg)
    incompatible = known - allowed
    if incompatible:
        msg = (
            f"episode type(s) {', '.join(sorted(incompatible))} are not compatible with event type "
            f"{event_type!r} (compatible: {', '.join(sorted(allowed))}); an episode_types override "
            "narrows the search, it never widens it"
        )
        raise AnalogyRequestError(msg)
    return known


def _abstain(
    query: AnalogyQuery, *, considered: int, best_similarity: float | None
) -> AnalogyRetrievalResult:
    return AnalogyRetrievalResult(
        status=RetrievalStatus.NO_RELIABLE_ANALOGY,
        message=NO_RELIABLE_ANALOGY,
        episode_types=tuple(sorted(query.episode_types)),
        model=query.model,
        model_version=query.model_version,
        top_k=query.top_k,
        min_similarity=query.min_similarity,
        considered_count=considered,
        best_similarity=best_similarity,
    )


def retrieve_analogy_candidates(session: Session, query: AnalogyQuery) -> AnalogyRetrievalResult:
    """Retrieve, threshold, and only then attach outcomes. The core both item 3 and item 4 call.

    Takes a vector, not an event, so the gold-set evaluator can score a labelled pair without
    inserting anything. Never calls an embedding API.
    """
    rows = session.execute(_ranking_statement(query)).all()
    candidates = [_candidate(row, query.regime_tags) for row in rows]
    best_similarity = max((c.similarity for c in candidates), default=None)

    matched = [c for c in candidates if c.similarity >= query.min_similarity]
    if not matched:
        # The spec's explicit, allowed answer. Not an exception, and not the near-misses.
        return _abstain(query, considered=len(candidates), best_similarity=best_similarity)

    outcomes = _load_outcomes(session, [c.episode_id for c in matched])
    parents = _load_parents(
        session, sorted({c.parent_episode_id for c in matched if c.parent_episode_id})
    )
    matches = tuple(
        MatchedAnalogy(
            candidate=candidate,
            outcome=outcomes.get(
                candidate.episode_id,
                HistoricalOutcome(
                    episode_id=candidate.episode_id,
                    outcome_summary=None,
                    outcomes=(),
                    resolution_mechanism=None,
                ),
            ),
            parent=parents.get(candidate.parent_episode_id)
            if candidate.parent_episode_id
            else None,
        )
        for candidate in matched
    )
    return AnalogyRetrievalResult(
        status=RetrievalStatus.MATCHED,
        message=f"{len(matches)} analog episode(s) at or above {query.min_similarity}",
        episode_types=tuple(sorted(query.episode_types)),
        model=query.model,
        model_version=query.model_version,
        top_k=query.top_k,
        min_similarity=query.min_similarity,
        matches=matches,
        distribution=summarize_outcomes(matches),
        considered_count=len(candidates),
        best_similarity=best_similarity,
    )


def retrieve_analogies_for_event(
    session: Session,
    event_id: uuid.UUID,
    *,
    regime_tags: Iterable[str] | None = None,
    geographies: Iterable[str] | None = None,
    industries: Iterable[str] | None = None,
    episode_types: Iterable[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
) -> AnalogyRetrievalResult:
    """Retrieve analogies for a persisted event, in the configured embedding space.

    Reads the event's stored vector; it never embeds. An event that has not been embedded yet is
    a precondition failure the caller can act on (run the embedding task), not a no-match, so it
    raises. An event whose ``event_type`` names no episode family abstains instead: the honest
    answer to "what is this like?" for an unclassified event is nothing, never the nearest rows in
    an unrelated corpus. A caller who *asks* for an unknown, empty, or incompatible family is a
    third thing again -- a bug -- and :func:`_narrowed_families` raises on it.

    ``episode_types`` narrows the search within the event's own family set. It cannot widen it, and
    it cannot resurrect a search the event's type does not license: the compatibility gate runs
    first and is not optional.
    """
    model, model_version = resolve_embedding_identity(
        None, embedding_model, embedding_model_version
    )
    event = session.get(Event, event_id)
    if event is None:
        msg = f"event {event_id} does not exist"
        raise EventNotFoundError(msg)

    # The gate runs before the override is even looked at, so no argument can get past it.
    allowed = episode_types_for_event_type(event.event_type)
    if not allowed:
        return AnalogyRetrievalResult(
            status=RetrievalStatus.UNSUPPORTED_EVENT_TYPE,
            message=(
                f"{NO_RELIABLE_ANALOGY}: event type {event.event_type!r} maps to no episode family"
            ),
            episode_types=(),
            model=model,
            model_version=model_version,
            top_k=top_k,
            min_similarity=min_similarity,
        )

    families = (
        allowed
        if episode_types is None
        else _narrowed_families(episode_types, allowed, event.event_type)
    )

    vector = session.scalars(
        select(EventEmbedding.embedding).where(
            EventEmbedding.event_id == event_id,
            EventEmbedding.model == model,
            EventEmbedding.model_version == model_version,
        )
    ).first()
    if vector is None:
        msg = f"event {event_id} has no embedding in model space {model}@{model_version}"
        raise EventEmbeddingMissingError(msg)

    query = AnalogyQuery(
        vector=tuple(float(value) for value in vector),
        episode_types=frozenset(families),
        model=model,
        model_version=model_version,
        regime_tags=frozenset(regime_tags or ()),
        geographies=None if geographies is None else frozenset(geographies),
        industries=None if industries is None else frozenset(industries),
        top_k=top_k,
        min_similarity=min_similarity,
    )
    return retrieve_analogy_candidates(session, query)


__all__ = ["retrieve_analogies_for_event", "retrieve_analogy_candidates"]
