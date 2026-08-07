"""Alert pipeline Celery task tests (ADR 0010): stubbed repository, session, and notifier.

These tests exercise orchestration -- the task builds the right ``AlertObservation``, calls
the (already thoroughly tested elsewhere, in ``test_alert_persistence.py``) lifecycle service,
and delivers/acknowledges the resulting actions -- not the lifecycle policy itself. The real
:class:`~services.alerts.repository.InMemoryAlertRepository` is used in place of a live
Postgres session wherever the task lets us inject one, exactly as
``workers/entity_linking_tasks.py``'s docstring describes: "Unit tests replace ``SessionLocal``
and the two build hooks with fakes, so importing this module costs nothing and reaches
nothing."
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from db.models.enums import RiskLevel
from services.alerts import InMemoryAlertRepository
from services.alerts.notifications import AlertActionKind, RecordingAlertNotifier
from workers import alert_tasks
from workers.celery_app import Stage1Task, celery_app

USER_ID = uuid.uuid4()
NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _prediction_outputs_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy alert-worker tests exercise the diagnostic/open Gate-G path explicitly."""

    monkeypatch.setattr(
        alert_tasks,
        "get_settings",
        lambda: SimpleNamespace(crisis_prediction_reads_enabled=True),
    )


class CommitFailed(RuntimeError):
    """A commit the database refused, injected where the ordering matters."""


class FakeSession:
    """Only what ``run_alert_evaluation``/the sweep call directly on the session.

    ``fail_commits`` names the 1-based commits that raise. These tests assert *ordering* --
    which sends happen before which commits, and which action a failure costs -- because that is
    all a fake can honestly prove. What a rollback actually erases is proved against a real
    PostgreSQL in ``tests/integration/test_alert_delivery_durability.py``.
    """

    def __init__(self, fail_commits: frozenset[int] = frozenset()) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._fail_commits = fail_commits

    def commit(self) -> None:
        self.commits += 1
        if self.commits in self._fail_commits:
            raise CommitFailed(f"commit {self.commits} would not commit")

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeSweepSession(FakeSession):
    """Supports the sweep's two raw ``select(Alert)`` reads and the repository's ``get``."""

    def __init__(
        self,
        alerts: list[Any],
        query_results: list[list[Any]],
        fail_commits: frozenset[int] = frozenset(),
    ) -> None:
        super().__init__(fail_commits=fail_commits)
        self._alerts_by_id = {alert.id: alert for alert in alerts}
        self._query_results = list(query_results)
        self.flushes = 0

    def execute(self, _statement: Any) -> Any:
        rows = self._query_results.pop(0)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    def get(self, _model: Any, key: Any, **_kwargs: Any) -> Any:
        return self._alerts_by_id.get(key)

    def flush(self) -> None:
        self.flushes += 1


def _install_fakes(
    monkeypatch: Any,
    *,
    repository: InMemoryAlertRepository | None = None,
    fail_commits: frozenset[int] = frozenset(),
) -> tuple[InMemoryAlertRepository, RecordingAlertNotifier, list[FakeSession]]:
    repository = repository if repository is not None else InMemoryAlertRepository()
    notifier = RecordingAlertNotifier()
    sessions: list[FakeSession] = []

    def session_factory() -> FakeSession:
        session = FakeSession(fail_commits=fail_commits)
        sessions.append(session)
        return session

    monkeypatch.setattr(alert_tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(alert_tasks, "SQLAlchemyAlertRepository", lambda _session: repository)
    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: notifier)
    return repository, notifier, sessions


def _observation_payload(*, risk_score: float, **overrides: Any) -> dict[str, Any]:
    scope = {
        "user_id": str(USER_ID),
        "risk_type": "banking",
        "scope_entity": "eurozone",
        "condition_class": "composite_score",
        "title": "Eurozone banking stress",
        "message": "Composite banking risk is elevated.",
        "alert_type": "risk_score",
    }
    scope.update(overrides.pop("scope", {}))
    payload: dict[str, Any] = {"scope": scope, "risk_score": risk_score}
    payload.update(overrides)
    return payload


