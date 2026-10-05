"""Redis-free readiness and same-origin route/security boundaries for the personal app."""

from __future__ import annotations

import builtins
import json
import shutil
import subprocess
import sys
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from apps.api import deps, health, main, middleware
from packages.config.settings import Settings


def personal_settings(**updates):
    return Settings(
        _env_file=None,
        app_env="test",
        personal_processing_transport="subprocess",
        personal_processing_mode="personal",
        redis_url="",
        celery_broker_url="",
        celery_result_backend="",
        **updates,
    )


@pytest.fixture
def personal_app(monkeypatch):
    settings = personal_settings()
    for module in (deps, health, main, middleware):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[deps.check_database] = lambda: deps.ComponentStatus(
        "database", True, "reachable"
    )
    app.dependency_overrides[deps.check_worker_config] = lambda: deps.ComponentStatus(
        "launcher", True, "ready"
    )

    # Exercise protected requests without database access.
    @app.post("/api/v1/testing-mutation")
    def mutation():
        return {"ok": True}

    return app


def test_personal_health_never_imports_disabled_components(monkeypatch, personal_app):
    original_import = builtins.__import__

    def forbidden(name, *args, **kwargs):
        if name == "redis" or name.startswith(("celery", "workers.celery_app")):
            pytest.fail("disabled dependency initialized: " + name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbidden)
    assert deps.check_config().ok
    with TestClient(
        personal_app, base_url="http://127.0.0.1:8123", client=("127.0.0.1", 50000)
    ) as client:
        response = client.get("/health")
    assert response.status_code == 200
    for name in ("redis", "celery", "beat"):
        assert response.json()["components"][name] == {
            "ok": True,
            "detail": "not_required_in_personal_mode",
        }


@pytest.mark.parametrize("path", ["/", "/today", "/saved", "/briefs", "/event/test-id"])
def test_browser_and_assets_use_application_origin(personal_app, path):
    client = TestClient(personal_app)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "window.SIGNAL_API_BASE=window.location.origin" in response.text
    assert '<base href="/"' in response.text
    assert client.get("/app/main.jsx").status_code == 200


class _FirstScript(HTMLParser):
    """Read the actual first script element without reproducing its JavaScript."""

    def __init__(self):
        super().__init__()
        self.source = None
        self.attributes = None
        self._reading = False
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and self.source is None:
            self.attributes = dict(attrs)
            self._reading = True

    def handle_data(self, data):
        if self._reading:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._reading:
            self.source = "".join(self._parts)
            self._reading = False


@pytest.mark.parametrize(
    "path,initial_hash,expected_hash",
    [
        ("/", "", ""),
        ("/saved", "", "#/saved"),
        ("/brief/id", "", "#/brief/id"),
        ("/saved/", "", "#/saved"),
        ("/SIGNAL - Intelligence Platform.html", "", ""),
        ("/saved", "#/brief/existing", "#/brief/existing"),
    ],
)
def test_actual_injected_browser_script_executes_routes_and_same_origin(
    personal_app, path, initial_hash, expected_hash
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the served browser script")
    origin = "http://127.0.0.1:8123"
    response = TestClient(personal_app, base_url=origin).get(path)
    assert response.status_code == 200
    parser = _FirstScript()
    parser.feed(response.text)
    assert parser.source
    assert "src" not in parser.attributes

    # Execute the exact script extracted from the real HTML response. The
    # implementation's regexp and escaping are deliberately not copied here.
    driver = """
const fs = require("node:fs");
const vm = require("node:vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const window = {
  SIGNAL_API_BASE: "http://foreign.invalid",
  SIGNAL_PERSONAL_APP: false,
  location: { origin: input.origin, pathname: input.path, hash: input.hash },
};
vm.runInNewContext(input.script, { window }, { timeout: 1000 });
process.stdout.write(JSON.stringify({
  personal: window.SIGNAL_PERSONAL_APP,
  apiBase: window.SIGNAL_API_BASE,
  hash: window.location.hash,
  pathname: window.location.pathname,
}));
"""
    executed = subprocess.run(
        [node, "-e", driver],
        input=json.dumps(
            {
                "script": parser.source,
                "origin": origin,
                "path": path,
                "hash": initial_hash,
            }
        ),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert executed.returncode == 0, executed.stderr
    actual = json.loads(executed.stdout)
    assert actual == {
        "personal": True,
        "apiBase": origin,
        "hash": expected_hash,
        "pathname": path,
    }


@pytest.mark.parametrize(
    "path", ["/api/unknown", "/api/v1/personal/unknown", "/uploads/private", "/app/missing.js"]
)
def test_unknown_api_and_asset_routes_remain_json_errors(personal_app, path):
    response = TestClient(personal_app).get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "not_found"


def test_same_origin_mutation_keeps_foreign_origin_and_nonlocal_rejection(personal_app):
    client = TestClient(personal_app, base_url="http://127.0.0.1:8123", client=("127.0.0.1", 50000))
    path = "/api/v1/testing-mutation"
    assert client.post(path, headers={"Origin": "http://127.0.0.1:8123"}).status_code == 200
    assert client.post(path, headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
    assert client.post(path, headers={"Origin": "http://evil.invalid"}).status_code == 403
    hostile = TestClient(personal_app, base_url="http://evil.invalid", client=("127.0.0.1", 50000))
    assert hostile.post(path, headers={"Origin": "http://evil.invalid"}).status_code == 403
    remote_client = TestClient(
        personal_app, base_url="http://127.0.0.1:8123", client=("203.0.113.7", 50000)
    )
    assert remote_client.post(path, headers={"Origin": "http://127.0.0.1:8123"}).status_code == 403


def test_same_origin_does_not_bypass_configured_api_key(monkeypatch, personal_app):
    monkeypatch.setattr(middleware, "get_settings", lambda: personal_settings(api_key="test-key"))
    client = TestClient(personal_app, base_url="http://127.0.0.1:8123", client=("127.0.0.1", 50000))
    path = "/api/v1/testing-mutation"
    headers = {"Origin": "http://127.0.0.1:8123"}
    assert client.post(path, headers=headers).status_code == 401
    assert client.post(path, headers={**headers, "X-API-Key": "test-key"}).status_code == 200


def test_personal_shell_import_constructs_no_celery_or_redis():
    code = "import sys; import apps.api.main; assert 'workers.celery_app' not in sys.modules; assert 'celery' not in sys.modules; assert 'redis' not in sys.modules"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
