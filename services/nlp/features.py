"""Pure event-feature computation feeding the ``event_risk_features`` contract.

P4 computes the structural features it can derive from a cluster (volume, source diversity,
severity, confidence). Entity/mechanism/risk_type fields are filled by later stages.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArticleRecord:
    """Minimal per-article inputs for event feature computation."""

    key: str
    source_id: str
    published_at: datetime.datetime
    title: str = ""
    source_authority: float | None = None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def event_severity_score(article_count: int, source_count: int) -> float:
    """0-100 severity, monotonic and saturating in volume and source diversity."""
    volume = math.tanh(article_count / 5.0)
    diversity = math.tanh(source_count / 3.0)
    return round(100.0 * (0.6 * volume + 0.4 * diversity), 2)


# --------------------------------------------------------------------------------------
# Hotness (report-generation spec, "Content selection")
# --------------------------------------------------------------------------------------
# Hotness answers a different question from severity, which is why the daily brief ranks on
# it and why it cannot be severity under another name. Severity asks how *big* an event is,
# and reads volume and source diversity alone. Hotness asks how *newsworthy* it is right
# now: how much coverage arrived, how fast it arrived, how many independent outlets carried
# it, and how much those outlets can be trusted. A slow-burning story of great size and a
# small story breaking this hour are not the same brief item, so the two scores are
# deliberately different functions of an overlapping set of inputs.

#: Component weights. Explicit, and they sum to exactly 1.0 -- which, with every component
#: bounded to [0, 1], is what holds the score inside 0-100 by construction rather than by a
#: clamp that would quietly hide a mis-weighting.
HOTNESS_WEIGHTS: dict[str, float] = {
    "volume": 0.30,
    "velocity": 0.25,
    "diversity": 0.20,
    "authority": 0.15,
    "novelty": 0.10,
}

#: What a component contributes when it has no measurement behind it: the exact midpoint.
#: An event is neither rewarded nor punished for what the pipeline has not measured about
#: it. This is the honest alternative to substituting a plausible-looking number -- and it
#: is load-bearing for `novelty`, which no stage computes yet (see `compute_event_features`).
NEUTRAL_COMPONENT = 0.5

#: Saturation scales, in the units of their inputs: articles, articles/hour, sources. Each
#: is the value at which `tanh` reaches ~0.76, i.e. where more of the same stops moving the
#: score much. Volume saturates later than severity's (8 vs 5 articles): hotness is meant to
#: keep separating heavily-covered events after severity has flattened.
VOLUME_SCALE = 8.0
VELOCITY_SCALE = 3.0
DIVERSITY_SCALE = 4.0

#: Coverage that arrives inside an hour counts as an hour. Article timestamps have minute
#: resolution at best and a cluster can carry a single article, so dividing by the raw span
#: would turn a rounding artifact -- or a zero -- into an unbounded speed.
MIN_SPAN_HOURS = 1.0


def coverage_span_hours(
    first_published_at: datetime.datetime, last_published_at: datetime.datetime
) -> float:
    """Hours of coverage the cluster spans, floored at :data:`MIN_SPAN_HOURS`."""
    elapsed = (last_published_at - first_published_at).total_seconds() / 3600.0
    return max(elapsed, MIN_SPAN_HOURS)


def event_hotness_score(
    article_count: int,
    source_count: int,
    span_hours: float,
    mean_authority: float | None = None,
    novelty: float | None = None,
) -> float:
    """0-100 hotness of a cluster. Bounded by construction; never an alias of severity.

    ``mean_authority`` and ``novelty`` are 0-1 and optional: pass ``None`` when the input was
    not measured and the component contributes :data:`NEUTRAL_COMPONENT` explicitly, rather
    than a fabricated value.
    """
    components = {
        "volume": math.tanh(max(article_count, 0) / VOLUME_SCALE),
        "velocity": math.tanh(
            (max(article_count, 0) / max(span_hours, MIN_SPAN_HOURS)) / VELOCITY_SCALE
        ),
        "diversity": math.tanh(max(source_count, 0) / DIVERSITY_SCALE),
        "authority": NEUTRAL_COMPONENT if mean_authority is None else _clamp(mean_authority),
        "novelty": NEUTRAL_COMPONENT if novelty is None else _clamp(novelty),
    }
    score = sum(HOTNESS_WEIGHTS[name] * value for name, value in components.items())
    return round(100.0 * score, 2)


def source_diversity_score(article_count: int, source_count: int) -> float:
    """Fraction of distinct sources (0-1)."""
    if article_count <= 0:
        return 0.0
    return round(source_count / article_count, 4)


def event_confidence_score(source_count: int, mean_authority: float | None) -> float:
    """Confidence in the event, rising with independent sources and their authority."""
    base = _clamp(0.3 + 0.2 * source_count)
    if mean_authority is not None:
        base = _clamp(0.5 * base + 0.5 * _clamp(mean_authority))
    return round(base, 4)


def compute_event_features(records: Sequence[ArticleRecord]) -> dict[str, Any]:
    """Derive structural event features from a cluster of article records."""
    if not records:
        msg = "cannot compute features for an empty cluster"
        raise ValueError(msg)
    article_count = len(records)
    source_count = len({r.source_id for r in records})
    authorities = [r.source_authority for r in records if r.source_authority is not None]
    mean_authority = sum(authorities) / len(authorities) if authorities else None
    published = [r.published_at for r in records]
    first_seen_at = min(published)
    last_seen_at = max(published)
    return {
        "article_count": article_count,
        "source_count": source_count,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "severity_score": event_severity_score(article_count, source_count),
        # `novelty` is passed as None, not as the `novelty_score` below it: that 1.0 is a
        # placeholder for a measurement no stage makes yet, and feeding it to hotness as if it
        # were real would add a constant 0.10 to every event's score while dressing an
        # unmeasured input up as a maximal one. Hotness takes the neutral component instead,
        # and starts reading novelty for real the day a stage computes it.
        "hotness_score": event_hotness_score(
            article_count=article_count,
            source_count=source_count,
            span_hours=coverage_span_hours(first_seen_at, last_seen_at),
            mean_authority=mean_authority,
            novelty=None,
        ),
        "source_diversity_score": source_diversity_score(article_count, source_count),
        "source_authority_score": (
            round(mean_authority, 4) if mean_authority is not None else None
        ),
        "novelty_score": 1.0,
        "confidence_score": event_confidence_score(source_count, mean_authority),
        "evidence_keys": [r.key for r in records],
    }
