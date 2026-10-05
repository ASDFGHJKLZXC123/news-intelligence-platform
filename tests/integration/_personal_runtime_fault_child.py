"""Opt-in test-only adapter invoking the actual production child.main lifecycle.

No production setting imports this module. Only uniquely owned loopback disposable
PostgreSQL databases, blank paid credentials, and an explicit synthetic marker pass.
The 25/30/35 minute ratio is scaled to 2.5/3/3.5 seconds solely in this process.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
import uuid

from sqlalchemy.engine import make_url


def _validate():
    if (
        os.environ.get("APP_ENV") != "test"
        or os.environ.get("PERSONAL_RUNTIME_FAULT_TEST") != "owned.v1"
    ):
        raise RuntimeError("synthetic runtime fault adapter requires an explicit test process")
    url = make_url(os.environ["DATABASE_URL"])
    if (
        url.host not in {"127.0.0.1", "localhost", "::1"}
        or url.port in {None, 5432}
        or not (url.database or "").startswith("nip_stage7_")
    ):
        raise RuntimeError("synthetic fault adapter requires a uniquely owned disposable database")
    if any(
        os.environ.get(key)
        for key in (
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "API_KEY",
        )
    ):
        raise RuntimeError("synthetic runtime faults cannot use provider credentials")
    if os.environ.get("PERSONAL_PAID_RUNTIME_ENABLED") != "false":
        raise RuntimeError("ordinary paid activation must remain disabled")
    if os.environ.get("PERSONAL_RUNTIME_FAULT_SCENARIO") not in {
        "graceful",
        "ignore_graceful",
        "bootstrap_block",
    }:
        raise RuntimeError("unknown synthetic lifecycle scenario")


def main():
    _validate()
    from services.personal import runs

    runs.GRACEFUL_DEADLINE = dt.timedelta(seconds=2.5)
    runs.HARD_DEADLINE = dt.timedelta(seconds=3)
    runs.RUNNING_LEASE = dt.timedelta(seconds=3.5)
    if sys.argv[1] == "--cli":
        import services.personal.cli as cli
        from services.personal.supervisor import PersonalSupervisor

        def test_popen(argv, **kwargs):
            assert argv[:3] == [sys.executable, "-m", "services.personal.child"]
            return subprocess.Popen([sys.executable, "-m", __spec__.name, *argv[3:]], **kwargs)

        signal.signal(signal.SIGINT, signal.default_int_handler)
        cli.PersonalSupervisor = lambda: PersonalSupervisor(popen=test_popen)
        return cli.main(sys.argv[2:])
    if sys.argv[1] == "--launcher":
        from services.personal.supervisor import PersonalSupervisor

        def test_popen(argv, **kwargs):
            assert argv[:3] == [sys.executable, "-m", "services.personal.child"]
            return subprocess.Popen([sys.executable, "-m", __spec__.name, *argv[3:]], **kwargs)

        supervisor = PersonalSupervisor(popen=test_popen)
        run_id, token, generation = uuid.UUID(sys.argv[2]), uuid.UUID(sys.argv[3]), int(sys.argv[4])
        identity = supervisor.launch(run_id, token, generation)
        print(json.dumps({"launcher_pid": os.getpid(), "child_identity": identity}), flush=True)
        while True:
            time.sleep(1)
    if os.environ.get("PERSONAL_RUNTIME_FAULT_STALE_PROBE"):
        from pathlib import Path

        from sqlalchemy.orm import Session

        from db.models import PersonalRun

        original_get = Session.get

        def observed_initial_get(self, model, *args, **kwargs):
            value = original_get(self, model, *args, **kwargs)
            if model is PersonalRun and value is not None:
                Path(os.environ["PERSONAL_RUNTIME_FAULT_STALE_PROBE"]).write_text(
                    json.dumps(
                        {
                            "queried_run_id": str(value.id),
                            "observed_generation": value.fencing_generation,
                            "observed_state": value.state,
                            "stale_delivery": value.fencing_generation != int(sys.argv[3])
                            or value.ownership_token != uuid.UUID(sys.argv[2]),
                        },
                        sort_keys=True,
                    )
                )
            return value

        Session.get = observed_initial_get
    if os.environ["PERSONAL_RUNTIME_FAULT_SCENARIO"] == "bootstrap_block":
        from sqlalchemy.orm import Session

        from db.models import PersonalRun

        original_get = Session.get

        def blocked_initial_get(self, model, *args, **kwargs):
            if model is PersonalRun:
                signal.signal(signal.SIGUSR1, signal.SIG_IGN)
                self.connection().exec_driver_sql("SELECT pg_sleep(60)")
            return original_get(self, model, *args, **kwargs)

        Session.get = blocked_initial_get
    from services.personal import coordinator

    def hang_after_claim(run_id, token, *, session_factory, **_kwargs):
        from services.personal.processing import process_birth_identity
        from services.personal.runs import lock_owned_run

        descendant = None
        if os.environ["PERSONAL_RUNTIME_FAULT_SCENARIO"] == "ignore_graceful":
            signal.signal(signal.SIGUSR1, signal.SIG_IGN)
            descendant = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)",
                ],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        with session_factory() as session:
            run = lock_owned_run(session, run_id, token)
            run.stage_results = {
                **(run.stage_results or {}),
                "lifecycle_probe": {
                    "pid": os.getpid(),
                    "descendant_pid": descendant.pid if descendant else None,
                    "descendant_birth": process_birth_identity(descendant.pid)
                    if descendant
                    else None,
                    "synthetic": True,
                    "calls_started": 0,
                },
            }
            session.commit()
        while True:
            try:
                time.sleep(1)
            except Exception:
                # A production fatal cancellation must bypass item-level recovery.
                continue

    coordinator._capture_selected_feeds = hang_after_claim
    from services.personal.child import main as actual_child_main

    return actual_child_main()


if __name__ == "__main__":
    raise SystemExit(main())
