"""Unit tests for ADR 0006 entity redirect history: renames and mergers as data operations."""

from __future__ import annotations

import datetime
import uuid

import pytest

from db.models import EntityProfile, EntityRedirect
from services.provider_data import (
    RedirectAmbiguousError,
    RedirectCycleError,
    RedirectTargetMissingError,
    RedirectTooDeepError,
    entity_redirect_history,
    redirect_chain,
    resolve_entity_redirect,
    upsert_entity_redirect,
)
from services.provider_data.common import normalize_name
from services.provider_data.entity_redirects import REDIRECT_MAX_DEPTH
from tests.unit.test_provider_data_services import FakeSession

MERGED = datetime.date(2023, 4, 12)
RENAMED = datetime.date(2023, 7, 24)
SETTLED = datetime.date(2024, 1, 9)


def _profile(session: FakeSession, name: str) -> EntityProfile:
    profile = EntityProfile(
        id=uuid.uuid4(),
        canonical_name=name,
        normalized_name=normalize_name(name),
        entity_type="company",
    )
    session.add(profile)
    return profile


def _seed_redirect(
    session: FakeSession,
    old_entity_id: uuid.UUID,
    new_entity_id: uuid.UUID,
    effective_date: datetime.date = MERGED,
) -> EntityRedirect:
    """A row written straight to the table, the way a bulk load or a restored dump writes one.

    These rows never met ``upsert_entity_redirect``'s guards, which is exactly why the traversal
    has its own: the pathologies below are unreachable through the service API and reachable
    through the database.
    """

    redirect = EntityRedirect(
        id=uuid.uuid4(),
        old_entity_id=old_entity_id,
        new_entity_id=new_entity_id,
        effective_date=effective_date,
    )
    session.add(redirect)
    return redirect


def _hops(session: FakeSession, count: int) -> list[EntityProfile]:
    """``count`` hops through ``count + 1`` profiles, recorded hop by hop as an operator would."""

    profiles = [_profile(session, f"Co {index}") for index in range(count + 1)]
    for index in range(count):
        upsert_entity_redirect(
            session,
            old_entity_id=profiles[index].id,
            new_entity_id=profiles[index + 1].id,
            effective_date=MERGED,
        )
    return profiles


def test_a_redirect_records_the_rename_with_its_reason_and_effective_date() -> None:
    session = FakeSession()
    twitter = _profile(session, "Twitter, Inc.")
    x_corp = _profile(session, "X Corp.")

    redirect, created = upsert_entity_redirect(
        session,
        old_entity_id=twitter.id,
        new_entity_id=x_corp.id,
        effective_date=RENAMED,
        reason="rename",
    )

    assert created is True
    assert (redirect.old_entity_id, redirect.new_entity_id) == (twitter.id, x_corp.id)
    assert (redirect.reason, redirect.effective_date) == ("rename", RENAMED)
    # The linker follows the redirect at resolve time; the old entity is not deleted or merged.
    assert resolve_entity_redirect(session, twitter.id) == x_corp.id
    assert resolve_entity_redirect(session, x_corp.id) == x_corp.id
    assert len(session.all_of(EntityProfile)) == 2


def test_an_effective_date_may_be_passed_as_an_iso_string() -> None:
    session = FakeSession()
    old = _profile(session, "Old Co")
    new = _profile(session, "New Co")

    redirect, _ = upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=new.id, effective_date="2023-07-24"
    )

    assert redirect.effective_date == RENAMED


def test_rerunning_the_same_redirect_is_idempotent() -> None:
    session = FakeSession()
    old = _profile(session, "Old Co")
    new = _profile(session, "New Co")

    first, created = upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=new.id, effective_date=RENAMED, reason="rename"
    )
    second, recreated = upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=new.id, effective_date=RENAMED, reason="rename"
    )

    assert (created, recreated) == (True, False)
    assert second is first
    assert len(session.all_of(EntityRedirect)) == 1


