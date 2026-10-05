"""Phase 4A API selection stays independent of Celery and enforces local launch configuration."""

from __future__ import annotations

import subprocess
import sys

import pytest

from apps.api import deps
from packages.config.settings import Settings


def test_subprocess_health_does_not_probe_celery(monkeypatch):
    settings = Settings(
        _env_file=None,
        app_env="test",
        personal_processing_transport="subprocess",
        personal_processing_mode="personal",
    )
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    monkeypatch.setattr(
        deps,
        "_check_personal_launcher",
        lambda: deps.ComponentStatus("launcher", True, "ready"),
        raising=False,
    )
    monkeypatch.setattr(
        deps,
        "_inspect_active_worker_queues",
        lambda *_: pytest.fail("personal health must not construct a Celery broker"),
    )
    result = deps.check_worker_config()
    assert result.ok and result.name == "launcher"


def test_subprocess_config_does_not_require_broker(monkeypatch):
    settings = Settings(
        _env_file=None,
        app_env="test",
        personal_processing_transport="subprocess",
        personal_processing_mode="personal",
        celery_broker_url="",
        celery_result_backend="",
    )
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    assert deps.check_config().ok


@pytest.mark.parametrize(
    "overrides",
    [
        {"personal_bind_host": "0.0.0.0"},
        {"personal_app_workers": 2},
        {"personal_processing_mode": "legacy"},
    ],
)
def test_subprocess_config_blocks_public_multiworker_or_mode_mismatch(monkeypatch, overrides):
    settings = Settings(
        _env_file=None,
        app_env="test",
        personal_processing_transport="subprocess",
        personal_processing_mode="personal",
        **{k: v for k, v in overrides.items() if k != "personal_processing_mode"},
    )
    if "personal_processing_mode" in overrides:
        settings = settings.model_copy(update=overrides)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    assert not deps.check_config().ok


def test_importing_api_shell_does_not_construct_celery():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import apps.api.main; assert 'workers.celery_app' not in sys.modules; assert 'celery' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("transport", ["celery", "subprocess"])
def test_api_start_blocks_configured_durable_mode_disagreement(monkeypatch, transport):
    from fastapi import HTTPException

    from apps.api import personal
    from services.personal import processing

    settings = Settings(
        _env_file=None,
        app_env="test",
        personal_processing_transport=transport,
        personal_processing_mode="personal",
    )
    monkeypatch.setattr(personal, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "check_config", lambda: deps.ComponentStatus("config", True, "ready"))
    rolled_back = []

    class SessionDouble:
        def rollback(self):
            rolled_back.append(True)

    def mismatch(session, expected):
        assert expected == "personal"
        raise processing.WriterModeConflict(
            "deployment personal does not agree with durable legacy"
        )

    monkeypatch.setattr(processing, "assert_mode_agreement", mismatch)
    with pytest.raises(HTTPException) as rejected:
        personal._require_runtime_mode(SessionDouble())
    assert rejected.value.status_code == 409 and rolled_back == [True]


@pytest.mark.parametrize("transport", ["celery", "subprocess"])
def test_api_readiness_discloses_all_transport_mode_mismatch(monkeypatch, transport):
    from types import SimpleNamespace

    from apps.api import personal

    settings = Settings(
        _env_file=None,
        app_env="test",
        personal_processing_transport=transport,
        personal_processing_mode="personal",
    )
    monkeypatch.setattr(personal, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "check_config", lambda: deps.ComponentStatus("config", True, "ready"))
    ready, reasons = personal._readiness(
        SimpleNamespace(owner_id="synthetic-owner"), None, "legacy", legacy_active=False
    )
    assert not ready and "processing_mode_mismatch" in reasons
