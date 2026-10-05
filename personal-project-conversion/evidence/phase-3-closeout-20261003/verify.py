"""Phase 3 closeout owned-resource runner. No live provider credentials or shared services."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PYTHON = str(ROOT / ".venv/bin/python")
CREDS = [
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "FRED_API_KEY",
    "EIA_API_KEY",
    "NASA_FIRMS_MAP_KEY",
    "API_KEY",
]
BASE_ENV = {
    key: os.environ[key]
    for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "DOCKER_HOST", "DOCKER_CONTEXT")
    if key in os.environ
}
BASE_ENV.update({key: "" for key in CREDS})
BASE_ENV.update(
    APP_ENV="test",
    PYTHONDONTWRITEBYTECODE="1",
    PERSONAL_PAID_RUNTIME_ENABLED="false",
    PERSONAL_OFFLINE_FIXTURE_PATH="",
    REDIS_URL="redis://127.0.0.1:1/0",
    CELERY_BROKER_URL="memory://",
    CELERY_RESULT_BACKEND="cache+memory://",
)
os.environ.clear()
os.environ.update(BASE_ENV)
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def command(args, *, check=True, env=None):
    result = subprocess.run(args, cwd=ROOT, env=env or os.environ, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"{args[:3]} exit {result.returncode}: {result.stderr[-2000:]}")
    return result


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def container_inventory():
    ids = command(["docker", "ps", "-aq", "--no-trunc"]).stdout.split()
    if not ids:
        return []
    rows = json.loads(command(["docker", "inspect", *ids]).stdout)
    return sorted(
        [
            {
                "id": row["Id"],
                "name": row["Name"],
                "image": row["Image"],
                "labels": row["Config"]["Labels"],
                "mounts": sorted(
                    row["Mounts"], key=lambda mount: json.dumps(mount, sort_keys=True)
                ),
                "running": row["State"]["Running"],
                "ports": row["NetworkSettings"]["Ports"],
            }
            for row in rows
        ],
        key=lambda x: x["id"],
    )


def snapshot(name):
    paths = command(["git", "ls-files", "-co", "--exclude-standard", "-z"]).stdout.split("\0")
    hashes = {}
    for relative in sorted(set(paths)):
        path = ROOT / relative
        if not path.is_file() or relative.startswith("personal-project-conversion/evidence/"):
            continue
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    controls = {}
    for relative in [
        ".env",
        "personal-project-conversion/SELECTED_PREFERENCES.md",
        ".git/index",
        str(Path(__file__).relative_to(ROOT)),
    ]:
        path = ROOT / relative
        if path.is_file():
            controls[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    value = {
        "utc": dt.datetime.now(dt.UTC).isoformat(),
        "head": command(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "branch": command(["git", "branch", "--show-current"]).stdout.strip(),
        "hashes": hashes,
        "protected_hashes": controls,
        "staged_diff_sha256": hashlib.sha256(
            command(["git", "diff", "--cached", "--binary"]).stdout.encode()
        ).hexdigest(),
        "unstaged_diff_sha256": hashlib.sha256(
            command(["git", "diff", "--binary"]).stdout.encode()
        ).hexdigest(),
    }
    write(name + ".json", value)
    (OUT / (name + "-status.txt")).write_text(command(["git", "status", "--short"]).stdout)
    return value


def resource():
    record = json.loads((OUT / "owned-resource.json").read_text())
    row = json.loads(command(["docker", "inspect", record["id"]]).stdout)[0]
    assert row["Id"] == record["id"] and row["Name"] == "/" + record["name"]
    assert row["Config"]["Labels"]["nip.closeout.owner"] == record["owner"]
    assert row["Image"] == record["image"] and row["State"]["Running"]
    assert row["Created"] == record["docker_created"]
    assert row["NetworkSettings"]["Ports"]["5432/tcp"] == [
        {"HostIp": "127.0.0.1", "HostPort": str(record["port"])}
    ]
    assert row["HostConfig"]["Tmpfs"] == {"/var/lib/postgresql/data": "rw"}
    assert not row["Mounts"]
    return record


def configure(record):
    os.environ.update(
        POSTGRES_HOST_PORT=str(record["port"]),
        POSTGRES_USER="news",
        POSTGRES_PASSWORD="news",
        POSTGRES_DB=record["database"],
        DATABASE_URL=record["url"],
        REQUIRE_POSTGRES="1",
        REQUIRE_DATABASE_ROLE_HARDENING="1",
        POSTGRES_TOOL_CONTAINER=record["id"],
        POSTGRES_TOOL_CONTAINER_ADMIN_ROLE="news",
    )
    assert all(os.environ[key] == "" for key in CREDS)


def db_inventory(record):
    import psycopg2

    with psycopg2.connect(
        host="127.0.0.1", port=record["port"], user="news", password="news", dbname="postgres"
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT datname, oid FROM pg_database ORDER BY datname")
            databases = [{"name": name, "oid": oid} for name, oid in cursor.fetchall()]
            cursor.execute("SELECT rolname, oid FROM pg_roles ORDER BY rolname")
            roles = [{"name": name, "oid": oid} for name, oid in cursor.fetchall()]
    return {"databases": databases, "roles": roles}


def initialize():
    assert not (OUT / "owned-resource.json").exists()
    snapshot("source-before")
    before = container_inventory()
    write("containers-before.json", before)
    owner = uuid.uuid4().hex
    name = "nip-closeout-" + owner[:12]
    image = "sha256:f87fefc064506355f316088583365f286bde08bf8dd175ec08bcb3a6b1823c1c"
    assert json.loads(command(["docker", "image", "inspect", image]).stdout)[0]["Id"] == image
    database = "nip_closeout_base_" + owner[:12]
    args = [
        "docker",
        "run",
        "-d",
        "--pull",
        "never",
        "--name",
        name,
        "--label",
        "nip.closeout.owner=" + owner,
        "--label",
        "nip.closeout.purpose=phase3-closeout",
        "-p",
        "127.0.0.1::5432",
        "--tmpfs",
        "/var/lib/postgresql/data:rw",
        "-e",
        "POSTGRES_USER=news",
        "-e",
        "POSTGRES_PASSWORD=news",
        "-e",
        "POSTGRES_DB=" + database,
        image,
        "postgres",
        "-c",
        "log_statement=ddl",
    ]
    container_id = command(args).stdout.strip()
    row = json.loads(command(["docker", "inspect", container_id]).stdout)[0]
    port = int(row["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"])
    record = {
        "owner": owner,
        "name": name,
        "id": container_id,
        "image": image,
        "port": port,
        "database": database,
        "url": f"postgresql+psycopg2://news:news@127.0.0.1:{port}/{database}",
        "creation_command": args,
        "created_utc": dt.datetime.now(dt.UTC).isoformat(),
        "docker_created": row["Created"],
        "mounts": row["Mounts"],
        "tmpfs": row["HostConfig"]["Tmpfs"],
    }
    write("owned-resource.json", record)
    resource()
    for _ in range(60):
        ready = command(
            ["docker", "exec", container_id, "pg_isready", "-U", "news", "-d", database],
            check=False,
        )
        if not ready.returncode:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("owned database did not become ready")
    configure(record)
    write("database-before.json", db_inventory(record))
    result = command([PYTHON, "-m", "alembic", "upgrade", "head"], check=False)
    (OUT / "blank-upgrade.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0
    print(json.dumps({key: record[key] for key in ("id", "name", "port", "database")}))


def run_gate(mode, targets):
    record = resource()
    configure(record)
    before = snapshot(mode + "-source-before")
    inventories = db_inventory(record)
    owned = {}
    database_events = []
    role_events = {}
    import psycopg2
    import pytest
    from sqlalchemy import Engine, event

    from tests.integration import _stage6_db, _stage7_db

    _stage6_db.HOST = _stage7_db.HOST = "127.0.0.1"
    role_drop = re.compile(
        r'^\s*DROP ROLE IF EXISTS "(nip_(?:delegate|runtime|migrator)_([a-f0-9]+))"\s*$', re.I
    )
    initial_roles = {row["name"] for row in inventories["roles"]}
    pattern = re.compile(r'^\s*(CREATE|DROP) DATABASE(?: IF EXISTS)? "([a-zA-Z0-9_]+)"\s*$', re.I)

    @event.listens_for(Engine, "before_cursor_execute")
    def before_sql(connection, cursor, statement, parameters, context, executemany):
        role_match = role_drop.fullmatch(statement)
        if role_match:
            role_name, suffix = role_match.groups()
            assert role_name not in initial_roles and "nip_roles_" + suffix in owned
            with cursor.connection.cursor() as inspect_cursor:
                inspect_cursor.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (role_name,))
                row = inspect_cursor.fetchone()
            assert row is not None
            role_events[role_name] = {
                "name": role_name,
                "oid": row[0],
                "identity_verified_before_drop": True,
                "owned_database": "nip_roles_" + suffix,
            }
            return
        match = pattern.fullmatch(statement)
        if not match or match[1].upper() != "DROP":
            return
        name = match[2]
        assert name in owned, "refusing cleanup of a database not created in this run"
        with cursor.connection.cursor() as inspect_cursor:
            inspect_cursor.execute("SELECT oid FROM pg_database WHERE datname=%s", (name,))
            row = inspect_cursor.fetchone()
        assert row and row[0] == owned[name]["oid"]
        owned[name]["identity_verified_before_drop"] = True
        probe = psycopg2.connect(
            host="127.0.0.1", port=record["port"], user="news", password="news", dbname=name
        )
        try:
            with probe.cursor() as inspect_cursor:
                inspect_cursor.execute("SELECT to_regclass('public.alembic_version')")
                if inspect_cursor.fetchone()[0]:
                    inspect_cursor.execute(
                        "SELECT version_num FROM alembic_version ORDER BY version_num"
                    )
                    owned[name]["applied_revisions_before_drop"] = [
                        r[0] for r in inspect_cursor.fetchall()
                    ]
        finally:
            probe.close()

    @event.listens_for(Engine, "after_cursor_execute")
    def after_sql(connection, cursor, statement, parameters, context, executemany):
        role_match = role_drop.fullmatch(statement)
        if role_match:
            role_name = role_match[1]
            with cursor.connection.cursor() as inspect_cursor:
                inspect_cursor.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (role_name,))
                assert inspect_cursor.fetchone() is None
            role_events[role_name]["absent_after_drop"] = True
            return
        match = pattern.fullmatch(statement)
        if not match:
            return
        operation, name = match[1].upper(), match[2]
        with cursor.connection.cursor() as inspect_cursor:
            inspect_cursor.execute("SELECT oid FROM pg_database WHERE datname=%s", (name,))
            row = inspect_cursor.fetchone()
        if operation == "CREATE":
            assert name not in owned and row
            owned[name] = {"name": name, "oid": row[0], "created_by_this_run": True}
            database_events.append(owned[name])
        else:
            assert row is None
            owned[name]["absent_after_drop"] = True

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def verify_address(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host, port = address[:2]
            assert ipaddress.ip_address(host).is_loopback and port == record["port"], (
                "network blocked outside owned database"
            )

    def guarded_connect(sock, address):
        verify_address(sock, address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        verify_address(sock, address)
        return original_connect_ex(sock, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    args = ["-q", "-p", "no:cacheprovider", "--junitxml=" + str(OUT / (mode + ".xml")), *targets]
    write(
        mode + "-command.json",
        {
            "command": [PYTHON, str(Path(__file__)), mode, *targets],
            "pytest_args": args,
            "environment": dict(os.environ),
            "network_control": "Python socket connections allowed only to owned database port; production providers use synthetic MockTransport",
            "expected": "zero failures/errors/skips; full integration marker gate"
            if mode == "full-integration"
            else "all selected checks pass",
        },
    )
    try:
        with (
            (OUT / (mode + ".log")).open("w") as log,
            contextlib.redirect_stdout(log),
            contextlib.redirect_stderr(log),
        ):
            code = int(pytest.main(args))
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        event.remove(Engine, "before_cursor_execute", before_sql)
        event.remove(Engine, "after_cursor_execute", after_sql)
        after_inventory = db_inventory(record)
        write(
            mode + "-resources.json",
            {
                "container_id": record["id"],
                "before": inventories,
                "after": after_inventory,
                "inventory_unchanged": inventories == after_inventory,
                "databases": database_events,
                "roles": list(role_events.values()),
                "all_owned_databases_removed": all(
                    x.get("absent_after_drop") for x in database_events
                ),
            },
        )
        after = snapshot(mode + "-source-after")
        drift = sorted(
            k
            for k in set(before["hashes"]) | set(after["hashes"])
            if before["hashes"].get(k) != after["hashes"].get(k)
        )
        write(
            mode + "-result.json",
            {
                "exit_code": locals().get("code"),
                "source_drift": drift,
                "protected_unchanged": before["protected_hashes"] == after["protected_hashes"],
                "inventory_unchanged": inventories == after_inventory,
            },
        )
    print((OUT / (mode + ".log")).read_text()[-6000:])
    assert inventories == after_inventory and not drift
    assert before["protected_hashes"] == after["protected_hashes"]
    assert before["staged_diff_sha256"] == after["staged_diff_sha256"]
    sys.exit(code)


def cleanup():
    record = resource()
    inventory = db_inventory(record)
    assert inventory == json.loads((OUT / "database-before.json").read_text())
    command(["docker", "stop", record["id"]])
    row = json.loads(command(["docker", "inspect", record["id"]]).stdout)[0]
    assert (
        row["Id"] == record["id"]
        and row["Config"]["Labels"]["nip.closeout.owner"] == record["owner"]
    )
    assert not row["State"]["Running"] and not row["Mounts"]
    assert row["HostConfig"]["Tmpfs"] == {"/var/lib/postgresql/data": "rw"}
    command(["docker", "rm", record["id"]])
    remaining = command(
        ["docker", "ps", "-aq", "--filter", "label=nip.closeout.owner=" + record["owner"]]
    ).stdout.strip()
    assert not remaining
    old = json.loads((OUT / "containers-before.json").read_text())
    after = container_inventory()
    after_map = {row["id"]: row for row in after}
    preserved = all(after_map.get(row["id"]) == row for row in old)
    write(
        "resource-cleanup.json",
        {
            "container_id": record["id"],
            "identity_and_label_verified_before_removal": True,
            "container_absent": not remaining,
            "no_owned_volume": True,
            "database_role_inventory_restored": True,
            "preexisting_count": len(old),
            "preexisting_containers_unchanged": preserved,
            "containers_after": after,
            "completed_utc": dt.datetime.now(dt.UTC).isoformat(),
        },
    )
    assert preserved
    snapshot("source-final")
    print("owned container removed; preexisting containers preserved")


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "init":
        initialize()
    elif mode == "cleanup":
        cleanup()
    else:
        run_gate(mode, sys.argv[2:])