def test_history_is_never_collapsed_when_an_entity_moves_again() -> None:
    session = FakeSession()
    first_name = _profile(session, "Old Co")
    interim = _profile(session, "Interim Co")
    current = _profile(session, "Current Co")

    upsert_entity_redirect(
        session,
        old_entity_id=first_name.id,
        new_entity_id=interim.id,
        effective_date=MERGED,
        reason="merger",
    )
    upsert_entity_redirect(
        session,
        old_entity_id=interim.id,
        new_entity_id=current.id,
        effective_date=RENAMED,
        reason="rename",
    )

    # Both hops survive as rows, and resolution walks the chain to the live entity.
    assert len(session.all_of(EntityRedirect)) == 2
    assert redirect_chain(session, first_name.id) == [first_name.id, interim.id, current.id]
    assert resolve_entity_redirect(session, first_name.id) == current.id
    # Resolving as of a date before the second hop stops at the entity that was live then.
    assert resolve_entity_redirect(session, first_name.id, as_of=MERGED) == interim.id
    assert resolve_entity_redirect(session, first_name.id, as_of=datetime.date(2023, 1, 1)) == (
        first_name.id
    )


def test_a_repointed_redirect_keeps_the_superseded_row_and_the_latest_one_wins() -> None:
    session = FakeSession()
    old = _profile(session, "Old Co")
    first_target = _profile(session, "First Target")
    second_target = _profile(session, "Second Target")

    upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=first_target.id, effective_date=MERGED
    )
    upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=second_target.id, effective_date=RENAMED
    )

    history = entity_redirect_history(session, old.id)
    assert [row.new_entity_id for row in history] == [first_target.id, second_target.id]
    assert resolve_entity_redirect(session, old.id) == second_target.id
    assert resolve_entity_redirect(session, old.id, as_of=MERGED) == first_target.id


def test_a_new_effective_date_for_the_same_pair_is_a_new_row() -> None:
    session = FakeSession()
    old = _profile(session, "Old Co")
    new = _profile(session, "New Co")

    upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=new.id, effective_date=MERGED
    )
    _, created = upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=new.id, effective_date=RENAMED
    )

    assert created is True
    assert len(entity_redirect_history(session, old.id)) == 2


def test_a_self_redirect_is_rejected() -> None:
    session = FakeSession()
    entity = _profile(session, "Old Co")

    with pytest.raises(ValueError, match="itself"):
        upsert_entity_redirect(
            session,
            old_entity_id=entity.id,
            new_entity_id=entity.id,
            effective_date=RENAMED,
        )

    assert session.all_of(EntityRedirect) == []


def test_a_direct_cycle_is_rejected() -> None:
    session = FakeSession()
    old = _profile(session, "Old Co")
    new = _profile(session, "New Co")
    upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=new.id, effective_date=MERGED
    )

    with pytest.raises(ValueError, match="cycle"):
        upsert_entity_redirect(
            session, old_entity_id=new.id, new_entity_id=old.id, effective_date=RENAMED
        )

    assert len(session.all_of(EntityRedirect)) == 1
    assert resolve_entity_redirect(session, old.id) == new.id


def test_an_indirect_cycle_is_rejected() -> None:
    session = FakeSession()
    first = _profile(session, "A Co")
    second = _profile(session, "B Co")
    third = _profile(session, "C Co")
    upsert_entity_redirect(
        session, old_entity_id=first.id, new_entity_id=second.id, effective_date=MERGED
    )
    upsert_entity_redirect(
        session, old_entity_id=second.id, new_entity_id=third.id, effective_date=MERGED
    )

    # C -> A would close the loop A -> B -> C -> A and make resolution non-terminating.
    with pytest.raises(ValueError, match="cycle"):
        upsert_entity_redirect(
            session, old_entity_id=third.id, new_entity_id=first.id, effective_date=RENAMED
        )

    assert len(session.all_of(EntityRedirect)) == 2
    assert resolve_entity_redirect(session, first.id) == third.id


