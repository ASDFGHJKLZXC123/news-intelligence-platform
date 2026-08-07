"""Health and metrics endpoint tests.

Dependency functions are overridden so the suite never needs a live PostgreSQL or
Redis. The middleware (request id, API-key stub) is exercised through the real app.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api import deps
from apps.api.deps import (
    WORKER_INSPECT_TIMEOUT_SECONDS,
    ComponentStatus,
    check_config,
    check_database,
    check_redis,
    check_worker_config,
)
from apps.api.main import app
from apps.api.middleware import REQUEST_ID_HEADER
from packages.config.settings import get_settings


def _ok(name: str):
    return lambda: ComponentStatus(name=name, ok=True, detail="stub-ok")


def _bad(name: str):
    return lambda: ComponentStatus(name=name, ok=False, detail="stub-down")


def _assert_error_envelope(resp) -> dict:
    """A non-2xx health response uses the shared ``{"error": {...}}`` envelope with the
    correlation id echoed in both the body and the X-Request-ID header. Returns the
    inner error object for further assertions."""
    body = resp.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert error["request_id"] == resp.headers[REQUEST_ID_HEADER]
    return error


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
        # A degraded 503 uses the shared error envelope, not a bespoke top-level body.
        error = _assert_error_envelope(resp)
        assert error["code"] == "service_unavailable"
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
        error = _assert_error_envelope(resp)
        assert error["code"] == "service_unavailable"
        # The failing component's diagnostics are preserved in the envelope message.
        assert "config" in error["message"]
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
        error = _assert_error_envelope(resp)
        assert error["code"] == "service_unavailable"
        assert "worker" in error["message"]
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


def test_redis_health_probe_bounds_connect_and_response_waits(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Client:
        def ping(self) -> None:
            return None

        def close(self) -> None:
            return None

    def from_url(url: str, **kwargs):
        captured.update({"url": url, **kwargs})
        return Client()

    monkeypatch.setattr("redis.Redis.from_url", from_url)

    assert check_redis().ok is True
    assert captured["socket_connect_timeout"] == 2
    assert captured["socket_timeout"] == 2
    assert captured["retry_on_timeout"] is False


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


def test_worker_check_requires_live_workers_covering_every_configured_queue(monkeypatch) -> None:
    seen_timeouts: list[float] = []

    def inspect_queues(timeout_seconds: float) -> object:
        seen_timeouts.append(timeout_seconds)
        return {
            "worker-name-is-not-exposed": [
                {"name": "default"},
                {"name": "ingestion"},
                {"name": "pipeline"},
            ]
        }

    monkeypatch.setattr(deps, "_inspect_active_worker_queues", inspect_queues)

    status = check_worker_config()

    assert status == ComponentStatus(
        name="worker",
        ok=True,
        detail="reachable; live_workers=1; queues=default,ingestion,pipeline",
    )
    assert seen_timeouts == [WORKER_INSPECT_TIMEOUT_SECONDS]
    assert "worker-name-is-not-exposed" not in status.detail


def test_worker_check_degrades_when_no_worker_replies(monkeypatch) -> None:
    monkeypatch.setattr(deps, "_inspect_active_worker_queues", lambda _timeout: None)

    status = check_worker_config()

    assert status == ComponentStatus(name="worker", ok=False, detail="no valid live worker replies")


def test_worker_check_degrades_when_a_configured_queue_has_no_consumer(monkeypatch) -> None:
    monkeypatch.setattr(
        deps,
        "_inspect_active_worker_queues",
        lambda _timeout: {"worker-a": [{"name": "default"}, {"name": "ingestion"}]},
    )

    status = check_worker_config()

    assert status == ComponentStatus(
        name="worker",
        ok=False,
        detail="live workers missing required queues: pipeline",
    )
    assert "worker-a" not in status.detail


def test_worker_check_sanitizes_probe_failures(monkeypatch) -> None:
    def fail(_timeout: float) -> object:
        raise ConnectionError("redis://user:secret@internal-broker:6379/1")

    monkeypatch.setattr(deps, "_inspect_active_worker_queues", fail)

    status = check_worker_config()

    assert status == ComponentStatus(name="worker", ok=False, detail="ConnectionError")
    assert "secret" not in status.detail
    assert "internal-broker" not in status.detail


def test_worker_check_rejects_malformed_queue_replies(monkeypatch) -> None:
    monkeypatch.setattr(
        deps,
        "_inspect_active_worker_queues",
        lambda _timeout: {"worker-a": [{"routing_key": "default"}]},
    )

    status = check_worker_config()

    assert status == ComponentStatus(name="worker", ok=False, detail="no valid live worker replies")


def test_live_worker_probe_bounds_connection_retries_and_closes(monkeypatch) -> None:
    replies = {"worker-a": [{"name": "default"}]}
    connection_kwargs: dict[str, object] = {}
    inspect_kwargs: dict[str, object] = {}
    ensure_kwargs: list[dict[str, object]] = []

    class FakeConnection:
        closed = False

        def ensure(self, _obj, fun, **kwargs):
            ensure_kwargs.append(kwargs)
            return fun

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    class FakeInspector:
        def active_queues(self) -> object:
            # Model pidbox's retry-enabled publication after the probe has wrapped this
            # connection. Even an explicit larger value must be pinned to zero.
            operation = connection.ensure(object(), lambda: replies, max_retries=99)
            return operation()

    class FakeControl:
        def inspect(self, **kwargs):
            inspect_kwargs.update(kwargs)
            return FakeInspector()

    def connection_for_write(**kwargs):
        connection_kwargs.update(kwargs)
        return connection

    fake_app = SimpleNamespace(
        conf=SimpleNamespace(broker_transport_options={"visibility_timeout": 30}),
        connection_for_write=connection_for_write,
        control=FakeControl(),
    )
    monkeypatch.setattr("workers.celery_app.celery_app", fake_app)

    result = deps._inspect_active_worker_queues(WORKER_INSPECT_TIMEOUT_SECONDS)

    assert result == replies
    assert connection.closed is True
    assert connection_kwargs["connect_timeout"] == WORKER_INSPECT_TIMEOUT_SECONDS
    transport_options = connection_kwargs["transport_options"]
    assert isinstance(transport_options, dict)
    assert transport_options["visibility_timeout"] == 30
    assert transport_options["max_retries"] == 0
    assert transport_options["connect_retries_timeout"] == WORKER_INSPECT_TIMEOUT_SECONDS
    assert transport_options["socket_connect_timeout"] == WORKER_INSPECT_TIMEOUT_SECONDS
    assert transport_options["socket_timeout"] == WORKER_INSPECT_TIMEOUT_SECONDS
    assert inspect_kwargs == {
        "timeout": WORKER_INSPECT_TIMEOUT_SECONDS,
        "limit": deps.WORKER_INSPECT_MAX_REPLIES,
        "connection": connection,
    }
    assert ensure_kwargs[0]["max_retries"] == 0


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
