"""Numeric coercion shared by the alert policy primitives.

Alert policy is deterministic: every numeric input is normalised to :class:`~decimal.Decimal`
before it is compared against a threshold, so a float from the scoring pipeline and a
``Numeric`` column read back from Postgres decide identically. Booleans, NaN, and infinity
are rejected rather than silently compared.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: Numeric inputs the policy primitives accept.
Numeric = int | float | Decimal

SCORE_MIN = 0
SCORE_MAX = 100


def to_decimal(value: object, *, field: str) -> Decimal:
    """Coerce ``value`` to a finite ``Decimal``.

    ``bool`` is rejected even though it is an ``int``: a boolean risk score is a bug, not a
    zero. Floats route through ``str`` so ``0.1`` stays ``Decimal("0.1")`` instead of its
    binary expansion, which keeps threshold comparisons exact.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError(f"{field} must be an int, float, or Decimal, got {type(value).__name__}")
    try:
        coerced = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - str(int|float) always parses
        raise ValueError(f"{field} is not a valid number: {value!r}") from exc
    if not coerced.is_finite():
        raise ValueError(f"{field} must be finite, got {value!r}")
    return coerced


def to_score(value: object, *, field: str = "score") -> Decimal:
    """Coerce a 0-100 risk score, rejecting NaN, infinity, and out-of-range values."""
    score = to_decimal(value, field=field)
    if not SCORE_MIN <= score <= SCORE_MAX:
        raise ValueError(f"{field} must be within {SCORE_MIN}-{SCORE_MAX}, got {value!r}")
    return score