def test_both_endpoints_must_be_existing_entity_profiles() -> None:
    session = FakeSession()
    existing = _profile(session, "Old Co")
    missing = uuid.uuid4()

    with pytest.raises(ValueError, match="existing entity profile"):
        upsert_entity_redirect(
            session, old_entity_id=existing.id, new_entity_id=missing, effective_date=RENAMED
        )
    with pytest.raises(ValueError, match="existing entity profile"):
        upsert_entity_redirect(
            session, old_entity_id=missing, new_entity_id=existing.id, effective_date=RENAMED
        )

    assert session.all_of(EntityRedirect) == []


def test_an_effective_date_is_required() -> None:
    session = FakeSession()
    old = _profile(session, "Old Co")
    new = _profile(session, "New Co")

    with pytest.raises(ValueError, match="effective_date"):
        upsert_entity_redirect(
            session, old_entity_id=old.id, new_entity_id=new.id, effective_date=""
        )


# --- Traversal fails closed ----------------------------------------------------
def test_an_entity_nothing_redirects_away_from_resolves_to_itself() -> None:
    session = FakeSession()
    entity = _profile(session, "Old Co")

    assert redirect_chain(session, entity.id) == [entity.id]
    assert resolve_entity_redirect(session, entity.id) == entity.id
    # No redirect was followed, so no endpoint was looked up: an id with no redirect out of it
    # comes back as it came in, whatever else the caller holds it for.
    unknown = uuid.uuid4()
    assert resolve_entity_redirect(session, unknown) == unknown


def test_a_chain_of_exactly_the_maximum_depth_resolves() -> None:
    session = FakeSession()
    profiles = _hops(session, REDIRECT_MAX_DEPTH)

    chain = redirect_chain(session, profiles[0].id)

    assert chain == [profile.id for profile in profiles]
    assert len(chain) == REDIRECT_MAX_DEPTH + 1  # the origin, plus one node per hop
    assert resolve_entity_redirect(session, profiles[0].id) == profiles[-1].id


def test_a_chain_one_hop_past_the_maximum_depth_fails_instead_of_truncating() -> None:
    session = FakeSession()
    profiles = _hops(session, REDIRECT_MAX_DEPTH + 1)

    with pytest.raises(RedirectTooDeepError, match="maximum depth"):
        redirect_chain(session, profiles[0].id)
    with pytest.raises(RedirectTooDeepError):
        # Never the last node it could see: the middle of a chain is not an endpoint.
        resolve_entity_redirect(session, profiles[0].id)

    # One entity along, the same chain is within the limit and resolves normally.
    assert resolve_entity_redirect(session, profiles[1].id) == profiles[-1].id


def test_a_direct_cycle_written_around_the_upsert_guard_fails_closed() -> None:
    session = FakeSession()
    old = _profile(session, "Old Co")
    new = _profile(session, "New Co")
    upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=new.id, effective_date=MERGED
    )
    _seed_redirect(session, new.id, old.id, RENAMED)

    with pytest.raises(RedirectCycleError, match="cycle") as raised:
        redirect_chain(session, old.id)

    assert raised.value.entity_id == old.id
    assert raised.value.chain == (old.id, new.id, old.id)
    # The typed errors stay ValueErrors, so callers written against the old contract still catch.
    assert isinstance(raised.value, ValueError)
    with pytest.raises(RedirectCycleError):
        resolve_entity_redirect(session, new.id)


def test_a_transitive_cycle_fails_closed_from_every_entity_on_it() -> None:
    session = FakeSession()
    first = _profile(session, "A Co")
    second = _profile(session, "B Co")
    third = _profile(session, "C Co")
    upsert_entity_redirect(
        session, old_entity_id=first.id, new_entity_id=second.id, effective_date=MERGED
    )
    upsert_entity_redirect(
        session, old_entity_id=second.id, new_entity_id=third.id, effective_date=MERGED
    )
    _seed_redirect(session, third.id, first.id, RENAMED)

    for entity in (first, second, third):
        with pytest.raises(RedirectCycleError):
            redirect_chain(session, entity.id)
        with pytest.raises(RedirectCycleError):
            resolve_entity_redirect(session, entity.id)


