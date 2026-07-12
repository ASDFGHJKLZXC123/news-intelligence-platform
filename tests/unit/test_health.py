"""Health and metrics endpoint tests.

Dependency functions are overridden so the suite never needs a live PostgreSQL or
Redis. The middleware (request id, API-key stub) is exercised through the real app.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.deps import (
    ComponentStatus,
    check_config,
    check_database,
    check_redis,
    check_worker_config,
)
from apps.api.main import app
from packages.config.settings import get_settings


def _ok(name: str):
    return lambda: ComponentStatus(name=name, ok=True, detail="stub-ok")


def _bad(name: str):
    return lambda: ComponentStatus(name=name, ok=False, detail="stub-down")


def test_health_ok_when_all_components_up() -> None:
    app.dependency_overrides[check_config] = _ok("config")
    app.dependency_overrides[check_database] = _ok("database")
    app.dependency_overrides[check_redis] = _ok("redis")
    app.dependency_overrides[check_worker_config] = _ok("worker")
    try:
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # All four Stage 1 concerns are reported.
        assert set(body["components"]) == {"config", "database", "redis", "worker"}
        # Request-id middleware echoes a correlation id.
        assert resp.headers.get("X-Request-ID")
    finally:
        app.dependency_overrides.clear()


def test_health_degraded_when_database_down() -> None:
    app.dependency_overrides[check_config] = _ok("config")
    app.dependency_overrides[check_database] = _bad("database")
    app.dependency_overrides[check_redis] = _ok("redis")
    app.dependency_overrides[check_worker_config] = _ok("worker")
    try:
        resp = TestClient(app).get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"
    finally:
        app.dependency_overrides.clear()


def test_health_degraded_when_config_down() -> None:
    app.dependency_overrides[check_config] = _bad("config")
    app.dependency_overrides[check_database] = _ok("database")
    app.dependency_overrides[check_redis] = _ok("redis")
    app.dependency_overrides[check_worker_config] = _ok("worker")
    try:
        resp = TestClient(app).get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["components"]["config"]["ok"] is False
    finally:
        app.dependency_overrides.clear()


def test_health_degraded_when_worker_down() -> None:
    app.dependency_overrides[check_config] = _ok("config")
    app.dependency_overrides[check_database] = _ok("database")
    app.dependency_overrides[check_redis] = _ok("redis")
    app.dependency_overrides[check_worker_config] = _bad("worker")
    try:
        resp = TestClient(app).get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["components"]["worker"]["ok"] is False
    finally:
        app.dependency_overrides.clear()


def test_real_db_and_redis_checks_are_exception_safe() -> None:
    # The unit suite has no live PostgreSQL/Redis: the real checks must return a
    # ComponentStatus (degraded if unreachable), never raise.
    db_status = check_database()
    assert isinstance(db_status, ComponentStatus)
    assert db_status.name == "database"

    redis_status = check_redis()
    assert isinstance(redis_status, ComponentStatus)
    assert redis_status.name == "redis"


def test_config_check_requires_celery_result_backend(monkeypatch) -> None:
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "")
    get_settings.cache_clear()
    try:
        status = check_config()
        assert status.ok is False
        assert "celery_result_backend missing" in status.detail
    finally:
        get_settings.cache_clear()


def test_worker_config_check_requires_result_backend(monkeypatch) -> None:
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "")
    get_settings.cache_clear()
    try:
        status = check_worker_config()
        assert status.ok is False
        assert "result backend" in status.detail
    finally:
        get_settings.cache_clear()


def test_metrics_endpoint_reports_counters() -> None:
    resp = TestClient(app).get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert "requests_total" in body
    # Hitting the endpoint counts at least one request via the middleware.
    assert body["requests_total"] >= 1


def test_inbound_request_id_is_preserved() -> None:
    resp = TestClient(app).get("/metrics", headers={"X-Request-ID": "abc-123"})
    assert resp.headers.get("X-Request-ID") == "abc-123"
