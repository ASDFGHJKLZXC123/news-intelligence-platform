"""Alert operator endpoint tests: acknowledge, acknowledge-all-clear, supersede (ADR 0010).

These hit the FastAPI app through ``TestClient`` with the ``get_session`` dependency
overridden by a small fake session (matching the fake-DB-session convention used in
``tests/unit/test_provider_data_tasks.py``), not with a real Postgres connection. The fake
only needs to support ``.get(Alert, id, **kwargs)`` -- which is everything
``services.alerts.repository.SQLAlchemyAlertRepository.get_alert`` calls -- plus
``.flush()``/``.commit()``/``.close()``.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from db.base import get_session
from db.models.core import Alert

NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)
USER_ID = uuid.uuid4()


class FakeSession:
    """Backs both the endpoint's own ``session.get``/``session.commit`` and the repository's."""

    def __init__(self, alerts: dict[uuid.UUID, Any]) -> None:
        self._alerts = alerts
        self.commits = 0
        self.flushes = 0
        self.closed = False

    def get(self, model: Any, key: Any, **_kwargs: Any) -> Any:
        if model is Alert:
            return self._alerts.get(key)
        return None

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


def _alert(**overrides: Any) -> Any:
    """A row shaped like ``db.models.core.Alert`` -- attribute access and mutation, no ORM."""
    from types import SimpleNamespace

    defaults = dict(
        id=uuid.uuid4(),
        user_id=USER_ID,
        alert_rule_id=None,
        title="Eurozone banking stress",
        message="Composite banking risk is elevated.",
        severity="high",
        risk_score=decimal.Decimal("64"),
        alert_type="risk_score",
        state="open",
        related_event_id=None,
        related_company_id=None,
        related_industry_id=None,
        dedupe_key="banking:eurozone:composite_score",
        evidence_refs=[],
        evidence_signal_ids=[],
        score_version="v1",
        what_could_reduce_risk=[],
        news_driven=None,
        experimental=True,
        superseded_by=None,
        notified_at=None,
        all_clear_notified_at=None,
        created_at=NOW,
        updated_at=NOW,
        resolved_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def alerts_store() -> dict[uuid.UUID, Any]:
    return {}


@pytest.fixture
def client(alerts_store: dict[uuid.UUID, Any]) -> Iterator[TestClient]:
    session = FakeSession(alerts_store)

    def _fake_get_session() -> Iterator[Any]:
        yield session

    app.dependency_overrides[get_session] = _fake_get_session
    try:
        yield TestClient(app, client=("127.0.0.1", 5000))
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------------------
# acknowledge
# --------------------------------------------------------------------------------------


def test_acknowledge_sets_notified_at_and_returns_the_serialized_alert(
    client: TestClient, alerts_store: dict[uuid.UUID, Any]
) -> None:
    alert = _alert(state="escalated", notified_at=None)
    alerts_store[alert.id] = alert

    response = client.post(f"/api/v1/alerts/{alert.id}/acknowledge")

    assert response.status_code == 200
    body = response.json()["alert"]
    assert body["id"] == str(alert.id)
    assert body["status"] == "escalated"
    assert alert.notified_at is not None  # written by the service, visible on the same row


def test_acknowledge_unknown_alert_is_404(client: TestClient) -> None:
    response = client.post(f"/api/v1/alerts/{uuid.uuid4()}/acknowledge")

    assert response.status_code == 404
    assert "not found" in response.json()["error"]["message"]


# --------------------------------------------------------------------------------------
# acknowledge-all-clear
# --------------------------------------------------------------------------------------


def test_acknowledge_all_clear_on_a_resolved_alert_succeeds(
    client: TestClient, alerts_store: dict[uuid.UUID, Any]
) -> None:
    alert = _alert(state="resolved", severity="low", all_clear_notified_at=None)
    alerts_store[alert.id] = alert

    response = client.post(f"/api/v1/alerts/{alert.id}/acknowledge-all-clear")

    assert response.status_code == 200
    body = response.json()
    assert body["acknowledged"] is True
    assert body["alert"]["status"] == "resolved"
    assert alert.all_clear_notified_at is not None


def test_acknowledge_all_clear_twice_is_an_idempotent_noop(
    client: TestClient, alerts_store: dict[uuid.UUID, Any]
) -> None:
    already = NOW - datetime.timedelta(hours=1)
    alert = _alert(state="resolved", severity="low", all_clear_notified_at=already)
    alerts_store[alert.id] = alert

    response = client.post(f"/api/v1/alerts/{alert.id}/acknowledge-all-clear")

    assert response.status_code == 200
    assert response.json()["acknowledged"] is False
    assert alert.all_clear_notified_at == already  # not stamped a second, later time


def test_acknowledge_all_clear_on_a_live_alert_is_409(
    client: TestClient, alerts_store: dict[uuid.UUID, Any]
) -> None:
    alert = _alert(state="open")
    alerts_store[alert.id] = alert

    response = client.post(f"/api/v1/alerts/{alert.id}/acknowledge-all-clear")

    assert response.status_code == 409
    assert "cannot all-clear" in response.json()["error"]["message"]


def test_acknowledge_all_clear_unknown_alert_is_404(client: TestClient) -> None:
    response = client.post(f"/api/v1/alerts/{uuid.uuid4()}/acknowledge-all-clear")

    assert response.status_code == 404


# --------------------------------------------------------------------------------------
# supersede
# --------------------------------------------------------------------------------------


def test_supersede_replaces_the_narrower_alert_with_the_broader_one(
    client: TestClient, alerts_store: dict[uuid.UUID, Any]
) -> None:
    narrower = _alert(state="open", dedupe_key="banking:france:composite_score")
    broader = _alert(state="open", dedupe_key="banking:eurozone:composite_score")
    alerts_store[narrower.id] = narrower
    alerts_store[broader.id] = broader

    response = client.post(
        f"/api/v1/alerts/{narrower.id}/supersede",
        json={"broader_alert_id": str(broader.id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["alert"]["id"] == str(broader.id)
    assert body["superseded_ids"] == [str(narrower.id)]
    assert narrower.state == "superseded"
    assert narrower.superseded_by == broader.id
    assert broader.state == "open"  # the replacement itself is untouched


def test_supersede_unknown_broader_alert_is_404(
    client: TestClient, alerts_store: dict[uuid.UUID, Any]
) -> None:
    narrower = _alert(state="open")
    alerts_store[narrower.id] = narrower

    response = client.post(
        f"/api/v1/alerts/{narrower.id}/supersede",
        json={"broader_alert_id": str(uuid.uuid4())},
    )

    assert response.status_code == 404
    assert "unknown alert" in response.json()["error"]["message"]


def test_supersede_a_terminal_narrower_alert_is_409(
    client: TestClient, alerts_store: dict[uuid.UUID, Any]
) -> None:
    narrower = _alert(state="resolved")
    broader = _alert(state="open")
    alerts_store[narrower.id] = narrower
    alerts_store[broader.id] = broader

    response = client.post(
        f"/api/v1/alerts/{narrower.id}/supersede",
        json={"broader_alert_id": str(broader.id)},
    )

    assert response.status_code == 409
    assert "terminal" in response.json()["error"]["message"]


def test_supersede_across_owners_is_409(
    client: TestClient, alerts_store: dict[uuid.UUID, Any]
) -> None:
    other_user = uuid.uuid4()
    narrower = _alert(state="open", user_id=other_user)
    broader = _alert(state="open", user_id=USER_ID)
    alerts_store[narrower.id] = narrower
    alerts_store[broader.id] = broader

    response = client.post(
        f"/api/v1/alerts/{narrower.id}/supersede",
        json={"broader_alert_id": str(broader.id)},
    )

    assert response.status_code == 409
