"""What an alert evaluation owes a human, and who delivers it (ADR 0010)."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field

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


@dataclass
class _RecordingAcknowledger:
    notified: list[tuple[uuid.UUID, datetime.datetime]] = field(default_factory=list)
    all_cleared: list[tuple[uuid.UUID, datetime.datetime]] = field(default_factory=list)

    def acknowledge_notification(self, alert_id: uuid.UUID, *, at: datetime.datetime) -> bool:
        self.notified.append((alert_id, at))
        return True

    def acknowledge_all_clear(self, alert_id: uuid.UUID, *, at: datetime.datetime) -> bool:
        self.all_cleared.append((alert_id, at))
        return True


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
# deliver_actions: ordering, acknowledgement, and the two failure modes
# --------------------------------------------------------------------------------------


def test_delivery_order_is_preserved_and_a_declined_delivery_lands_in_failed() -> None:
    notifier = RecordingAlertNotifier(fail_kinds=[AlertActionKind.SUPERSESSION])
    acknowledger = _RecordingAcknowledger()
    a = _action(AlertActionKind.NOTIFICATION)
    b = _action(AlertActionKind.SUPERSESSION)
    c = _action(AlertActionKind.ALL_CLEAR)

    report = deliver_actions([a, b, c], notifier=notifier, acknowledger=acknowledger, now=NOW)

    assert isinstance(report, DeliveryReport)
    assert report.delivered == (a, c)
    assert report.failed == (b,)
    assert report.all_delivered is False
    assert [alert_id for alert_id, _ in acknowledger.notified] == [a.alert_id]
    assert [alert_id for alert_id, _ in acknowledger.all_cleared] == [c.alert_id]


def test_a_raising_notifier_does_not_abort_the_remaining_actions() -> None:
    notifier = RecordingAlertNotifier(raise_kinds=[AlertActionKind.NOTIFICATION])
    acknowledger = _RecordingAcknowledger()
    a = _action(AlertActionKind.NOTIFICATION)
    b = _action(AlertActionKind.ALL_CLEAR)

    report = deliver_actions([a, b], notifier=notifier, acknowledger=acknowledger, now=NOW)

    assert report.failed == (a,)
    assert report.delivered == (b,)
    assert acknowledger.notified == []  # the raising delivery is never acknowledged
    assert [alert_id for alert_id, _ in acknowledger.all_cleared] == [b.alert_id]


def test_a_declined_delivery_is_never_acknowledged() -> None:
    notifier = RecordingAlertNotifier(fail_kinds=[AlertActionKind.NOTIFICATION])
    acknowledger = _RecordingAcknowledger()
    action = _action(AlertActionKind.NOTIFICATION)

    report = deliver_actions([action], notifier=notifier, acknowledger=acknowledger, now=NOW)

    assert report.failed == (action,)
    assert acknowledger.notified == []


def test_supersession_and_budget_suppressed_actions_deliver_without_acknowledgement() -> None:
    notifier = RecordingAlertNotifier()
    acknowledger = _RecordingAcknowledger()
    supersession = _action(AlertActionKind.SUPERSESSION)
    suppressed = _action(AlertActionKind.BUDGET_SUPPRESSED)

    report = deliver_actions(
        [supersession, suppressed], notifier=notifier, acknowledger=acknowledger, now=NOW
    )

    assert report.delivered == (supersession, suppressed)
    assert acknowledger.notified == []
    assert acknowledger.all_cleared == []


def test_all_delivered_is_true_only_when_nothing_failed() -> None:
    notifier = RecordingAlertNotifier()
    acknowledger = _RecordingAcknowledger()

    report = deliver_actions(
        [_action(AlertActionKind.NOTIFICATION)], notifier=notifier, acknowledger=acknowledger, now=NOW
    )

    assert report.all_delivered is True


def test_an_empty_batch_delivers_nothing() -> None:
    notifier = RecordingAlertNotifier()
    acknowledger = _RecordingAcknowledger()

    report = deliver_actions([], notifier=notifier, acknowledger=acknowledger, now=NOW)

    assert report.delivered == ()
    assert report.failed == ()
    assert report.all_delivered is True
