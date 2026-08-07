"""Static safety checks for database role provisioning artifacts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROVISION_SCRIPT = ROOT / "scripts" / "provision-postgres-roles.sh"
PROVISION_TEMPLATE = ROOT / "infra" / "sql" / "provision-app-roles.psql"


def test_role_provisioning_artifacts_exist_and_parse() -> None:
    assert PROVISION_SCRIPT.is_file()
    assert os.access(PROVISION_SCRIPT, os.X_OK)
    assert PROVISION_TEMPLATE.is_file()
    subprocess.run(["bash", "-n", str(PROVISION_SCRIPT)], check=True)


def test_role_provisioning_requires_external_credentials() -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DATABASE_ADMIN_URL": "postgresql://admin@localhost/news",
        "APP_DATABASE_NAME": "news",
        "MIGRATION_DATABASE_ROLE": "news_migrator",
        "RUNTIME_DATABASE_ROLE": "news_runtime",
    }

    completed = subprocess.run(
        [str(PROVISION_SCRIPT)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "MIGRATION_DATABASE_PASSWORD is required" in completed.stderr
    template = PROVISION_TEMPLATE.read_text()
    assert "\\getenv migration_password NIP_MIGRATION_PASSWORD" in template
    assert "\\getenv runtime_password NIP_RUNTIME_PASSWORD" in template


@pytest.mark.parametrize("forbidden_role", ["postgres", "PG_monitor", "rdsadmin", "root"])
def test_role_provisioning_rejects_reserved_or_administrative_roles(
    forbidden_role: str,
) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DATABASE_ADMIN_URL": "postgresql://operator@localhost/news",
        "APP_DATABASE_NAME": "news",
        "MIGRATION_DATABASE_ROLE": forbidden_role,
        "MIGRATION_DATABASE_PASSWORD": "migration-password-value",
        "RUNTIME_DATABASE_ROLE": "news_runtime",
        "RUNTIME_DATABASE_PASSWORD": "runtime-password-value",
    }

    completed = subprocess.run(
        [str(PROVISION_SCRIPT)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "role" in completed.stderr


def test_role_template_enforces_runtime_least_privilege() -> None:
    sql = PROVISION_TEMPLATE.read_text()
    script = PROVISION_SCRIPT.read_text()

    assert "REVOKE CREATE, TEMPORARY ON DATABASE" in sql
    assert "REVOKE CREATE ON SCHEMA" in sql
    assert "GRANT USAGE ON SCHEMA" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s" in sql
    assert "GRANT USAGE, SELECT ON SEQUENCE %s" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES" not in sql
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES" not in sql
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE" in sql
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES" in sql
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES" in sql
    assert "REVOKE ALL PRIVILEGES ON ROUTINE" in sql
    assert "GRANT EXECUTE ON ALL FUNCTIONS" not in sql
    assert "GRANT EXECUTE ON FUNCTIONS TO" not in sql
    assert "AND NOT routines.prosecdef" in sql
    assert "migration_security_definer_exists" in sql
    assert "exposed_security_definer" in sql
    assert "memberships.member, memberships.roleid" in sql
    assert "alembic_version" in sql
    assert "external_relation_has_direct_runtime_acl" in sql
    assert "WITH GRANT OPTION" not in sql
    assert "CREATE EXTENSION" not in sql
    assert "SUPERUSER" not in sql.replace("NOSUPERUSER", "")
    assert 'PGDATABASE="${native_admin_url}" command psql' in script
    assert '--dbname "${native_admin_url}"' not in script
