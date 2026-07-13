"""Parent exposure propagation at scoring time (ADR 0005).

The rule under test: a brand stays linked to the child that owns its alias, and the parent's
exposure is *derived* from the `parent_of` relation when the event is scored -- never written
back as a direct mention.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from db.models import EventEntity
from services.entities.event_links import ROLE_ASSERTED, EventEntityLink
from services.entities.parent_exposure import (
    MAX_PARENT_DEPTH,
    PROPAGATED_PARENT,
    event_parent_exposures,
    parent_exposures,
)
from services.nlp.assertions import AssertionStatus
from tests.unit.entity_linking_fakes import FakeSession, parent_edge, profile
from tests.unit.test_event_entity_links import _persist

EVENT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
AS_OF = datetime.date(2026, 6, 1)


def _link(entity_id: uuid.UUID, *, confidence: float = 0.9, role: str = ROLE_ASSERTED):
    return EventEntityLink(
        event_id=EVENT,
        entity_profile_id=entity_id,
        role=role,
        confidence_score=confidence,
    )


# --- Direction, marking, and evidence ---------------------------------------------------


def test_a_child_mention_exposes_its_parent_and_never_the_other_way_round() -> None:
    parent, child = profile("Example Holdings"), profile("Acme Brands")
    session = FakeSession(parent, child, parent_edge(parent, child, confidence_score=0.9))

    from_child = parent_exposures(session, [_link(child.id, confidence=0.8)])
    from_parent = parent_exposures(session, [_link(parent.id, confidence=0.8)])

    assert [item.entity_profile_id for item in from_child] == [parent.id]
    assert from_child[0].source_entity_id == child.id
    assert from_child[0].depth == 1
    assert from_child[0].exposure_type == PROPAGATED_PARENT
    # A mention of the *parent* does not expose its subsidiaries: exposure runs upward only.
    assert from_parent == ()


def test_the_exposure_carries_the_relationship_evidence_it_was_derived_from() -> None:
    parent, child = profile("Example Holdings"), profile("Acme Brands")
    edge = parent_edge(
        parent, child, provider="gleif", confidence_score=0.9, valid_from=datetime.date(2020, 1, 1)
    )
    session = FakeSession(parent, child, edge)

    exposure = parent_exposures(session, [_link(child.id, confidence=0.8)], as_of=AS_OF)[0]

    assert exposure.path == (child.id, parent.id)
    assert len(exposure.evidence) == 1
    evidence = exposure.evidence[0]
    assert (evidence.parent_entity_id, evidence.child_entity_id) == (parent.id, child.id)
    assert (evidence.provider, evidence.relationship_type) == ("gleif", "parent_of")
    assert evidence.confidence_score == 0.9


def test_confidence_is_the_direct_link_times_the_relationship_confidence() -> None:
    parent, child = profile("Example Holdings"), profile("Acme Brands")
    session = FakeSession(parent, child, parent_edge(parent, child, confidence_score=0.5))

    exposure = parent_exposures(session, [_link(child.id, confidence=0.8)])[0]

    assert exposure.confidence_score == pytest.approx(0.4)


def test_an_edge_with_no_recorded_confidence_does_not_attenuate() -> None:
    """Unknown is not weak. The ADR specifies no attenuation, so none is invented for a null."""

    parent, child = profile("Example Holdings"), profile("Acme Brands")
    session = FakeSession(parent, child, parent_edge(parent, child, confidence_score=None))

    exposure = parent_exposures(session, [_link(child.id, confidence=0.8)])[0]

    assert exposure.confidence_score == pytest.approx(0.8)


# --- Validity windows -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("valid_from", "valid_to", "expected"),
    [
        (None, None, True),
        (datetime.date(2020, 1, 1), None, True),
        (datetime.date(2020, 1, 1), datetime.date(2026, 12, 31), True),
        (datetime.date(2026, 7, 1), None, False),  # ownership began after the article
        (None, datetime.date(2026, 5, 31), False),  # ownership ended before it
    ],
)
def test_only_relationships_in_force_on_the_scored_date_propagate(
    valid_from: datetime.date | None, valid_to: datetime.date | None, expected: bool
) -> None:
    parent, child = profile("Example Holdings"), profile("Acme Brands")
    session = FakeSession(
        parent, child, parent_edge(parent, child, valid_from=valid_from, valid_to=valid_to)
    )

    exposures = parent_exposures(session, [_link(child.id)], as_of=AS_OF)

    assert bool(exposures) is expected


def test_a_relationship_that_is_not_a_parent_edge_never_propagates() -> None:
    other, child = profile("Some Bank"), profile("Acme Brands")
    session = FakeSession(other, child, parent_edge(other, child, relationship_type="creditor_of"))

    assert parent_exposures(session, [_link(child.id)]) == ()


# --- Multi-level, cycles, depth ---------------------------------------------------------


def test_a_grandparent_is_exposed_through_the_whole_chain() -> None:
    top, middle, child = profile("Top NV"), profile("Middle Ltd"), profile("Acme Brands")
    session = FakeSession(
        top,
        middle,
        child,
        parent_edge(middle, child, confidence_score=0.9),
        parent_edge(top, middle, confidence_score=0.5),
    )

    exposures = parent_exposures(session, [_link(child.id, confidence=1.0)])
    by_entity = {item.entity_profile_id: item for item in exposures}

    assert set(by_entity) == {middle.id, top.id}
    assert by_entity[middle.id].depth == 1
    assert by_entity[middle.id].confidence_score == pytest.approx(0.9)
    # The chain multiplies through: 1.0 * 0.9 * 0.5. No extra depth penalty is invented.
    assert by_entity[top.id].depth == 2
    assert by_entity[top.id].confidence_score == pytest.approx(0.45)
    assert by_entity[top.id].path == (child.id, middle.id, top.id)


def test_a_cycle_terminates_and_never_exposes_the_child_to_itself() -> None:
    parent, child = profile("Example Holdings"), profile("Acme Brands")
    session = FakeSession(
        parent,
        child,
        parent_edge(parent, child),
        parent_edge(child, parent),  # the store says each owns the other
    )

    exposures = parent_exposures(session, [_link(child.id)])

    assert [item.entity_profile_id for item in exposures] == [parent.id]


def test_the_walk_stops_at_the_bounded_depth() -> None:
    """One mention of one brand must not light up an entire conglomerate."""

    chain = [profile(f"Level {index}") for index in range(MAX_PARENT_DEPTH + 3)]
    edges = [parent_edge(chain[index + 1], chain[index]) for index in range(len(chain) - 1)]
    session = FakeSession(*chain, *edges)

    exposures = parent_exposures(session, [_link(chain[0].id)])
    limited = parent_exposures(session, [_link(chain[0].id)], max_depth=1)

    assert len(exposures) == MAX_PARENT_DEPTH
    assert max(item.depth for item in exposures) == MAX_PARENT_DEPTH
    assert [item.entity_profile_id for item in limited] == [chain[1].id]


@pytest.mark.parametrize("depth", [0, MAX_PARENT_DEPTH + 1])
def test_an_out_of_range_depth_is_refused(depth: int) -> None:
    with pytest.raises(ValueError, match="max_depth"):
        parent_exposures(FakeSession(), [_link(uuid.uuid4())], max_depth=depth)


# --- Dedup, order, and eligibility -------------------------------------------------------


def test_two_providers_naming_the_same_parent_are_one_exposure() -> None:
    parent, child = profile("Example Holdings"), profile("Acme Brands")
    session = FakeSession(
        parent,
        child,
        parent_edge(parent, child, provider="gleif", confidence_score=0.9),
        parent_edge(parent, child, provider="wikidata", confidence_score=0.6),
    )

    exposures = parent_exposures(session, [_link(child.id, confidence=1.0)])

    # One exposure for the pair, and it keeps the strongest evidence rather than the last read.
    assert len(exposures) == 1
    assert exposures[0].confidence_score == pytest.approx(0.9)
    assert exposures[0].evidence[0].provider == "gleif"


def test_exposures_are_ordered_deterministically_strongest_first() -> None:
    strong, weak = profile("Strong Holdings"), profile("Weak Holdings")
    first, second = profile("Acme Brands"), profile("Beta Brands")
    session = FakeSession(
        strong,
        weak,
        first,
        second,
        parent_edge(weak, first, confidence_score=0.4),
        parent_edge(strong, second, confidence_score=1.0),
    )

    exposures = parent_exposures(
        session, [_link(first.id, confidence=0.9), _link(second.id, confidence=0.9)]
    )

    assert [item.entity_profile_id for item in exposures] == [strong.id, weak.id]
    assert exposures == parent_exposures(
        session, [_link(second.id, confidence=0.9), _link(first.id, confidence=0.9)]
    )


@pytest.mark.parametrize("status", [AssertionStatus.DENIED, AssertionStatus.SPECULATIVE])
def test_a_denied_or_speculative_mention_exposes_no_parent(status: AssertionStatus) -> None:
    """ADR 0005: those links are not risk inputs, so they are not parent exposures either."""

    parent, child = profile("Example Holdings"), profile("Acme Brands")
    session = FakeSession(parent, child, parent_edge(parent, child))
    _persist(session, child.id, confidence=0.95, status=status)

    assert event_parent_exposures(session, EVENT) == ()

    # The same mention, asserted, does expose the parent -- the status is the only difference.
    _persist(session, child.id, confidence=0.95, status=AssertionStatus.ASSERTED)
    assert [item.entity_profile_id for item in event_parent_exposures(session, EVENT)] == [
        parent.id
    ]


def test_propagation_writes_nothing_at_all() -> None:
    """The parent's exposure is derived at scoring time; it is never an event_entities row."""

    parent, child = profile("Example Holdings"), profile("Acme Brands")
    session = FakeSession(parent, child, parent_edge(parent, child))
    _persist(session, child.id, confidence=0.95)

    exposures = event_parent_exposures(session, EVENT)

    assert [item.entity_profile_id for item in exposures] == [parent.id]
    # The only persisted link is still the child's direct mention. The parent has no row.
    rows = session.all_of(EventEntity)
    assert [row.entity_profile_id for row in rows] == [child.id]
    assert rows[0].role == ROLE_ASSERTED


def test_an_exposure_is_serializable() -> None:
    parent, child = profile("Example Holdings"), profile("Acme Brands")
    session = FakeSession(parent, child, parent_edge(parent, child, confidence_score=0.9))

    payload = parent_exposures(session, [_link(child.id, confidence=0.8)])[0].as_dict()

    assert payload["entity_profile_id"] == str(parent.id)
    assert payload["source_entity_id"] == str(child.id)
    assert payload["exposure_type"] == PROPAGATED_PARENT
    assert payload["confidence_score"] == pytest.approx(0.72)
    assert payload["evidence"][0]["provider"] == "gleif"
