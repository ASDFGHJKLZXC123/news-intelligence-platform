"""Personal startup must keep paid processing closed and preserve explicit datastore choice."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "personal_local_startup", REPO_ROOT / "scripts/personal-local.py"
)
assert spec is not None and spec.loader is not None
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


def test_wrapper_overrides_paid_public_and_multiple_supervisor_settings():
    inherited = {
        "DATABASE_URL": "postgresql+psycopg2://user:private@127.0.0.1:5678/retained_workspace",
        "PERSONAL_PAID_RUNTIME_ENABLED": "true",
        "PERSONAL_PROCESSING_TRANSPORT": "celery",
        "PERSONAL_PROCESSING_MODE": "legacy",
        "PERSONAL_BIND_HOST": "0.0.0.0",
        "PERSONAL_APP_WORKERS": "4",
        "API_KEY": "retained-authentication",
        "CORS_ALLOW_ORIGINS": "https://unrelated.example",
    }
    original = dict(inherited)
    environment = startup.personal_environment(inherited, app_port=8123)
    assert environment["DATABASE_URL"] == inherited["DATABASE_URL"]
    assert environment["API_KEY"] == inherited["API_KEY"]
    assert environment["PERSONAL_PAID_RUNTIME_ENABLED"] == "false"
    assert environment["PERSONAL_PROCESSING_TRANSPORT"] == "subprocess"
    assert environment["PERSONAL_PROCESSING_MODE"] == "personal"
    assert environment["PERSONAL_BIND_HOST"] == "127.0.0.1"
    assert environment["PERSONAL_APP_WORKERS"] == "1"
    assert environment["CORS_ALLOW_ORIGINS"] == "http://127.0.0.1:8123"
    assert environment["PERSONAL_OFFLINE_FIXTURE_PATH"] == ""
    assert inherited == original


def test_default_database_is_separate_and_matches_personal_compose_defaults():
    config = yaml.safe_load((REPO_ROOT / "infra/docker-compose.personal.yml").read_text())
    assert set(config["services"]) == {"postgres"}
    postgres = config["services"]["postgres"]
    assert postgres["ports"] == ["127.0.0.1:${PERSONAL_POSTGRES_PORT:-55443}:5432"]
    assert postgres["environment"]["POSTGRES_DB"] == "${PERSONAL_POSTGRES_DB:-news_personal}"
    assert set(config["volumes"]) == {"personal_pgdata"}
    assert (
        startup.personal_environment(
            {"PERSONAL_POSTGRES_PORT": "", "PERSONAL_POSTGRES_PASSWORD": ""}
        )["DATABASE_URL"]
        == startup.personal_environment({})["DATABASE_URL"]
    )
    assert startup.personal_environment({})["DATABASE_URL"] == (
        "postgresql+psycopg2://news:personal_local_only@127.0.0.1:55443/news_personal"
    )


def test_custom_compose_database_values_are_url_encoded_and_fixture_is_explicit():
    environment = startup.personal_environment(
        {
            "PERSONAL_POSTGRES_USER": "news/user",
            "PERSONAL_POSTGRES_PASSWORD": "p@ss:word",
            "PERSONAL_POSTGRES_DB": "personal copy",
            "PERSONAL_POSTGRES_PORT": "55435",
            "PERSONAL_OFFLINE_FIXTURE_PATH": "/explicit/synthetic.json",
        }
    )
    assert environment["DATABASE_URL"] == (
        "postgresql+psycopg2://news%2Fuser:p%40ss%3Aword@127.0.0.1:55435/personal%20copy"
    )
    assert environment["PERSONAL_OFFLINE_FIXTURE_PATH"] == "/explicit/synthetic.json"


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_invalid_database_port_fails_before_startup(port):
    with pytest.raises(ValueError, match="PERSONAL_POSTGRES_PORT"):
        startup.personal_environment({"PERSONAL_POSTGRES_PORT": port})


def test_app_executes_single_foreground_module_without_migration_or_daily_run(monkeypatch):
    calls = []
    monkeypatch.setattr(startup.os, "chdir", lambda path: calls.append(("cwd", path)))
    monkeypatch.setattr(
        startup.os, "execve", lambda executable, argv, env: calls.append((executable, argv, env))
    )
    monkeypatch.setenv("PERSONAL_PAID_RUNTIME_ENABLED", "true")
    startup.main(["app", "--port", "8123"])
    assert calls[0] == ("cwd", REPO_ROOT)
    executable, argv, env = calls[1]
    assert executable == startup.sys.executable
    assert argv == [executable, "-m", "services.personal.app", "--port", "8123"]
    assert env["PERSONAL_PAID_RUNTIME_ENABLED"] == "false"


@pytest.mark.parametrize("command", ["retry", "recover"])
def test_recovery_identifiers_are_validated_before_delegate(command):
    with pytest.raises(SystemExit):
        startup.command_arguments([command, "not-a-run-identity"])
    run_id = uuid.uuid4()
    arguments, _ = startup.command_arguments([command, str(run_id)])
    assert arguments == ["-m", "services.personal.cli", command, str(run_id)]


def test_explicit_update_and_idle_mode_commands_use_existing_guarded_cli():
    assert startup.command_arguments(["update"])[0] == ["-m", "services.personal.cli", "start"]
    assert startup.command_arguments(["mode", "legacy"])[0] == [
        "-m",
        "services.personal.cli",
        "mode",
        "legacy",
    ]
