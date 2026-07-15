"""HTTP wire-foundation contracts (Stage 7): the error envelope, request-id correlation,
CORS for the no-build static origin, and UTC-``Z`` timestamp serialization.

The envelope/middleware tests run against a tiny app wired with the *real* middleware and
exception handlers from ``apps.api.middleware`` (no database needed); the CORS and 404
tests run against the real application to prove ``apps.api.main`` wires them in.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

import apps.api.middleware as mw
from apps.api import intelligence, provider_data
from apps.api.deps import (
    ComponentStatus,
    check_config,
    check_database,
    check_redis,
    check_worker_config,
)
from apps.api.main import app as real_app
from apps.api.middleware import (
    REQUEST_ID_HEADER,
    APIKeyMiddleware,
    RequestIDMiddleware,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from packages.config.settings import Settings

_LOOPBACK = ("127.0.0.1", 50000)
_REMOTE = ("203.0.113.7", 50000)
_STATIC_ORIGIN = "http://localhost:3000"


def _wire_app() -> FastAPI:
    """A minimal app carrying the real wire-foundation middleware and handlers."""
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(RequestIDMiddleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/boom")
    def boom() -> dict:
        raise RuntimeError("secret internal detail that must not leak")

    @app.get("/raise-404")
    def raise_404() -> dict:
        raise HTTPException(status_code=404, detail="widget not found")

    @app.get("/items/{item_id}")
    def item(item_id: int) -> dict:
        return {"item_id": item_id}

    @app.post("/mutate")
    def mutate() -> dict:
        return {"ok": True}

    return app


def _assert_envelope(body: dict, request_id: str) -> None:
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert error["request_id"] == request_id


# --- Error envelope -------------------------------------------------------------------
def test_route_handler_http_exception_uses_envelope() -> None:
    resp = TestClient(_wire_app()).get("/raise-404")
    assert resp.status_code == 404
    body = resp.json()
    _assert_envelope(body, resp.headers[REQUEST_ID_HEADER])
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "widget not found"


def test_route_not_found_uses_envelope() -> None:
    resp = TestClient(_wire_app()).get("/no-such-route")
    assert resp.status_code == 404
    _assert_envelope(resp.json(), resp.headers[REQUEST_ID_HEADER])
    assert resp.json()["error"]["code"] == "not_found"


def test_validation_error_uses_envelope() -> None:
    resp = TestClient(_wire_app()).get("/items/not-an-int")
    assert resp.status_code == 422
    body = resp.json()
    _assert_envelope(body, resp.headers[REQUEST_ID_HEADER])
    assert body["error"]["code"] == "validation_error"


def test_unhandled_exception_becomes_a_safe_500_envelope() -> None:
    # raise_server_exceptions=False so the re-raised error is observed as an HTTP 500.
    resp = TestClient(_wire_app(), raise_server_exceptions=False).get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    _assert_envelope(body, resp.headers[REQUEST_ID_HEADER])
    assert body["error"]["code"] == "internal_error"
    # The generic message never leaks the internal exception text.
    assert "secret internal detail" not in resp.text


def test_request_id_is_preserved_and_matches_body_and_header() -> None:
    resp = TestClient(_wire_app()).get("/raise-404", headers={REQUEST_ID_HEADER: "corr-42"})
    assert resp.headers[REQUEST_ID_HEADER] == "corr-42"
    assert resp.json()["error"]["request_id"] == "corr-42"


def test_generated_request_id_matches_body_and_header() -> None:
    resp = TestClient(_wire_app()).get("/no-such-route")
    header_id = resp.headers[REQUEST_ID_HEADER]
    assert header_id  # a fresh correlation id was generated
    assert resp.json()["error"]["request_id"] == header_id


def test_inbound_request_id_is_preserved_on_a_500() -> None:
    resp = TestClient(_wire_app(), raise_server_exceptions=False).get(
        "/boom", headers={REQUEST_ID_HEADER: "corr-500"}
    )
    assert resp.status_code == 500
    assert resp.headers[REQUEST_ID_HEADER] == "corr-500"
    assert resp.json()["error"]["request_id"] == "corr-500"


# --- API-key middleware: envelope shape, unchanged authorization ----------------------
def _api_key_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, client: tuple[str, int] = _LOOPBACK
) -> TestClient:
    monkeypatch.setattr(mw, "get_settings", lambda: settings)
    return TestClient(_wire_app(), client=client)


def test_api_key_rejection_uses_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _api_key_client(Settings(app_env="prod", api_key="secret"), monkeypatch)
    resp = client.post("/mutate", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401
    body = resp.json()
    _assert_envelope(body, resp.headers[REQUEST_ID_HEADER])
    assert body["error"]["code"] == "unauthorized"


def test_no_key_remote_mutation_rejected_with_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _api_key_client(Settings(app_env="local", api_key=""), monkeypatch, client=_REMOTE)
    resp = client.post("/mutate")
    assert resp.status_code == 401
    _assert_envelope(resp.json(), resp.headers[REQUEST_ID_HEADER])


def test_mutation_authorization_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Good key still passes; local loopback without a key still passes -- only the
    # rejection body shape changed.
    good = _api_key_client(Settings(app_env="prod", api_key="secret"), monkeypatch)
    assert good.post("/mutate", headers={"X-API-Key": "secret"}).status_code == 200

    loopback = _api_key_client(Settings(app_env="local", api_key=""), monkeypatch, client=_LOOPBACK)
    assert loopback.post("/mutate").status_code == 200


# --- CORS for the no-build static origin ----------------------------------------------
def test_cors_default_allow_list_covers_both_static_host_spellings() -> None:
    origins = Settings().cors_origins_list
    assert "http://localhost:3000" in origins
    assert "http://127.0.0.1:3000" in origins
    assert "*" not in origins


def test_cors_preflight_from_static_origin_succeeds_without_credentials() -> None:
    resp = TestClient(real_app).options(
        "/api/v1/events",
        headers={
            "Origin": _STATIC_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == _STATIC_ORIGIN
    # allow_credentials is off, so the header is absent (or explicitly not "true").
    assert resp.headers.get("access-control-allow-credentials") in (None, "false")


def test_real_app_route_404_uses_envelope() -> None:
    resp = TestClient(real_app).get("/api/v1/definitely-not-a-route")
    assert resp.status_code == 404
    _assert_envelope(resp.json(), resp.headers[REQUEST_ID_HEADER])


def test_degraded_health_503_uses_envelope() -> None:
    # A non-2xx /health is a non-2xx response and must not escape the error envelope.
    real_app.dependency_overrides[check_config] = lambda: ComponentStatus(
        name="config", ok=True, detail="ok"
    )
    real_app.dependency_overrides[check_database] = lambda: ComponentStatus(
        name="database", ok=False, detail="down"
    )
    real_app.dependency_overrides[check_redis] = lambda: ComponentStatus(
        name="redis", ok=True, detail="ok"
    )
    real_app.dependency_overrides[check_worker_config] = lambda: ComponentStatus(
        name="worker", ok=True, detail="ok"
    )
    try:
        resp = TestClient(real_app).get("/health")
        assert resp.status_code == 503
        _assert_envelope(resp.json(), resp.headers[REQUEST_ID_HEADER])
        assert resp.json()["error"]["code"] == "service_unavailable"
    finally:
        real_app.dependency_overrides.clear()


# --- UTC-Z timestamp convergence ------------------------------------------------------
def test_iso_stamps_naive_datetime_as_utc_z() -> None:
    naive = datetime.datetime(2026, 6, 17, 12, 0, 0)
    assert provider_data._iso(naive) == "2026-06-17T12:00:00Z"


def test_iso_converts_aware_datetime_to_utc_z() -> None:
    eastern = datetime.timezone(datetime.timedelta(hours=-4))
    aware = datetime.datetime(2026, 6, 17, 8, 0, 0, tzinfo=eastern)
    assert provider_data._iso(aware) == "2026-06-17T12:00:00Z"


def test_iso_keeps_date_only_semantics() -> None:
    assert provider_data._iso(datetime.date(2026, 6, 17)) == "2026-06-17"


def test_iso_none_is_none() -> None:
    assert provider_data._iso(None) is None


def test_json_value_datetime_is_utc_z() -> None:
    naive = datetime.datetime(2026, 1, 2, 3, 4, 5)
    assert provider_data._json_value(naive) == "2026-01-02T03:04:05Z"


def test_intelligence_iso_still_emits_z() -> None:
    # Accepted Stage 6 behavior is unchanged and remains the shared reference.
    naive = datetime.datetime(2026, 6, 17, 12, 0, 0)
    assert intelligence._iso(naive) == "2026-06-17T12:00:00Z"
    assert intelligence._iso(datetime.date(2026, 6, 17)) == "2026-06-17"
