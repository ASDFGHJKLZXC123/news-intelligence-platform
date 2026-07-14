"""The platform alert budget: 3 Critical, 10 High, ranked (ADR 0010).

The budget exists to stop fatigue: a platform that can show any number of Critical alerts shows
none of them credibly. ADR 0010 caps the *active* population at 3 Critical and 10 High, and says
a new Critical beyond budget "must supersede an existing one -- forces ranking".

**Top-K, not last-writer-wins.** Read literally, "a new Critical supersedes an existing one" would
let the weakest possible newcomer evict the strongest incumbent, and a platform whose three
Criticals are simply the three most recent is exactly the platform the cap was meant to prevent.
So the band keeps its strongest K: a candidate gets in only by *strictly* outranking the weakest
incumbent. The budget is enforced on entry into a band -- create, escalate into it, or downgrade
into it -- and never on an alert already holding a slot, so an ordinary update evicts nobody.

**The rank is the risk score, and nothing else.** It is the one quantity that says how bad the
risk is; every other column on the row (recency, evidence count, owner) says something else.
An exact tie is therefore *not* an outranking: the incumbent keeps the slot, and an equal-scored
newcomer waits. That is what stops two conditions scoring 82 from swapping the last Critical slot
back and forth on every run -- the churn the cap exists to prevent. Recency appears in exactly one
place: when the weakest incumbents tie dead even, the *newest* of them is the one evicted, so of
two equally weak alerts the one a user has been watching longest survives.

**Losing safely.** A candidate refused a slot is never left half-admitted:

* a **created** alert is deleted -- it was never visible and nothing was sent. Its pre-fire
  velocity streak is untouched (:mod:`services.alerts.service` only retires the condition-state
  row once the budget has admitted the alert), so the condition's persistence is not lost either;
* an **escalation** into a full band is withheld: the alert keeps the severity, state, and peak it
  already had. Its new score is still persisted, so the next run ranks it on fresh evidence;
* a **downgrade** into a full band (Critical -> High, the only capped demotion) is likewise
  withheld: the alert stays one band high, in the slot it already holds. Admitting it would put an
  eleventh alert in the High band, and dropping it instead would take a live risk off the platform
  to satisfy a cap it did not breach -- the one unrecoverable outcome. Holding it high is the
  recoverable one, and it self-heals: the very next run retries the demotion.

Only the *replaced* incumbent is superseded, and supersession is not resolution: no all-clear is
ever emitted for it (:mod:`services.alerts.supersession`).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from db.models.core import Alert
from db.models.enums import RiskLevel
from services.alerts.hysteresis import coerce_level
from services.alerts.lifecycle import ACTIVE_STATES, AlertState
from services.alerts.notifications import AlertActionKind, PendingAlertAction
from services.alerts.numeric import to_decimal
from services.alerts.supersession import supersede, supersession_action

#: ADR 0010: the platform-wide caps on *active* alerts, by current persisted severity. Medium and
#: Low are uncapped: they are not what fatigue is made of.
ACTIVE_BUDGET: Mapping[RiskLevel, int] = MappingProxyType({RiskLevel.CRITICAL: 3, RiskLevel.HIGH: 10})

#: A NULL `risk_score` ranks below every real score rather than crashing the comparison: a row
#: that never recorded why it fired cannot outrank one that did.
_NO_SCORE = Decimal(-1)
_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)


class BudgetRepository(Protocol):
    """The persistence surface the budget needs, and nothing more."""

    def active_alerts_at_severity(self, severity: str) -> list[Alert]: ...

    def delete_alert(self, alert: Alert) -> None: ...

    def flush(self) -> None: ...


class BudgetOutcomeKind(StrEnum):
    """What the budget did to the alert it was handed."""

    WITHIN_BUDGET = "within_budget"
    REPLACED = "replaced"
    SUPPRESSED = "suppressed"
    WITHHELD = "withheld"


@dataclass(frozen=True)
class LifecycleSnapshot:
    """The lifecycle fields an alert held *before* this run, so a refusal can restore them."""

    state: AlertState
    severity: RiskLevel
    peak_severity: RiskLevel | None

    @classmethod
    def of(cls, alert: Alert | None) -> LifecycleSnapshot | None:
        """Snapshot a persisted alert, or ``None`` when the key has no live alert yet."""
        if alert is None:
            return None
        peak = None if alert.peak_severity is None else coerce_level(alert.peak_severity)
        return cls(
            state=AlertState(alert.state),
            severity=coerce_level(alert.severity),
            peak_severity=peak,
        )


@dataclass(frozen=True)
class BudgetOutcome:
    """The typed verdict of one budget enforcement, for delivery and audit."""

    kind: BudgetOutcomeKind
    reason: str
    severity: RiskLevel | None = None
    cap: int | None = None
    alert_id: uuid.UUID | None = None
    victim_id: uuid.UUID | None = None
    action: PendingAlertAction | None = None

    @property
    def admitted(self) -> bool:
        """Whether the alert holds a slot in its band after this run."""
        return self.kind in (BudgetOutcomeKind.WITHIN_BUDGET, BudgetOutcomeKind.REPLACED)


def alert_rank(alert: Alert) -> Decimal:
    """How strong an alert is. Deterministic: same row, same rank, in any process."""
    if alert.risk_score is None:
        return _NO_SCORE
    return to_decimal(alert.risk_score, field="risk_score")


def weakest(alerts: Sequence[Alert]) -> Alert:
    """The incumbent a candidate must strictly outrank to enter a full band.

    Lowest score wins the eviction. Among alerts that score exactly the same, the newest is
    evicted -- of two equally weak alerts, the one a user has watched longest survives -- and the
    highest id breaks even that, so the choice is total and reproducible rather than a function of
    row order.
    """
    newest_first = sorted(alerts, key=lambda alert: (alert.created_at or _EPOCH, alert.id), reverse=True)
    return min(newest_first, key=alert_rank)  # min() keeps the first minimum: the newest weakest


def enforce_budget(
    repository: BudgetRepository,
    alert: Alert,
    *,
    previous: LifecycleSnapshot | None,
    now: datetime.datetime,
) -> BudgetOutcome:
    """Apply the platform budget to an alert this run just wrote. Flushes; never commits.

    ``previous`` is the alert's lifecycle before this run, or ``None`` when the run created it.
    The band's active rows are read under a row lock in a stable id order, so two workers admitting
    into the same band serialise instead of both seeing three Criticals and both making a fourth.
    """
    state = AlertState(alert.state)
    severity = coerce_level(alert.severity)
    cap = ACTIVE_BUDGET.get(severity)

    if state not in ACTIVE_STATES:
        # A resolved or superseded alert holds no slot -- it has just freed one.
        return BudgetOutcome(
            kind=BudgetOutcomeKind.WITHIN_BUDGET,
            reason=f"a {state.value} alert holds no slot; its band has one more free",
            alert_id=alert.id,
        )
    if cap is None:
        return BudgetOutcome(
            kind=BudgetOutcomeKind.WITHIN_BUDGET,
            reason=f"{severity.value} alerts are not capped",
            severity=severity,
            alert_id=alert.id,
        )
    if previous is not None and previous.severity is severity:
        # It already held this slot; an update is not an admission, and it evicts nobody.
        return BudgetOutcome(
            kind=BudgetOutcomeKind.WITHIN_BUDGET,
            reason=f"the alert already holds a {severity.value} slot",
            severity=severity,
            cap=cap,
            alert_id=alert.id,
        )

    incumbents = [
        row for row in repository.active_alerts_at_severity(severity.value) if row.id != alert.id
    ]
    if len(incumbents) < cap:
        return BudgetOutcome(
            kind=BudgetOutcomeKind.WITHIN_BUDGET,
            reason=f"{len(incumbents) + 1}/{cap} {severity.value} alerts are active; the band has room",
            severity=severity,
            cap=cap,
            alert_id=alert.id,
        )

    victim = weakest(incumbents)
    candidate_score, victim_score = alert_rank(alert), alert_rank(victim)
    banner = f"the {severity.value} budget is full ({cap})"

    if candidate_score > victim_score:  # strictly stronger, or the incumbent keeps the slot
        reason = (
            f"{banner}; risk {candidate_score} outranks the weakest incumbent {victim.id} at "
            f"{victim_score}: it is superseded by {alert.id}"
        )
        supersede(victim, replacement=alert, now=now)
        repository.flush()
        return BudgetOutcome(
            kind=BudgetOutcomeKind.REPLACED,
            reason=reason,
            severity=severity,
            cap=cap,
            alert_id=alert.id,
            victim_id=victim.id,
            action=supersession_action(victim, reason=reason),
        )

    refusal = (
        f"{banner} and risk {candidate_score} does not outrank the weakest incumbent "
        f"{victim.id} at {victim_score}"
    )

    if previous is None:
        return _suppress(repository, alert, severity=severity, cap=cap, refusal=refusal)
    return _withhold(repository, alert, previous, severity=severity, cap=cap, refusal=refusal, now=now)


def _suppress(
    repository: BudgetRepository, alert: Alert, *, severity: RiskLevel, cap: int, refusal: str
) -> BudgetOutcome:
    """Refuse a newly created alert a slot, and leave no trace of it behind."""
    reason = f"{refusal}; the alert is not opened"
    action = PendingAlertAction(
        kind=AlertActionKind.BUDGET_SUPPRESSED,
        alert_id=None,  # the row is about to be deleted: it never existed as far as anyone can see
        user_id=alert.user_id,
        dedupe_key=alert.dedupe_key,
        severity=severity,
        reason=reason,
    )
    repository.delete_alert(alert)
    return BudgetOutcome(
        kind=BudgetOutcomeKind.SUPPRESSED,
        reason=reason,
        severity=severity,
        cap=cap,
        action=action,
    )


def _withhold(
    repository: BudgetRepository,
    alert: Alert,
    previous: LifecycleSnapshot,
    *,
    severity: RiskLevel,
    cap: int,
    refusal: str,
    now: datetime.datetime,
) -> BudgetOutcome:
    """Refuse a transition into a full band, putting back the band membership it tried to change.

    Everything else this run wrote stays: the score is the newest reading (and is what the next
    run's ranking is judged on), and the evidence is history. Only the band membership -- the thing
    the budget governs -- is rolled back. `clear_band_since` is never touched, because a withheld
    transition is always an entry into High or Critical and the resolve timer only ever runs at Low.
    """
    alert.state = previous.state.value
    alert.severity = previous.severity.value
    alert.peak_severity = None if previous.peak_severity is None else previous.peak_severity.value
    alert.updated_at = now
    repository.flush()

    reason = f"{refusal}; the alert stays {previous.severity.value} and keeps the slot it already held"
    return BudgetOutcome(
        kind=BudgetOutcomeKind.WITHHELD,
        reason=reason,
        severity=severity,
        cap=cap,
        alert_id=alert.id,
        action=PendingAlertAction(
            kind=AlertActionKind.BUDGET_SUPPRESSED,
            alert_id=alert.id,
            user_id=alert.user_id,
            dedupe_key=alert.dedupe_key,
            severity=previous.severity,
            reason=reason,
        ),
    )
