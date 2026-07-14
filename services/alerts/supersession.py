"""Supersession: one alert replaced by another (ADR 0010).

Supersession is **not** resolution. A resolved alert says "the risk ended" and emits an
all-clear; a superseded alert says "this row is no longer the right way to say it" -- the risk
is still there, it is just carried by another alert now. So a superseded row never gets
``resolved_at``, never arms the 24h cooldown (:meth:`AlertRepository.latest_resolved_for_key`
excludes it by state), and never emits an all-clear. It emits a supersession notice instead.

Two callers replace an alert, and both land here:

* the platform budget (:mod:`services.alerts.budget`), where a stronger candidate takes the
  slot of the weakest incumbent in a full band, and
* :func:`supersede_with_broader_alert`, where an explicitly-supplied broader alert absorbs the
  narrower ones a caller names.

Scope hierarchy is never *inferred*. There is no rule here that reads a `scope_entity` string
and concludes that "eurozone" contains "france": the caller states which broader alert replaces
which narrower ones, and every id is validated against the persisted rows before anything is
written. A terminal alert is never revived, an alert never supersedes itself, and an alert
never rewrites an alert belonging to another user -- the dedupe key is platform-wide, so one
orchestrated owner owns a key, and a cross-owner replacement fails closed.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from db.models.core import Alert
from services.alerts.hysteresis import coerce_level
from services.alerts.lifecycle import ACTIVE_STATES, AlertState
from services.alerts.notifications import AlertActionKind, PendingAlertAction


class SupersessionError(ValueError):
    """A supersession that cannot be performed as asked, refused rather than guessed at."""


class SupersessionRepository(Protocol):
    """The persistence surface a supersession needs: a locking read and a flush."""

    def get_alert(self, alert_id: uuid.UUID) -> Alert | None: ...

    def flush(self) -> None: ...


@dataclass(frozen=True)
class SupersessionResult:
    """What one supersession call replaced, and what must now be delivered."""

    replacement_id: uuid.UUID
    superseded_ids: tuple[uuid.UUID, ...]
    actions: tuple[PendingAlertAction, ...]


def supersede(victim: Alert, *, replacement: Alert, now: datetime.datetime) -> None:
    """Mark ``victim`` superseded by ``replacement``. Writes the row; does not flush.

    Terminal history is preserved: evidence, severity, peak severity, and the score the alert
    ended on all stay exactly as they were. Only the lifecycle fields move.
    """
    if victim.id == replacement.id:
        raise SupersessionError(f"alert {victim.id} cannot supersede itself")
    victim.state = AlertState.SUPERSEDED.value
    victim.superseded_by = replacement.id
    victim.updated_at = now
    # Deliberately not written: `resolved_at` (this is not a resolution) and `cooldown_until`
    # (a superseded key is free to re-fire immediately -- nothing about the risk ended).


def supersession_action(victim: Alert, *, reason: str) -> PendingAlertAction:
    """The typed notice a superseded row owes its owner, for delivery and audit."""
    return PendingAlertAction(
        kind=AlertActionKind.SUPERSESSION,
        alert_id=victim.id,
        user_id=victim.user_id,
        dedupe_key=victim.dedupe_key,
        severity=coerce_level(victim.severity),
        reason=reason,
        superseded_by=victim.superseded_by,
    )


def supersede_with_broader_alert(
    repository: SupersessionRepository,
    *,
    broader_alert_id: uuid.UUID,
    narrower_alert_ids: Sequence[uuid.UUID],
    now: datetime.datetime,
) -> SupersessionResult:
    """Replace one or more active narrower alerts with an explicitly-named broader one.

    The broader alert must already exist and be active: supersession points at a live successor,
    and a caller cannot mint one here. Every narrower alert must exist, be active, belong to the
    same owner, and not be the broader alert itself. Any failure refuses the whole call -- no row
    is written -- so a caller can never half-replace a set of alerts.

    Rows are locked in ascending id order (the broader alert included), which is the same order
    the budget takes, so two concurrent replacements cannot deadlock against each other.
    """
    _require_aware(now)
    broader_id = _coerce_id(broader_alert_id, field="broader_alert_id")
    victims = _validated_victims(repository, broader_id, narrower_alert_ids)
    broader = victims.pop(broader_id)

    actions: list[PendingAlertAction] = []
    for alert_id in sorted(victims):
        victim = victims[alert_id]
        reason = (
            f"superseded by the broader alert {broader.id} "
            f"({broader.dedupe_key}); the risk is now carried by that alert"
        )
        supersede(victim, replacement=broader, now=now)
        actions.append(supersession_action(victim, reason=reason))
    repository.flush()

    return SupersessionResult(
        replacement_id=broader.id,
        superseded_ids=tuple(action.alert_id for action in actions),
        actions=tuple(actions),
    )


def _validated_victims(
    repository: SupersessionRepository,
    broader_alert_id: uuid.UUID,
    narrower_alert_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, Alert]:
    """Load and validate the broader alert and its victims, locking them in a stable order."""
    broader_id = _coerce_id(broader_alert_id, field="broader_alert_id")
    narrower = {_coerce_id(value, field="narrower_alert_ids") for value in narrower_alert_ids}
    if not narrower:
        raise SupersessionError("a supersession must name at least one narrower alert")
    if broader_id in narrower:
        raise SupersessionError(f"alert {broader_id} cannot supersede itself")

    rows: dict[uuid.UUID, Alert] = {}
    for alert_id in sorted({broader_id} | narrower):
        alert = repository.get_alert(alert_id)
        if alert is None:
            raise SupersessionError(f"unknown alert {alert_id}")
        if AlertState(alert.state) not in ACTIVE_STATES:
            role = "broader" if alert_id == broader_id else "narrower"
            raise SupersessionError(
                f"the {role} alert {alert_id} is {alert.state}; a terminal alert is neither "
                "revived nor superseded a second time"
            )
        rows[alert_id] = alert

    broader = rows[broader_id]
    for alert_id in narrower:
        victim = rows[alert_id]
        if victim.user_id != broader.user_id:
            # The dedupe key is platform-wide, so two owners can hold alerts on one condition.
            # Rewriting another user's row would end their alert on a stranger's say-so.
            raise SupersessionError(
                f"alert {alert_id} belongs to user {victim.user_id}, not {broader.user_id}; "
                "an alert is never superseded on another owner's behalf"
            )
        if victim.dedupe_key is not None and victim.dedupe_key == broader.dedupe_key:
            raise SupersessionError(
                f"alert {alert_id} shares dedupe key {victim.dedupe_key!r} with the broader "
                "alert; one key has one live alert, and it cannot replace itself"
            )
    return rows


def _coerce_id(value: uuid.UUID | str, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise SupersessionError(f"{field} is not a UUID: {value!r}") from exc
    raise SupersessionError(f"{field} must be a UUID, got {type(value).__name__}")


def _require_aware(now: datetime.datetime) -> None:
    if not isinstance(now, datetime.datetime):
        raise TypeError(f"now must be a datetime, got {type(now).__name__}")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
