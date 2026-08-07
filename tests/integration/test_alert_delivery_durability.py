"""What a real commit makes durable, and what a real rollback erases (ADR 0010).

The unit suite can prove the *ordering* the alert tasks now keep -- lifecycle committed before
the first send, one receipt committed before the next send -- because ordering is observable
against a fake. It cannot prove what those commits actually mean, and that is the whole defect
this file exists for: delivering inside the evaluation's transaction, and committing afterwards,
meant a failed commit rolled the resolution back after the all-clear had gone out, so the retry
resolved the alert a second time and announced the ending twice. The one message ADR 0010 says
must never be sent twice.

So every assertion here is a row read back out of PostgreSQL after a real COMMIT or a real
ROLLBACK, with the failure injected at exactly the commit that matters:

* the lifecycle commit fails      -> no alert row, and no notifier was even called;
* a receipt commit fails          -> the alert row *stands*, its timestamp is NULL, the retry
                                     neither re-creates nor re-resolves it, and the sweep
                                     redelivers under the same idempotency key;
* one receipt commit of several   -> the receipts committed before it stay committed.

The notifier is the only fake: it is a hook point by design (``build_notifier``), and a channel
that really sent an email is not something a test can un-send either.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from db.base import SessionLocal, engine
from db.models import Alert, User
from services.alerts.notifications import AlertActionKind, RecordingAlertNotifier
from workers import alert_tasks

pytestmark = pytest.mark.integration

_ALEMBIC = Config("alembic.ini")

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)
DOWNGRADE_AT = NOW + datetime.timedelta(hours=1)
RESOLVE_AT = DOWNGRADE_AT + datetime.timedelta(days=7, seconds=1)


class CommitRefused(RuntimeError):
    """What a database that will not commit looks like from inside the task."""


class FlakySession:
    """A real session whose chosen commits refuse, and whose rollbacks are real rollbacks.

    Everything except ``commit``/``rollback``/``close`` is the underlying session's, so the rows
    these tests read back really went through PostgreSQL. A refused commit leaves the transaction
    exactly as a lost connection would: nothing written, and the caller's ``rollback()`` (which
    :func:`~services.alerts.notifications.deliver_actions` issues) is what discards the receipt
    that never landed.
    """

    def __init__(self, session: Any, fail_commits: frozenset[int]) -> None:
        self._session = session
        self._fail_commits = fail_commits
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def commit(self) -> None:
        self.commits += 1
        if self.commits in self._fail_commits:
            raise CommitRefused(f"commit {self.commits} refused")
        self._session.commit()

    def rollback(self) -> None:
        self.rollbacks += 1
        self._session.rollback()

    def close(self) -> None:
        self.closed = True
        self._session.close()


@dataclass
class Harness:
    """The task's two injection seams: the session factory and the notifier."""

    notifier: RecordingAlertNotifier
    sessions: list[FlakySession] = field(default_factory=list)
    #: The 1-based commits of the *next* task run that will refuse. Reset it between runs.
    fail_commits: frozenset[int] = frozenset()

    def sent(self, kind: AlertActionKind) -> list[Any]:
        return [action for action in self.notifier.attempted if action.kind is kind]


@pytest.fixture
def alerts_db(require_postgres: None):
    """A migrated database holding one user and no alerts."""
    command.upgrade(_ALEMBIC, "head")
    _truncate()
    with SessionLocal() as session:
        session.add(User(id=USER_ID, email="alert-durability@test.example"))
        session.commit()
    try:
        yield
    finally:
        _truncate()


@pytest.fixture
def harness(alerts_db: None, monkeypatch) -> Harness:
    state = Harness(notifier=RecordingAlertNotifier())

    def session_factory() -> FlakySession:
        session = FlakySession(SessionLocal(), state.fail_commits)
        state.sessions.append(session)
        return session

    monkeypatch.setattr(alert_tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: state.notifier)
    monkeypatch.setattr(
        alert_tasks,
        "get_settings",
        lambda: SimpleNamespace(crisis_prediction_reads_enabled=True),
    )
    return state


def _truncate() -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users, alert_condition_states RESTART IDENTITY CASCADE"))


