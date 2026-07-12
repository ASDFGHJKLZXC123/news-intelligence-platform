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
    return {
        "article_count": article_count,
        "source_count": source_count,
        "first_seen_at": min(published),
        "last_seen_at": max(published),
        "severity_score": event_severity_score(article_count, source_count),
        "source_diversity_score": source_diversity_score(article_count, source_count),
        "source_authority_score": (
            round(mean_authority, 4) if mean_authority is not None else None
        ),
        "novelty_score": 1.0,
        "confidence_score": event_confidence_score(source_count, mean_authority),
        "evidence_keys": [r.key for r in records],
    }
