"""The two rules that run before any vector is compared (historical-episode spec, "Retrieval").

Pure vocabulary and regime logic: no database, no session, no vector. What is proved here is
what retrieval is *allowed to look at* -- which is the whole of the hard-filter contract.
"""

from __future__ import annotations

import pytest

from db.models.core import EPISODE_TYPES
from services.analogies.compatibility import (
    canonical_episode_type,
    episode_types_for_event_type,
    normalize_token,
    partition_episode_types,
    regime_caveats,
)

CURRENT = ("post_QE", "post_dodd_frank")


# --- episode-type compatibility -------------------------------------------------------
@pytest.mark.parametrize("episode_type", EPISODE_TYPES)
def test_every_schema_episode_type_is_its_own_family(episode_type: str) -> None:
    assert canonical_episode_type(episode_type) == episode_type
    assert episode_types_for_event_type(episode_type) == frozenset({episode_type})


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("Banking Stress", "banking_stress"),
        ("banking-stress", "banking_stress"),
        ("  BANKING_STRESS  ", "banking_stress"),
        ("supply_chain", "supply_shock"),  # the alias table, not a widening
        ("bank_run", "banking_stress"),
        ("sovereign_default", "sovereign_debt"),
        ("corporate_distress", "company_distress"),
    ],
)
def test_known_tokens_normalize_onto_exactly_one_family(event_type: str, expected: str) -> None:
    assert episode_types_for_event_type(event_type) == frozenset({expected})


@pytest.mark.parametrize(
    "event_type", [None, "", "   ", "earthquake", "celebrity_gossip", "crisis"]
)
def test_an_unknown_event_type_maps_to_nothing_rather_than_to_all_of_history(
    event_type: str | None,
) -> None:
    # The failure this forbids: an unclassified event silently ranked against every episode.
    assert canonical_episode_type(event_type) is None
    assert episode_types_for_event_type(event_type) == frozenset()


def test_the_map_never_widens_across_families() -> None:
    """A company_distress event is not admitted to industry_shock because one can spill over."""
    for episode_type in EPISODE_TYPES:
        assert episode_types_for_event_type(episode_type) == frozenset({episode_type})


def test_partitioning_separates_the_recognized_from_the_unrecognized() -> None:
    known, unknown = partition_episode_types(["Banking Stress", "supply_chain", "earthquake", ""])

    assert known == frozenset({"banking_stress", "supply_shock"})
    assert unknown == ("", "earthquake")  # deterministic, sorted


def test_normalize_token_folds_case_and_separators() -> None:
    assert normalize_token(" Supply-Shock ") == "supply_shock"
    assert normalize_token("post QE") == "post_qe"


# --- the regime gate ------------------------------------------------------------------
def test_sharing_one_current_regime_tag_is_normal() -> None:
    required, reasons = regime_caveats(CURRENT, ["post_QE", "pre_social_media"])

    assert required is False
    assert reasons == ()


def test_an_episode_sharing_no_current_tag_stays_a_candidate_but_must_be_caveated() -> None:
    required, reasons = regime_caveats(CURRENT, ["pre_QE", "pre_fiat"])

    assert required is True
    assert reasons == (
        "episode shares none of the current regime tags "
        "(current: post_dodd_frank, post_qe; episode: pre_fiat, pre_qe)",
    )


def test_an_untagged_episode_is_unverified_not_comparable() -> None:
    for tags in ([], None):
        required, reasons = regime_caveats(CURRENT, tags)

        assert required is True
        assert reasons == ("episode carries no regime tags; regime comparability is unverified",)


@pytest.mark.parametrize("current", [(), None])
def test_no_current_regime_tags_cannot_manufacture_a_mismatch(current: tuple[()] | None) -> None:
    for episode_tags in (["pre_fiat"], [], None):
        assert regime_caveats(current, episode_tags) == (False, ())


def test_the_gate_is_case_and_separator_insensitive() -> None:
    assert regime_caveats(["post_QE"], ["POST-QE"]) == (False, ())


def test_reasons_are_deterministic_regardless_of_tag_order() -> None:
    first = regime_caveats(["post_QE", "post_dodd_frank"], ["pre_QE", "pre_fiat"])
    second = regime_caveats(["post_dodd_frank", "post_QE"], ["pre_fiat", "pre_QE"])

    assert first == second