def _iso(value: datetime.datetime) -> str:
    return value.isoformat()


# --------------------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------------------


def test_alert_tasks_are_registered_with_retry_defaults() -> None:
    names = {
        "workers.alert_tasks.run_alert_evaluation",
        "workers.alert_tasks.run_pending_alert_notification_sweep",
    }
    assert names <= set(celery_app.tasks)
    for name in names:
        assert isinstance(celery_app.tasks[name], Stage1Task)


def test_alert_evaluation_is_not_beat_scheduled_but_the_sweep_is() -> None:
    # See workers/celery_app.py's ALERT_BEAT_SCHEDULE comment: the evaluation task takes
    # observations no clock can invent yet, the sweep reads real rows and can run on a clock.
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert "workers.alert_tasks.run_pending_alert_notification_sweep" in scheduled
    assert "workers.alert_tasks.run_alert_evaluation" not in scheduled


@pytest.mark.parametrize(
    ("task", "args", "zero_field"),
    [
        (
            alert_tasks.run_alert_evaluation,
            ([_observation_payload(risk_score=40)],),
            "evaluated",
        ),
        (alert_tasks.run_pending_alert_notification_sweep, (), "pending_notifications"),
    ],
)
def test_gate_g_closed_skips_alert_tasks_before_db_or_notifier(
    monkeypatch: pytest.MonkeyPatch,
    task: Any,
    args: tuple[Any, ...],
    zero_field: str,
) -> None:
    monkeypatch.setattr(
        alert_tasks,
        "get_settings",
        lambda: SimpleNamespace(crisis_prediction_reads_enabled=False),
    )
    monkeypatch.setattr(
        alert_tasks,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("closed gate opened a DB session")),
    )
    monkeypatch.setattr(
        alert_tasks,
        "build_notifier",
        lambda: (_ for _ in ()).throw(AssertionError("closed gate built a notifier")),
    )

    result = task(*args, now=_iso(NOW))

    assert result["status"] == "skipped"
    assert result["state"] == "skipped"
    assert result["reason"] == "crisis_prediction_reads_disabled"
    assert result[zero_field] == 0


# --------------------------------------------------------------------------------------
# observation_from_payload
# --------------------------------------------------------------------------------------


def test_observation_from_payload_builds_a_typed_observation() -> None:
    payload = _observation_payload(risk_score=42, score_version="v3", score_basis="composite")

    observation = alert_tasks.observation_from_payload(payload)

    assert observation.scope.user_id == USER_ID
    assert observation.scope.scope_entity == "eurozone"
    assert observation.score == 42
    assert observation.score_version == "v3"
    assert observation.score_basis.value == "composite"


# --------------------------------------------------------------------------------------
# run_alert_evaluation: happy path
# --------------------------------------------------------------------------------------


def test_run_alert_evaluation_opens_an_alert_and_delivers_the_notification(monkeypatch) -> None:
    repository, notifier, sessions = _install_fakes(monkeypatch)

    result = alert_tasks.run_alert_evaluation([_observation_payload(risk_score=40)], now=_iso(NOW))

    assert result["status"] == "ok"
    assert result["evaluated"] == 1
    outcome = result["outcomes"][0]
    assert outcome["kind"] == "created"
    assert outcome["notification_required"] is True
    assert outcome["all_clear_required"] is False
    assert result["delivered"] == 1
    assert result["failed"] == 0

    assert len(repository.alerts) == 1
    alert = repository.alerts[0]
    assert alert.state == "open"
    assert alert.notified_at is not None  # acknowledged only after a successful delivery
    assert len(notifier.delivered) == 1
    assert notifier.delivered[0].kind is AlertActionKind.NOTIFICATION

    # One commit for the lifecycle, before anything was sent; one for the receipt of the one
    # action that was.
    assert sessions[-1].commits == 2
    assert sessions[-1].rollbacks == 0
    assert sessions[-1].closed is True


