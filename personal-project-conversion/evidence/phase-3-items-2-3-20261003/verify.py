"""Scoped, offline pytest runner retaining owned PostgreSQL resource evidence."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.update(
    {
        "APP_ENV": "test",
        "PYTHONDONTWRITEBYTECODE": "1",
        "REQUIRE_POSTGRES": "1",
        "POSTGRES_HOST_PORT": "58553",
        "DATABASE_URL": "postgresql+psycopg2://news:news@127.0.0.1:58553/postgres",
        "OPENAI_API_KEY": "",
        "GEMINI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "FRED_API_KEY": "",
        "EIA_API_KEY": "",
        "API_KEY": "",
        "NASA_FIRMS_MAP_KEY": "",
    }
)

import psycopg2  # noqa: E402 - environment must be safe before test imports
import pytest  # noqa: E402
from sqlalchemy import Engine, event  # noqa: E402

CONTAINER = "nip-phase3-tests-02ea38048d"
CONTAINER_ID = "1e7c72d3ff3bd27f581ea5682e5b8a51206ddf98118be06cea933fb8bb2b55b6"
container = json.loads(subprocess.check_output(["docker", "inspect", CONTAINER]))[0]
assert container["Id"] == CONTAINER_ID
assert container["Config"]["Labels"]["nip.phase3.owned"] == CONTAINER
assert container["NetworkSettings"]["Ports"]["5432/tcp"] == [
    {"HostIp": "127.0.0.1", "HostPort": "58553"}
]
assert container["State"]["Running"]


def connect(database="postgres"):
    return psycopg2.connect(
        host="127.0.0.1", port=58553, user="news", password="news", dbname=database
    )


def inventory():
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT datname, oid FROM pg_database ORDER BY datname")
            return [{"name": name, "oid": oid} for name, oid in cursor.fetchall()]
    finally:
        connection.close()


mode = sys.argv[1]
record = {
    "mode": mode,
    "container_id": CONTAINER_ID,
    "container_name": CONTAINER,
    "container_label": {"nip.phase3.owned": CONTAINER},
    "loopback_port": 58553,
    "maintenance_database": "postgres",
    "environment": "test; all seven provider credential fields and application API_KEY cleared",
    "before": inventory(),
    "databases": [],
}
owned = {}
pattern = re.compile(
    r'^(CREATE|DROP) DATABASE(?: IF EXISTS)? "(nip_(?:stage7_|pipeline_slice_)[a-f0-9]+)"$', re.I
)


@event.listens_for(Engine, "before_cursor_execute")
def before_sql(connection, cursor, statement, parameters, context, executemany):
    match = pattern.fullmatch(statement.strip())
    if not match or match[1].upper() != "DROP":
        return
    name = match[2]
    assert name in owned, "cleanup requires database created by this runner"
    with cursor.connection.cursor() as inspect_cursor:
        inspect_cursor.execute("SELECT current_database()")
        assert inspect_cursor.fetchone()[0] == "postgres"
        inspect_cursor.execute("SELECT oid FROM pg_database WHERE datname=%s", (name,))
        assert inspect_cursor.fetchone()[0] == owned[name]["oid"]
    probe = connect(name)
    try:
        with probe.cursor() as probe_cursor:
            probe_cursor.execute("SELECT current_database()")
            assert probe_cursor.fetchone()[0] == name
            probe_cursor.execute("SELECT to_regclass('public.alembic_version')")
            if probe_cursor.fetchone()[0]:
                probe_cursor.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
                owned[name]["applied_heads"] = [row[0] for row in probe_cursor.fetchall()]
    finally:
        probe.close()
    owned[name]["identity_verified_before_drop"] = True


@event.listens_for(Engine, "after_cursor_execute")
def after_sql(connection, cursor, statement, parameters, context, executemany):
    match = pattern.fullmatch(statement.strip())
    if not match:
        return
    operation, name = match[1].upper(), match[2]
    with cursor.connection.cursor() as inspect_cursor:
        inspect_cursor.execute("SELECT oid FROM pg_database WHERE datname=%s", (name,))
        row = inspect_cursor.fetchone()
    if operation == "CREATE":
        assert name not in owned and row is not None
        owned[name] = {"name": name, "oid": row[0], "created_by_this_runner": True}
        record["databases"].append(owned[name])
    else:
        assert row is None
        owned[name]["absent_after_drop"] = True


choices = {
    "baseline-vertical": ["tests/integration/test_daily_pipeline_vertical_slice.py"],
    "baseline-spending": ["tests/integration/test_personal_spending_metadata.py"],
    "final-integration": [
        "tests/integration/test_personal_spending.py",
        "tests/integration/test_personal_spending_metadata.py",
        "tests/integration/test_daily_pipeline_vertical_slice.py",
    ],
}
assert mode in choices
try:
    with (
        (OUT / (mode + ".log")).open("w") as log,
        contextlib.redirect_stdout(log),
        contextlib.redirect_stderr(log),
    ):
        code = int(pytest.main(["-q", "-p", "no:cacheprovider", *choices[mode]]))
    record["pytest_exit_code"] = code
finally:
    record["after"] = inventory()
    record["inventory_unchanged"] = record["before"] == record["after"]
    record["all_owned_databases_removed"] = all(
        item.get("absent_after_drop") for item in record["databases"]
    )
    (OUT / (mode + "-resources.json")).write_text(json.dumps(record, indent=2) + "\n")
print((OUT / (mode + ".log")).read_text()[-4000:])
print(
    json.dumps(
        {
            key: record[key]
            for key in (
                "mode",
                "pytest_exit_code",
                "inventory_unchanged",
                "all_owned_databases_removed",
            )
        }
    )
)
assert record["inventory_unchanged"] and record["all_owned_databases_removed"]
sys.exit(code)
