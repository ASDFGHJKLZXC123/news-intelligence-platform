"""Parent exposure propagation at scoring time (ADR 0005).

The ADR's rule, exactly: *"Subsidiary/brand mentions link to the subsidiary entity; impact
propagates to the parent via an explicit ``parent_of`` relation at scoring time, never by
aliasing the brand to the parent."*

So nothing here writes. A mention of a brand stays linked to the entity that owns the alias --
the child -- in ``event_entities``, and this module derives, read-only, the exposures its parents
carry *because of* that link. The derived exposures are marked ``propagated_parent`` and are
returned as values; they are never persisted as direct mentions, because they are not mentions.

Only risk-eligible (asserted) direct links propagate: a denied or speculative mention of a
subsidiary is not an exposure for the subsidiary, so it is not one for its parent either.

Confidence is transparent rather than clever. The ADR specifies no attenuation for propagation,
so none is invented: an exposure's confidence is the direct link's confidence multiplied by the
confidence of each ``parent_of`` edge on the path. An edge whose provider recorded no confidence
contributes 1.0 -- unknown is not the same as weak, and inventing a penalty for it would silently
re-weight the identity providers against each other.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from db.models import EntityRelationship
from services.entities.event_links import EventEntityLink, risk_eligible_event_links
from services.provider_data.common import find_all, json_safe

#: The relationship type ADR 0006 identity ingestion writes for every parent edge, from either
#: provider (GLEIF Level 2, Wikidata P749/P355). Rows always run parent -> child.
PARENT_RELATIONSHIP_TYPE: Final = "parent_of"

#: What a derived exposure is, so a consumer can never mistake one for a direct mention.
PROPAGATED_PARENT: Final = "propagated_parent"

#: A conservative ceiling on how far up an ownership chain one mention may propagate. Real
#: corporate structures are a handful of levels deep; anything past this is a data pathology,
#: and walking it would let one mention of one brand light up an entire conglomerate.
MAX_PARENT_DEPTH: Final[int] = 4

#: An edge whose provider recorded no confidence neither strengthens nor weakens the path.
DEFAULT_EDGE_CONFIDENCE: Final[float] = 1.0


@dataclass(frozen=True, slots=True)
class RelationshipEvidence:
    """One ``parent_of`` edge on the path, as the identity store holds it."""

    parent_entity_id: uuid.UUID
    child_entity_id: uuid.UUID
    relationship_type: str
    provider: str | None
    confidence_score: float | None
    valid_from: datetime.date | None
    valid_to: datetime.date | None

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


@dataclass(frozen=True, slots=True)
class ParentExposure:
    """A derived, non-persisted exposure a parent carries because a child was mentioned."""

    entity_profile_id: uuid.UUID  # the parent that carries the exposure
    source_entity_id: uuid.UUID  # the directly-linked child the exposure came from
    depth: int  # 1 = immediate parent
    confidence_score: float
    path: tuple[uuid.UUID, ...]  # child -> ... -> parent, inclusive
    evidence: tuple[RelationshipEvidence, ...]
    exposure_type: str = PROPAGATED_PARENT

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


@dataclass(frozen=True, slots=True)
class _Walk:
    """One partial path up the ownership chain, and the confidence accumulated along it."""

    path: tuple[uuid.UUID, ...]
    confidence: float
    evidence: tuple[RelationshipEvidence, ...]


def parent_exposures(
    session: Any,
    links: Sequence[EventEntityLink],
    *,
    as_of: datetime.date | None = None,
    max_depth: int = MAX_PARENT_DEPTH,
) -> tuple[ParentExposure, ...]:
    """Derive the parent exposures a set of direct links implies. Reads only; writes nothing.

    ``links`` is filtered to the risk-eligible ones here as well as at the call site, so a caller
    that hands over every link of an event cannot accidentally propagate a denial.
    """

    depth_limit = _bounded_depth(max_depth)
    exposures: dict[tuple[str, str], ParentExposure] = {}

    for link in sorted(links, key=lambda item: str(item.entity_profile_id)):
        if not link.is_risk_eligible:
            continue
        child = link.entity_profile_id
        frontier = [
            _Walk(path=(child,), confidence=_bounded(link.confidence_score or 0.0), evidence=())
        ]
        for depth in range(1, depth_limit + 1):
            next_frontier: list[_Walk] = []
            for walk in frontier:
                for edge in _parent_edges(session, walk.path[-1], as_of=as_of):
                    parent = edge.parent_entity_id
                    # Cycle guard: a store that says A owns B and B owns A must not be walked
                    # forever, and an entity is never its own parent's exposure.
                    if parent in walk.path:
                        continue
                    step = _Walk(
                        path=(*walk.path, parent),
                        confidence=_bounded(walk.confidence * _edge_confidence(edge)),
                        evidence=(*walk.evidence, _evidence(edge)),
                    )
                    next_frontier.append(step)
                    _keep_best(
                        exposures,
                        ParentExposure(
                            entity_profile_id=parent,
                            source_entity_id=child,
                            depth=depth,
                            confidence_score=step.confidence,
                            path=step.path,
                            evidence=step.evidence,
                        ),
                    )
            if not next_frontier:
                break
            frontier = next_frontier

    return tuple(sorted(exposures.values(), key=_exposure_sort_key))


def event_parent_exposures(
    session: Any,
    event_id: uuid.UUID,
    *,
    as_of: datetime.date | None = None,
    max_depth: int = MAX_PARENT_DEPTH,
) -> tuple[ParentExposure, ...]:
    """The parent exposures an event's asserted direct links imply, for the risk model to score."""

    return parent_exposures(
        session,
        risk_eligible_event_links(session, event_id),
        as_of=as_of,
        max_depth=max_depth,
    )


