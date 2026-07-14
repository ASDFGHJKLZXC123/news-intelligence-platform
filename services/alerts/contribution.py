"""The `news_driven` contribution badge (ADR 0010).

``news_driven = sum(news-velocity term contributions) / total score``, so a reader can tell a
confirmatory (media-lagging) alert from a leading one. Persisted to `alerts.news_driven`,
which the schema constrains to 0..1.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, Decimal

from services.alerts.numeric import Numeric, to_decimal

_ZERO = Decimal(0)
_ONE = Decimal(1)
_QUANTUM = Decimal("0.000001")


def news_driven_contribution(
    news_contributions: Iterable[Numeric],
    total_score: Numeric,
) -> Decimal:
    """Return the share of ``total_score`` contributed by news-velocity terms, in 0..1.

    Safe behaviour at the edges, because this value is rendered as a badge and must never
    misattribute a score:

    * A total score of zero or below carries no attributable risk, so the share is 0. There
      is no division; the alternative (an undefined or infinite ratio) has no meaning here.
    * A negative net news contribution -- news terms pulling the score *down* -- is not a
      news-driven share, so it clamps to 0.
    * A share above 1 (news terms exceeding the total, possible when other terms are
      negative) clamps to 1, satisfying the `ck_alerts_news_driven` constraint.

    NaN and infinity raise: they are defects, and clamping them would hide the defect.
    """
    total = to_decimal(total_score, field="total_score")
    news = sum(
        (to_decimal(value, field="news contribution") for value in news_contributions),
        start=_ZERO,
    )
    if total <= _ZERO or news <= _ZERO:
        return _ZERO
    ratio = news / total
    clamped = min(ratio, _ONE)
    return clamped.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
