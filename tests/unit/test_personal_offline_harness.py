"""Focused safety checks for the disposable Phase 2 offline harness."""

from __future__ import annotations

import argparse
import importlib.util
import json
import stat
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "personal_phase2_offline_db.py"
START_SCRIPT = ROOT / "scripts" / "start-personal-phase2-offline.sh"
STOP_SCRIPT = ROOT / "scripts" / "stop-personal-phase2-offline.sh"
SPEC = importlib.util.spec_from_file_location("personal_phase2_offline_db", SCRIPT_PATH)
assert SPEC and SPEC.loader
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


def _prepared(tmp_path: Path) -> Path:
    manifest = tmp_path / "state" / "manifest.json"
    HARNESS.prepare(
        argparse.Namespace(
            manifest=str(manifest),
            run_id="unit_case",
            database_name="nip_personal_phase2_offline_unit_case",
            owner_token="owner-token",
            postgres_mode="external",
            postgres_container=None,
            redis_container="nip-personal-phase2-offline-redis-unit-case",
        )
    )
    return manifest


def test_prepare_creates_private_bound_manifest(tmp_path: Path) -> None:
    manifest = _prepared(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    assert payload["schema"] == HARNESS.SCHEMA
    assert payload["dataset"] == "synthetic"
    assert Path(payload["state_dir"]) == manifest.parent.resolve()
    assert stat.S_IMODE(manifest.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert payload["database"]["owned"] is False
    assert payload["postgres"]["owned"] is False
    assert payload["redis"]["owned"] is False

    moved = tmp_path / "other"
    moved.mkdir()
    copied = moved / "manifest.json"
    copied.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not own this state directory"):
        HARNESS._load_manifest(copied)


def test_manifest_records_each_exact_resource_only_once(tmp_path: Path) -> None:
    manifest = _prepared(tmp_path)
    args = argparse.Namespace(
        manifest=str(manifest), kind="redis", container_id="sha256:owned", host_port=49100
    )
    HARNESS.record_container(args)
    with pytest.raises(RuntimeError, match="already owns"):
        HARNESS.record_container(args)

    process = argparse.Namespace(
        manifest=str(manifest),
        kind="api",
        pid=12345,
        started="Mon Sep  9 00:00:00 2026",
        command_marker="--port 49101",
    )
    HARNESS.record_process(process)
    with pytest.raises(RuntimeError, match="already owns"):
        HARNESS.record_process(process)


def test_database_names_and_external_admin_urls_fail_closed(tmp_path: Path) -> None:
    assert (
        HARNESS._database_name("nip_personal_phase2_offline_safe_12")
        == "nip_personal_phase2_offline_safe_12"
    )
    for unsafe in ("postgres", "nip_personal_phase2_offline_bad-name", "NIP_personal"):
        with pytest.raises(ValueError, match="reserved namespace"):
            HARNESS._database_name(unsafe)

    manifest = _prepared(tmp_path)
    for url in (
        "postgresql+psycopg2://news:secret@database.example:55432/postgres",
        "postgresql+psycopg2://news:secret@127.0.0.1:5432/postgres",
        "postgresql+psycopg2://news:secret@127.0.0.1/postgres",
    ):
        with pytest.raises(ValueError, match="loopback-only|non-5432"):
            HARNESS.init_database(
                argparse.Namespace(manifest=str(manifest), maintenance_url=url)
            )


def test_database_url_keeps_credentials_for_runtime_connection() -> None:
    rendered = HARNESS._database_url(
        "postgresql+psycopg2://news:p%40ss@127.0.0.1:55432/postgres",
        "nip_personal_phase2_offline_url_case",
    )
    parsed = make_url(rendered)
    assert parsed.password == "p@ss"
    assert parsed.database == "nip_personal_phase2_offline_url_case"


def test_frontend_origin_is_explicit_loopback_http_only() -> None:
    assert HARNESS._frontend_origin("http://127.0.0.1:3000/") == "http://127.0.0.1:3000"
    assert HARNESS._frontend_origin("http://localhost:3011") == "http://localhost:3011"
    assert HARNESS._frontend_origin("http://[::1]:3000") == "http://[::1]:3000"
    for unsafe in (
        "https://127.0.0.1:3000",
        "http://example.com:3000",
        "http://user:secret@127.0.0.1:3000",
        "http://127.0.0.1",
        "http://127.0.0.1:3000/path",
        "http://127.0.0.1:3000?key=value",
    ):
        with pytest.raises(ValueError, match="frontend origin"):
            HARNESS._frontend_origin(unsafe)


def test_option_looking_process_marker_parses_as_one_value() -> None:
    args = HARNESS._parser().parse_args(
        [
            "record-process",
            "--manifest",
            "/tmp/example/manifest.json",
            "--kind",
            "api",
            "--pid",
            "123",
            "--started",
            "timestamp",
            "--command-marker=--port 49101",
        ]
    )
    assert args.command_marker == "--port 49101"


def test_spawn_process_detaches_allowed_child_and_keeps_log_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _prepared(tmp_path)
    captured: dict[str, object] = {}

    class Process:
        pid = 24680

    def fake_popen(command: list[str], **kwargs: object) -> Process:
        captured.update({"command": command, **kwargs})
        return Process()

    monkeypatch.setattr(HARNESS.subprocess, "Popen", fake_popen)
    capsys.readouterr()
    HARNESS.spawn_process(
        argparse.Namespace(
            manifest=str(manifest),
            kind="api",
            log=str(manifest.parent / "api.log"),
            command=[
                "--",
                ".venv/bin/python",
                "-m",
                "uvicorn",
                "apps.api.main:app",
                "--port",
                "49101",
            ],
        )
    )

    assert capsys.readouterr().out.strip() == "24680"
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True
    assert captured["stdin"] == HARNESS.subprocess.DEVNULL
    assert captured["command"] == [
        ".venv/bin/python",
        "-m",
        "uvicorn",
        "apps.api.main:app",
        "--port",
        "49101",
    ]
    assert stat.S_IMODE((manifest.parent / "api.log").stat().st_mode) == 0o600

    with pytest.raises(RuntimeError, match="outside its owned state"):
        HARNESS.spawn_process(
            argparse.Namespace(
                manifest=str(manifest),
                kind="api",
                log=str(tmp_path / "outside.log"),
                command=["--", ".venv/bin/python", "-m", "uvicorn", "apps.api.main:app"],
            )
        )


def test_shell_contract_has_failure_cleanup_identity_checks_and_blank_provider_keys() -> None:
    start = START_SCRIPT.read_text(encoding="utf-8")
    stop = STOP_SCRIPT.read_text(encoding="utf-8")

    assert "trap 'exit 130' INT" in start
    assert "trap 'exit 143' TERM" in start
    assert "trap cleanup_on_exit EXIT" in start
    assert "record_container_or_remove" in start
    assert "record_process_or_stop" in start
    assert "spawn-process" in start
    assert "nohup" not in start
    assert "start_new_session=True" in SCRIPT_PATH.read_text(encoding="utf-8")
    assert 'grep -Fq "workers.personal_tasks.run_personal_daily"' in start
    assert 'grep -Fq "${WORKER_HOSTNAME} ready."' in start
    assert 'kill -0 "${worker_pid}"' in start
    assert "Frontend: %s/SIGNAL%%20-%%20Intelligence%%20Platform.html?api=" in start
    assert '"${API_KEY_VALUE}" =~ ^[A-Za-z0-9_-]{16,128}$' in start
    for variable in (
        "ANTHROPIC_API_KEY=",
        "OPENAI_API_KEY=",
        "GEMINI_API_KEY=",
        "DEEPSEEK_API_KEY=",
    ):
        assert variable in start
        assert start.index(f"export {variable}") < start.index('RUN_ID="${PERSONAL_HARNESS_ID')
    assert start.index("export APP_ENV=test") < start.index('RUN_ID="${PERSONAL_HARNESS_ID')

    assert 'actual_started="$(ps -o lstart= -p "${pid}" | xargs)"' in stop
    assert 'actual_id="$(docker inspect --format' in stop
    assert '"${actual_id}" != "${expected_id}"' in stop
