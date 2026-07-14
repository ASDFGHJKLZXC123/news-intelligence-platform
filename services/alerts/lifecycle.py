"""Alert lifecycle transitions (ADR 0010).

Pure decision data: :func:`decide_lifecycle` reads the lifecycle fields persisted on an
`alerts` row and returns the fields it should hold after this pipeline run. It touches no
database and no clock -- ``now`` is always supplied by the caller.

State machine::

    open -> escalated                 severity rises
    open/escalated -> downgraded      a clear band is crossed (severity falls)
    downgraded -> resolved            7 full days continuously below the clear band
    downgraded -> escalated           a rebound that raises severity
    downgraded -> downgraded          a rebound above the clear band: timer cancelled, alert lives

Resolution requires the alert to have cleared *every* alerting band -- severity Low, score at
or below 25 -- for 7 full days. A High alert that decays to a score of 45 downgrades to
Medium and stays live: it is still a Medium risk, and an all-clear on it would be a lie. That
is the difference between a downgrade (the risk eased) and a resolution (the risk ended).

`superseded` is a vocabulary member but never a *decision* here: supersession is an
orchestration concern (one alert replaced by a broader one), not a function of one score.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum

from db.models.enums import RiskLevel
from services.alerts.hysteresis import (
    RESOLVE_CLEAR_THRESHOLD,
    coerce_level,
    is_below_clear_band,
    next_severity,
    severity_rank,
)
from services.alerts.numeric import Numeric, to_score

#: A downgraded alert resolves only after this long continuously below the clear band.
RESOLVE_AFTER = datetime.timedelta(days=7)


class AlertState(StrEnum):
    """Lifecycle states; mirrors `db.models.core.ALERT_STATES`."""

    OPEN = "open"
    ESCALATED = "escalated"
    DOWNGRADED = "downgraded"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


#: States in which an alert is still live. Mirrors `db.models.core.ACTIVE_ALERT_STATES`.
ACTIVE_STATES: frozenset[AlertState] = frozenset(
    {AlertState.OPEN, AlertState.ESCALATED, AlertState.DOWNGRADED}
)
_TERMINAL_STATES: frozenset[AlertState] = frozenset(
    {AlertState.RESOLVED, AlertState.SUPERSEDED}
)


class LifecycleTrigger(StrEnum):
    """Why the lifecycle moved (or did not) on this run."""

    UNCHANGED = "unchanged"
    ESCALATED = "escalated"
    DOWNGRADED = "downgraded"
    REBOUNDED = "rebounded"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class AlertLifecycleState:
    """The lifecycle fields a persisted alert carries into a run."""

    state: AlertState
    severity: RiskLevel
    clear_band_since: datetime.datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", AlertState(self.state))
        object.__setattr__(self, "severity", coerce_level(self.severity))
        if self.clear_band_since is not None:
            _require_aware(self.clear_band_since, field="clear_band_since")


@dataclass(frozen=True)
class LifecycleDecision:
    """The lifecycle fields the alert should hold after this run."""

    state: AlertState
    severity: RiskLevel
    clear_band_since: datetime.datetime | None
    resolved_at: datetime.datetime | None
    trigger: LifecycleTrigger
    reason: str

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def emits_all_clear(self) -> bool:
        """Resolution is information, not silence: it emits an explicit all-clear."""
        return self.state is AlertState.RESOLVED


def decide_lifecycle(
    current: AlertLifecycleState,
    score: Numeric,
    *,
    now: datetime.datetime,
) -> LifecycleDecision:
    """Decide the lifecycle transition for one alert at one score.

    Raises ``ValueError`` for a terminal alert: a resolved or superseded row is never
    re-evaluated in place, it is re-fired as a new alert under the dedupe cooldown.
    """
    _require_aware(now, field="now")
    if current.state in _TERMINAL_STATES:
        raise ValueError(f"cannot evaluate a {current.state.value} alert")

    value = to_score(score)
    severity = next_severity(value, current.severity)
    rank_delta = severity_rank(severity) - severity_rank(current.severity)
    was_downgraded = current.state is AlertState.DOWNGRADED

    if rank_delta > 0:
        # A rise cancels any resolve timer. (A rise can never land at Low, so it never
        # collides with the below-clear branch below.)
        return LifecycleDecision(
            state=AlertState.ESCALATED,
            severity=severity,
            clear_band_since=None,
            resolved_at=None,
            trigger=LifecycleTrigger.ESCALATED,
            reason=f"score {value} raised severity {current.severity.value} -> {severity.value}",
        )

    if is_below_clear_band(value, severity):
        # The alert has cleared every alerting band. Keep a running timer, start one if this
        # is the first such run, and resolve once it has run for 7 full days.
        since = current.clear_band_since if was_downgraded and current.clear_band_since else now
        elapsed = now - since
        if elapsed >= RESOLVE_AFTER:
            return LifecycleDecision(
                state=AlertState.RESOLVED,
                severity=severity,
                clear_band_since=since,
                resolved_at=now,
                trigger=LifecycleTrigger.RESOLVED,
                reason=f"score {value} stayed below the clear band for {elapsed}; all-clear",
            )
        started = since == now
        return LifecycleDecision(
            state=AlertState.DOWNGRADED,
            severity=severity,
            clear_band_since=since,
            resolved_at=None,
            trigger=LifecycleTrigger.DOWNGRADED if started else LifecycleTrigger.UNCHANGED,
            reason=f"score {value} is below the clear band ({RESOLVE_CLEAR_THRESHOLD})",
        )

    if rank_delta < 0:
        # Dropped a band but is still an alerting severity: downgrade, and do not start a
        # resolve timer -- the risk has eased, not ended.
        return LifecycleDecision(
            state=AlertState.DOWNGRADED,
            severity=severity,
            clear_band_since=None,
            resolved_at=None,
            trigger=LifecycleTrigger.DOWNGRADED,
            reason=f"score {value} cleared severity {current.severity.value} -> {severity.value}",
        )

    if was_downgraded and current.clear_band_since is not None:
        # The score climbed back above the clear band before the 7 days were up. Hysteresis
        # may still hold the severity at Low, so the alert stays downgraded -- but the
        # resolve timer is cancelled and must restart from scratch.
        return LifecycleDecision(
            state=AlertState.DOWNGRADED,
            severity=severity,
            clear_band_since=None,
            resolved_at=None,
            trigger=LifecycleTrigger.REBOUNDED,
            reason=(
                f"score {value} rebounded above the clear band "
                f"({RESOLVE_CLEAR_THRESHOLD}); resolve timer reset"
            ),
        )

    return LifecycleDecision(
        state=current.state,
        severity=severity,
        clear_band_since=None,
        resolved_at=None,
        trigger=LifecycleTrigger.UNCHANGED,
        reason=f"score {value} holds severity {severity.value}",
    )


def _require_aware(value: datetime.datetime, *, field: str) -> None:
    """Reject naive datetimes: the lifecycle columns are all `timestamptz`."""
    if not isinstance(value, datetime.datetime):
        raise TypeError(f"{field} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must be timezone-aware")
