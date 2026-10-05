"""Transactional processing mode, global ownership and diagnostics.

Lock order is control -> run -> spending -> business rows. Only explicit claim,
recovery and mode operations take FOR UPDATE. Business writes use NO KEY UPDATE;
accounting handoffs use KEY SHARE so an independent durable ledger can commit while
an adapter still owns a business transaction. Every takeover takes FOR UPDATE.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Job, PersonalRun, PersonalWriterMode

QUEUE_LEASE = dt.timedelta(minutes=2)
GRACEFUL_DEADLINE = dt.timedelta(minutes=25)
HARD_DEADLINE = dt.timedelta(minutes=30)
RUNNING_LEASE = dt.timedelta(minutes=35)


class PersonalRunConflict(RuntimeError):
    pass


class PersonalOwnershipLost(RuntimeError):
    pass


class WriterModeConflict(RuntimeError):
    pass


class ProcessingBusy(PersonalRunConflict):
    def __init__(self, active_run_id: uuid.UUID | None, reason: str = "processing_busy") -> None:
        self.active_run_id, self.code = active_run_id, "processing_busy"
        super().__init__(
            f"{reason}; active run {active_run_id or 'requires explicit reconciliation'}"
        )


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("processing timestamps must include a timezone")
    return value.astimezone(dt.UTC)


def lock_processing_control(session: Session, *, lock: str = "exclusive") -> PersonalWriterMode:
    statement = select(PersonalWriterMode).where(PersonalWriterMode.singleton.is_(True))
    if lock == "exclusive":
        statement = statement.with_for_update()
    elif lock == "business":
        statement = statement.with_for_update(key_share=True)  # FOR NO KEY UPDATE
    elif lock == "dispatch":
        statement = statement.with_for_update(read=True, key_share=True)  # FOR KEY SHARE
    elif lock != "read":
        raise ValueError("unsupported processing lock")
    control = session.scalar(statement.execution_options(populate_existing=True))
    if control is None:
        raise WriterModeConflict("processing mode is not initialized")
    return control


def assert_mode_agreement(session: Session, expected_mode: str) -> PersonalWriterMode:
    if expected_mode not in {"legacy", "personal"}:
        raise ValueError("processing mode must be legacy or personal")
    control = lock_processing_control(session, lock="read")
    if control.mode != expected_mode:
        raise WriterModeConflict(
            f"deployment processing mode {expected_mode} does not agree with durable mode {control.mode}"
        )
    return control


def require_mode(control: PersonalWriterMode, expected_mode: str) -> None:
    if control.mode != expected_mode:
        other = "legacy" if expected_mode == "personal" else "personal"
        raise WriterModeConflict(
            f"{expected_mode} processing is unavailable while the {other} writer mode is active"
        )


def require_idle(session: Session, control: PersonalWriterMode, *, now: dt.datetime) -> None:
    # Even an expired record needs explicit run recovery: starts never silently rerun it.
    _sync_child_uncertainty(control)
    if control.active_run_id is not None or control.unconfirmed_child:
        raise ProcessingBusy(control.active_run_id)
    active = session.scalar(select(Job.id).where(Job.state.in_(("queued", "running"))).limit(1))
    if active is not None:
        raise ProcessingBusy(None, "durable queued/running writer requires explicit recovery")


def switch_processing_mode(
    session: Session, mode: str, *, now: dt.datetime | None = None
) -> PersonalWriterMode:
    if mode not in {"personal", "legacy"}:
        raise ValueError("processing mode must be legacy or personal")
    control = lock_processing_control(session)
    require_idle(session, control, now=_utc(now or utc_now()))
    if control.mode != mode:
        control.mode = mode
        control.generation += 1
        control.delivery_token = None
        control.ownership_token = None
        control.launcher_id = None
        control.child_pid = None
        control.child_identity = None
        session.flush()
    return control


def queue_control(
    session: Session, control: PersonalWriterMode, run: PersonalRun, *, now: dt.datetime
) -> None:
    # Caller locks control before the run and has checked existing/global owners.
    if control.active_run_id not in {None, run.id}:
        raise ProcessingBusy(control.active_run_id)
    control.generation += 1
    run.fencing_generation = control.generation
    run.queued_at = _utc(now)
    run.started_at = run.graceful_deadline_at = run.hard_deadline_at = None
    control.workspace_id = run.workspace_id
    control.active_run_id, control.active_attempt = run.id, run.attempt
    control.delivery_token, control.ownership_token = run.ownership_token, None
    control.queued_at = run.queued_at
    control.started_at = control.graceful_deadline_at = control.hard_deadline_at = None
    control.lease_expires_at = run.lease_expires_at
    control.launcher_id = control.child_pid = control.child_identity = None
    _sync_child_uncertainty(control)


def verify_control(
    control: PersonalWriterMode,
    run: PersonalRun,
    token: uuid.UUID,
    *,
    now: dt.datetime,
    allow_queued: bool = False,
    generation: int | None = None,
) -> None:
    require_mode(control, "personal")
    expected_token = control.delivery_token if run.state == "queued" else control.ownership_token
    if (
        control.active_run_id != run.id
        or control.active_attempt != run.attempt
        or control.generation != run.fencing_generation
        or (generation is not None and control.generation != generation)
        or run.ownership_token != token
        or expected_token != token
        or run.state not in ({"queued", "running"} if allow_queued else {"running"})
    ):
        raise PersonalOwnershipLost("personal write refused because attempt ownership changed")
    stamp = _utc(now)
    if (
        control.lease_expires_at is None
        or run.lease_expires_at is None
        or control.lease_expires_at <= stamp
        or run.lease_expires_at <= stamp
    ):
        raise PersonalOwnershipLost("personal ownership lease expired")


def release_control(control: PersonalWriterMode) -> None:
    control.active_run_id = None
    control.active_attempt = None
    control.lease_expires_at = None
    # Preserve launch identity and delivery token until exact-owned exit is confirmed.
    # A PID alone cannot authorize release or switching.


def record_launcher(
    session: Session,
    run_id: uuid.UUID,
    token: uuid.UUID,
    generation: int,
    launcher_id: uuid.UUID,
    *,
    child_pid: int | None = None,
    child_identity: str | None = None,
    child_started_at: str | float | None = None,
) -> None:
    control = lock_processing_control(session)
    run = session.get(PersonalRun, run_id, with_for_update=True, populate_existing=True)
    if (
        run is None
        or control.generation != generation
        or run.fencing_generation != generation
        or control.delivery_token != token
    ):
        raise PersonalOwnershipLost("launcher attempt ownership changed")
    if control.launcher_id not in {None, launcher_id}:
        raise PersonalOwnershipLost("launcher identity changed")
    if run.state not in {"queued", "running"} and control.launcher_id is None:
        raise PersonalOwnershipLost("launcher run is terminal")
    control.launcher_id = launcher_id
    key = child_identity or f"{generation}:{launcher_id}:{run_id}"
    history = {name: dict(entry) for name, entry in (control.child_history or {}).items()}
    entry = history.get(
        key,
        {
            "identity": key,
            "run_id": str(run_id),
            "generation": generation,
            "delivery_token": str(token),
            "launcher_id": str(launcher_id),
            "pid": None,
            "started_at": None,
            "launcher_pid": os.getpid(),
            "launcher_started_at": process_birth_identity(os.getpid()),
            "executable": sys.executable,
            "handoff_expires_at": control.lease_expires_at.isoformat()
            if control.lease_expires_at
            else None,
            "exited": False,
        },
    )
    if child_pid is not None:
        entry["pid"] = child_pid
        entry["started_at"] = str(child_started_at) if child_started_at is not None else None
    history[key] = entry
    control.child_history = history
    _sync_child_uncertainty(control)
    run.delivery_transport = "subprocess"
    run.celery_task_id = None
    if child_pid is not None:
        control.child_pid = child_pid
        control.child_identity = (child_identity or "")[:256]
        control.unconfirmed_child = True
    session.flush()


def record_child_exit(
    session: Session, run_id: uuid.UUID, token: uuid.UUID, generation: int, launcher_id: uuid.UUID
) -> bool:
    control = lock_processing_control(session)
    history = {name: dict(entry) for name, entry in (control.child_history or {}).items()}
    matched = False
    for entry in history.values():
        if (
            entry.get("run_id") == str(run_id)
            and entry.get("generation") == generation
            and entry.get("delivery_token") == str(token)
            and entry.get("launcher_id") == str(launcher_id)
        ):
            entry["exited"] = True
            entry["exit_confirmed_at"] = utc_now().isoformat()
            entry["exit_evidence"] = "owned_launcher_wait"
            matched = True
    if matched:
        control.child_history = history
        _sync_child_uncertainty(control)
        session.flush()
    return matched


def fail_owned_launch(
    session: Session,
    run_id: uuid.UUID,
    token: uuid.UUID,
    generation: int,
    *,
    code: str,
    message: str = "Personal child could not complete; explicit retry may be available.",
    allow_running: bool = False,
    now: dt.datetime | None = None,
    launcher_id: uuid.UUID | None = None,
) -> bool:
    control = lock_processing_control(session)
    run = session.get(PersonalRun, run_id, with_for_update=True, populate_existing=True)
    if (
        run is None
        or control.active_run_id != run.id
        or control.generation != generation
        or run.fencing_generation != generation
        or control.delivery_token != token
        or (launcher_id is not None and control.launcher_id != launcher_id)
        or run.state not in ({"queued", "running"} if allow_running else {"queued"})
    ):
        return False
    # Confirmed process death permits terminalization even after expiry; takeover already
    # changed generation and is rejected above. No business response is written here.
    run.state = "failed"
    run.error = {"code": code, "message": message, "retryable": run.attempt < run.max_attempts}
    run.lease_expires_at = None
    job = session.get(Job, run.job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise RuntimeError("personal run job is unavailable")
    job.state, job.error = "failed", run.error
    release_control(control)
    session.flush()
    return True


def processing_diagnostics(control: PersonalWriterMode) -> dict[str, object]:
    def encoded(value):
        return (
            value.isoformat()
            if isinstance(value, dt.datetime)
            else str(value)
            if isinstance(value, uuid.UUID)
            else value
        )

    return {
        key: encoded(getattr(control, key))
        for key in (
            "mode",
            "generation",
            "active_run_id",
            "active_attempt",
            "queued_at",
            "started_at",
            "graceful_deadline_at",
            "hard_deadline_at",
            "lease_expires_at",
            "launcher_id",
            "child_pid",
            "child_identity",
            "unconfirmed_child",
        )
    }


def diagnostics(session: Session) -> dict[str, object]:
    return processing_diagnostics(lock_processing_control(session, lock="read"))


def recover_expired_owner(
    session: Session,
    run_id: uuid.UUID,
    *,
    now: dt.datetime | None = None,
    generation: int | None = None,
    confirm_dead_children: bool = False,
) -> PersonalRun:
    """Explicitly fail an expired owner without starting work or changing allowances.

    Child uncertainty is retained. Recovery is a durable state repair, never proof that
    an operating-system process is dead; a later mode switch still needs that proof.
    """
    control = lock_processing_control(session)
    require_mode(control, "personal")
    run = session.get(PersonalRun, run_id, with_for_update=True, populate_existing=True)
    if (
        run is None
        or control.active_run_id != run.id
        or run.fencing_generation != control.generation
        or (generation is not None and control.generation != generation)
        or run.state not in {"queued", "running"}
    ):
        raise PersonalOwnershipLost("expired recovery ownership changed")
    stamp = _utc(now or utc_now())
    if (
        run.lease_expires_at is not None
        and run.lease_expires_at > stamp
        or control.lease_expires_at is not None
        and control.lease_expires_at > stamp
    ):
        raise ProcessingBusy(run.id, "ownership lease has not expired")
    if confirm_dead_children:
        confirm_absent_children(control)
    run.state = "failed"
    run.error = {
        "code": "ownership_lease_expired",
        "message": "Interrupted owner explicitly recovered; retry requires a separate action.",
        "retryable": run.attempt < run.max_attempts,
    }
    run.lease_expires_at = None
    job = session.get(Job, run.job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise RuntimeError("personal run job is unavailable")
    job.state, job.error = "failed", run.error
    release_control(control)
    session.flush()
    return run


def _sync_child_uncertainty(control: PersonalWriterMode) -> None:
    control.unconfirmed_child = any(
        not entry.get("exited", False) for entry in (control.child_history or {}).values()
    )


def process_birth_identity(pid: int) -> str | None:
    """Read process birth using fixed local arguments; never signal a diagnostic PID."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("process identity requires a positive integer PID")
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        shell=False,
        capture_output=True,
        text=True,
        timeout=2,
        env=environment,
    )
    value = result.stdout.strip()
    if result.returncode == 1 and not value:
        return None
    if result.returncode != 0 or not value:
        raise RuntimeError("process birth identity is unavailable")
    return value


