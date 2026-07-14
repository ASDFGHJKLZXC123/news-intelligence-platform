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

Which is only true if the receipt outlives the send. A send cannot be un-sent, so it must never
sit inside a transaction that can still roll back the evaluation that owed it: a rollback after a
delivery is a resolution the retry re-resolves and announces a second time. :func:`deliver_actions`
therefore requires the caller's lifecycle work to be committed *before* it is called, and commits
each receipt it writes before delivering the next action -- so a crash costs the one receipt in
flight and nothing else. What that leaves is an at-least-once channel with a stable name for every
message (:attr:`PendingAlertAction.idempotency_key`); the platform owes the key, and a channel that
honours it owes delivering that key once.

Four kinds, and the difference between them is the product:

* ``NOTIFICATION`` -- a new or escalated alert. Acknowledged into `notified_at`. An escalation
  re-notifies by design, so the timestamp records the *latest* delivery, and a redelivery is
  tolerated: this message is at-least-once, and carries no idempotency key (see below).
* ``ALL_CLEAR`` -- a resolved alert, and the one message that must never be sent twice or wrongly.
  Acknowledged into `all_clear_notified_at`, which is written once: a second acknowledgement of the
  same alert is refused, so a retry after a partial failure cannot announce an ending twice. It is
  the kind that carries a stable idempotency key, because it is the kind that must not repeat.
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

    @property
    def idempotency_key(self) -> str | None:
        """A stable name for *this message*, or ``None`` where no honest one exists.

        Two actions carrying the same key are the same message, however many times a task retry or
        a sweep re-derives them from the row. Delivery is at-least-once; a channel that honours the
        key makes it once. That is the whole contract, and it is deliberately only claimed where it
        is true:

        * ``ALL_CLEAR`` -- ``all_clear:<alert_id>``. An alert row resolves exactly once (`resolved`
          is terminal; a key that re-fires opens a *new* row with a new id), so this names one
          real-world ending forever, and the evaluation task and the sweep derive it identically.
        * ``SUPERSESSION`` -- ``supersession:<alert_id>:<successor_id>``. A row is superseded once,
          by one successor.
        * ``NOTIFICATION`` -- ``None``, and deliberately. Nothing on `alerts` records *which*
          evaluation owes the message, so every key derivable from the row today (id, severity,
          state) is reused when an alert escalates, downgrades, and escalates back into the same
          band -- and a key-honouring channel would then swallow a real re-escalation. Duplicating
          a notification is tolerated by design here; losing one is not.
        * ``BUDGET_SUPPRESSED`` -- ``None``. Audit only: no row, and nothing ever redelivers it.
        """
        if self.alert_id is None:
            return None
        if self.kind is AlertActionKind.ALL_CLEAR:
            return f"all_clear:{self.alert_id}"
        if self.kind is AlertActionKind.SUPERSESSION and self.superseded_by is not None:
            return f"supersession:{self.alert_id}:{self.superseded_by}"
        return None

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
            "idempotency_key": self.idempotency_key,
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


@runtime_checkable
class AckTransaction(Protocol):
    """The unit of durability around one receipt. A :class:`sqlalchemy.orm.Session` is one.

    :func:`deliver_actions` commits after each receipt it writes, and rolls back the single receipt
    it could not write. It never commits the caller's lifecycle work: that is already durable
    before the first notifier call, and making it so is the caller's job.
    """

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


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
    transaction: AckTransaction,
    now: datetime.datetime,
) -> DeliveryReport:
    """Deliver the actions in the order given, committing each receipt before the next send.

    Three orderings, each of them load-bearing:

    * **The lifecycle is already durable.** The caller committed the evaluation that produced these
      actions before calling this, and nothing here writes lifecycle state -- only receipts. A send
      cannot be un-sent, so it must not sit inside a transaction that can still roll the alert
      back: a rollback after a delivery is a resolution the retry re-resolves and announces twice.
    * **Delivery first, receipt second.** A timestamp written before a send that then fails is a
      message the platform believes it delivered and never will.
    * **Receipt committed before the next send.** A receipt that will not commit costs its own
      action and nothing else: everything acknowledged earlier this run stays durable, and the
      failed action keeps its NULL timestamp and stays owed.

    A notifier that raises is caught rather than aborting the run -- one dead channel must not
    swallow the other alerts this run owes -- and its action lands in ``failed``, unacknowledged
    and still owed. So does a delivery whose receipt would not commit: it really was sent, and the
    redelivery it will get carries the same :attr:`~PendingAlertAction.idempotency_key`.
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
        if not action.requires_acknowledgement:
            delivered.append(action)  # no receipt to write, so no transaction to commit
            continue
        try:
            _acknowledge(action, acknowledger=acknowledger, now=now)
            transaction.commit()
        except Exception:
            # The send happened; only its receipt did not. Roll back that receipt alone -- the
            # lifecycle and every earlier receipt are already committed -- and leave the action
            # owed, so the sweep redelivers it under the same idempotency key.
            transaction.rollback()
            logger.exception("alert action receipt was not committed", extra=action.as_dict())
            failed.append(action)
            continue
        delivered.append(action)
    return DeliveryReport(delivered=tuple(delivered), failed=tuple(failed))


def _acknowledge(
    action: PendingAlertAction, *, acknowledger: AlertAcknowledger, now: datetime.datetime
) -> None:
    """Write the receipt for a delivered action. ``alert_id`` is non-NULL by construction."""
    assert action.alert_id is not None  # noqa: S101 - guarded by `requires_acknowledgement`
    if action.kind is AlertActionKind.NOTIFICATION:
        acknowledger.acknowledge_notification(action.alert_id, at=now)
    elif not acknowledger.acknowledge_all_clear(action.alert_id, at=now):
        # Refused: this all-clear already had a receipt, so it has just been announced a second
        # time -- the residual at-least-once window a key-honouring channel is what closes. The
        # original receipt stands (no second, later timestamp), and the duplicate is visible here
        # rather than silent.
        logger.warning("all-clear redelivered: it was already acknowledged", extra=action.as_dict())
