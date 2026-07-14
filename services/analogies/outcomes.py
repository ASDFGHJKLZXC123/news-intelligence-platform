"""Base rates over matched episodes (historical-episode spec, "Retrieval").

Pure, and deliberately dumb: it counts. "Conflicting analog outcomes are reported as a
distribution, never averaged into one narrative" -- so there is no weighting by similarity, no
majority vote, no single predicted outcome anywhere in this module. Five episodes that ended
three different ways come back as three tallies, and the caller has to show all three.

Two kinds of episode that a naive count would hide are surfaced instead:

* the **untagged** -- curated with no ``outcomes[]`` at all. They stay in the denominator, so
  "2 of 5 -> failure" cannot silently become "2 of 2" by dropping what we do not know;
* the **multi-tagged** -- a run that was bailed out *and* ended in failure. Both tags are counted,
  which is why the rates can sum past 1.0. That is the honest shape of the data, not an error.
"""

from __future__ import annotations

from collections.abc import Sequence

from db.models.core import EPISODE_OUTCOMES
from services.analogies.contracts import MatchedAnalogy, OutcomeDistribution, OutcomeTally

_RATE_PRECISION = 4

#: Canonical tag order (db.models EPISODE_OUTCOMES), so equal counts always tie-break the same.
_CANONICAL_ORDER = {outcome: index for index, outcome in enumerate(EPISODE_OUTCOMES)}


def _rate(count: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(count / denominator, _RATE_PRECISION)


def summarize_outcomes(matches: Sequence[MatchedAnalogy]) -> OutcomeDistribution:
    """Count what the matched episodes became. The denominator is every match, always."""
    matched_count = len(matches)
    counts: dict[str, int] = {}
    untagged = 0
    multi_tagged = 0
    counterexamples = 0

    for match in matches:
        tags = tuple(dict.fromkeys(match.outcome.outcomes))  # one episode counts once per tag
        if not tags:
            untagged += 1
        if len(tags) > 1:
            multi_tagged += 1
        for tag in tags:
            counts[tag] = counts.get(tag, 0) + 1
        if match.candidate.is_counterexample:
            counterexamples += 1

    tallies = tuple(
        OutcomeTally(outcome=outcome, count=count, rate=_rate(count, matched_count))
        for outcome, count in sorted(
            counts.items(),
            key=lambda item: (
                -item[1],
                _CANONICAL_ORDER.get(item[0], len(_CANONICAL_ORDER)),
                item[0],
            ),
        )
    )
    return OutcomeDistribution(
        matched_count=matched_count,
        tallies=tallies,
        untagged_count=untagged,
        untagged_rate=_rate(untagged, matched_count),
        multi_tagged_count=multi_tagged,
        counterexample_count=counterexamples,
        counterexample_rate=_rate(counterexamples, matched_count),
    )


__all__ = ["summarize_outcomes"]