def test_run_alert_evaluation_evaluates_a_batch_independently(monkeypatch) -> None:
    repository, notifier, _sessions = _install_fakes(monkeypatch)

    result = alert_tasks.run_alert_evaluation(
        [
            _observation_payload(risk_score=40, scope={"scope_entity": "eurozone"}),
            _observation_payload(risk_score=20, scope={"scope_entity": "nordics"}),
        ],
        now=_iso(NOW),
    )

    assert result["evaluated"] == 2
    kinds = {outcome["dedupe_key"]: outcome["kind"] for outcome in result["outcomes"]}
    # 40 clears the Medium enter threshold (37); 20 does not, so the second is a no-op.
    assert any(kind == "created" for kind in kinds.values())
    assert any(kind == "noop" for kind in kinds.values())
    assert len(repository.alerts) == 1
    assert len(notifier.delivered) == 1


def test_run_alert_evaluation_rolls_back_and_reraises_on_failure(monkeypatch) -> None:
    _repository, _notifier, sessions = _install_fakes(monkeypatch)

    bad_payload = {"scope": {"user_id": str(USER_ID)}, "risk_score": 40}  # missing required keys

    try:
        alert_tasks.run_alert_evaluation([bad_payload], now=_iso(NOW))
        raised = False
    except Exception:
        raised = True

    assert raised is True
    assert sessions[-1].rollbacks == 1
    assert sessions[-1].commits == 0
    assert sessions[-1].closed is True


# --------------------------------------------------------------------------------------
# run_alert_evaluation: the transaction boundary around a delivery
# --------------------------------------------------------------------------------------


def test_a_lifecycle_that_will_not_commit_delivers_nothing_at_all(monkeypatch) -> None:
    """The defect this repair exists for, from the other side: if the lifecycle commit fails,
    the run must not already have sent anything, because a send cannot be rolled back with it.
    """
    _repository, notifier, sessions = _install_fakes(monkeypatch, fail_commits=frozenset({1}))

    with pytest.raises(CommitFailed):
        alert_tasks.run_alert_evaluation([_observation_payload(risk_score=40)], now=_iso(NOW))

    assert notifier.attempted == []  # not "nothing delivered" -- nothing even *tried*
    assert sessions[-1].commits == 1
    assert sessions[-1].rollbacks == 1
    assert sessions[-1].closed is True


def test_a_receipt_that_will_not_commit_leaves_the_action_owed_and_the_task_successful(
    monkeypatch,
) -> None:
    # Commit 1 is the lifecycle (durable); commit 2 is the notification's receipt, and it fails.
    # The notification really went out, so the run reports it failed rather than delivered: the
    # receipt is NULL, and the sweep will find the message still owed.
    _repository, notifier, sessions = _install_fakes(monkeypatch, fail_commits=frozenset({2}))

    result = alert_tasks.run_alert_evaluation([_observation_payload(risk_score=40)], now=_iso(NOW))

    assert result["status"] == "ok"  # a receipt fault is not an evaluation fault
    assert result["outcomes"][0]["kind"] == "created"
    assert (result["delivered"], result["failed"]) == (0, 1)
    assert len(notifier.attempted) == 1
    assert sessions[-1].commits == 2
    assert sessions[-1].rollbacks == 1  # the receipt alone, never the lifecycle above it


def test_one_receipt_failure_does_not_cost_the_receipts_around_it(monkeypatch) -> None:
    # Two alerts open, two notifications. Commit 1 is the lifecycle, 2 is the first receipt, 3 is
    # the second -- and only 3 fails.
    _repository, notifier, sessions = _install_fakes(monkeypatch, fail_commits=frozenset({3}))

    result = alert_tasks.run_alert_evaluation(
        [
            _observation_payload(risk_score=40, scope={"scope_entity": "eurozone"}),
            _observation_payload(risk_score=40, scope={"scope_entity": "nordics"}),
        ],
        now=_iso(NOW),
    )

    assert (result["delivered"], result["failed"]) == (1, 1)
    assert len(notifier.attempted) == 2  # both really went out, in observation order
    assert sessions[-1].commits == 3
    assert sessions[-1].rollbacks == 1


