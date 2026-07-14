"""What an alert evaluation owes a human, and who delivers it (ADR 0010)."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field, replace

import pytest

from db.models.enums import RiskLevel
from services.alerts import (
    AlertActionKind,
    DeliveryReport,
    LoggingAlertNotifier,
    NotifierDeliveryError,
    PendingAlertAction,
    RecordingAlertNotifier,
    deliver_actions,
)

NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)
USER = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _action(kind: AlertActionKind, *, alert_id: uuid.UUID | None = None) -> PendingAlertAction:
    if alert_id is None and kind is not AlertActionKind.BUDGET_SUPPRESSED:
        alert_id = uuid.uuid4()
    return PendingAlertAction(
        kind=kind,
        alert_id=alert_id,
        user_id=USER,
        dedupe_key="risk:banking:eurozone",
        severity=RiskLevel.HIGH,
        reason="because",
    )


class _CommitFailed(RuntimeError):
    """A receipt that would not commit: the send happened, the timestamp did not."""


@dataclass
class _RecordingAcknowledger:
    journal: list[str] = field(default_factory=list)
    notified: list[tuple[uuid.UUID, datetime.datetime]] = field(default_factory=list)
    all_cleared: list[tuple[uuid.UUID, datetime.datetime]] = field(default_factory=list)
    #: Alert ids whose all-clear receipt is already written: `acknowledge_all_clear` refuses those.
    already_all_cleared: frozenset[uuid.UUID] = frozenset()

    def acknowledge_notification(self, alert_id: uuid.UUID, *, at: datetime.datetime) -> bool:
        self.journal.append("ack")
        self.notified.append((alert_id, at))
        return True

    def acknowledge_all_clear(self, alert_id: uuid.UUID, *, at: datetime.datetime) -> bool:
        self.journal.append("ack")
        if alert_id in self.already_all_cleared:
            return False
        self.all_cleared.append((alert_id, at))
        return True


@dataclass
class _RecordingTransaction:
    """A commit/rollback pair -- all :func:`deliver_actions` needs of a session.

    ``fail_commits`` names the 1-based commits that raise, so a test can put a database fault
    exactly where the ordering matters.
    """

    journal: list[str] = field(default_factory=list)
    fail_commits: frozenset[int] = frozenset()
    commits: int = 0
    rollbacks: int = 0

    def commit(self) -> None:
        self.commits += 1
        if self.commits in self.fail_commits:
            self.journal.append("commit-failed")
            raise _CommitFailed(f"commit {self.commits} would not commit")
        self.journal.append("commit")

    def rollback(self) -> None:
        self.rollbacks += 1
        self.journal.append("rollback")


class _JournallingNotifier:
    """Records each send in a journal shared with the acknowledger and the transaction."""

    def __init__(self, journal: list[str], *, fail_kinds: tuple[AlertActionKind, ...] = ()) -> None:
        self.journal = journal
        self.attempted: list[PendingAlertAction] = []
        self._fail_kinds = frozenset(fail_kinds)

    def deliver(self, action: PendingAlertAction) -> bool:
        self.journal.append(f"send:{action.kind.value}")
        self.attempted.append(action)
        return action.kind not in self._fail_kinds


def _delivery_harness(
    *,
    fail_commits: frozenset[int] = frozenset(),
    fail_kinds: tuple[AlertActionKind, ...] = (),
    already_all_cleared: frozenset[uuid.UUID] = frozenset(),
) -> tuple[list[str], _JournallingNotifier, _RecordingAcknowledger, _RecordingTransaction]:
    journal: list[str] = []
    notifier = _JournallingNotifier(journal, fail_kinds=fail_kinds)
    acknowledger = _RecordingAcknowledger(journal=journal, already_all_cleared=already_all_cleared)
    transaction = _RecordingTransaction(journal=journal, fail_commits=fail_commits)
    return journal, notifier, acknowledger, transaction


# --------------------------------------------------------------------------------------
# PendingAlertAction: what gets acknowledged, and what never does
# --------------------------------------------------------------------------------------


def test_a_notification_and_an_all_clear_require_acknowledgement() -> None:
    assert _action(AlertActionKind.NOTIFICATION).requires_acknowledgement is True
    assert _action(AlertActionKind.ALL_CLEAR).requires_acknowledgement is True


def test_a_supersession_notice_is_never_acknowledged_even_with_an_alert_id() -> None:
    assert _action(AlertActionKind.SUPERSESSION).requires_acknowledgement is False


def test_a_budget_suppressed_action_has_no_alert_id_and_is_never_acknowledged() -> None:
    action = _action(AlertActionKind.BUDGET_SUPPRESSED)
    assert action.alert_id is None
    assert action.requires_acknowledgement is False


def test_as_dict_is_json_serialisable_and_stringifies_ids() -> None:
    action = _action(AlertActionKind.SUPERSESSION)
    payload = action.as_dict()

    assert payload["kind"] == "supersession"
    assert payload["alert_id"] == str(action.alert_id)
    assert payload["user_id"] == str(USER)
    assert payload["severity"] == "high"


# --------------------------------------------------------------------------------------
# RecordingAlertNotifier: the two ways a channel can fail a delivery
# --------------------------------------------------------------------------------------


def test_recording_notifier_tracks_every_attempt_and_only_successful_deliveries() -> None:
    notifier = RecordingAlertNotifier(fail_kinds=[AlertActionKind.SUPERSESSION])
    ok_action = _action(AlertActionKind.NOTIFICATION)
    fail_action = _action(AlertActionKind.SUPERSESSION)

    assert notifier.deliver(ok_action) is True
    assert notifier.deliver(fail_action) is False
    assert notifier.attempted == [ok_action, fail_action]
    assert notifier.delivered == [ok_action]


def test_recording_notifier_raises_for_configured_kinds_without_acknowledging() -> None:
    notifier = RecordingAlertNotifier(raise_kinds=[AlertActionKind.NOTIFICATION])
    action = _action(AlertActionKind.NOTIFICATION)

    with pytest.raises(NotifierDeliveryError):
        notifier.deliver(action)

    assert notifier.attempted == [action]
    assert notifier.delivered == []


def test_the_logging_notifier_always_reports_success() -> None:
    notifier = LoggingAlertNotifier()

    assert notifier.deliver(_action(AlertActionKind.NOTIFICATION)) is True


# --------------------------------------------------------------------------------------
# idempotency_key: a stable name for the messages that must not repeat, and nothing else
# --------------------------------------------------------------------------------------


def test_an_all_clear_is_named_by_its_alert_so_a_redelivery_carries_the_same_key() -> None:
    alert_id = uuid.uuid4()
    first = _action(AlertActionKind.ALL_CLEAR, alert_id=alert_id)
    # What the sweep rebuilds from the row: a different reason, the same message.
    redelivered = PendingAlertAction(
        kind=AlertActionKind.ALL_CLEAR,
        alert_id=alert_id,
        user_id=USER,
        dedupe_key="risk:banking:eurozone",
        severity=RiskLevel.LOW,
        reason="retry: all-clear was not yet acknowledged",
    )

    assert first.idempotency_key == f"all_clear:{alert_id}"
    assert redelivered.idempotency_key == first.idempotency_key


def test_a_supersession_is_named_by_the_row_and_its_successor() -> None:
    action = _action(AlertActionKind.SUPERSESSION)
    successor = uuid.uuid4()
    with_successor = replace(action, superseded_by=successor)

    assert with_successor.idempotency_key == f"supersession:{action.alert_id}:{successor}"


def test_a_notification_carries_no_key_because_no_stable_one_exists() -> None:
    # An alert can escalate, downgrade, and escalate back into the same band, so every key the
    # row can produce today is reused -- and a key-honouring channel would swallow the second,
    # real escalation. A notification is at-least-once by design; a duplicate is the tolerated
    # failure, a lost re-escalation is not.
    assert _action(AlertActionKind.NOTIFICATION).idempotency_key is None
    assert _action(AlertActionKind.BUDGET_SUPPRESSED).idempotency_key is None


def test_the_key_is_carried_on_the_wire_for_the_channel_to_honour() -> None:
    action = _action(AlertActionKind.ALL_CLEAR)

    assert action.as_dict()["idempotency_key"] == f"all_clear:{action.alert_id}"


# --------------------------------------------------------------------------------------
# deliver_actions: ordering, acknowledgement, and the three failure modes
# --------------------------------------------------------------------------------------


def test_each_receipt_is_committed_before_the_next_action_is_sent() -> None:
    journal, notifier, acknowledger, transaction = _delivery_harness()
    a = _action(AlertActionKind.NOTIFICATION)
    b = _action(AlertActionKind.ALL_CLEAR)

    report = deliver_actions(
        [a, b], notifier=notifier, acknowledger=acknowledger, transaction=transaction, now=NOW
    )

    # The whole repair, in one assertion: no send is still sitting in an uncommitted transaction
    # when the next one goes out.
    assert journal == ["send:notification", "ack", "commit", "send:all_clear", "ack", "commit"]
    assert report.delivered == (a, b)
    assert transaction.rollbacks == 0


def test_delivery_order_is_preserved_and_a_declined_delivery_lands_in_failed() -> None:
    notifier = RecordingAlertNotifier(fail_kinds=[AlertActionKind.SUPERSESSION])
    acknowledger = _RecordingAcknowledger()
    transaction = _RecordingTransaction()
    a = _action(AlertActionKind.NOTIFICATION)
    b = _action(AlertActionKind.SUPERSESSION)
    c = _action(AlertActionKind.ALL_CLEAR)

    report = deliver_actions(
        [a, b, c], notifier=notifier, acknowledger=acknowledger, transaction=transaction, now=NOW
    )

    assert isinstance(report, DeliveryReport)
    assert report.delivered == (a, c)
    assert report.failed == (b,)
    assert report.all_delivered is False
    assert [alert_id for alert_id, _ in acknowledger.notified] == [a.alert_id]
    assert [alert_id for alert_id, _ in acknowledger.all_cleared] == [c.alert_id]
    assert (transaction.commits, transaction.rollbacks) == (2, 0)


def test_a_raising_notifier_does_not_abort_the_remaining_actions() -> None:
    notifier = RecordingAlertNotifier(raise_kinds=[AlertActionKind.NOTIFICATION])
    acknowledger = _RecordingAcknowledger()
    transaction = _RecordingTransaction()
    a = _action(AlertActionKind.NOTIFICATION)
    b = _action(AlertActionKind.ALL_CLEAR)

    report = deliver_actions(
        [a, b], notifier=notifier, acknowledger=acknowledger, transaction=transaction, now=NOW
    )

    assert report.failed == (a,)
    assert report.delivered == (b,)
    assert acknowledger.notified == []  # the raising delivery is never acknowledged
    assert [alert_id for alert_id, _ in acknowledger.all_cleared] == [b.alert_id]


def test_a_declined_delivery_is_never_acknowledged_and_commits_nothing() -> None:
    notifier = RecordingAlertNotifier(fail_kinds=[AlertActionKind.NOTIFICATION])
    acknowledger = _RecordingAcknowledger()
    transaction = _RecordingTransaction()
    action = _action(AlertActionKind.NOTIFICATION)

    report = deliver_actions(
        [action], notifier=notifier, acknowledger=acknowledger, transaction=transaction, now=NOW
    )

    assert report.failed == (action,)
    assert acknowledger.notified == []
    assert (transaction.commits, transaction.rollbacks) == (0, 0)


def test_a_receipt_that_will_not_commit_fails_only_its_own_action() -> None:
    """The send happened and the timestamp did not, so the action is still owed -- and the
    receipt committed before it stays committed. This is the window the whole repair is about.
    """
    journal, notifier, acknowledger, transaction = _delivery_harness(fail_commits=frozenset({2}))
    first = _action(AlertActionKind.NOTIFICATION)
    doomed = _action(AlertActionKind.ALL_CLEAR)
    after = _action(AlertActionKind.NOTIFICATION)

    report = deliver_actions(
        [first, doomed, after],
        notifier=notifier,
        acknowledger=acknowledger,
        transaction=transaction,
        now=NOW,
    )

    assert report.delivered == (first, after)
    assert report.failed == (doomed,)  # really sent, never acknowledged, still owed
    assert doomed in notifier.attempted
    assert journal == [
        "send:notification",
        "ack",
        "commit",  # the first receipt is durable...
        "send:all_clear",
        "ack",
        "commit-failed",
        "rollback",  # ...and only the receipt that would not commit is rolled back
        "send:notification",
        "ack",
        "commit",
    ]
    assert (transaction.commits, transaction.rollbacks) == (3, 1)


def test_an_all_clear_that_was_already_acknowledged_keeps_its_first_receipt() -> None:
    # The residual at-least-once window made visible: the sweep redelivered an all-clear whose
    # receipt had in fact been written. The second acknowledgement is refused, so no later
    # timestamp overwrites the first, and the action is still reported delivered.
    action = _action(AlertActionKind.ALL_CLEAR)
    _journal, notifier, acknowledger, transaction = _delivery_harness(
        already_all_cleared=frozenset({action.alert_id})
    )

    report = deliver_actions(
        [action], notifier=notifier, acknowledger=acknowledger, transaction=transaction, now=NOW
    )

    assert report.delivered == (action,)
    assert acknowledger.all_cleared == []
    assert (transaction.commits, transaction.rollbacks) == (1, 0)


def test_supersession_and_budget_suppressed_actions_deliver_without_acknowledgement() -> None:
    notifier = RecordingAlertNotifier()
    acknowledger = _RecordingAcknowledger()
    transaction = _RecordingTransaction()
    supersession = _action(AlertActionKind.SUPERSESSION)
    suppressed = _action(AlertActionKind.BUDGET_SUPPRESSED)

    report = deliver_actions(
        [supersession, suppressed],
        notifier=notifier,
        acknowledger=acknowledger,
        transaction=transaction,
        now=NOW,
    )

    assert report.delivered == (supersession, suppressed)
    assert acknowledger.notified == []
    assert acknowledger.all_cleared == []
    assert transaction.commits == 0  # no receipt to write, so no transaction to commit


def test_all_delivered_is_true_only_when_nothing_failed() -> None:
    notifier = RecordingAlertNotifier()
    acknowledger = _RecordingAcknowledger()

    report = deliver_actions(
        [_action(AlertActionKind.NOTIFICATION)],
        notifier=notifier,
        acknowledger=acknowledger,
        transaction=_RecordingTransaction(),
        now=NOW,
    )

    assert report.all_delivered is True


def test_an_empty_batch_delivers_nothing() -> None:
    notifier = RecordingAlertNotifier()
    acknowledger = _RecordingAcknowledger()
    transaction = _RecordingTransaction()

    report = deliver_actions(
        [], notifier=notifier, acknowledger=acknowledger, transaction=transaction, now=NOW
    )

    assert report.delivered == ()
    assert report.failed == ()
    assert report.all_delivered is True
    assert transaction.commits == 0
