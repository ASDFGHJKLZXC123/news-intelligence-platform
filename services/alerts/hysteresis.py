"""Severity hysteresis for alerts (ADR 0010).

The canonical severity mapping (`risk_level_for_score`: 0-30 Low, 31-55 Medium, 56-75 High,
76-100 Critical) describes a *score*. An *alert's* severity may not use those bare
boundaries, or a score oscillating around one re-alerts daily. ADR 0010 therefore moves
level transitions onto raise/clear bands of +/-6 around each canonical floor:

===========  ==============  ==============  ==================
Band         Canonical floor  Enter (>=)     Clear (<=)
===========  ==============  ==============  ==================
Medium       31               37             25
High         56               62             50
Critical     76               82             70
===========  ==============  ==============  ==================

Both tables are derived from the same floors, so there is exactly one severity mapping in
the codebase. Between a band's clear and enter thresholds lies its *hold zone*
(e.g. 51-61 for High): a score in the hold zone leaves the current severity untouched.
"""

from __future__ import annotations

from decimal import Decimal

from db.models.enums import RiskLevel, risk_level_for_score
from services.alerts.numeric import Numeric, to_score

#: Canonical band floors, i.e. the boundaries `risk_level_for_score` uses.
CANONICAL_BAND_FLOOR: dict[RiskLevel, int] = {
    RiskLevel.MEDIUM: 31,
    RiskLevel.HIGH: 56,
    RiskLevel.CRITICAL: 76,
}

#: ADR 0010: raise/clear bands sit +/-6 around each canonical floor.
HYSTERESIS_MARGIN = 6

#: Enter a band only at floor + margin: Medium >= 37, High >= 62, Critical >= 82.
ENTER_THRESHOLD: dict[RiskLevel, int] = {
    level: floor + HYSTERESIS_MARGIN for level, floor in CANONICAL_BAND_FLOOR.items()
}

#: Clear a band only at floor - margin: Medium <= 25, High <= 50, Critical <= 70.
CLEAR_THRESHOLD: dict[RiskLevel, int] = {
    level: floor - HYSTERESIS_MARGIN for level, floor in CANONICAL_BAND_FLOOR.items()
}

#: Severity, weakest to strongest.
SEVERITY_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.CRITICAL,
)

_RANK: dict[RiskLevel, int] = {level: rank for rank, level in enumerate(SEVERITY_ORDER)}


def severity_rank(level: RiskLevel | str) -> int:
    """Return the ordinal of a severity, so callers can compare levels without an ordered enum."""
    return _RANK[coerce_level(level)]


def coerce_level(level: RiskLevel | str) -> RiskLevel:
    """Coerce a severity to :class:`RiskLevel`, rejecting unknown values."""
    try:
        return RiskLevel(level)
    except ValueError as exc:
        raise ValueError(f"unknown severity {level!r}") from exc


def canonical_severity(score: Numeric) -> RiskLevel:
    """Return the canonical (non-hysteretic) band of a score.

    This is the band used for *scores and signals*. An existing alert's severity is governed
    by :func:`next_severity` instead -- never by these bare boundaries.
    """
    return risk_level_for_score(float(to_score(score)))


#: The weakest severity that still sustains an alert. Below it, there is nothing to alert on.
ALERTING_FLOOR = RiskLevel.MEDIUM

#: The clear band that ends an alert outright: Medium's, at 25.
RESOLVE_CLEAR_THRESHOLD = CLEAR_THRESHOLD[ALERTING_FLOOR]


def applicable_clear_threshold(severity: RiskLevel | str) -> int | None:
    """Return the clear threshold an alert at ``severity`` must stay under to resolve.

    Only an alert that has already cleared out of every alerting band -- severity Low -- is
    under a clear band at all, and that band is Medium's (25). An alert still holding an
    alerting severity returns ``None``: it has somewhere to downgrade *to*, so it is not a
    candidate for an all-clear. This is what stops a High alert that decays to a score of 45
    from resolving while it is still a live Medium risk.
    """
    if severity_rank(severity) >= severity_rank(ALERTING_FLOOR):
        return None
    return RESOLVE_CLEAR_THRESHOLD


def is_below_clear_band(score: Numeric, severity: RiskLevel | str) -> bool:
    """Return whether an alert at ``severity`` scoring ``score`` is below its clear band.

    True only at Low severity *and* at or below 25. Low's hold zone (26-36) is deliberately
    not below the clear band: the score has rebounded above the threshold even though
    hysteresis still holds the severity at Low, so the resolve timer must not keep running.
    """
    threshold = applicable_clear_threshold(severity)
    return threshold is not None and to_score(score) <= threshold


def raise_target(score: Numeric) -> RiskLevel:
    """Return the strongest band whose *enter* threshold ``score`` satisfies."""
    value = to_score(score)
    for level in reversed(SEVERITY_ORDER[1:]):
        if value >= ENTER_THRESHOLD[level]:
            return level
    return RiskLevel.LOW


def _cleared_level(score: Decimal, current: RiskLevel) -> RiskLevel:
    """Drop ``current`` through every clear band ``score`` has fallen below."""
    level = current
    while level is not RiskLevel.LOW and score <= CLEAR_THRESHOLD[level]:
        level = SEVERITY_ORDER[_RANK[level] - 1]
    return level


def next_severity(score: Numeric, current: RiskLevel | str | None = None) -> RiskLevel:
    """Return the severity an alert holds at ``score``, given the severity it holds now.

    ``current=None`` means "no alert yet": a new alert must clear the *enter* threshold, so
    nothing below 37 raises one. For an existing alert the result is the stronger of

    * the band the score newly enters (``raise_target``), and
    * the band left after applying every clear band the score has fallen below.

    Consequences worth stating: a score in a hold zone preserves the current severity (High
    holds anywhere in 51-61); a multi-band jump resolves in a single step in either direction
    (Critical at score 20 goes straight to Low, Low at score 95 straight to Critical).
    """
    value = to_score(score)
    if current is None:
        return raise_target(value)
    held = _cleared_level(value, coerce_level(current))
    entered = raise_target(value)
    return entered if _RANK[entered] > _RANK[held] else held
