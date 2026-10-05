#!/usr/bin/env python3
"""Foreground two-service personal configuration; never migrates or starts work implicitly.

Usage: python scripts/personal-local.py app --port 8000
       python scripts/personal-local.py update
       python scripts/personal-local.py mode personal
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PORT = 55443


def personal_environment(inherited: Mapping[str, str], *, app_port: int = 8000) -> dict[str, str]:
    """Preserve explicit datastore/auth settings while closing paid/public execution."""
    environment = dict(inherited)
    if not environment.get("DATABASE_URL"):
        try:
            database_port = int(environment.get("PERSONAL_POSTGRES_PORT") or DEFAULT_DATABASE_PORT)
        except ValueError as exc:
            raise ValueError("PERSONAL_POSTGRES_PORT must be between 1 and 65535") from exc
        if not 1 <= database_port <= 65535:
            raise ValueError("PERSONAL_POSTGRES_PORT must be between 1 and 65535")
        username = quote((environment.get("PERSONAL_POSTGRES_USER") or "news"), safe="")
        password = quote(
            (environment.get("PERSONAL_POSTGRES_PASSWORD") or "personal_local_only"), safe=""
        )
        database = quote((environment.get("PERSONAL_POSTGRES_DB") or "news_personal"), safe="")
        environment["DATABASE_URL"] = (
            f"postgresql+psycopg2://{username}:{password}@127.0.0.1:{database_port}/{database}"
        )
    environment.update(
        PERSONAL_PROCESSING_TRANSPORT="subprocess",
        PERSONAL_PROCESSING_MODE="personal",
        PERSONAL_BIND_HOST="127.0.0.1",
        PERSONAL_APP_WORKERS="1",
        PERSONAL_PAID_RUNTIME_ENABLED="false",
        CORS_ALLOW_ORIGINS=f"http://127.0.0.1:{app_port}",
    )
    # An offline fixture is opt-in in this shell; stale .env test configuration is not used.
    environment["PERSONAL_OFFLINE_FIXTURE_PATH"] = inherited.get(
        "PERSONAL_OFFLINE_FIXTURE_PATH", ""
    )
    return environment


def command_arguments(argv: list[str] | None = None) -> tuple[list[str], int]:
    parser = argparse.ArgumentParser(
        description="Run the personal app or an explicit workflow command, with paid runtime disabled"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    app = commands.add_parser(
        "app", help="Start one foreground loopback application; Ctrl-C stops it"
    )
    app.add_argument("--port", type=int, default=8000)
    commands.add_parser("update", help="Explicitly supervise the current date's update")
    commands.add_parser("status")
    commands.add_parser("reconcile-children")
    for name in ("retry", "recover"):
        command = commands.add_parser(name)
        command.add_argument("run_id", type=uuid.UUID)
    mode = commands.add_parser("mode", help="Switch durable mode only while genuinely idle")
    mode.add_argument("mode", choices=("personal", "legacy"))
    args = parser.parse_args(argv)
    if args.command == "app":
        if not 1 <= args.port <= 65535:
            parser.error("port must be between 1 and 65535")
        return ["-m", "services.personal.app", "--port", str(args.port)], args.port
    arguments = [
        "-m",
        "services.personal.cli",
        "start" if args.command == "update" else args.command,
    ]
    if args.command in {"retry", "recover"}:
        arguments.append(str(args.run_id))
    elif args.command == "mode":
        arguments.append(args.mode)
    return arguments, 8000


def main(argv: list[str] | None = None) -> None:
    arguments, app_port = command_arguments(argv)
    environment = personal_environment(os.environ, app_port=app_port)
    os.chdir(REPO_ROOT)
    # Replacing this process preserves signals and leaves exactly one app supervisor.
    os.execve(sys.executable, [sys.executable, *arguments], environment)


if __name__ == "__main__":
    main()
