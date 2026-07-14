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

from db.models.enums import RiskLevel
from services.alerts import InMemoryAlertRepository
from services.alerts.notifications import AlertActionKind, RecordingAlertNotifier
from workers import alert_tasks
from workers.celery_app import Stage1Task, celery_app

USER_ID = uuid.uuid4()
NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)


class FakeSession:
    """Only what ``run_alert_evaluation``/the sweep call directly on the session."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeSweepSession(FakeSession):
    """Supports the sweep's two raw ``select(Alert)`` reads and the repository's ``get``."""

    def __init__(self, alerts: list[Any], query_results: list[list[Any]]) -> None:
        super().__init__()
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
    monkeypatch: Any, *, repository: InMemoryAlertRepository | None = None
) -> tuple[InMemoryAlertRepository, RecordingAlertNotifier, list[FakeSession]]:
    repository = repository if repository is not None else InMemoryAlertRepository()
    notifier = RecordingAlertNotifier()
    sessions: list[FakeSession] = []

    def session_factory() -> FakeSession:
        session = FakeSession()
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

    result = alert_tasks.run_alert_evaluation(
        [_observation_payload(risk_score=40)], now=_iso(NOW)
    )

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

    assert sessions[-1].commits == 1
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
# run_alert_evaluation: all-clear on resolution
# --------------------------------------------------------------------------------------


def test_run_alert_evaluation_delivers_an_explicit_all_clear_on_resolution(monkeypatch) -> None:
    """ADR 0010: "resolution emits an explicit all-clear entry -- de-escalation is
    information, not silence." Three runs: open, drop below the clear band (starts the
    7-day resolve timer), then resolve once the timer has fully elapsed.
    """
    repository, notifier, _sessions = _install_fakes(monkeypatch)

    opened = alert_tasks.run_alert_evaluation(
        [_observation_payload(risk_score=40)], now=_iso(NOW)
    )
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
    alert_tasks.run_alert_evaluation(
        [_observation_payload(risk_score=10)], now=_iso(downgrade_at)
    )
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
    assert session.commits == 1
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
