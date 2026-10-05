"""Documented manual workflow through the same supervisor as the browser API.

Usage: python -m services.personal.cli start
       python -m services.personal.cli retry RUN_UUID
       python -m services.personal.cli status
       python -m services.personal.cli mode personal|legacy
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import uuid

from services.personal.supervisor import PersonalSupervisor, validate_personal_runtime_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise one explicit personal news-desk update")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start")
    retry = commands.add_parser("retry")
    retry.add_argument("run_id", type=uuid.UUID)
    commands.add_parser("status")
    commands.add_parser("reconcile-children")
    recover = commands.add_parser("recover")
    recover.add_argument("run_id", type=uuid.UUID)
    mode = commands.add_parser("mode")
    mode.add_argument("mode", choices=("legacy", "personal"))
    args = parser.parse_args(argv)
    from db.base import SessionLocal
    from packages.config.settings import get_settings
    from services.personal.processing import (
        diagnostics,
        reconcile_child_lifetimes,
        recover_expired_owner,
        switch_processing_mode,
    )
    from services.personal.runs import create_daily_run, retry_run
    from services.personal.workspace import ensure_workspace

    settings = get_settings()
    if args.command == "status":
        with SessionLocal() as session:
            print(json.dumps(diagnostics(session), default=str, sort_keys=True), flush=True)
        return 0
    if args.command == "reconcile-children":
        with SessionLocal() as session:
            confirmed = reconcile_child_lifetimes(session)
            session.commit()
            print(json.dumps({"confirmed_child_exits": confirmed}, sort_keys=True), flush=True)
        return 0
    if args.command == "recover":
        with SessionLocal() as session:
            run = recover_expired_owner(session, args.run_id, confirm_dead_children=True)
            session.commit()
            print(
                json.dumps(
                    {"run_id": str(run.id), "state": run.state, "recovered": True}, sort_keys=True
                ),
                flush=True,
            )
        return 0
    if args.command == "mode":
        with SessionLocal() as session:
            switch_processing_mode(session, args.mode)
            session.commit()
            print(json.dumps(diagnostics(session), default=str, sort_keys=True), flush=True)
        return 0
    validate_personal_runtime_settings(settings)
    supervisor = PersonalSupervisor()
    try:
        with SessionLocal() as session:
            workspace, _ = ensure_workspace(session)
            now = dt.datetime.now(dt.UTC)
            decision = (
                create_daily_run(session, workspace, now=now)
                if args.command == "start"
                else retry_run(session, workspace, args.run_id, now=now)
            )
            run_id, token, generation = (
                decision.run.id,
                decision.run.ownership_token,
                decision.run.fencing_generation,
            )
            session.commit()
        print(
            json.dumps(
                {
                    "run_id": str(run_id),
                    "queued": decision.should_enqueue,
                    "already_processed": decision.already_processed,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if not decision.should_enqueue:
            return 0
        try:
            supervisor.launch(run_id, token, generation)
        except Exception:
            from services.personal.processing import fail_owned_launch

            with SessionLocal() as session:
                fail_owned_launch(
                    session,
                    run_id,
                    token,
                    generation,
                    code="dispatch_failed",
                    message="Personal child could not start; use explicit retry.",
                )
                session.commit()
            raise
        result = supervisor.wait(run_id)
        return 0 if result in {None, 0} else 1
    except KeyboardInterrupt:
        supervisor.shutdown()
        return 130
    finally:
        supervisor.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