# --------------------------------------------------------------------------------------
# run_alert_evaluation: all-clear on resolution
# --------------------------------------------------------------------------------------


def test_run_alert_evaluation_delivers_an_explicit_all_clear_on_resolution(monkeypatch) -> None:
    """ADR 0010: "resolution emits an explicit all-clear entry -- de-escalation is
    information, not silence." Three runs: open, drop below the clear band (starts the
    7-day resolve timer), then resolve once the timer has fully elapsed.
    """
    repository, notifier, _sessions = _install_fakes(monkeypatch)

    opened = alert_tasks.run_alert_evaluation([_observation_payload(risk_score=40)], now=_iso(NOW))
    assert opened["outcomes"][0]["kind"] == "created"

    downgrade_at = NOW + datetime.timedelta(hours=1)
    downgraded = alert_tasks.run_alert_evaluation(
        [_observation_payload(risk_score=10)], now=_iso(downgrade_at)
    )
    assert downgraded["outcomes"][0]["kind"] == "downgraded"
    assert downgraded["outcomes"][0]["all_clear_required"] is False

    resolve_at = downgrade_at + datetime.timedelta(days=7, seconds=1)
    resolved = alert_tasks.run_alert_evaluation(
        [_observation_payload(risk_score=10)], now=_iso(resolve_at)
    )
    outcome = resolved["outcomes"][0]
    assert outcome["kind"] == "resolved"
    assert outcome["all_clear_required"] is True
    assert resolved["delivered"] == 1

    all_clear_actions = [a for a in notifier.delivered if a.kind is AlertActionKind.ALL_CLEAR]
    assert len(all_clear_actions) == 1

    alert = repository.get_alert(uuid.UUID(outcome["alert_id"]))
    assert alert.state == "resolved"
    assert alert.all_clear_notified_at is not None


def test_run_alert_evaluation_leaves_all_clear_unacknowledged_when_delivery_fails(
    monkeypatch,
) -> None:
    repository = InMemoryAlertRepository()
    _repo, _notifier, _sessions = _install_fakes(monkeypatch, repository=repository)
    failing_notifier = RecordingAlertNotifier(fail_kinds=(AlertActionKind.ALL_CLEAR,))
    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: failing_notifier)

    alert_tasks.run_alert_evaluation([_observation_payload(risk_score=40)], now=_iso(NOW))
    downgrade_at = NOW + datetime.timedelta(hours=1)
    alert_tasks.run_alert_evaluation([_observation_payload(risk_score=10)], now=_iso(downgrade_at))
    resolve_at = downgrade_at + datetime.timedelta(days=7, seconds=1)
    result = alert_tasks.run_alert_evaluation(
        [_observation_payload(risk_score=10)], now=_iso(resolve_at)
    )

    assert result["failed"] == 1
    assert result["delivered"] == 0
    alert = repository.alerts[0]
    assert alert.state == "resolved"
    # The notifier reported failure, so the receipt is never written: a future sweep still
    # finds this all-clear owed.
    assert alert.all_clear_notified_at is None


# --------------------------------------------------------------------------------------
# run_pending_alert_notification_sweep
# --------------------------------------------------------------------------------------


