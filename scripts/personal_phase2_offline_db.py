#!/usr/bin/env python3
"""Own the disposable database and manifest for the Phase 2 offline harness."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from services.personal.offline_fixture import offline_fixture_route

SCHEMA = "personal-phase2-offline-harness.v2"
DATABASE_PREFIX = "nip_personal_phase2_offline_"
FEED_URL = "https://offline.personal.test/feed.xml"
FIXTURE_ID = "phase-2-offline-workflow-v1"
MARKER_TABLE = "personal_offline_harness_ownership"


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise RuntimeError("offline harness manifest schema is unsupported")
    if Path(payload.get("state_dir", "")).resolve() != path.parent.resolve():
        raise RuntimeError("offline harness manifest does not own this state directory")
    return payload


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _database_name(value: str) -> str:
    if not value.startswith(DATABASE_PREFIX) or not re.fullmatch(r"[a-z0-9_]+", value):
        raise ValueError("offline harness database name is outside its reserved namespace")
    return value


def _frontend_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("offline harness frontend origin is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "offline harness frontend origin must be credential-free loopback HTTP with a port"
        )
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"http://{host}:{port}"


def prepare(args: argparse.Namespace) -> None:
    manifest = Path(args.manifest).resolve()
    if manifest.exists():
        raise RuntimeError("offline harness manifest already exists")
    manifest.parent.mkdir(mode=0o700, parents=False, exist_ok=False)
    payload = {
        "schema": SCHEMA,
        "dataset": "synthetic",
        "run_id": args.run_id,
        "state_dir": str(manifest.parent),
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "database_name": _database_name(args.database_name),
        "owner_token": args.owner_token,
        "postgres": {
            "mode": args.postgres_mode,
            "container_name": args.postgres_container or None,
            "container_id": None,
            "host_port": None,
            "owned": False,
        },
        "redis": {
            "container_name": args.redis_container,
            "container_id": None,
            "host_port": None,
            "owned": False,
        },
        "database": {"owned": False, "url": None},
        "processes": {"api": None, "worker": None},
        "fixture_id": FIXTURE_ID,
        "feed_url": FEED_URL,
    }
    _write_manifest(manifest, payload)
    print(manifest.parent)


def record_container(args: argparse.Namespace) -> None:
    manifest = Path(args.manifest).resolve()
    payload = _load_manifest(manifest)
    resource = payload[args.kind]
    if resource.get("owned"):
        raise RuntimeError(f"manifest already owns a {args.kind} container")
    resource.update(
        {"container_id": args.container_id, "host_port": args.host_port, "owned": True}
    )
    _write_manifest(manifest, payload)


def _database_url(maintenance_url: str, database_name: str) -> str:
    return make_url(maintenance_url).set(database=database_name).render_as_string(
        hide_password=False
    )


def init_database(args: argparse.Namespace) -> None:
    from db.models import Source
    from services.personal.workspace import configure_profile, ensure_workspace

    manifest = Path(args.manifest).resolve()
    payload = _load_manifest(manifest)
    if payload["database"]["owned"]:
        raise RuntimeError("manifest already owns a database")
    database_name = _database_name(payload["database_name"])
    owner_token = payload["owner_token"]
    maintenance_url = args.maintenance_url
    parsed_maintenance = make_url(maintenance_url)
    if parsed_maintenance.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("offline harness external PostgreSQL must be loopback-only")
    if parsed_maintenance.port in {None, 5432}:
        raise ValueError("offline harness external PostgreSQL must use an explicit non-5432 port")
    database_url = _database_url(maintenance_url, database_name)
    admin = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            if connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database_name},
            ):
                raise RuntimeError("offline harness database already exists")
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        database = create_engine(database_url)
        try:
            try:
                with database.begin() as connection:
                    connection.execute(
                        text(
                            f"CREATE TABLE {MARKER_TABLE} ("
                            "owner_token text PRIMARY KEY, created_at timestamptz NOT NULL)"
                        )
                    )
                    connection.execute(
                        text(
                            f"INSERT INTO {MARKER_TABLE} (owner_token, created_at) "
                            "VALUES (:token, now())"
                        ),
                        {"token": owner_token},
                    )
            except Exception:
                with admin.connect() as connection:
                    connection.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :name AND pid <> pg_backend_pid()"
                        ),
                        {"name": database_name},
                    )
                    connection.execute(text(f'DROP DATABASE "{database_name}"'))
                raise
        finally:
            database.dispose()
        payload["database"] = {"owned": True, "url": database_url}
        _write_manifest(manifest, payload)

        environment = dict(os.environ)
        environment["DATABASE_URL"] = database_url
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
            cwd=Path(__file__).parents[1],
            env=environment,
        )
        engine = create_engine(database_url)
        try:
            with Session(engine) as session:
                source = Source(
                    name="Synthetic Phase 2 acceptance feed",
                    feed_url=FEED_URL,
                    active=True,
                )
                session.add(source)
                session.flush()
                workspace, _ = ensure_workspace(session)
                configure_profile(
                    session,
                    workspace,
                    selected_source_ids=[source.id],
                    include_phrases=["agency"],
                    settings={"model_route": offline_fixture_route(FIXTURE_ID)},
                )
                session.commit()
                payload.update(
                    {"workspace_id": str(workspace.id), "source_id": str(source.id)}
                )
                _write_manifest(manifest, payload)
        finally:
            engine.dispose()
    finally:
        admin.dispose()


def record_process(args: argparse.Namespace) -> None:
    manifest = Path(args.manifest).resolve()
    payload = _load_manifest(manifest)
    if payload["processes"].get(args.kind) is not None:
        raise RuntimeError(f"manifest already owns a {args.kind} process")
    payload["processes"][args.kind] = {
        "pid": args.pid,
        "started": args.started,
        "command_marker": args.command_marker,
    }
    _write_manifest(manifest, payload)


def cleanup_database(args: argparse.Namespace) -> None:
    manifest = Path(args.manifest).resolve()
    payload = _load_manifest(manifest)
    database = payload["database"]
    if not database.get("owned"):
        return
    database_name = _database_name(payload["database_name"])
    database_url = database.get("url")
    if not isinstance(database_url, str) or not database_url:
        raise RuntimeError("owned database URL is missing from manifest")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            token = connection.scalar(text(f"SELECT owner_token FROM {MARKER_TABLE}"))
            if token != payload["owner_token"]:
                raise RuntimeError("database ownership marker does not match manifest")
    finally:
        engine.dispose()
    maintenance_url = make_url(database_url).set(database="postgres").render_as_string(
        hide_password=False
    )
    admin = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
    finally:
        admin.dispose()
    payload["database"]["owned"] = False
    _write_manifest(manifest, payload)


def value(args: argparse.Namespace) -> None:
    payload: Any = _load_manifest(Path(args.manifest).resolve())
    for part in args.field.split("."):
        if payload is None:
            return
        payload = payload[part]
    if payload is None:
        return
    if isinstance(payload, bool):
        print("true" if payload else "false")
    else:
        print(payload)


def validate_frontend_origin(args: argparse.Namespace) -> None:
    print(_frontend_origin(args.origin))


def spawn_process(args: argparse.Namespace) -> None:
    """Start one harness child in its own session and return only its PID."""

    manifest = Path(args.manifest).resolve()
    _load_manifest(manifest)
    project_root = Path(__file__).resolve().parents[1]
    log_path = Path(args.log).resolve()
    expected_log = manifest.parent / f"{args.kind}.log"
    if log_path != expected_log:
        raise RuntimeError("offline harness process log is outside its owned state directory")
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise RuntimeError("offline harness process command is missing")
    executable = (project_root / command[0]).resolve() if not Path(command[0]).is_absolute() else Path(command[0]).resolve()
    expected_executable = (
        (project_root / ".venv/bin/python").resolve()
        if args.kind == "api"
        else (project_root / ".venv/bin/celery").resolve()
    )
    required_prefix = (
        [command[0], "-m", "uvicorn", "apps.api.main:app"]
        if args.kind == "api"
        else [command[0], "-A", "workers.celery_app.celery_app", "worker"]
    )
    if executable != expected_executable or command[: len(required_prefix)] != required_prefix:
        raise RuntimeError("offline harness process command is not an allowed API/worker command")
    log_path.touch(mode=0o600, exist_ok=True)
    os.chmod(log_path, 0o600)
    with log_path.open("ab", buffering=0) as log_handle:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=project_root,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    print(process.pid)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    command = subparsers.add_parser("prepare")
    command.add_argument("--manifest", required=True)
    command.add_argument("--run-id", required=True)
    command.add_argument("--database-name", required=True)
    command.add_argument("--owner-token", required=True)
    command.add_argument("--postgres-mode", choices=("owned", "external"), required=True)
    command.add_argument("--postgres-container")
    command.add_argument("--redis-container", required=True)
    command.set_defaults(handler=prepare)
    command = subparsers.add_parser("record-container")
    command.add_argument("--manifest", required=True)
    command.add_argument("--kind", choices=("postgres", "redis"), required=True)
    command.add_argument("--container-id", required=True)
    command.add_argument("--host-port", type=int, required=True)
    command.set_defaults(handler=record_container)
    command = subparsers.add_parser("init")
    command.add_argument("--manifest", required=True)
    command.add_argument("--maintenance-url", required=True)
    command.set_defaults(handler=init_database)
    command = subparsers.add_parser("record-process")
    command.add_argument("--manifest", required=True)
    command.add_argument("--kind", choices=("api", "worker"), required=True)
    command.add_argument("--pid", type=int, required=True)
    command.add_argument("--started", required=True)
    command.add_argument("--command-marker", required=True)
    command.set_defaults(handler=record_process)
    command = subparsers.add_parser("cleanup-db")
    command.add_argument("--manifest", required=True)
    command.set_defaults(handler=cleanup_database)
    command = subparsers.add_parser("value")
    command.add_argument("--manifest", required=True)
    command.add_argument("--field", required=True)
    command.set_defaults(handler=value)
    command = subparsers.add_parser("validate-frontend-origin")
    command.add_argument("--origin", required=True)
    command.set_defaults(handler=validate_frontend_origin)
    command = subparsers.add_parser("spawn-process")
    command.add_argument("--manifest", required=True)
    command.add_argument("--kind", choices=("api", "worker"), required=True)
    command.add_argument("--log", required=True)
    command.add_argument("command", nargs=argparse.REMAINDER)
    command.set_defaults(handler=spawn_process)
    return parser


def main() -> None:
    args = _parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
