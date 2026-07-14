"""Velocity-alert persistence: z > 2.5 must hold for 2 consecutive runs (ADR 0010).

The streak lives on the alert row (`alerts.velocity_streak`), never in worker memory, so a
worker restart cannot fabricate or lose persistence. This module consumes the persisted
streak and returns the value to persist next.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from services.alerts.numeric import Numeric, to_decimal

#: A velocity condition qualifies strictly above this z-score; z == 2.5 does not qualify.
VELOCITY_Z_THRESHOLD = 2.5

#: Consecutive qualifying runs required before a velocity alert may fire.
VELOCITY_REQUIRED_RUNS = 2

#: How long a *spent* condition-state row (streak 0) is kept before it may be reclaimed. A row at
#: streak 0 carries no decision -- the next qualifying run would rebuild it from nothing -- so the
#: TTL is only a margin against clock skew and a paused pipeline, not a correctness horizon.
STALE_CONDITION_STATE_TTL = datetime.timedelta(days=7)


@dataclass(frozen=True)
class VelocityDecision:
    """The outcome of one run of the velocity persistence rule."""

    streak: int
    """The streak to persist to `alerts.velocity_streak` (0 after a non-qualifying run)."""

    qualified: bool
    """Whether *this* run's z-score cleared the threshold."""

    persisted: bool
    """Whether the condition has now held for the required consecutive runs."""


def decide_velocity_persistence(
    z_score: Numeric | None,
    streak: int,
    *,
    threshold: Numeric = VELOCITY_Z_THRESHOLD,
    required_runs: int = VELOCITY_REQUIRED_RUNS,
) -> VelocityDecision:
    """Advance or reset the velocity streak for one pipeline run.

    ``z_score=None`` means the run produced no velocity reading; it is treated as
    non-qualifying and resets the streak, so a gap in the data cannot be mistaken for
    persistence. A NaN or infinite z-score is a defect, not missing data, and raises.
    """
    if not isinstance(streak, int) or isinstance(streak, bool):
        raise TypeError(f"streak must be an int, got {type(streak).__name__}")
    if streak < 0:
        raise ValueError(f"streak must be non-negative, got {streak}")
    if required_runs < 1:
        raise ValueError(f"required_runs must be at least 1, got {required_runs}")

    limit = to_decimal(threshold, field="threshold")
    qualified = z_score is not None and to_decimal(z_score, field="z_score") > limit
    next_streak = streak + 1 if qualified else 0
    return VelocityDecision(
        streak=next_streak,
        qualified=qualified,
        persisted=next_streak >= required_runs,
    )


class ConditionStateRow(Protocol):
    """The pre-fire condition-state fields the staleness rule reads. `AlertConditionState` fits."""

    id: UUID
    velocity_streak: int
    last_evaluated_at: datetime.datetime | None
    created_at: datetime.datetime | None


def is_stale_condition_state(
    state: ConditionStateRow,
    *,
    now: datetime.datetime,
    ttl: datetime.timedelta = STALE_CONDITION_STATE_TTL,
) -> bool:
    """Whether a pre-fire condition-state row is spent and may be reclaimed.

    A row with a **live streak is never stale, at any age**. Deleting one would silently reset the
    2-consecutive-runs rule to zero and delay -- or, if the condition then lapses, permanently
    lose -- an alert that had already half-fired. That is the one outcome ADR 0010 moved this
    streak out of worker memory to prevent, so age alone can never authorise it.

    A row at streak 0 holds no decision at all: whatever it once counted has already been reset by
    a non-qualifying run, and the next qualifying run would recreate it from nothing. Once it has
    sat that way for ``ttl`` it is reclaimable. A row whose age cannot be established (no
    `last_evaluated_at` and no `created_at`) is left alone -- an unknown age is not an old age.
    """
    _require_aware(now, field="now")
    if ttl < datetime.timedelta(0):
        raise ValueError(f"ttl must be non-negative, got {ttl}")
    if (state.velocity_streak or 0) > 0:
        return False
    last = state.last_evaluated_at or state.created_at
    if last is None:
        return False
    _require_aware(last, field="last_evaluated_at")
    return now - last >= ttl


def select_stale_condition_states(
    states: Iterable[ConditionStateRow],
    *,
    now: datetime.datetime,
    ttl: datetime.timedelta = STALE_CONDITION_STATE_TTL,
) -> Sequence[ConditionStateRow]:
    """The reclaimable rows out of ``states``, in a stable id order. Selects; never deletes."""
    stale = [state for state in states if is_stale_condition_state(state, now=now, ttl=ttl)]
    return tuple(sorted(stale, key=lambda state: state.id))


def _require_aware(value: datetime.datetime, *, field: str) -> None:
    if not isinstance(value, datetime.datetime):
        raise TypeError(f"{field} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must be timezone-aware")
