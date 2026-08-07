"""Checks for database operations artifacts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_database_operations_artifacts_exist() -> None:
    assert (ROOT / "scripts" / "backup-postgres.sh").is_file()
    assert (ROOT / "scripts" / "restore-postgres.sh").is_file()
    assert (ROOT / "scripts" / "test-postgres-restore.sh").is_file()
    assert (ROOT / "scripts" / "lib" / "postgres-tools.sh").is_file()
    assert (ROOT / "infra" / "sql" / "retention-preview.sql").is_file()
    assert (ROOT / "docs" / "operations" / "database-runbook.md").is_file()


def test_database_operations_artifacts_are_non_destructive_by_default() -> None:
    retention_sql = (ROOT / "infra" / "sql" / "retention-preview.sql").read_text()
    assert "SELECT" in retention_sql
    assert "DELETE" not in retention_sql
    assert "UPDATE" not in retention_sql

    backup_script = (ROOT / "scripts" / "backup-postgres.sh").read_text()
    restore_script = (ROOT / "scripts" / "restore-postgres.sh").read_text()
    assert "pg_dump" in backup_script
    assert "pg_restore" in restore_script
    assert "RESTORE_TARGET_DISPOSABLE" in restore_script
    assert "RESTORE_EXPECTED_DATABASE" in restore_script
    assert "--single-transaction" in restore_script


def test_native_postgres_url_normalization() -> None:
    helper = ROOT / "scripts" / "lib" / "postgres-tools.sh"

    for configured, expected in (
        (
            "postgresql+psycopg2://news:secret@localhost:5432/news",
            "postgresql://news:secret@localhost:5432/news",
        ),
        (
            "postgresql+asyncpg://news:secret@localhost:5432/news?sslmode=require",
            "postgresql://news:secret@localhost:5432/news?sslmode=require",
        ),
        ("postgresql://news@localhost/news", "postgresql://news@localhost/news"),
        ("postgres://news@localhost/news", "postgres://news@localhost/news"),
    ):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; normalize_postgres_url_for_native_tools "$2"',
                "bash",
                str(helper),
                configured,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert completed.stdout.strip() == expected


def test_restore_refuses_without_disposable_target_acknowledgement(tmp_path: Path) -> None:
    backup_file = tmp_path / "candidate.dump"
    backup_file.write_bytes(b"not a real archive")
    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql+psycopg2://news:news@localhost/news"
    env.pop("RESTORE_TARGET_DISPOSABLE", None)
    env.pop("RESTORE_EXPECTED_DATABASE", None)

    completed = subprocess.run(
        [str(ROOT / "scripts" / "restore-postgres.sh"), str(backup_file)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "restore refused" in completed.stderr
    assert "RESTORE_TARGET_DISPOSABLE=1" in completed.stderr


def test_database_backups_are_excluded_from_git_and_docker_contexts() -> None:
    for ignore_file in (ROOT / ".gitignore", ROOT / ".dockerignore"):
        patterns = ignore_file.read_text().splitlines()
        assert "backups/" in patterns
        assert "*.dump" in patterns
        assert "*.dump.sha256" in patterns


def test_compose_validation_does_not_render_environment_secrets() -> None:
    makefile = (ROOT / "Makefile").read_text()

    assert "\t@docker compose config --quiet\n" in makefile
    assert "\tdocker compose config\n" not in makefile


def test_disposable_database_url_matches_the_ipv4_loopback_bind() -> None:
    integration_script = (ROOT / "scripts" / "test-integration-fresh.sh").read_text()

    assert "@127.0.0.1:${port}/${db_name}" in integration_script
    assert "@localhost:${port}/${db_name}" not in integration_script
