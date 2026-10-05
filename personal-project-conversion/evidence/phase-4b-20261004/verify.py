"""Phase 4B owned-resource runner. No live provider credentials or shared services."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import signal
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
    REDIS_URL="",
    CELERY_BROKER_URL="",
    CELERY_RESULT_BACKEND="",
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
    return value


def resource():
    record = json.loads((OUT / "owned-resource.json").read_text())
    row = json.loads(command(["docker", "inspect", record["id"]]).stdout)[0]
    assert row["Id"] == record["id"] and row["Name"] == "/" + record["name"]
    assert row["Config"]["Labels"]["nip.phase4b.owner"] == record["owner"]
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
        PHASE4B_OWNED_POSTGRES_PORT=str(record["port"]),
        PYTHONPATH=str(OUT / "owned-network-guard") + os.pathsep + str(ROOT),
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
    before = container_inventory()
    write("containers-before.json", before)
    owner = uuid.uuid4().hex
    name = "nip-phase4b-" + owner[:12]
    image = "sha256:f87fefc064506355f316088583365f286bde08bf8dd175ec08bcb3a6b1823c1c"
    assert json.loads(command(["docker", "image", "inspect", image]).stdout)[0]["Id"] == image
    database = "nip_phase4b_base_" + owner[:12]
    args = [
        "docker",
        "run",
        "-d",
        "--pull",
        "never",
        "--name",
        name,
        "--label",
        "nip.phase4b.owner=" + owner,
        "--label",
        "nip.phase4b.purpose=phase4b-verification",
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
    for attempt in range(60):
        try:
            inventory = db_inventory(record)
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise RuntimeError("Owned PostgreSQL did not accept its loopback connection")
    write("database-before.json", inventory)
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
                "network blocked outside owned PostgreSQL"
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
            "environment": {key: os.environ[key] for key in sorted(os.environ) if key not in {"HOME", "PATH", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT"}},
            "network_control": "Python socket connections allowed only to exact owned PostgreSQL ports; providers use synthetic MockTransport",
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
        and row["Config"]["Labels"]["nip.phase4b.owner"] == record["owner"]
    )
    assert not row["State"]["Running"] and not row["Mounts"]
    assert row["HostConfig"]["Tmpfs"] == {"/var/lib/postgresql/data": "rw"}
    command(["docker", "rm", record["id"]])
    remaining = command(
        ["docker", "ps", "-aq", "--filter", "label=nip.phase4b.owner=" + record["owner"]]
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
    print("owned container removed; preexisting containers preserved")


# These operational helpers only target identities created and recorded by this run.
STACK = OUT / "stack-resource.json"


def state():
    value = json.loads(STACK.read_text())
    assert value["owner"] == resource()["owner"]
    return value


def save(value):
    write(STACK.name, value)


def database_url(record, database):
    return record["url"].rsplit("/", 1)[0] + "/" + database


def environment(record, stack, key):
    result = dict(BASE_ENV)
    result.update(
        DATABASE_URL=database_url(record, stack["databases"][key]["name"]),
        PERSONAL_PROCESSING_TRANSPORT="subprocess",
        PERSONAL_PROCESSING_MODE="personal",
        PERSONAL_BIND_HOST="127.0.0.1",
        PERSONAL_APP_WORKERS="1",
        PERSONAL_OFFLINE_FIXTURE_PATH=str(
            ROOT / "personal-project-conversion/fixtures/phase-2-offline-workflow.json"
        ),
        CORS_ALLOW_ORIGINS="http://127.0.0.1:" + str(stack["api_port"]),
        PYTHONPATH=str(OUT / "owned-network-guard") + os.pathsep + str(ROOT),
        PHASE4B_OWNED_POSTGRES_PORT=str(record["port"]),
        STACK_OWNER=record["owner"],
    )
    assert all(result[key] == "" for key in CREDS)
    assert result["PERSONAL_PAID_RUNTIME_ENABLED"] == "false"
    return result


def create_database(stack, key, *, empty=False):
    import psycopg2
    from psycopg2 import sql
    record = resource()
    name = "nip_phase4b_" + key + "_" + record["owner"][:12]
    connection = psycopg2.connect(
        host="127.0.0.1", port=record["port"], user="news", password="news", dbname="postgres"
    )
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT oid FROM pg_database WHERE datname=%s", (name,))
            assert cursor.fetchone() is None
            query = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
            if not empty:
                query += sql.SQL(" TEMPLATE {}").format(sql.Identifier(record["database"]))
            cursor.execute(query)
            cursor.execute("SELECT oid FROM pg_database WHERE datname=%s", (name,))
            stack["databases"][key] = {
                "name": name, "oid": cursor.fetchone()[0], "created_by_owner": record["owner"],
            }
    finally:
        connection.close()
    save(stack)


def python_code(record, stack, key, code):
    return command([PYTHON, "-c", code], env=environment(record, stack, key))


def setup_stack():
    import ast
    assert not STACK.exists()
    record = resource()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        app_port = sock.getsockname()[1]
    stack = {
        "owner": record["owner"], "api_port": app_port, "databases": {}, "processes": {},
        "persistent_services": ["application", "owned PostgreSQL"],
        "created_redis_celery_beat": False,
    }
    save(stack)
    guard_directory = OUT / "owned-network-guard"
    guard_directory.mkdir()
    guard_code = """import os,socket,ipaddress