def _alert_row(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=USER_ID,
        dedupe_key="banking:eurozone:composite_score",
        severity=RiskLevel.HIGH.value,
        state="open",
        notified_at=None,
        all_clear_notified_at=None,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_sweep_redelivers_owed_notifications_and_all_clears(monkeypatch) -> None:
    never_notified = _alert_row(state="escalated", notified_at=None)
    never_all_cleared = _alert_row(state="resolved", severity=RiskLevel.LOW.value)

    session = FakeSweepSession(
        alerts=[never_notified, never_all_cleared],
        query_results=[[never_notified], [never_all_cleared]],
    )
    notifier = RecordingAlertNotifier()
    monkeypatch.setattr(alert_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: notifier)

    result = alert_tasks.run_pending_alert_notification_sweep(now=_iso(NOW))

    assert result["status"] == "ok"
    assert result["pending_notifications"] == 1
    assert result["pending_all_clears"] == 1
    assert result["delivered"] == 2
    assert result["failed"] == 0
    assert never_notified.notified_at == NOW
    assert never_all_cleared.all_clear_notified_at == NOW
    # One commit per receipt, and no batch commit around them: the sweep writes no lifecycle
    # state, so it has nothing to commit before it starts sending.
    assert session.commits == 2
    assert session.rollbacks == 0
    assert session.closed is True


def test_sweep_is_a_noop_when_nothing_is_owed(monkeypatch) -> None:
    session = FakeSweepSession(alerts=[], query_results=[[], []])
    notifier = RecordingAlertNotifier()
    monkeypatch.setattr(alert_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: notifier)

    result = alert_tasks.run_pending_alert_notification_sweep(now=_iso(NOW))

    assert result["pending_notifications"] == 0
    assert result["pending_all_clears"] == 0
    assert result["delivered"] == 0
    assert result["failed"] == 0
    assert notifier.delivered == []
    assert session.commits == 0


def test_a_sweep_delivery_the_channel_declines_stays_owed(monkeypatch) -> None:
    owed = _alert_row(state="resolved", severity=RiskLevel.LOW.value)
    session = FakeSweepSession(alerts=[owed], query_results=[[], [owed]])
    notifier = RecordingAlertNotifier(fail_kinds=(AlertActionKind.ALL_CLEAR,))
    monkeypatch.setattr(alert_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: notifier)

    result = alert_tasks.run_pending_alert_notification_sweep(now=_iso(NOW))

    assert (result["delivered"], result["failed"]) == (0, 1)
    assert owed.all_clear_notified_at is None  # never acknowledged, so the next sweep re-owes it
    assert session.commits == 0  # nothing was delivered, so there is no receipt to commit


def test_a_sweep_receipt_that_will_not_commit_stays_owed(monkeypatch) -> None:
    delivered_ok = _alert_row(state="escalated")
    doomed = _alert_row(state="resolved", severity=RiskLevel.LOW.value)
    session = FakeSweepSession(
        alerts=[delivered_ok, doomed],
        query_results=[[delivered_ok], [doomed]],
        fail_commits=frozenset({2}),
    )
    notifier = RecordingAlertNotifier()
    monkeypatch.setattr(alert_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: notifier)

    result = alert_tasks.run_pending_alert_notification_sweep(now=_iso(NOW))

    assert (result["delivered"], result["failed"]) == (1, 1)
    assert delivered_ok.notified_at == NOW  # committed before the all-clear was even sent
    assert len(notifier.attempted) == 2
    assert (session.commits, session.rollbacks) == (2, 1)
    assert session.closed is True


def test_the_sweep_redelivers_an_all_clear_under_its_original_idempotency_key(monkeypatch) -> None:
    """The residual window, and what closes it: an all-clear whose receipt never committed is
    sent again -- and a channel that honours the key can tell it is the same ending, not a new one.
    """
    owed = _alert_row(state="resolved", severity=RiskLevel.LOW.value)
    session = FakeSweepSession(alerts=[owed], query_results=[[], [owed]])
    notifier = RecordingAlertNotifier()
    monkeypatch.setattr(alert_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(alert_tasks, "build_notifier", lambda: notifier)

    alert_tasks.run_pending_alert_notification_sweep(now=_iso(NOW))

    redelivered = notifier.delivered[0]
    assert redelivered.kind is AlertActionKind.ALL_CLEAR
    assert redelivered.idempotency_key == f"all_clear:{owed.id}"
