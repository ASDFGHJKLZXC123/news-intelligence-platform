"""One shared, bounded on-demand child supervisor for the API and manual command."""

from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services.personal.deadlines import SHUTDOWN_SECONDS


class LauncherUnavailable(RuntimeError):
    pass


@dataclass
class _OwnedChild:
    process: Any
    run_id: uuid.UUID
    token: uuid.UUID
    generation: int
    identity: uuid.UUID
    thread: threading.Thread | None = None


def _record_launch(run_id, token, generation, launcher_id, process, identity):
    from db.base import SessionLocal
    from services.personal.processing import process_birth_identity, record_launcher

    with SessionLocal() as session:
        record_launcher(
            session,
            run_id,
            token,
            generation,
            launcher_id,
            child_pid=process.pid if process is not None else None,
            child_identity=str(identity),
            child_started_at=process_birth_identity(process.pid) if process is not None else None,
        )
        session.commit()


def _record_exit(run_id, token, generation, launcher_id, _returncode):
    from db.base import SessionLocal
    from services.personal.processing import fail_owned_launch, record_child_exit

    with SessionLocal() as session:
        if record_child_exit(session, run_id, token, generation, launcher_id):
            fail_owned_launch(
                session,
                run_id,
                token,
                generation,
                code="dispatch_failed" if _returncode is None else "child_exited",
                allow_running=True,
                launcher_id=launcher_id,
            )
        session.commit()


def _read_deadline(run_id, token, generation):
    from sqlalchemy import select

    from db.base import SessionLocal
    from db.models import PersonalWriterMode

    with SessionLocal() as session:
        control = session.scalar(
            select(PersonalWriterMode).where(PersonalWriterMode.singleton.is_(True))
        )
        if (
            control is None
            or control.active_run_id != run_id
            or control.delivery_token != token
            or control.generation != generation
        ):
            return None
        return control.hard_deadline_at or control.lease_expires_at