port=os.environ.get('PHASE4B_OWNED_POSTGRES_PORT')
if port:
 original_connect,original_connect_ex=socket.socket.connect,socket.socket.connect_ex
 def validate(sock,address):
  if sock.family in (socket.AF_INET,socket.AF_INET6):
   host,target_port=address[:2]
   assert ipaddress.ip_address(host).is_loopback and target_port==int(port),'network blocked outside exact owned PostgreSQL'
 def connect(sock,address):
  validate(sock,address);return original_connect(sock,address)
 def connect_ex(sock,address):
  validate(sock,address);return original_connect_ex(sock,address)
 socket.socket.connect=connect
 socket.socket.connect_ex=connect_ex
"""
    guard_path = guard_directory / "sitecustomize.py"
    guard_path.write_text(guard_code)
    stack["network_guard"] = {"path": str(guard_path),
                              "sha256": hashlib.sha256(guard_code.encode()).hexdigest(),
                              "inherited_by_app_cli_children_and_test_subprocesses": True}
    save(stack)
    # Parse a literal, without importing or executing any previous evidence helper.
    tree = ast.parse((OUT.parent / "phase-4a-20261004/stack.py").read_text())
    seed = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SEED" for target in node.targets)
    )
    seed = seed.replace("Synthetic Phase 4A owned feed", "Synthetic Phase 4B owned feed")
    for key in ("browser", "manual"):
        create_database(stack, key)
        result = python_code(record, stack, key, seed)
        (OUT / (key + "-setup.log")).write_text(result.stdout + result.stderr)
    write("stack-setup-result.json", {
        "owner": record["owner"], "container_id": record["id"],
        "app_port": app_port, "databases": stack["databases"],
        "empty_migrated_databases_then_supported_workspace_setup": True,
        "synthetic_fixture": "phase-2-offline-workflow-v1",
        "credential_fields_blank": CREDS, "paid_activation": False,
        "redis_celery_beat_created": False,
    })
    print(json.dumps({"app_port": app_port, "databases": stack["databases"]}))


def install_network_guard():
    """Python socket guard for the owned app/CLI/test parent; libpq is separately URL-scoped."""
    allowed_port = int(os.environ["PHASE4B_OWNED_POSTGRES_PORT"])
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    def validate(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host, target_port = address[:2]
            assert ipaddress.ip_address(host).is_loopback and target_port == allowed_port, (
                "network blocked outside exact owned PostgreSQL"
            )
    def guarded_connect(sock, address):
        validate(sock, address)
        return original_connect(sock, address)
    def guarded_connect_ex(sock, address):
        validate(sock, address)
        return original_connect_ex(sock, address)
    socket.socket.connect, socket.socket.connect_ex = guarded_connect, guarded_connect_ex


def process_inventory():
    result = command(["ps", "-axo", "pid=,ppid=,pgid=,lstart=,comm="])
    return result.stdout.splitlines()


def start_process(stack, kind, args, env):
    assert kind not in stack["processes"]
    with (OUT / (kind + ".log")).open("w") as log:
        process = subprocess.Popen(
            args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, shell=False,
        )
    created = command(["ps", "-p", str(process.pid), "-o", "lstart="]).stdout.strip()
    cmdline = command(["ps", "-ww", "-p", str(process.pid), "-o", "command="]).stdout.strip()
    stack["processes"][kind] = {
        "pid": process.pid, "created": created, "cmdline": cmdline,
        "pgid": os.getpgid(process.pid), "args": args, "owner": stack["owner"],
        "started_utc": dt.datetime.now(dt.UTC).isoformat(),
    }
    save(stack)


def stop_process(stack, kind):
    entry = stack["processes"][kind]
    if entry.get("stopped_utc"):
        return
    started = command(["ps", "-p", str(entry["pid"]), "-o", "lstart="], check=False).stdout.strip()
    current = command(["ps", "-ww", "-p", str(entry["pid"]), "-o", "command="], check=False).stdout.strip()
    if started:
        assert started == entry["created"] and current == entry["cmdline"]
        assert os.getpgid(entry["pid"]) == entry["pgid"] == entry["pid"]
        entry["identity_verified_before_signal"] = True
        os.killpg(entry["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            status = command(["ps", "-p", str(entry["pid"]), "-o", "stat="], check=False).stdout.strip()
            if not status or "Z" in status:
                break
            time.sleep(0.1)
        else:
            assert command(["ps", "-p", str(entry["pid"]), "-o", "lstart="]).stdout.strip() == entry["created"]
            assert command(["ps", "-ww", "-p", str(entry["pid"]), "-o", "command="]).stdout.strip() == entry["cmdline"]
            os.killpg(entry["pid"], signal.SIGKILL)
            entry["hard_cleanup_required"] = True
    else:
        entry["already_absent"] = True
    entry["stopped_utc"] = dt.datetime.now(dt.UTC).isoformat()
    save(stack)


def confirm_app_exec_identity(stack):
    entry = stack["processes"]["app"]
    assert entry["owner"] == stack["owner"]
    created = command(["ps", "-p", str(entry["pid"]), "-o", "lstart="]).stdout.strip()
    current = command(["ps", "-ww", "-p", str(entry["pid"]), "-o", "command="]).stdout.strip()
    framework_binary = Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python"
    executable = str(framework_binary.resolve() if framework_binary.is_file() else Path(PYTHON).resolve())
    expected = " ".join([executable, "-m", "services.personal.app", "--port", str(stack["api_port"])])
    assert created == entry["created"]
    assert os.getpgid(entry["pid"]) == entry["pgid"] == entry["pid"]
    assert current == expected, (current, expected)
    if entry["cmdline"] != current:
        entry["exec_transition_wrapper_cmdline"] = entry["cmdline"]
    entry["cmdline"] = current
    entry["expected_exec_module_verified"] = True
    save(stack)


def start_app():
    import urllib.request
    record, stack = resource(), state()
    before = process_inventory()
    write("processes-before-app.json", before)
    start_process(
        stack, "app", [PYTHON, str(ROOT / "scripts/personal-local.py"), "app", "--port", str(stack["api_port"])],
        environment(record, stack, "browser"),
    )
    address = "http://127.0.0.1:" + str(stack["api_port"])
    for _ in range(100):
        try:
            with urllib.request.urlopen(address + "/health", timeout=2) as response:
                health = json.loads(response.read())
                assert response.status == 200
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("Owned app did not reach healthy readiness; inspect app.log")
    confirm_app_exec_identity(stack)
    write("app-readiness.json", {
        "address": address, "health": health, "process": stack["processes"]["app"],
        "process_inventory": process_inventory(), "containers": container_inventory(),
        "paid_activation": False, "credentials_blank": True,
        "redis_celery_beat_created": False,
    })
    print(address, flush=True)


def manual():
    record, stack = resource(), state()
    args = [PYTHON, str(ROOT / "scripts/personal-local.py"), "update"]
    result = command(args, check=False, env=environment(record, stack, "manual"))
    (OUT / "manual-cli.log").write_text(result.stdout + result.stderr)
    code = """