def confirm_absent_children(control: PersonalWriterMode) -> int:
    """Confirm absence/PID reuse for retained lifetimes; caller holds global control.

    Unknown pre-launch PIDs and unreadable identities remain unconfirmed. A matching
    birth identity remains live. No process is signalled by this recovery inspection.
    """
    history = {name: dict(entry) for name, entry in (control.child_history or {}).items()}
    confirmed = 0
    for entry in history.values():
        if entry.get("exited"):
            continue
        evidence = None
        if type(entry.get("pid")) is int:
            try:
                current = process_birth_identity(entry["pid"])
            except (RuntimeError, OSError, subprocess.SubprocessError):
                continue
            if current is None:
                evidence = "recorded_process_absent"
            elif entry.get("started_at") is not None and current != entry["started_at"]:
                evidence = "recorded_process_birth_changed"
        else:
            # A pre-Popen crash has no child PID. The original launcher must be
            # proven dead, the handoff expired, and the exact immutable child argv
            # absent. No process is killed and unrelated process text is not retained.
            try:
                expires = dt.datetime.fromisoformat(entry["handoff_expires_at"])
                launcher = process_birth_identity(entry["launcher_pid"])
                launcher_dead = launcher is None or (
                    entry.get("launcher_started_at") is not None
                    and launcher != entry["launcher_started_at"]
                )
                if utc_now() < expires or not launcher_dead or _matching_child_argv_exists(entry):
                    continue
            except (
                KeyError,
                TypeError,
                ValueError,
                RuntimeError,
                OSError,
                subprocess.SubprocessError,
            ):
                continue
            evidence = "dead_launcher_expired_handoff_child_argv_absent"
        if evidence:
            entry["exited"] = True
            entry["exit_confirmed_at"] = utc_now().isoformat()
            entry["exit_evidence"] = evidence
            confirmed += 1
    control.child_history = history
    _sync_child_uncertainty(control)
    return confirmed


def _matching_child_argv_exists(entry: dict) -> bool:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    result = subprocess.run(
        ["ps", "-A", "-ww", "-o", "pid=,command="],
        shell=False,
        capture_output=True,
        text=True,
        timeout=2,
        env=environment,
    )
    if result.returncode:
        raise RuntimeError("fixed child process inspection is unavailable")
    suffix = f" -m services.personal.child {entry['run_id']} {entry['delivery_token']} {entry['generation']}"
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        command = fields[1]
        # ps command text is not shell-quoted argv: executable paths may contain
        # spaces. Parse the fixed known suffix from the right without shlex.
        if command.endswith(suffix):
            executable = command[: -len(suffix)]
            if os.path.realpath(executable) == os.path.realpath(entry["executable"]):
                return True
        # Relevant but unparseable identity text cannot prove a child is absent.
        if "services.personal.child" in command and all(
            str(entry[key]) in command for key in ("run_id", "delivery_token", "generation")
        ):
            return True
    return False


def reconcile_child_lifetimes(session: Session) -> int:
    """Explicit lifetime-only recovery, including already terminal/idle runs."""
    control = lock_processing_control(session)
    confirmed = confirm_absent_children(control)
    session.flush()
    return confirmed
