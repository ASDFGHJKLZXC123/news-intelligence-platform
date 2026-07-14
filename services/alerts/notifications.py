"""What an alert evaluation owes a human, and who delivers it (ADR 0010).

The lifecycle decides; it does not send. Every message an evaluation produces is a typed
:class:`PendingAlertAction` -- a *request*, never a receipt -- and it is delivered through an
injected :class:`AlertNotifier`. No vendor lives in this package: the notifier shipped here writes
a structured log line, and a real email/Slack/webhook adapter is a later, separate concern that
only has to satisfy this one-method protocol.

Acknowledgement is what makes delivery honest. `alerts.notified_at` and
`alerts.all_clear_notified_at` are written *after* the notifier reports success, never before, so
a failed delivery leaves the timestamp NULL -- and a NULL timestamp is exactly how a retry finds
the message still owed. A notifier that returns ``False``, raises, or times out therefore claims
nothing and loses nothing.

Four kinds, and the difference between them is the product:

* ``NOTIFICATION`` -- a new or escalated alert. Acknowledged into `notified_at`. An escalation
  re-notifies by design, so the timestamp records the *latest* delivery.
* ``ALL_CLEAR`` -- a resolved alert, and the one message that must never be sent twice or wrongly.
  Acknowledged into `all_clear_notified_at`, which is written once: a second acknowledgement of the
  same alert is refused, so a retry after a partial failure cannot announce an ending twice.
* ``SUPERSESSION`` -- an alert replaced by another (by the budget, or by a broader alert). It
  carries the successor's id, and it is emphatically *not* an all-clear: nothing ended, and no
  resolution timestamp is written anywhere on its path.
* ``BUDGET_SUPPRESSED`` -- audit only. The platform budget refused an alert a slot, so there is no
  live row to acknowledge against (a suppressed creation has been deleted). It tells an operator
  why a condition that fired is not on the platform.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from db.models.enums import RiskLevel
from packages.config.logging import get_logger

logger = get_logger("services.alerts.notifications")


class AlertActionKind(StrEnum):
    """What an evaluation owes its owner."""

    NOTIFICATION = "notification"
    ALL_CLEAR = "all_clear"
    SUPERSESSION = "supersession"
    BUDGET_SUPPRESSED = "budget_suppressed"


#: The kinds whose delivery is recorded on the alert row. The other two have no timestamp column:
#: a supersession is not a lifecycle receipt, and a suppressed alert has no row to write to.
ACKNOWLEDGED_KINDS: frozenset[AlertActionKind] = frozenset(
    {AlertActionKind.NOTIFICATION, AlertActionKind.ALL_CLEAR}
)


@dataclass(frozen=True)
class PendingAlertAction:
    """One message an alert owes its owner. Not yet delivered, and never self-acknowledging."""

    kind: AlertActionKind
    alert_id: uuid.UUID | None
    user_id: uuid.UUID
    dedupe_key: str | None
    severity: RiskLevel | None
    reason: str
    superseded_by: uuid.UUID | None = None

    @property
    def requires_acknowledgement(self) -> bool:
        """Whether a successful delivery is written back to the alert row."""
        return self.kind in ACKNOWLEDGED_KINDS and self.alert_id is not None

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable view, for task payloads and audit logs."""
        return {
            "kind": self.kind.value,
            "alert_id": None if self.alert_id is None else str(self.alert_id),
            "user_id": str(self.user_id),
            "dedupe_key": self.dedupe_key,
            "severity": None if self.severity is None else self.severity.value,
            "reason": self.reason,
            "superseded_by": None if self.superseded_by is None else str(self.superseded_by),
        }


@runtime_checkable
class AlertNotifier(Protocol):
    """Delivers one action. Returns whether it was actually delivered.

    An implementation may return ``False`` or raise; both are a failed delivery, and neither
    acknowledges anything. It must never report a success it did not achieve.
    """

    def deliver(self, action: PendingAlertAction) -> bool: ...