from sqlalchemy import select
from db.base import SessionLocal,engine
from db.models import PersonalRun,PersonalPaidRequest
with SessionLocal() as session:
 runs=session.scalars(select(PersonalRun)).all()
 assert len(runs)==1 and runs[0].state=='succeeded'
 assert session.scalars(select(PersonalPaidRequest)).all()==[]
 print(__import__('json').dumps({'run_id':str(runs[0].id),'state':runs[0].state,'report_id':str(runs[0].report_id),'paid_requests':0}))
engine.dispose()
"""
    inspect = python_code(record, stack, "manual", code) if result.returncode == 0 else None
    write("manual-cli-result.json", {
        "command": args, "exit_code": result.returncode,
        "result": json.loads(inspect.stdout.strip().splitlines()[-1]) if inspect else None,
        "synthetic": True, "redis_celery_beat_created": False, "paid_activation": False,
    })
    assert result.returncode == 0, result.stdout[-1000:] + result.stderr[-1000:]
    print("Actual isolated CLI completed with a published synthetic brief and zero paid requests")


def table_fingerprints(record, database):
    import psycopg2
    from psycopg2 import sql
    connection = psycopg2.connect(
        host="127.0.0.1", port=record["port"], user="news", password="news", dbname=database
    )
    result = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            )
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                cursor.execute(sql.SQL(
                    "SELECT row_to_json(t)::text FROM public.{} t ORDER BY row_to_json(t)::text"
                ).format(sql.Identifier(table)))
                rows = [row[0] for row in cursor.fetchall()]
                result[table] = {
                    "count": len(rows),
                    "sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest(),
                }
    finally:
        connection.close()
    return result


def backup_restore():
    record, stack = resource(), state()
    # The browser work must already be complete before this explicit invocation.
    source = stack["databases"]["browser"]["name"]
    before = table_fingerprints(record, source)
    write("backup-source-tables.json", before)
    backup_environment = environment(record, stack, "browser")
    backup_environment.update(
        DATABASE_URL="postgresql://news:news@127.0.0.1:5432/" + source,
        POSTGRES_TOOL_CONTAINER=record["id"],
        BACKUP_DIR=str(OUT / "backups"),
    )
    dump_args = ["bash", str(ROOT / "scripts/backup-postgres.sh")]
    result = command(dump_args, env=backup_environment)
    (OUT / "backup-command.log").write_text(result.stdout + result.stderr)
    backup = Path(result.stdout.strip().splitlines()[-1])
    assert backup.parent == OUT / "backups"
    checksum = backup.with_suffix(backup.suffix + ".sha256")
    assert checksum.read_text().strip() == hashlib.sha256(backup.read_bytes()).hexdigest()
    create_database(stack, "restored", empty=True)
    restored = stack["databases"]["restored"]["name"]
    restore_environment = dict(backup_environment)
    restore_environment.update(
        DATABASE_URL="postgresql://news:news@127.0.0.1:5432/" + restored,
        RESTORE_TARGET_DISPOSABLE="1", RESTORE_EXPECTED_DATABASE=restored,
    )
    restore_args = ["bash", str(ROOT / "scripts/restore-postgres.sh"), str(backup)]
    restore = command(restore_args, env=restore_environment)
    (OUT / "backup-restore.log").write_text(restore.stdout + restore.stderr)
    after = table_fingerprints(record, restored)
    write("restored-tables.json", after)
    assert before == after
    # Reconcile exact dead-child lifetimes first, then perform the supported idle mode switch.
    reconcile = command(
        [PYTHON, "-m", "services.personal.cli", "reconcile-children"],
        env=environment(record, stack, "restored"),
    )
    switch = command(
        [PYTHON, "-m", "services.personal.cli", "mode", "legacy"],
        env=environment(record, stack, "restored"),
    )
    final = table_fingerprints(record, restored)
    drift = [name for name in before if before[name] != final[name]]
    assert set(drift).issubset({"personal_writer_mode", "personal_processing_children"})
    assert all(before[name]["count"] == final[name]["count"] for name in before)
    assert json.loads(switch.stdout.strip().splitlines()[-1])["mode"] == "legacy"
    read_code = """