def _parent_edges(
    session: Any, child_id: uuid.UUID, *, as_of: datetime.date | None
) -> list[EntityRelationship]:
    """The ``parent_of`` edges *out of* a child, in force at ``as_of``, deterministically ordered.

    The direction is the one thing this must not get wrong: the rows run parent -> child, so the
    parents of ``child_id`` are the rows whose ``child_entity_id`` is it. Reading them the other
    way round would propagate a conglomerate's exposure down onto every subsidiary it owns.
    """

    edges = [
        edge
        for edge in find_all(
            session,
            EntityRelationship,
            child_entity_id=child_id,
            relationship_type=PARENT_RELATIONSHIP_TYPE,
        )
        if _is_valid(edge, as_of)
    ]
    return sorted(edges, key=lambda edge: (str(edge.parent_entity_id), str(edge.provider or "")))


def _is_valid(edge: EntityRelationship, as_of: datetime.date | None) -> bool:
    """True when the edge held on the date being scored. No date means no interval filter."""

    if as_of is None:
        return True
    if edge.valid_from is not None and edge.valid_from > as_of:
        return False
    return not (edge.valid_to is not None and edge.valid_to < as_of)


def _edge_confidence(edge: EntityRelationship) -> float:
    if edge.confidence_score is None:
        return DEFAULT_EDGE_CONFIDENCE
    return _bounded(float(edge.confidence_score))


def _evidence(edge: EntityRelationship) -> RelationshipEvidence:
    return RelationshipEvidence(
        parent_entity_id=edge.parent_entity_id,
        child_entity_id=edge.child_entity_id,
        relationship_type=edge.relationship_type,
        provider=edge.provider,
        confidence_score=None if edge.confidence_score is None else float(edge.confidence_score),
        valid_from=edge.valid_from,
        valid_to=edge.valid_to,
    )


def _keep_best(exposures: dict[tuple[str, str], ParentExposure], candidate: ParentExposure) -> None:
    """One exposure per (parent, originating child): two paths to one parent are one exposure.

    The strongest path wins, and ties break on the shortest path and then on the path itself, so
    the evidence a caller sees does not depend on which order the edges came back in.
    """

    key = (str(candidate.entity_profile_id), str(candidate.source_entity_id))
    current = exposures.get(key)
    if current is None or _path_rank(candidate) < _path_rank(current):
        exposures[key] = candidate


def _path_rank(exposure: ParentExposure) -> tuple[float, int, tuple[str, ...]]:
    return (
        -exposure.confidence_score,
        exposure.depth,
        tuple(str(entity_id) for entity_id in exposure.path),
    )


def _exposure_sort_key(exposure: ParentExposure) -> tuple[float, str, str]:
    return (
        -exposure.confidence_score,
        str(exposure.entity_profile_id),
        str(exposure.source_entity_id),
    )


def _bounded(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def _bounded_depth(max_depth: int) -> int:
    if not 1 <= max_depth <= MAX_PARENT_DEPTH:
        msg = f"max_depth must be within [1, {MAX_PARENT_DEPTH}], got {max_depth}"
        raise ValueError(msg)
    return max_depth
