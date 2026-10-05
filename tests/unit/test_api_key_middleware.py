"""API-key middleware stub tests.

The stub's contract: public read methods are open. Mutating methods and internal operator
reads require a matching ``X-API-Key`` once a key is configured (compared in constant time).
When no key is set, protected calls are allowed only from a local environment calling over a
loopback address; remote or nonlocal callers are denied. Settings are swapped via monkeypatch since
``get_settings`` is process-cached, and the caller host is set via ``TestClient``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apps.api.middleware as mw
from apps.api.middleware import APIKeyMiddleware
from packages.config.settings import Settings

_LOOPBACK = ("127.0.0.1", 50000)
_REMOTE = ("203.0.113.7", 50000)


def _client(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    client: tuple[str, int] = _LOOPBACK,
) -> TestClient:
    monkeypatch.setattr(mw, "get_settings", lambda: settings)
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    @app.get("/read")
    def read():
        return {"ok": True}

    @app.post("/jobs/ingest")
    def mutate():
        return {"ok": True}

    @app.get("/api/v1/internal/jobs/process/2026-07-29")
    def internal_read():
        return {"ok": True}

    return TestClient(app, client=client)


def test_read_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(Settings(app_env="prod", api_key="secret"), monkeypatch)
    assert client.get("/read").status_code == 200


def test_internal_read_uses_operator_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(Settings(app_env="prod", api_key="secret"), monkeypatch)
    path = "/api/v1/internal/jobs/process/2026-07-29"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-API-Key": "secret"}).status_code == 200


def test_local_loopback_mutation_allowed_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(Settings(app_env="local", api_key=""), monkeypatch, client=_LOOPBACK)
    assert client.post("/jobs/ingest").status_code == 200


def test_local_remote_mutation_denied_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(Settings(app_env="local", api_key=""), monkeypatch, client=_REMOTE)
    assert client.post("/jobs/ingest").status_code == 401


def test_prod_mutation_denied_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even from loopback, a nonlocal environment with no key refuses mutations.
    client = _client(Settings(app_env="prod", api_key=""), monkeypatch, client=_LOOPBACK)
    assert client.post("/jobs/ingest").status_code == 401


def test_bad_key_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(Settings(app_env="prod", api_key="secret"), monkeypatch)
    assert client.post("/jobs/ingest").status_code == 401
    assert client.post("/jobs/ingest", headers={"X-API-Key": "wrong"}).status_code == 401


def test_good_key_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(Settings(app_env="prod", api_key="secret"), monkeypatch)
    assert client.post("/jobs/ingest", headers={"X-API-Key": "secret"}).status_code == 200


def test_browser_origin_must_be_allowlisted_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    settings = Settings(app_env="local", api_key="")
    monkeypatch.setattr(mw, "get_settings", lambda: settings)
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    @app.post("/api/v1/personal/runs")
    def mutate():
        nonlocal calls
        calls += 1
        return {"ok": True}

    client = TestClient(app, client=_LOOPBACK)
    rejected = client.post(
        "/api/v1/personal/runs", headers={"Origin": "https://untrusted.example"}
    )
    assert rejected.status_code == 403
    assert calls == 0
    allowed = client.post(
        "/api/v1/personal/runs", headers={"Origin": "http://localhost:3000"}
    )
    assert allowed.status_code == 200
    assert calls == 1


def test_valid_api_key_does_not_override_browser_origin_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(Settings(app_env="local", api_key="secret"), monkeypatch)
    assert (
        client.post(
            "/jobs/ingest",
            headers={"Origin": "https://untrusted.example", "X-API-Key": "secret"},
        ).status_code
        == 403
    )
