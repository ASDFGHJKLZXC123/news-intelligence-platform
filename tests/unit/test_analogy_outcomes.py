"""Base rates over matched episodes: counts and rates, never one averaged narrative.

Pure math over the matched set. The cases that matter are the dishonest ones a simpler
implementation would produce: a denominator that quietly drops untagged episodes, a
multi-tagged episode counted once, a distribution collapsed to its modal outcome.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from services.analogies.contracts import (
    EpisodeCandidate,
    HistoricalOutcome,
    MatchedAnalogy,
)
from services.analogies.outcomes import summarize_outcomes


def _match(
    *outcomes: str, is_counterexample: bool = False, similarity: float = 0.9
) -> MatchedAnalogy:
    episode_id = uuid.uuid4()
    candidate = EpisodeCandidate(
        episode_id=episode_id,
        name="episode",
        episode_type="banking_stress",
        onset_date=datetime.date(2023, 3, 8),
        peak_date=None,
        end_date=None,
        onset_summary="Onset.",
        onset_indicators=None,
        geography="United States",
        affected_industries=("banking",),
        regime_tags=("post_QE",),
        is_counterexample=is_counterexample,
        source_refs=None,
        parent_episode_id=None,
        similarity=similarity,
        regime_caveats_required=False,
        regime_caveat_reasons=(),
    )
    outcome = HistoricalOutcome(
        episode_id=episode_id,
        outcome_summary=None,
        outcomes=tuple(outcomes),
        resolution_mechanism=None,
    )
    return MatchedAnalogy(candidate=candidate, outcome=outcome)


def _tallies(distribution: Any) -> list[tuple[str, int, float]]:
    return [(t.outcome, t.count, t.rate) for t in distribution.tallies]


def test_conflicting_outcomes_are_reported_as_a_distribution_not_averaged() -> None:
    distribution = summarize_outcomes(
        [_match("failure"), _match("failure"), _match("contained"), _match("recovery")]
    )

    assert distribution.matched_count == 4
    assert _tallies(distribution) == [
        ("failure", 2, 0.5),
        ("contained", 1, 0.25),
        ("recovery", 1, 0.25),
    ]


def test_untagged_episodes_stay_in_the_denominator() -> None:
    """Dropping them would turn "2 of 5 -> failure" into "2 of 2" and double the base rate."""
    distribution = summarize_outcomes([_match("failure"), _match("failure"), _match(), _match()])

    assert distribution.matched_count == 4
    assert (distribution.untagged_count, distribution.untagged_rate) == (2, 0.5)
    assert _tallies(distribution) == [("failure", 2, 0.5)]


def test_a_multi_tagged_episode_counts_once_per_tag_so_rates_may_exceed_one() -> None:
    distribution = summarize_outcomes([_match("bailout", "failure"), _match("failure")])

    assert distribution.multi_tagged_count == 1
    assert _tallies(distribution) == [("failure", 2, 1.0), ("bailout", 1, 0.5)]
    assert sum(tally.rate for tally in distribution.tallies) > 1.0  # honest, not normalized


def test_a_repeated_tag_on_one_episode_is_still_one_episode() -> None:
    distribution = summarize_outcomes([_match("failure", "failure")])

    assert _tallies(distribution) == [("failure", 1, 1.0)]
    assert distribution.multi_tagged_count == 0


def test_counterexamples_are_counted_and_rated_against_the_same_denominator() -> None:
    distribution = summarize_outcomes(
        [
            _match("contained", is_counterexample=True),
            _match("recovery", is_counterexample=True),
            _match("failure"),
            _match("failure"),
        ]
    )

    assert (distribution.counterexample_count, distribution.counterexample_rate) == (2, 0.5)
    assert distribution.matched_count == 4


def test_equal_counts_tie_break_on_the_canonical_outcome_order() -> None:
    first = summarize_outcomes([_match("recovery"), _match("contained")])
    second = summarize_outcomes([_match("contained"), _match("recovery")])

    # `contained` precedes `recovery` in db.models EPISODE_OUTCOMES, so the order is stable
    # whichever way the matches arrive.
    assert _tallies(first) == _tallies(second) == [("contained", 1, 0.5), ("recovery", 1, 0.5)]


def test_rates_are_rounded_deterministically() -> None:
    distribution = summarize_outcomes([_match("failure"), _match(), _match()])

    assert _tallies(distribution) == [("failure", 1, 0.3333)]
    assert distribution.untagged_rate == 0.6667


def test_an_empty_match_set_yields_zeroes_rather_than_dividing_by_zero() -> None:
    distribution = summarize_outcomes([])

    assert distribution.matched_count == 0
    assert distribution.tallies == ()
    assert (distribution.untagged_rate, distribution.counterexample_rate) == (0.0, 0.0)