from fastapi.testclient import TestClient
from apps.api.main import app
from sqlalchemy import select
from db.base import SessionLocal,engine
from db.models import Report,PersonalRun
with SessionLocal() as session:
 runs=session.scalars(select(PersonalRun)).all()
 reports=session.scalars(select(Report)).all()
 assert any(run.state=='succeeded' for run in runs)
 assert any(str(report.status)=='published' for report in reports)
with TestClient(app,client=('127.0.0.1',50001)) as client:
 checks={}
 for path in ('/api/v1/personal/events','/api/v1/personal/saved','/api/v1/personal/briefs'):
  response=client.get(path);checks[path]=response.status_code;assert response.status_code==200
 print(__import__('json').dumps(checks))
engine.dispose()
"""
    # Prior supported runtime is selected without starting a worker or dispatching work.
    restored_env = environment(record, stack, "restored")
    restored_env.update(PERSONAL_PROCESSING_TRANSPORT="celery", PERSONAL_PROCESSING_MODE="legacy",
                        CELERY_BROKER_URL="memory://", CELERY_RESULT_BACKEND="cache+memory://")
    read = command([PYTHON, "-c", read_code], env=restored_env)
    write("backup-restore-result.json", {
        "source_database": source, "restored_database": restored,
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "backup_script_command": dump_args, "restore_script_command": restore_args,
        "backup_checksum_file": str(checksum), "checksum_verified": True,
        "restored_oid": stack["databases"]["restored"]["oid"],
        "all_public_tables_equal_after_restore": before == after,
        "table_count": len(before), "table_fingerprints": before,
        "mode_switch_to": "legacy",
        "all_row_counts_retained_after_mode_switch": True,
        "only_control_metadata_changed": drift,
        "reconciliation": json.loads(reconcile.stdout.strip().splitlines()[-1]),
        "mode_switch": json.loads(switch.stdout.strip().splitlines()[-1]),
        "restored_stored_read_endpoints": json.loads(read.stdout.strip().splitlines()[-1]),
        "schema_downgraded": False,
    })
    print("Populated database backup/restoration verified, and idle legacy switch retained every row")


def cleanup_stack():
    import psycopg2
    from psycopg2 import sql
    record, stack = resource(), state()
    for kind in reversed(tuple(stack["processes"])):
        stop_process(stack, kind)
    connection = psycopg2.connect(
        host="127.0.0.1", port=record["port"], user="news", password="news", dbname="postgres"
    )
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for entry in stack["databases"].values():
                cursor.execute("SELECT oid FROM pg_database WHERE datname=%s", (entry["name"],))
                assert cursor.fetchone() == (entry["oid"],)
                cursor.execute("SELECT pid FROM pg_stat_activity WHERE datname=%s", (entry["name"],))
                assert cursor.fetchall() == []
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(entry["name"])))
                cursor.execute("SELECT oid FROM pg_database WHERE datname=%s", (entry["name"],))
                assert cursor.fetchone() is None
                entry["identity_verified_before_drop"] = True
                entry["absent_after_drop"] = True
    finally:
        connection.close()
    guard_path = Path(stack["network_guard"]["path"])
    assert guard_path.parent == OUT / "owned-network-guard"
    assert hashlib.sha256(guard_path.read_bytes()).hexdigest() == stack["network_guard"]["sha256"]
    guard_path.unlink()
    guard_path.parent.rmdir()
    stack["network_guard"]["identity_verified_removed"] = True
    save(stack)
    write("stack-cleanup.json", {
        "owner": stack["owner"], "processes": stack["processes"],
        "databases": stack["databases"], "redis_celery_beat_created": False,
        "completed_utc": dt.datetime.now(dt.UTC).isoformat(),
    })
    cleanup()



def populate_obligations():
    import ast
    record, stack = resource(), state()
    tree = ast.parse((OUT.parent / "phase-4a-20261004/backup_restore.py").read_text())
    seed = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SEED" for target in node.targets)
    )
    # Preserve the original active profile as immutable history, then append its equivalent
    # after the explicitly authored synthetic accounting experiment.
    seed = seed.replace(
        "now=dt.datetime.now(dt.UTC)+dt.timedelta(days=1)",
        """from services.personal.workspace import configure_profile
