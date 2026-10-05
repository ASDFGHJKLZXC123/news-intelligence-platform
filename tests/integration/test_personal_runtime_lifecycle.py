"""Real OS child.main, real durable claims, explicit scaled-deadline synthetic faults."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Article, Event, PersonalRun, PersonalWriterMode, Report
from services.personal.processing import (
    fail_owned_launch,
    process_birth_identity,
    reconcile_child_lifetimes,
    record_child_exit,
    record_launcher,
    recover_expired_owner,
)
from services.personal.runs import PersonalRunConflict, retry_run
from services.personal.supervisor import PersonalSupervisor
from services.personal.workspace import get_workspace
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_worker import FIXTURE_PATH, _queued_run

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
FAULT_MODULE = "tests.integration._personal_runtime_fault_child"


def _environment(engine, scenario):
    env = {
        **os.environ,
        "APP_ENV": "test",
        "PERSONAL_RUNTIME_FAULT_TEST": "owned.v1",
        "PERSONAL_RUNTIME_FAULT_SCENARIO": scenario,
        "DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "PERSONAL_PROCESSING_MODE": "personal",
        "PERSONAL_PROCESSING_TRANSPORT": "subprocess",
        "PERSONAL_OFFLINE_FIXTURE_PATH": str(FIXTURE_PATH),
        "PERSONAL_PAID_RUNTIME_ENABLED": "false",
        "DATABASE_CONNECT_TIMEOUT_SECONDS": "1",
        "DATABASE_STATEMENT_TIMEOUT_MS": "500",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in (
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "API_KEY",
    ):
        env[key] = ""
    return env


def _supervisor(engine, scenario, tmp_path):
    children = []

    def launched(run_id, token, generation, launcher_id, process, identity):
        with Session(engine) as session:
            record_launcher(
                session,
                run_id,
                token,
                generation,
                launcher_id,
                child_pid=process.pid if process else None,
                child_identity=str(identity),
                child_started_at=process_birth_identity(process.pid) if process else None,
            )
            session.commit()

    def exited(run_id, token, generation, launcher_id, result):
        with Session(engine) as session:
            if record_child_exit(session, run_id, token, generation, launcher_id):
                fail_owned_launch(
                    session,
                    run_id,
                    token,
                    generation,
                    code="child_exited",
                    allow_running=True,
                    launcher_id=launcher_id,
                )
            session.commit()

    def deadline(run_id, token, generation):
        with Session(engine) as session:
            control = session.scalar(select(PersonalWriterMode))
            return control.hard_deadline_at or control.lease_expires_at

    def popen(argv, **kwargs):
        assert argv[:3] == [sys.executable, "-m", "services.personal.child"]
        argv = [sys.executable, "-m", FAULT_MODULE, *argv[3:]]
        kwargs["env"] = {
            **kwargs["env"],
            **_environment(engine, scenario),
            "PERSONAL_CHILD_IDENTITY": kwargs["env"]["PERSONAL_CHILD_IDENTITY"],
            "PERSONAL_LAUNCHER_ID": kwargs["env"]["PERSONAL_LAUNCHER_ID"],
        }
        with (tmp_path / "child.log").open("w") as log:
            process = subprocess.Popen(
                argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, **kwargs
            )
        children.append(process)
        return process

    return PersonalSupervisor(
        record_launch=launched, record_exit=exited, read_deadline=deadline, popen=popen
    ), children


def _generation(engine, run_id):
    with Session(engine) as session:
        return session.get(PersonalRun, run_id).fencing_generation


def _running(engine, run_id, process, tmp_path):
    bound = time.monotonic() + 10
    while time.monotonic() < bound:
        if process.poll() is not None:
            pytest.fail(
                "child exited before durable running claim: "
                + next(
                    path
                    for path in (tmp_path / "child.log", tmp_path / "launcher.log")
                    if path.exists()
                ).read_text()
            )
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            probe = (run.stage_results or {}).get("lifecycle_probe")
            if run.state == "running" and probe:
                assert (run.graceful_deadline_at - run.started_at).total_seconds() == 2.5
                assert (run.hard_deadline_at - run.started_at).total_seconds() == 3
                assert (run.lease_expires_at - run.started_at).total_seconds() == 3.5
                return {
                    "run_id": str(run.id),
                    "attempt": run.attempt,
                    "generation": run.fencing_generation,
                    "started_at": run.started_at.isoformat(),
                    "graceful_deadline_at": run.graceful_deadline_at.isoformat(),
                    "hard_deadline_at": run.hard_deadline_at.isoformat(),
                    "lease_expires_at": run.lease_expires_at.isoformat(),
                    "child_pid": probe["pid"],
                    "descendant_pid": probe.get("descendant_pid"),
                }
        time.sleep(0.02)
    pytest.fail("child never persisted its real running claim")


def _dead(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=2,
        shell=False,
    )
    return not result.stdout.strip() or "Z" in result.stdout


def _wait_dead(pid, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _dead(pid):
            return
        time.sleep(0.025)
    pytest.fail(f"owned synthetic process {pid} survived its original hard deadline")


def _assert_empty_business(engine):
    with Session(engine) as session:
        assert all(
            session.scalar(select(func.count()).select_from(model)) == 0
            for model in (Article, Event, Report)
        )


def test_real_child_normal_shutdown_is_bounded_and_durable(tmp_path, record_testsuite_property):
    with migrated_disposable_engine() as engine:
        run_id, token, _ = _queued_run(engine)
        supervisor, children = _supervisor(engine, "graceful", tmp_path)
        try:
            supervisor.launch(run_id, token, _generation(engine, run_id))
            proof = _running(engine, run_id, children[0], tmp_path)
            began = time.monotonic()
            supervisor.shutdown(timeout=2)
            children[0].wait(timeout=2)
            proof["shutdown_seconds"] = time.monotonic() - began
            assert proof["shutdown_seconds"] < 2.5
            assert children[0].returncode == 130
            with Session(engine) as session:
                run = session.get(PersonalRun, run_id)
                assert run.state == "failed" and run.attempt == 1
                assert run.stage_results["workflow"]["code"] == "runtime_cancelled"
                assert session.scalar(select(PersonalWriterMode.active_run_id)) is None
            _assert_empty_business(engine)
            record_testsuite_property("actual_child_shutdown", json.dumps(proof, sort_keys=True))
        finally:
            supervisor.shutdown(timeout=0.5)
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)


def test_real_child_ignores_graceful_then_watchdog_kills_owned_group(
    tmp_path, record_testsuite_property
):
    with migrated_disposable_engine() as engine:
        run_id, token, _ = _queued_run(engine)
        supervisor, children = _supervisor(engine, "ignore_graceful", tmp_path)
        try:
            supervisor.launch(run_id, token, _generation(engine, run_id))
            proof = _running(engine, run_id, children[0], tmp_path)
            assert proof["descendant_pid"]
            result = supervisor.wait(run_id)
            assert result == -signal.SIGKILL
            proof["hard_exit_utc"] = dt.datetime.now(dt.UTC).isoformat()
            lateness = (
                dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(proof["hard_deadline_at"])
            ).total_seconds()
            assert 0 <= lateness < 1
            _wait_dead(proof["descendant_pid"], timeout=2)
            supervisor.shutdown(timeout=2)
            with Session(engine) as session:
                run = session.get(PersonalRun, run_id)
                assert run.state == "failed" and run.attempt == 1
                assert run.error["code"] == "child_exited"
            _assert_empty_business(engine)
            record_testsuite_property(
                "actual_child_hard_deadline", json.dumps(proof, sort_keys=True)
            )
        finally:
            supervisor.shutdown(timeout=0.5)
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)


def test_real_launcher_loss_keeps_original_child_deadlines_and_requires_recovery(
    tmp_path, record_testsuite_property
):
    with migrated_disposable_engine() as engine:
        run_id, token, _ = _queued_run(engine)
        generation = _generation(engine, run_id)
        env = _environment(engine, "ignore_graceful")
        with (tmp_path / "launcher.log").open("w") as log:
            launcher = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    FAULT_MODULE,
                    "--launcher",
                    str(run_id),
                    str(token),
                    str(generation),
                ],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                shell=False,
            )
        child_pid = None
        birth = None
        try:
            proof = _running(
                engine, run_id, launcher, tmp_path
            )  # Probe belongs to real child, launcher remains alive.
            child_pid = proof["child_pid"]
            birth = process_birth_identity(child_pid)
            assert child_pid != launcher.pid and os.getpgid(child_pid) == child_pid
            launcher.kill()
            launcher.wait(timeout=2)
            assert not _dead(child_pid), (
                "child must survive launcher loss until its original deadline"
            )
            _wait_dead(child_pid)
            _wait_dead(proof["descendant_pid"], timeout=2)
            proof["launcher_pid"] = launcher.pid
            proof["child_exit_utc"] = dt.datetime.now(dt.UTC).isoformat()
            lateness = (
                dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(proof["hard_deadline_at"])
            ).total_seconds()
            assert 0 <= lateness < 1
            with Session(engine) as session:
                run = session.get(PersonalRun, run_id)
                assert run.state == "running" and run.attempt == 1
                assert run.lease_expires_at.isoformat() == proof["lease_expires_at"]
                workspace, _ = get_workspace(session)
                with pytest.raises(PersonalRunConflict):
                    retry_run(session, workspace, run_id, now=dt.datetime.now(dt.UTC))
                session.rollback()
            lease = dt.datetime.fromisoformat(proof["lease_expires_at"])
            while dt.datetime.now(dt.UTC) <= lease:
                time.sleep(0.025)
            with Session(engine) as session:
                assert reconcile_child_lifetimes(session) >= 1
                run = recover_expired_owner(session, run_id, confirm_dead_children=True)
                assert run.state == "failed" and run.attempt == 1
                workspace, _ = get_workspace(session)
                recovered = retry_run(session, workspace, run_id, now=dt.datetime.now(dt.UTC))
                assert recovered.should_enqueue and recovered.run.attempt == 2
                assert (
                    recovered.run.fencing_generation > generation
                    and recovered.run.ownership_token != token
                )
                session.commit()
            _assert_empty_business(engine)
            record_testsuite_property("actual_launcher_loss", json.dumps(proof, sort_keys=True))
        finally:
            if launcher.poll() is None:
                launcher.kill()
            launcher.wait(timeout=2)
            if child_pid is not None and not _dead(child_pid):
                assert (
                    process_birth_identity(child_pid) == birth
                    and os.getpgid(child_pid) == child_pid
                )
                os.killpg(child_pid, signal.SIGKILL)
                _wait_dead(child_pid)


def test_real_old_child_after_handoff_recovery_cannot_mutate_replacement(
    tmp_path, record_testsuite_property
):
    with migrated_disposable_engine() as engine:
        run_id, old_token, _ = _queued_run(engine)
        old_generation = _generation(engine, run_id)
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert (run.lease_expires_at - run.queued_at).total_seconds() == 120
            old_handoff_deadline = run.lease_expires_at
            recover_expired_owner(
                session, run_id, now=run.lease_expires_at + dt.timedelta(seconds=1)
            )
            workspace, _ = get_workspace(session)
            replacement = retry_run(session, workspace, run_id, now=dt.datetime.now(dt.UTC))
            new_token = replacement.run.ownership_token
            new_generation = replacement.run.fencing_generation
            session.commit()
        env = _environment(engine, "graceful")
        env["PERSONAL_CHILD_IDENTITY"], env["PERSONAL_LAUNCHER_ID"] = (
            str(uuid.uuid4()),
            str(uuid.uuid4()),
        )
        env["PERSONAL_HANDOFF_DEADLINE"] = old_handoff_deadline.isoformat()
        stale_probe_path = tmp_path / "stale-delivery.json"
        env["PERSONAL_RUNTIME_FAULT_STALE_PROBE"] = str(stale_probe_path)
        old = subprocess.Popen(
            [sys.executable, "-m", FAULT_MODULE, str(run_id), str(old_token), str(old_generation)],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            shell=False,
        )
        try:
            assert old.wait(timeout=10) == 1
        finally:
            if old.poll() is None:
                assert os.getpgid(old.pid) == old.pid
                os.killpg(old.pid, signal.SIGKILL)
            old.wait(timeout=2)
        stale_probe = json.loads(stale_probe_path.read_text())
        assert stale_probe == {
            "queried_run_id": str(run_id),
            "observed_generation": new_generation,
            "observed_state": "queued",
            "stale_delivery": True,
        }
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert (
                run.state == "queued"
                and run.ownership_token == new_token
                and run.fencing_generation == new_generation
            )
            assert run.attempt == 2 and not run.stage_results.get("lifecycle_probe")
        supervisor, children = _supervisor(engine, "graceful", tmp_path)
        try:
            supervisor.launch(run_id, new_token, new_generation)
            proof = _running(engine, run_id, children[0], tmp_path)
            supervisor.shutdown(timeout=2)
            assert children[0].wait(timeout=2) == 130
            _assert_empty_business(engine)
            record_testsuite_property("actual_stale_handoff", json.dumps(proof, sort_keys=True))
        finally:
            supervisor.shutdown(timeout=0.5)
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)


def test_actual_cli_ctrl_c_stops_active_child_and_preserves_failed_attempt(
    tmp_path, record_testsuite_property
):
    with migrated_disposable_engine() as engine:
        run_id, token, _ = _queued_run(engine)
        with Session(engine) as session:
            fail_owned_launch(
                session, run_id, token, _generation(engine, run_id), code="synthetic_setup"
            )
            session.commit()
        with (tmp_path / "launcher.log").open("w") as log:
            launcher = subprocess.Popen(
                [sys.executable, "-m", FAULT_MODULE, "--cli", "retry", str(run_id)],
                cwd=ROOT,
                env=_environment(engine, "graceful"),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                shell=False,
            )
        child_pid = birth = None
        try:
            proof = _running(engine, run_id, launcher, tmp_path)
            child_pid = proof["child_pid"]
            birth = process_birth_identity(child_pid)
            began = time.monotonic()
            launcher.send_signal(signal.SIGINT)
            assert launcher.wait(timeout=5) == 130
            proof["ctrl_c_shutdown_seconds"] = time.monotonic() - began
            assert proof["ctrl_c_shutdown_seconds"] < 3
            _wait_dead(child_pid, timeout=2)
            with Session(engine) as session:
                run = session.get(PersonalRun, run_id)
                assert run.state == "failed" and run.attempt == 2
                assert run.stage_results["workflow"]["code"] == "runtime_cancelled"
                assert session.scalar(select(PersonalWriterMode.active_run_id)) is None
            _assert_empty_business(engine)
            record_testsuite_property("actual_cli_ctrl_c", json.dumps(proof, sort_keys=True))
        finally:
            if launcher.poll() is None:
                launcher.kill()
            launcher.wait(timeout=2)
            if child_pid is not None and not _dead(child_pid):
                assert (
                    process_birth_identity(child_pid) == birth
                    and os.getpgid(child_pid) == child_pid
                )
                os.killpg(child_pid, signal.SIGKILL)
                _wait_dead(child_pid)


def test_actual_child_bootstrap_obeys_original_handoff_despite_large_db_timeouts(
    tmp_path, record_testsuite_property
):
    with migrated_disposable_engine() as engine:
        run_id, token, _ = _queued_run(engine)
        deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            run.lease_expires_at = deadline
            control = session.scalar(select(PersonalWriterMode))
            control.lease_expires_at = deadline
            generation = run.fencing_generation
            session.commit()
        env = _environment(engine, "bootstrap_block")
        env.update(
            {
                "PERSONAL_CHILD_IDENTITY": str(uuid.uuid4()),
                "PERSONAL_LAUNCHER_ID": str(uuid.uuid4()),
                "PERSONAL_HANDOFF_DEADLINE": deadline.isoformat(),
                "DATABASE_CONNECT_TIMEOUT_SECONDS": "100",
                "DATABASE_STATEMENT_TIMEOUT_MS": "999999",
            }
        )
        with (tmp_path / "child.log").open("w") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", FAULT_MODULE, str(run_id), str(token), str(generation)],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                shell=False,
            )
        try:
            child.wait(timeout=7)
            assert child.returncode == -signal.SIGKILL
            ended = dt.datetime.now(dt.UTC)
            assert 0 <= (ended - deadline).total_seconds() < 1
            with Session(engine) as session:
                run = session.get(PersonalRun, run_id)
                assert run.state == "queued" and run.attempt == 1 and run.started_at is None
                assert run.lease_expires_at == deadline
            _assert_empty_business(engine)
            record_testsuite_property(
                "actual_child_bootstrap",
                json.dumps(
                    {
                        "run_id": str(run_id),
                        "child_pid": child.pid,
                        "generation": generation,
                        "original_handoff_deadline": deadline.isoformat(),
                        "exit_at": ended.isoformat(),
                        "configured_connect_seconds": 100,
                        "configured_statement_ms": 999999,
                        "no_running_claim": True,
                        "calls_started": 0,
                    },
                    sort_keys=True,
                ),
            )
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=2)
