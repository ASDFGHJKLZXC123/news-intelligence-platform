"""Offline proof for the secret-safe production configuration preflight."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from packages.config.settings import Settings
from services.operations.production_config import build_production_config_report

_ACTIVE_SNAPSHOT = "nip-es2-20260731-5e17cfbc0b7562ca228f"


def _production_settings() -> Settings:
    return Settings(
        app_env="prod",
        debug=False,
        api_key="api-key-that-is-longer-than-thirty-two-characters",
        cors_allow_origins="https://news.example.com",
        database_url=(
            "postgresql+psycopg2://runtime:database-password-long@db.internal:5432/news"
            "?sslmode=verify-full"
        ),
        redis_url="rediss://:redis-password-long@cache.internal:6380/0",
        celery_broker_url="rediss://:redis-password-long@cache.internal:6380/1",
        celery_result_backend="rediss://:redis-password-long@cache.internal:6380/2",
        openai_api_key="openai-production-key-long",
        gemini_api_key="gemini-production-key-long",
        deepseek_api_key="deepseek-production-key-long",
        embedding_model_version=_ACTIVE_SNAPSHOT,
        llm_models={
            "T0": "text-embedding-3-small",
            "T1": "gemini-3.5-flash-lite",
            "T2": "gpt-4.1",
            "T3": "deepseek-v4-pro",
        },
        llm_tier_providers={"T0": "openai", "T1": "gemini", "T2": "openai", "T3": "deepseek"},
        llm_tier_fallbacks={
            "T1": [{"provider": "deepseek", "model": "deepseek-v4-flash"}],
            "T2": [{"provider": "deepseek", "model": "deepseek-v4-pro"}],
        },
    )


def test_complete_production_configuration_passes_without_external_io_or_secret_output() -> None:
    settings = _production_settings()

    report = build_production_config_report(settings)

    assert report["ready"] is True
    assert all(check["passed"] for check in report["checks"])
    assert report["external_io"] == {
        "network": False,
        "database": False,
        "redis": False,
        "broker": False,
        "files_written": False,
    }
    serialized = json.dumps(report, sort_keys=True)
    for secret in (
        settings.api_key,
        settings.database_url,
        settings.redis_url,
        settings.openai_api_key,
        settings.gemini_api_key,
        settings.deepseek_api_key,
    ):
        assert secret not in serialized


@pytest.mark.parametrize(
    ("override", "failed_check"),
    [
        ({"app_env": "local"}, "deployment_environment"),
        ({"debug": True}, "debug_disabled"),
        ({"api_key": "short"}, "api_key_strong"),
        ({"cors_allow_origins": "http://localhost:3000"}, "cors_https_nonlocal"),
        ({"cors_allow_origins": "https://news.example.com, *"}, "cors_https_nonlocal"),
        (
            {"database_url": "postgresql+psycopg2://news:news@localhost:5432/news"},
            "database_tls_nonlocal",
        ),
        ({"redis_url": "redis://localhost:6379/0"}, "redis_tls_nonlocal"),
        ({"llm_budget_enforced": False}, "llm_budget_enforced"),
        ({"openai_api_key": ""}, "llm_routes_complete"),
        ({"openai_base_url": "https://localhost:9443"}, "llm_routes_complete"),
        ({"embedding_model_version": "current"}, "embedding_snapshot_active"),
        ({"crisis_prediction_reads_enabled": True}, "crisis_prediction_reads_closed"),
    ],
)
def test_each_production_control_fails_closed(
    override: dict[str, object], failed_check: str
) -> None:
    settings = _production_settings().model_copy(update=override)

    report = build_production_config_report(settings)

    checks = {check["name"]: check["passed"] for check in report["checks"]}
    assert report["ready"] is False
    assert checks[failed_check] is False


def test_cli_exit_code_and_output_follow_the_secret_safe_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "check-production-config.py"
    spec = importlib.util.spec_from_file_location("production_config_cli", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_settings", _production_settings)

    assert module.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is True
