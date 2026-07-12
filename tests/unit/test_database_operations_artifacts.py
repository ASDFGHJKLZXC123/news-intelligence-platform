"""Checks for database operations artifacts."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_database_operations_artifacts_exist() -> None:
    assert (ROOT / "scripts" / "backup-postgres.sh").is_file()
    assert (ROOT / "scripts" / "restore-postgres.sh").is_file()
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
