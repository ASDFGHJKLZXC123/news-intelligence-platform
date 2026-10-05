#!/usr/bin/env python3
"""Prepare private Phase 5 records or inspect an explicitly selected local database."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.personal.trial_records import (  # noqa: E402
    application_identity,
    bind_runtime,
    closed_sessions,
    initialize_trial,
    read_json,
    record_session,
    sample_history,
    write_new_json,
)


def capture(destination: Path, run_id: uuid.UUID) -> Path:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    # Never inherit a database implicitly from .env; never print its credentials.
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("export the intended loopback DATABASE_URL explicitly")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("capture requires an explicitly selected loopback PostgreSQL database")
    trial = read_json(destination / "trial.json")
    from services.personal.trial_capture import capture_session_evidence

    engine = create_engine(database_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout = '30s'"))
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                with Session(bind=connection, autoflush=False) as session:
                    with session.begin():
                        evidence = capture_session_evidence(session, run_id)
        session_id = str(uuid.uuid4())
        document = {
            **evidence,
            "schema": "personal-session.v1",
            "trial_id": trial["trial_id"],
            "session_id": session_id,
            "session_status": "draft",
            "actual_reading_session": False,
            "workflow_completed": None,
            "developer_intervention": None,
            "captured_at": dt.datetime.now(dt.UTC).isoformat(),
            "exporter_source_identity": application_identity(REPO_ROOT),
            "application_revision": None,
            "runtime_revision_basis": None,
            "observed_migration_head": revision,
        }
        path = destination / "captures" / f"{session_id}.json"
        write_new_json(path, document)
        return path
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a new raw-reading trial and blank logs")
    init.add_argument("directory", type=Path)
    collect = commands.add_parser(
        "capture", help="read one run in a consistent read-only transaction"
    )
    collect.add_argument("directory", type=Path)
    collect.add_argument("--run-id", type=uuid.UUID, required=True)
    bind = commands.add_parser(
        "bind-runtime", help="retain actual pretrial launch/profile observations"
    )
    bind.add_argument("directory", type=Path)
    bind.add_argument("--record", type=Path, required=True)
    record = commands.add_parser("record-session", help="attach explicit user reading observations")
    record.add_argument("directory", type=Path)
    record.add_argument("--capture", type=Path, required=True)
    record.add_argument("--observations", type=Path, required=True)
    for name in ("sample", "reliability"):
        command = commands.add_parser(name)
        command.add_argument("directory", type=Path)
        if name == "sample":
            command.add_argument("--previous", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            trial = initialize_trial(args.directory, REPO_ROOT)
            print(f"Prepared raw trial {trial['trial_id']} at {args.directory.resolve()}")
        elif args.command == "capture":
            try:
                path = capture(args.directory, args.run_id)
            except Exception:
                print(
                    "Capture failed. Check the explicit local database and run identity; no processing was started.",
                    file=sys.stderr,
                )
                return 1
            print(path.resolve())
        elif args.command == "bind-runtime":
            bind_runtime(args.directory, read_json(args.record))
            print("Retained pretrial runtime binding; no runtime configuration was changed.")
        elif args.command == "record-session":
            result = record_session(
                args.directory, read_json(args.capture), read_json(args.observations)
            )
            print(f"Recorded session {result['session_id']}")
        else:
            from services.personal.trial_sampling import assess_reliability, choose_samples

            trial = read_json(args.directory / "trial.json")
            sessions = closed_sessions(args.directory)
            if args.command == "sample":
                existing = sample_history(args.directory)
                latest = existing[-1] if existing else None
                if latest is not None and (
                    args.previous is None or args.previous.resolve() != latest.resolve()
                ):
                    raise ValueError(
                        "supply --previous with the latest manifest to preserve frozen samples"
                    )
                if latest is None and args.previous is not None:
                    raise ValueError("previous manifest must belong to this trial's sample history")
                previous = read_json(args.previous) if args.previous else None
                result = choose_samples(trial, sessions, previous=previous)
                result["history_sequence"] = len(existing) + 1
                result["previous_manifest_sha256"] = (
                    hashlib.sha256(latest.read_bytes()).hexdigest() if latest else None
                )
                folder = "samples"
            else:
                result = assess_reliability(sessions)
                folder = "analyses"
            path = args.directory / folder / f"{uuid.uuid4()}.json"
            write_new_json(path, result)
            print(path.resolve())
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"Trial record refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