def test_a_self_redirect_that_got_past_the_check_constraint_fails_closed() -> None:
    session = FakeSession()
    entity = _profile(session, "Old Co")
    _seed_redirect(session, entity.id, entity.id)

    # Resolving it to itself would be the right *answer* read out of corrupt data, and would hide
    # the row that should never have existed.
    with pytest.raises(RedirectCycleError):
        resolve_entity_redirect(session, entity.id)
    with pytest.raises(RedirectCycleError):
        redirect_chain(session, entity.id)


def test_a_redirect_onto_a_profile_that_is_not_there_fails_closed() -> None:
    session = FakeSession()
    old = _profile(session, "Old Co")
    deleted = uuid.uuid4()
    _seed_redirect(session, old.id, deleted)

    with pytest.raises(RedirectTargetMissingError, match="live entity") as raised:
        redirect_chain(session, old.id)

    assert raised.value.chain == (old.id, deleted)
    with pytest.raises(RedirectTargetMissingError):
        resolve_entity_redirect(session, old.id)


def test_competing_redirects_on_one_effective_date_are_never_guessed_between() -> None:
    session = FakeSession()
    old = _profile(session, "Old Co")
    left = _profile(session, "Left Co")
    right = _profile(session, "Right Co")

    # The unique key is (old, new, effective_date), so both rows are legal and neither one wins.
    upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=left.id, effective_date=RENAMED
    )
    upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=right.id, effective_date=RENAMED
    )

    with pytest.raises(RedirectAmbiguousError, match="competing redirects"):
        redirect_chain(session, old.id)
    with pytest.raises(RedirectAmbiguousError):
        resolve_entity_redirect(session, old.id)
    # Before that date neither row is in force, so the ambiguity is not yet reachable.
    assert resolve_entity_redirect(session, old.id, as_of=MERGED) == old.id

    # A later hop settles it, because only the latest date in force is read.
    upsert_entity_redirect(
        session, old_entity_id=old.id, new_entity_id=left.id, effective_date=SETTLED
    )
    assert resolve_entity_redirect(session, old.id) == left.id
    assert len(entity_redirect_history(session, old.id)) == 3


def test_an_ambiguity_further_down_the_chain_fails_the_whole_walk() -> None:
    session = FakeSession()
    start = _profile(session, "Start Co")
    old = _profile(session, "Old Co")
    left = _profile(session, "Left Co")
    right = _profile(session, "Right Co")
    _seed_redirect(session, start.id, old.id, MERGED)
    _seed_redirect(session, old.id, left.id, RENAMED)
    _seed_redirect(session, old.id, right.id, RENAMED)

    with pytest.raises(RedirectAmbiguousError) as raised:
        resolve_entity_redirect(session, start.id)

    assert raised.value.entity_id == start.id
    assert raised.value.chain == (start.id, old.id)


def test_an_upsert_refuses_an_endpoint_whose_own_chain_does_not_resolve() -> None:
    session = FakeSession()
    start = _profile(session, "Start Co")
    old = _profile(session, "Old Co")
    _seed_redirect(session, old.id, uuid.uuid4())  # old now redirects into a profile that is gone

    # Pointing a live entity at one that resolves to nothing would bury a second entity in the
    # same pathology, and the write is refused rather than recorded.
    with pytest.raises(ValueError, match="does not resolve"):
        upsert_entity_redirect(
            session, old_entity_id=start.id, new_entity_id=old.id, effective_date=RENAMED
        )

    assert len(session.all_of(EntityRedirect)) == 1
