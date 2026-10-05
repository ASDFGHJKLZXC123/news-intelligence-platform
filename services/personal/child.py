"""Fixed-argument child entry point; no queue, automatic retry, or shell execution."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sys
import uuid

from services.personal.deadlines import (
    RuntimeBudget,
    RuntimeCancelled,
    RuntimeWatchdog,
    bind_budget,
    install_budget,
    remaining_call_budget,
    require_call_budget,
)
from services.personal.supervisor import validate_personal_runtime_settings

BOOTSTRAP_CONNECT_SECONDS = 3
BOOTSTRAP_STATEMENT_MS = 5_000
HANDOFF_SECONDS = 120


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3:
        raise SystemExit("child requires run UUID, delivery UUID, and fencing generation")
    run_id, token = uuid.UUID(argv[0]), uuid.UUID(argv[1])
    generation = int(argv[2])
    if generation < 0 or os.getpgrp() != os.getpid():
        raise SystemExit("child requires a valid generation and its own process group")
    identity = uuid.UUID(os.environ["PERSONAL_CHILD_IDENTITY"])
    launcher_id = uuid.UUID(os.environ["PERSONAL_LAUNCHER_ID"])
    handoff_deadline = dt.datetime.fromisoformat(os.environ["PERSONAL_HANDOFF_DEADLINE"])
    if (
        handoff_deadline.tzinfo is None
        or (handoff_deadline - dt.datetime.now(dt.UTC)).total_seconds() > HANDOFF_SECONDS
    ):
        raise SystemExit("child requires the original bounded handoff deadline")

    current_budget = RuntimeBudget(handoff_deadline, handoff_deadline)
    install_budget(current_budget)
    watchdog = RuntimeWatchdog(current_budget, owned_group=True)
    engine = None
    event = None
    bootstrap = True

    def cancelled(_signum, _frame):
        current_budget.cancel("personal processing cancellation requested")
        raise RuntimeCancelled("personal processing cancellation requested")

    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    signal.signal(signal.SIGUSR1, cancelled)
    # This original, supervisor-read absolute deadline is armed before the first
    # connection. The durable row must agree before any child diagnostic or claim.
    watchdog.start()
    try:
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import NullPool

        from db.models import PersonalRun
        from packages.config.settings import get_settings
        from services.personal.runner import execute_personal_task

        settings = get_settings()
        validate_personal_runtime_settings(settings)

        def connection_bound():
            return (
                min(settings.database_connect_timeout_seconds, BOOTSTRAP_CONNECT_SECONDS)
                if bootstrap
                else settings.database_connect_timeout_seconds
            )

        def statement_bound():
            return (
                min(settings.database_statement_timeout_ms, BOOTSTRAP_STATEMENT_MS)
                if bootstrap
                else settings.database_statement_timeout_ms
            ) / 1000

        # No shared pool wait. Bootstrap bounds have a fixed conservative ceiling;
        # all acquisition and statement calls must also fit the original budget.
        engine = create_engine(
            settings.database_url,
            poolclass=NullPool,
            connect_args={
                "connect_timeout": connection_bound(),
                "options": f"-c statement_timeout={int(statement_bound() * 1000)}",
            },
        )
        session_factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)

        def before_connect(_dialect, _record, _args, kwargs):
            kwargs["connect_timeout"] = require_call_budget(connection_bound())
            bound = remaining_call_budget(statement_bound())
            kwargs["options"] = f"-c statement_timeout={max(1, int(bound * 1000))}"

        event.listen(engine, "do_connect", before_connect)

        # Dynamic bounds apply to processing and independently owned accounting
        # sessions; direct cursor SET LOCAL avoids event recursion.
        def before_statement(_conn, cursor, _statement, _params, _context, _many):
            bound = remaining_call_budget(statement_bound())
            milliseconds = max(1, int(bound * 1000))
            cursor.execute(f"SET LOCAL statement_timeout = {milliseconds}")
            cursor.execute(f"SET LOCAL lock_timeout = {milliseconds}")

        event.listen(engine, "before_cursor_execute", before_statement)

        def acquired(run):
            nonlocal watchdog, current_budget, bootstrap
            watchdog.close()
            current_budget = RuntimeBudget(run.graceful_deadline_at, run.hard_deadline_at)
            install_budget(current_budget)
            bootstrap = False
            watchdog = RuntimeWatchdog(current_budget, owned_group=True)
            watchdog.start()

        with session_factory() as session:
            run = session.get(PersonalRun, run_id)
            if (
                run is None
                or run.ownership_token != token
                or run.fencing_generation != generation
                or run.state != "queued"
                or run.lease_expires_at != handoff_deadline
            ):
                raise RuntimeError("child delivery is stale or unavailable")
            from services.personal.processing import process_birth_identity, record_launcher

            record_launcher(
                session,
                run_id,
                token,
                generation,
                launcher_id,
                child_pid=os.getpid(),
                child_identity=str(identity),
                child_started_at=process_birth_identity(os.getpid()),
            )
            session.commit()
        result = execute_personal_task(
            run_id,
            token,
            generation=generation,
            on_acquired=acquired,
            rotate_token=True,
            session_factory=session_factory,
            delivery_transport="subprocess",
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0 if result["status"] == "succeeded" else 1
    except RuntimeCancelled:
        return 130
    except Exception:
        # Database loss retains ownership for an explicit later recovery/retry.
        return 1
    finally:
        try:
            with bind_budget(None):
                if engine is not None:
                    if event is not None:
                        event.remove(engine, "before_cursor_execute", before_statement)
                        event.remove(engine, "do_connect", before_connect)
                    engine.dispose()
        finally:
            # Keep the independent hard stop armed throughout bounded cleanup.
            watchdog.close()


if __name__ == "__main__":
    raise SystemExit(main())