with SessionLocal() as s:
 w=s.scalar(select(PersonalWorkspace))
 p=s.get(PersonalProfileRevision,w.active_profile_revision_id)
 original=dict(selected_source_ids=list(p.selected_source_ids),
               include_phrases=list(p.include_phrases),exclude_phrases=list(p.exclude_phrases),
               execution_profile=p.execution_profile,settings=dict(p.settings or {}))
now=dt.datetime.now(dt.UTC)+dt.timedelta(days=1)"""
    )
    seed = seed.replace(
        "print(json.dumps({'run_id':",
        """with SessionLocal() as s:
 w=s.scalar(select(PersonalWorkspace))
 configure_profile(s,w,**original);s.commit()
print(json.dumps({'authored_clock':'next_day_synthetic_accounting_only','run_id':"""
    )
    result = python_code(record, stack, "browser", seed)
    (OUT / "synthetic-ledger-setup.log").write_text(result.stdout + result.stderr)
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    tables = table_fingerprints(record, stack["databases"]["browser"]["name"])
    assert tables["personal_paid_requests"]["count"] >= 2
    evidence.update(
        paid_table=tables["personal_paid_requests"], original_offline_profile_restored=True,
        runtime_credentials_blank=True,
    )
    write("synthetic-ledger-setup-result.json", evidence)
    print("Synthetic known and uncertain obligations retained; no provider HTTP call")


def restart_app():
    import urllib.request
    record, stack = resource(), state()
    before = table_fingerprints(record, stack["databases"]["browser"]["name"])
    assert before["personal_paid_requests"]["count"] >= 2
    write("restart-tables-before.json", before)
    readiness = OUT / "app-readiness.json"
    (OUT / "app-readiness-before-restart.json").write_bytes(readiness.read_bytes())
    stop_process(stack, "app")
    (OUT / "app-before-restart.log").write_bytes((OUT / "app.log").read_bytes())
    stack["processes"]["app_before_restart"] = stack["processes"].pop("app")
    save(stack)
    start_app()
    after = table_fingerprints(record, stack["databases"]["browser"]["name"])
    write("restart-tables-after.json", after)
    assert before == after
    address = "http://127.0.0.1:" + str(stack["api_port"])
    reads = {}
    for path in ("/api/v1/personal/events", "/api/v1/personal/saved",
                 "/api/v1/personal/briefs", "/api/v1/personal/spending"):
        with urllib.request.urlopen(address + path, timeout=5) as response:
            reads[path] = response.status
            assert response.status == 200
    final = table_fingerprints(record, stack["databases"]["browser"]["name"])
    assert after == final
    write("restart-result.json", {
        "all_public_tables_equal": True, "table_count": len(before),
        "run_count_before": before["personal_runs"]["count"],
        "run_count_after": after["personal_runs"]["count"],
        "paid_request_count": before["personal_paid_requests"]["count"],
        "no_automatic_replacement": True, "stored_read_endpoints": reads,
        "paid_activation": False, "redis_celery_beat_created": False,
        "source": stack["databases"]["browser"],
    })
    print("App restart retained all public-table rows, spending, history and run count")


def accepted_browser():
    import ast
    record, stack = resource(), state()
    assert "browser_initial" not in stack["databases"]
    original_tables = table_fingerprints(record, stack["databases"]["browser"]["name"])
    write("browser-initial-preserved-tables.json", original_tables)
    stop_process(stack, "app")
    (OUT / "app-initial.log").write_bytes((OUT / "app.log").read_bytes())
    (OUT / "app-initial-readiness.json").write_bytes((OUT / "app-readiness.json").read_bytes())
    stack["databases"]["browser_initial"] = stack["databases"].pop("browser")
    stack["processes"]["app_initial"] = stack["processes"].pop("app")
    save(stack)
    create_database(stack, "browser_accepted")
    stack["databases"]["browser"] = stack["databases"].pop("browser_accepted")
    stack["databases"]["browser"]["evidence_epoch"] = "accepted_final_production_code"
    save(stack)
    tree = ast.parse((OUT.parent / "phase-4a-20261004/stack.py").read_text())
    seed = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SEED" for target in node.targets)
    )
    seed = seed.replace("Synthetic Phase 4A owned feed", "Synthetic Phase 4B accepted owned feed")
    result = python_code(record, stack, "browser", seed)
    (OUT / "browser-accepted-setup.log").write_text(result.stdout + result.stderr)
    source = snapshot("browser-accepted-source-before")
    start_app()
    original_after = table_fingerprints(record, stack["databases"]["browser_initial"]["name"])
    assert original_tables == original_after
    fresh_tables = table_fingerprints(record, stack["databases"]["browser"]["name"])
    assert fresh_tables["personal_runs"]["count"] == 0
    assert fresh_tables["personal_paid_requests"]["count"] == 0
    write("browser-accepted-setup-result.json", {
        "source": stack["databases"]["browser"],
        "original": stack["databases"]["browser_initial"],
        "original_all_public_tables_unchanged": True,
        "supported_seed": "phase-2-offline-workflow-v1",
        "fresh_zero_runs_and_paid_requests": True,
        "source_identity": "browser-accepted-source-before.json",
        "ordinary_paid_activation": False,
    })
    print("Accepted browser ready: http://127.0.0.1:" + str(stack["api_port"]), flush=True)

if __name__ == "__main__":
    import signal
    mode = sys.argv[1]
    if mode == "init":
        initialize()
    elif mode == "prepare":
        record = resource()
        configure(record)
        write("database-before.json", db_inventory(record))
        result = command([PYTHON, "-m", "alembic", "upgrade", "head"], check=False)
        (OUT / "blank-upgrade.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0
        print("Owned base migration complete")
    elif mode == "setup":
        setup_stack()
    elif mode == "confirm-app":
        confirm_app_exec_identity(state())
    elif mode == "app":
        start_app()
    elif mode == "serve":
        install_network_guard()
        from services.personal.app import main
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        main()
    elif mode == "cli":
        install_network_guard()
        from services.personal.cli import main
        raise SystemExit(main(sys.argv[2:]))
    elif mode == "manual":
        manual()
    elif mode == "accepted-browser":
        accepted_browser()
    elif mode == "obligations":
        populate_obligations()
    elif mode == "restart":
        restart_app()
    elif mode == "backup":
        backup_restore()
    elif mode == "cleanup":
        cleanup_stack()
    else:
        run_gate(mode, sys.argv[2:])
