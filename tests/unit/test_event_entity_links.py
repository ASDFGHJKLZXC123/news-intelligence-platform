"""Accepted event/entity links, assertion roles, and risk eligibility (ADR 0005)."""

from __future__ import annotations

import uuid

import pytest

from db.models import EventEntity
from services.entities.event_links import (
    RISK_ELIGIBLE_ROLES,
    ROLE_ASSERTED,
    ROLE_DENIED,
    ROLE_SPECULATIVE,
    event_entity_links,
    merge_roles,
    persist_event_entity_link,
    risk_eligible_event_links,
    role_for_assertion,
)
from services.nlp.assertions import AssertionStatus
from tests.unit.entity_linking_fakes import FakeSession

EVENT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
ACME = uuid.UUID("11111111-1111-4111-8111-111111111111")
ACORN = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _persist(
    session: FakeSession,
    entity_id: uuid.UUID,
    *,
    confidence: float,
    status: AssertionStatus = AssertionStatus.ASSERTED,
    event_id: uuid.UUID = EVENT,
) -> EventEntity:
    return persist_event_entity_link(
        session,
        event_id=event_id,
        entity_profile_id=entity_id,
        confidence_score=confidence,
        assertion_status=status,
    )


# --- The role vocabulary carries the assertion status ----------------------------------


def test_every_assertion_status_maps_to_a_stable_role() -> None:
    assert role_for_assertion(AssertionStatus.ASSERTED) == ROLE_ASSERTED
    assert role_for_assertion(AssertionStatus.DENIED) == ROLE_DENIED
    assert role_for_assertion(AssertionStatus.SPECULATIVE) == ROLE_SPECULATIVE
    assert RISK_ELIGIBLE_ROLES == {ROLE_ASSERTED}


def test_an_accepted_mention_persists_one_row_with_its_score_and_role() -> None:
    session = FakeSession()

    link = _persist(session, ACME, confidence=0.91)

    assert (link.event_id, link.entity_profile_id) == (EVENT, ACME)
    assert link.confidence_score == 0.91
    assert link.role == ROLE_ASSERTED
    # Flushed on insert, so the next mention of the same entity reads it back instead of
    # adding a second row that only fails at commit on the primary key.
    assert session.flushes == 1
    assert len(session.all_of(EventEntity)) == 1


@pytest.mark.parametrize("score", [-0.01, 1.01])
def test_a_confidence_outside_the_check_constraint_is_refused(score: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        _persist(FakeSession(), ACME, confidence=score)


# --- Reruns and repeat mentions merge; they never duplicate or lower --------------------


def test_rerunning_the_same_mention_changes_nothing() -> None:
    session = FakeSession()

    first = _persist(session, ACME, confidence=0.91)
    second = _persist(session, ACME, confidence=0.91)

    assert first is second
    assert len(session.all_of(EventEntity)) == 1
    assert second.confidence_score == 0.91


def test_the_strongest_confidence_is_kept_and_never_lowered() -> None:
    session = FakeSession()

    _persist(session, ACME, confidence=0.88)
    link = _persist(session, ACME, confidence=0.61)

    assert link.confidence_score == 0.88
    assert len(session.all_of(EventEntity)) == 1


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (AssertionStatus.SPECULATIVE, AssertionStatus.ASSERTED, ROLE_ASSERTED),
        (AssertionStatus.ASSERTED, AssertionStatus.SPECULATIVE, ROLE_ASSERTED),
        (AssertionStatus.DENIED, AssertionStatus.ASSERTED, ROLE_ASSERTED),
        (AssertionStatus.ASSERTED, AssertionStatus.DENIED, ROLE_ASSERTED),
        (AssertionStatus.SPECULATIVE, AssertionStatus.DENIED, ROLE_DENIED),
        (AssertionStatus.DENIED, AssertionStatus.SPECULATIVE, ROLE_DENIED),
    ],
)
def test_one_asserted_mention_keeps_the_link_risk_eligible(
    first: AssertionStatus, second: AssertionStatus, expected: str
) -> None:
    """A company asserted once and speculated about once was still asserted about once."""

    session = FakeSession()

    _persist(session, ACME, confidence=0.9, status=first)
    _persist(session, ACME, confidence=0.9, status=second)

    links = event_entity_links(session, EVENT)
    assert [link.role for link in links] == [expected]
    assert links[0].is_risk_eligible is (expected == ROLE_ASSERTED)


def test_merging_is_order_independent_and_leaves_a_foreign_role_alone() -> None:
    assert merge_roles(None, ROLE_SPECULATIVE) == ROLE_SPECULATIVE
    # A role this module does not own carries no assertion semantics, so it is replaced.
    assert merge_roles("primary", ROLE_DENIED) == ROLE_DENIED
    assert merge_roles(ROLE_ASSERTED, ROLE_DENIED) == merge_roles(ROLE_DENIED, ROLE_ASSERTED)


# --- Risk eligibility is the ADR's exclusion rule, made queryable ------------------------


def test_only_asserted_links_are_risk_inputs_and_the_rest_stay_auditable() -> None:
    session = FakeSession()
    denied = uuid.UUID("33333333-3333-4333-8333-333333333333")

    _persist(session, ACME, confidence=0.9, status=AssertionStatus.ASSERTED)
    _persist(session, ACORN, confidence=0.95, status=AssertionStatus.SPECULATIVE)
    _persist(session, denied, confidence=0.99, status=AssertionStatus.DENIED)

    eligible = risk_eligible_event_links(session, EVENT)
    everything = event_entity_links(session, EVENT)

    # The speculative and denied links score higher, and are still not risk inputs.
    assert [link.entity_profile_id for link in eligible] == [ACME]
    assert len(everything) == 3
    assert {link.role for link in everything} == {ROLE_ASSERTED, ROLE_SPECULATIVE, ROLE_DENIED}


def test_links_are_returned_strongest_first_and_scoped_to_their_event() -> None:
    session = FakeSession()
    other_event = uuid.uuid4()

    _persist(session, ACME, confidence=0.62)
    _persist(session, ACORN, confidence=0.94)
    _persist(session, ACME, confidence=0.99, event_id=other_event)

    links = event_entity_links(session, EVENT)

    assert [link.entity_profile_id for link in links] == [ACORN, ACME]
    assert [link.confidence_score for link in links] == [0.94, 0.62]


def test_a_link_is_serializable() -> None:
    session = FakeSession()
    _persist(session, ACME, confidence=0.9)

    payload = event_entity_links(session, EVENT)[0].as_dict()

    assert payload == {
        "event_id": str(EVENT),
        "entity_profile_id": str(ACME),
        "role": ROLE_ASSERTED,
        "confidence_score": 0.9,
    }