@runtime_checkable
class AlertAcknowledger(Protocol):
    """Writes a delivery receipt onto the alert row. :class:`AlertLifecycleService` implements it."""

    def acknowledge_notification(self, alert_id: uuid.UUID, *, at: datetime.datetime) -> bool: ...

    def acknowledge_all_clear(self, alert_id: uuid.UUID, *, at: datetime.datetime) -> bool: ...


class LoggingAlertNotifier(AlertNotifier):
    """The default notifier: one structured log line per action, no vendor.

    A log line is a real delivery surface -- it is what the on-call operator reads today -- and it
    is the honest one to ship before an email or Slack adapter exists, because it never claims a
    channel the platform has not built.
    """

    def deliver(self, action: PendingAlertAction) -> bool:
        logger.info("alert action delivered", extra=action.as_dict())
        return True


class RecordingAlertNotifier(AlertNotifier):
    """An in-memory notifier for tests and local runs.

    ``fail_kinds`` fails a delivery without raising and ``raise_kinds`` fails it by raising, so
    both ways a channel can let the platform down are exercised against the same retry path.
    """

    def __init__(
        self,
        *,
        fail_kinds: Iterable[AlertActionKind] = (),
        raise_kinds: Iterable[AlertActionKind] = (),
    ) -> None:
        self.delivered: list[PendingAlertAction] = []
        self.attempted: list[PendingAlertAction] = []
        self._fail_kinds = frozenset(fail_kinds)
        self._raise_kinds = frozenset(raise_kinds)

    def deliver(self, action: PendingAlertAction) -> bool:
        self.attempted.append(action)
        if action.kind in self._raise_kinds:
            raise NotifierDeliveryError(f"the channel refused a {action.kind.value}")
        if action.kind in self._fail_kinds:
            return False
        self.delivered.append(action)
        return True


class NotifierDeliveryError(RuntimeError):
    """A channel failed to deliver. Never acknowledged, always retryable."""


@dataclass(frozen=True)
class DeliveryReport:
    """The outcome of delivering one run's actions.

    ``failed`` is the retry list: every one of those actions is still owed, and the row it belongs
    to still carries a NULL timestamp for it.
    """

    delivered: tuple[PendingAlertAction, ...] = ()
    failed: tuple[PendingAlertAction, ...] = ()

    @property
    def all_delivered(self) -> bool:
        return not self.failed


def deliver_actions(
    actions: Iterable[PendingAlertAction],
    *,
    notifier: AlertNotifier,
    acknowledger: AlertAcknowledger,
    now: datetime.datetime,
) -> DeliveryReport:
    """Deliver each action and acknowledge only the ones that actually arrived.

    Delivery comes first and the receipt second, never the other way round: a timestamp written
    before a send that then fails is a message the platform believes it delivered and never will.
    A notifier that raises is caught here rather than aborting the remaining actions -- one dead
    channel must not swallow the other alerts this run owes -- and its action lands in ``failed``,
    unacknowledged and still owed.
    """
    delivered: list[PendingAlertAction] = []
    failed: list[PendingAlertAction] = []
    for action in actions:
        try:
            accepted = bool(notifier.deliver(action))
        except Exception:
            logger.exception("alert action delivery raised", extra=action.as_dict())
            failed.append(action)
            continue
        if not accepted:
            logger.warning("alert action was not delivered", extra=action.as_dict())
            failed.append(action)
            continue
        if action.requires_acknowledgement:
            _acknowledge(action, acknowledger=acknowledger, now=now)
        delivered.append(action)
    return DeliveryReport(delivered=tuple(delivered), failed=tuple(failed))


def _acknowledge(
    action: PendingAlertAction, *, acknowledger: AlertAcknowledger, now: datetime.datetime
) -> None:
    """Write the receipt for a delivered action. ``alert_id`` is non-NULL by construction."""
    assert action.alert_id is not None  # noqa: S101 - guarded by `requires_acknowledgement`
    if action.kind is AlertActionKind.NOTIFICATION:
        acknowledger.acknowledge_notification(action.alert_id, at=now)
    elif action.kind is AlertActionKind.ALL_CLEAR:
        # Returns False when the all-clear had already been acknowledged: a redelivered resolution
        # keeps its original receipt rather than stamping a second, later one.
        acknowledger.acknowledge_all_clear(action.alert_id, at=now)