class PersonalSupervisor:
    def __init__(
        self,
        *,
        record_launch: Callable = _record_launch,
        record_exit: Callable = _record_exit,
        read_deadline: Callable = _read_deadline,
        popen: Callable = subprocess.Popen,
    ) -> None:
        self.launcher_id = uuid.uuid4()
        self._record_launch, self._record_exit, self._read_deadline, self._popen = (
            record_launch,
            record_exit,
            read_deadline,
            popen,
        )
        self._lock = threading.RLock()
        self._children: dict[uuid.UUID, _OwnedChild] = {}
        self._completed: dict[uuid.UUID, int] = {}
        self._closing = False

    @property
    def ready(self) -> bool:
        return not self._closing and os.name == "posix" and bool(sys.executable)

    def launch(self, run_id: uuid.UUID, delivery_token: uuid.UUID, generation: int) -> str:
        if (
            not isinstance(run_id, uuid.UUID)
            or not isinstance(delivery_token, uuid.UUID)
            or type(generation) is not int
            or generation < 0
        ):
            raise ValueError("launcher accepts only validated durable run identifiers")
        with self._lock:
            if not self.ready:
                raise LauncherUnavailable("personal child launcher is unavailable")
            existing = self._children.get(run_id)
            if existing is not None and existing.process.poll() is None:
                if existing.token != delivery_token or existing.generation != generation:
                    raise LauncherUnavailable("previous owned child has not exited")
                return str(existing.identity)
            identity = uuid.uuid4()
            environment = dict(os.environ)
            environment["PERSONAL_CHILD_IDENTITY"] = str(identity)
            environment["PERSONAL_LAUNCHER_ID"] = str(self.launcher_id)
            self._record_launch(
                run_id, delivery_token, generation, self.launcher_id, None, identity
            )
            try:
                handoff_deadline = self._read_deadline(run_id, delivery_token, generation)
                if handoff_deadline is None or handoff_deadline.tzinfo is None:
                    raise LauncherUnavailable("personal queued handoff deadline is unavailable")
                environment["PERSONAL_HANDOFF_DEADLINE"] = handoff_deadline.isoformat()
                process = self._popen(
                    [
                        sys.executable,
                        "-m",
                        "services.personal.child",
                        str(run_id),
                        str(delivery_token),
                        str(generation),
                    ],
                    shell=False,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    env=environment,
                )
            except BaseException:
                try:
                    self._record_exit(run_id, delivery_token, generation, self.launcher_id, None)
                except Exception:
                    pass
                raise
            child = _OwnedChild(process, run_id, delivery_token, generation, identity)
            self._children[run_id] = child
            try:
                self._record_launch(
                    run_id, delivery_token, generation, self.launcher_id, process, identity
                )
            except BaseException:
                self._signal_owned(child, signal.SIGKILL)
                process.wait(timeout=5)
                try:
                    self._record_exit(
                        run_id, delivery_token, generation, self.launcher_id, process.returncode
                    )
                except Exception:
                    pass
                self._children.pop(run_id, None)
                raise
            child.thread = threading.Thread(
                target=self._monitor, args=(child,), daemon=True, name="personal-child-monitor"
            )
            child.thread.start()
            return str(identity)

    def _signal_owned(self, child: _OwnedChild, signum: int) -> None:
        # Popen retains ownership of an unreaped child. A diagnostic PID alone is
        # never accepted as a reason to signal any process or process group.
        if child.process.poll() is not None:
            return
        try:
            if os.getpgid(child.process.pid) == child.process.pid:
                os.killpg(child.process.pid, signum)
        except ProcessLookupError:
            pass

    def _monitor(self, child: _OwnedChild) -> None:
        deadline = None
        while child.process.poll() is None:
            try:
                deadline = (
                    self._read_deadline(child.run_id, child.token, child.generation) or deadline
                )
            except Exception:
                # Loss of DB never extends the known original deadline. The
                # child's independent watchdog remains authoritative as well.
                pass
            if deadline is not None and dt.datetime.now(dt.UTC) >= deadline:
                self._signal_owned(child, signal.SIGKILL)
            time.sleep(0.1)
        try:
            self._record_exit(
                child.run_id,
                child.token,
                child.generation,
                self.launcher_id,
                getattr(child.process, "returncode", child.process.poll()),
            )
        except Exception:
            # Leave durable ownership intact for explicit recovery after DB loss.
            pass
        with self._lock:
            self._completed[child.run_id] = getattr(
                child.process, "returncode", child.process.poll()
            )
            if self._children.get(child.run_id) is child:
                self._children.pop(child.run_id)

    def wait(self, run_id: uuid.UUID) -> int | None:
        with self._lock:
            child = self._children.get(run_id)
        if child is None:
            return self._completed.get(run_id)
        return child.process.wait()

    def shutdown(self, *, timeout: float = SHUTDOWN_SECONDS) -> None:
        timeout = max(0.0, min(float(timeout), SHUTDOWN_SECONDS))
        deadline = time.monotonic() + timeout
        with self._lock:
            self._closing = True
            children = list(self._children.values())
        for child in children:
            self._signal_owned(child, signal.SIGTERM)
        while (
            any(child.process.poll() is None for child in children) and time.monotonic() < deadline
        ):
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        for child in children:
            self._signal_owned(child, signal.SIGKILL)
        for child in children:
            if child.thread is not None:
                child.thread.join(timeout=max(0.0, deadline - time.monotonic()))


_singleton: PersonalSupervisor | None = None
_singleton_lock = threading.Lock()


def get_supervisor() -> PersonalSupervisor:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PersonalSupervisor()
        return _singleton


def validate_personal_runtime_settings(settings: Any) -> None:
    """Fail closed before writing or launching if local runtime configuration disagrees."""
    if settings.personal_processing_transport != "subprocess":
        raise LauncherUnavailable("manual/child execution requires subprocess transport")
    if settings.personal_processing_mode != "personal":
        raise LauncherUnavailable("personal child requires configured personal processing mode")
    if settings.personal_bind_host not in {"127.0.0.1", "localhost", "::1"}:
        raise LauncherUnavailable("personal mode must bind to loopback")
    if settings.personal_app_workers != 1:
        raise LauncherUnavailable("personal mode requires one application supervisor")