def _payload(*, risk_score: float, scope_entity: str = "eurozone") -> dict[str, Any]:
    return {
        "scope": {
            "user_id": str(USER_ID),
            "risk_type": "banking",
            "scope_entity": scope_entity,
            "condition_class": "composite_score",
            "title": "Eurozone banking stress",
            "message": "Composite banking risk is elevated.",
            "alert_type": "risk_score",
        },
        "risk_score": risk_score,
    }


def _alerts() -> list[Alert]:
    with SessionLocal() as session:
        return list(session.execute(select(Alert).order_by(Alert.created_at, Alert.id)).scalars())


def _evaluate(payloads: list[dict[str, Any]], at: datetime.datetime) -> dict[str, Any]:
    return alert_tasks.run_alert_evaluation(payloads, now=at.isoformat())


def _open_then_resolve(harness: Harness) -> None:
    """Drive one alert to the brink of resolution: opened, downgraded, resolve timer elapsed."""
    _evaluate([_payload(risk_score=40)], NOW)
    _evaluate([_payload(risk_score=10)], DOWNGRADE_AT)


# --------------------------------------------------------------------------------------
# The lifecycle commit: nothing is sent until it lands
# --------------------------------------------------------------------------------------


def test_a_lifecycle_that_will_not_commit_writes_no_alert_and_sends_nothing(
    harness: Harness,
) -> None:
    harness.fail_commits = frozenset({1})

    with pytest.raises(CommitRefused):
        _evaluate([_payload(risk_score=40)], NOW)

    assert harness.notifier.attempted == []  # not merely undelivered: never attempted
    assert _alerts() == []  # and the evaluation really did roll back
    assert harness.sessions[-1].rollbacks == 1
    assert harness.sessions[-1].closed is True


# --------------------------------------------------------------------------------------
# A notification receipt that will not commit
# --------------------------------------------------------------------------------------


def test_a_notification_receipt_failure_keeps_the_alert_and_re_owes_only_the_message(
    harness: Harness,
) -> None:
    # Commit 1 is the lifecycle and lands; commit 2 is the notification's receipt and refuses.
    harness.fail_commits = frozenset({2})
    result = _evaluate([_payload(risk_score=40)], NOW)

    assert (result["delivered"], result["failed"]) == (0, 1)
    assert len(harness.sent(AlertActionKind.NOTIFICATION)) == 1  # it really went out
    [alert] = _alerts()
    assert alert.state == "open"  # the lifecycle survived the receipt's rollback...
    assert alert.notified_at is None  # ...and the message is owed again, which is the truth

    # The retry Stage1Task would run. It must not open a second alert, and the replayed
    # evaluation owes nothing new: the row it would have created is already there.
    harness.fail_commits = frozenset()
    retried = _evaluate([_payload(risk_score=40)], NOW)

    assert retried["outcomes"][0]["kind"] == "updated"
    assert retried["outcomes"][0]["notification_required"] is False
    assert retried["delivered"] == 0
    assert len(_alerts()) == 1
    assert len(harness.sent(AlertActionKind.NOTIFICATION)) == 1  # no second send from the retry

    # The sweep is what redelivers it -- and the receipt, this time, commits.
    sweep = alert_tasks.run_pending_alert_notification_sweep(now=NOW.isoformat())

    assert sweep["delivered"] == 1
    assert len(harness.sent(AlertActionKind.NOTIFICATION)) == 2
    assert _alerts()[0].notified_at is not None


# --------------------------------------------------------------------------------------
# An all-clear receipt that will not commit: the invariant this repair exists for
# --------------------------------------------------------------------------------------


def test_an_all_clear_receipt_failure_never_re_resolves_or_re_announces_the_ending(
    harness: Harness,
) -> None:
    _open_then_resolve(harness)

    # The resolve run: lifecycle commits, the all-clear is delivered, and its receipt refuses.
    harness.fail_commits = frozenset({2})
    resolved = _evaluate([_payload(risk_score=10)], RESOLVE_AT)

    assert resolved["outcomes"][0]["kind"] == "resolved"
    assert (resolved["delivered"], resolved["failed"]) == (0, 1)
    [alert] = _alerts()
    # Before the repair this row was rolled back to `downgraded` -- and the retry below resolved
    # it a second time and announced the ending a second time. The resolution is durable now.
    assert alert.state == "resolved"
    assert alert.resolved_at is not None
    assert alert.all_clear_notified_at is None
    assert len(harness.sent(AlertActionKind.ALL_CLEAR)) == 1
    first_key = harness.sent(AlertActionKind.ALL_CLEAR)[0].idempotency_key

    # The retry. The alert is already resolved, so there is no live row for the key: the
    # observation decides nothing, and no second all-clear is produced.
    harness.fail_commits = frozenset()
    retried = _evaluate([_payload(risk_score=10)], RESOLVE_AT)

    assert retried["outcomes"][0]["kind"] == "noop"
    assert retried["outcomes"][0]["all_clear_required"] is False
    assert len(harness.sent(AlertActionKind.ALL_CLEAR)) == 1  # the retry announced nothing
    assert len(_alerts()) == 1
    assert _alerts()[0].state == "resolved"

    # The sweep redelivers the one all-clear that really is still owed -- under the same
    # idempotency key, so a channel that honours it can see this is the same ending, not a new
    # one. This is the residual at-least-once window, named and closed at the channel.
    sweep = alert_tasks.run_pending_alert_notification_sweep(now=RESOLVE_AT.isoformat())

    assert sweep["delivered"] == 1
    redelivered = harness.sent(AlertActionKind.ALL_CLEAR)[1]
    assert redelivered.idempotency_key == first_key == f"all_clear:{alert.id}"
    assert _alerts()[0].all_clear_notified_at is not None

    # And once the receipt is durable, nothing owes the ending again. Ever.
    again = alert_tasks.run_pending_alert_notification_sweep(now=RESOLVE_AT.isoformat())

    assert (again["pending_all_clears"], again["delivered"]) == (0, 0)
    assert len(harness.sent(AlertActionKind.ALL_CLEAR)) == 2


# --------------------------------------------------------------------------------------
# One receipt among several
# --------------------------------------------------------------------------------------


def test_a_receipt_failure_does_not_roll_back_the_receipts_committed_before_it(
    harness: Harness,
) -> None:
    # Commit 1 is the lifecycle, 2 is the first alert's receipt, 3 is the second's -- and only 3
    # refuses. Actions are delivered in observation order, so the first alert is `eurozone`.
    harness.fail_commits = frozenset({3})

    result = _evaluate(
        [
            _payload(risk_score=40, scope_entity="eurozone"),
            _payload(risk_score=40, scope_entity="nordics"),
        ],
        NOW,
    )

    assert (result["delivered"], result["failed"]) == (1, 1)
    assert len(harness.sent(AlertActionKind.NOTIFICATION)) == 2  # both really went out
    by_key = {alert.dedupe_key: alert for alert in _alerts()}
    assert by_key["banking:eurozone:composite_score"].notified_at is not None  # durable, and stays
    assert by_key["banking:nordics:composite_score"].notified_at is None  # owed, and retryable


# --------------------------------------------------------------------------------------
# A channel that declines: still the sweep's job, still no receipt
# --------------------------------------------------------------------------------------


def test_a_declined_delivery_stays_owed_until_a_sweep_the_channel_accepts(
    harness: Harness, monkeypatch
) -> None:
    declining = RecordingAlertNotifier(fail_kinds=(AlertActionKind.NOTIFICATION,))
    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: declining)

    opened = _evaluate([_payload(risk_score=40)], NOW)
    assert (opened["delivered"], opened["failed"]) == (0, 1)
    assert _alerts()[0].notified_at is None
    assert harness.sessions[-1].commits == 1  # the lifecycle; no receipt to commit

    declined_sweep = alert_tasks.run_pending_alert_notification_sweep(now=NOW.isoformat())
    assert (declined_sweep["delivered"], declined_sweep["failed"]) == (0, 1)
    assert _alerts()[0].notified_at is None

    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: harness.notifier)
    accepted_sweep = alert_tasks.run_pending_alert_notification_sweep(now=NOW.isoformat())

    assert accepted_sweep["delivered"] == 1
    assert _alerts()[0].notified_at is not None
