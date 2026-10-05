"""Fixed personal runtime budgets independent of Celery and launcher lifetime."""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import math
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator

GRACEFUL_SECONDS = 25 * 60
HARD_SECONDS = 30 * 60
OWNERSHIP_SECONDS = 35 * 60
SHUTDOWN_SECONDS = 30


class RuntimeCancelled(BaseException):
    """Fatal cancellation. Item-level ``except Exception`` cannot consume it."""


class RuntimeBudget:
    def __init__(
        self,
        graceful_deadline: dt.datetime,
        hard_deadline: dt.datetime,
        *,
        wall_clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if graceful_deadline.tzinfo is None or hard_deadline.tzinfo is None:
            raise ValueError("runtime deadlines must be timezone-aware")
        if hard_deadline < graceful_deadline:
            raise ValueError("hard deadline precedes graceful deadline")
        self.graceful_deadline, self.hard_deadline = graceful_deadline, hard_deadline
        self.wall_clock, self.monotonic = wall_clock, monotonic
        self._started_monotonic = monotonic()
        now = wall_clock()
        self._graceful_original = max(0.0, (graceful_deadline - now).total_seconds())
        self._hard_original = max(0.0, (hard_deadline - now).total_seconds())
        self._cancel_reason: str | None = None

    def remaining(self) -> float:
        return max(
            0.0,
            min(
                (self.graceful_deadline - self.wall_clock()).total_seconds(),
                self._graceful_original - (self.monotonic() - self._started_monotonic),
            ),
        )

    def hard_remaining(self) -> float:
        return max(
            0.0,
            min(
                (self.hard_deadline - self.wall_clock()).total_seconds(),
                self._hard_original - (self.monotonic() - self._started_monotonic),
            ),
        )

    def cancel(self, reason: str = "runtime cancellation requested") -> None:
        self._cancel_reason = reason

    def check(self) -> None:
        if self._cancel_reason:
            raise RuntimeCancelled(self._cancel_reason)
        if self.remaining() <= 0:
            raise RuntimeCancelled("personal graceful execution deadline elapsed")

    def require_call(self, configured_seconds: float) -> float:
        self.check()
        if (
            isinstance(configured_seconds, bool)
            or not math.isfinite(configured_seconds)
            or configured_seconds <= 0
        ):
            raise ValueError("call timeout must be finite and positive")
        if self.remaining() < configured_seconds:
            raise RuntimeCancelled("insufficient graceful budget for configured request deadline")
        return configured_seconds


_current_budget: contextvars.ContextVar[RuntimeBudget | None] = contextvars.ContextVar(
    "personal_runtime_budget", default=None
)


def install_budget(budget: RuntimeBudget) -> contextvars.Token:
    return _current_budget.set(budget)


def check_budget() -> None:
    budget = _current_budget.get()
    if budget is not None:
        budget.check()


def require_call_budget(configured_seconds: float) -> float:
    budget = _current_budget.get()
    return configured_seconds if budget is None else budget.require_call(configured_seconds)


def remaining_call_budget(configured_seconds: float) -> float:
    budget = _current_budget.get()
    if budget is None:
        return configured_seconds
    budget.check()
    return min(configured_seconds, budget.remaining())


@contextlib.contextmanager
def bind_budget(budget: RuntimeBudget | None) -> Iterator[None]:
    token = _current_budget.set(budget)
    try:
        yield
    finally:
        _current_budget.reset(token)


class RuntimeWatchdog:
    """An owned child enforces its persisted deadlines even after its launcher exits."""

    def __init__(self, budget: RuntimeBudget, *, owned_group: bool = False) -> None:
        self.budget, self.owned_group = budget, owned_group
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, name="personal-deadline-watchdog", daemon=True
        )

    def start(self) -> None:
        if self.owned_group and os.getpgrp() != os.getpid():
            raise RuntimeError("child watchdog requires its own process group")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _watch(self) -> None:
        graceful_sent = False
        while not self._stop.wait(min(0.05, max(0.001, self.budget.hard_remaining()))):
            if self.budget.remaining() <= 0 and not graceful_sent:
                self.budget.cancel("personal graceful execution deadline elapsed")
                graceful_sent = True
                # Signal interrupts the main thread's blocking call; the child installs
                # its fatal cancellation handler before starting this watchdog.
                os.kill(os.getpid(), signal.SIGUSR1)
            if self.budget.hard_remaining() <= 0:
                if self.owned_group:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                else:
                    os._exit(124)


_external_fatal: contextvars.ContextVar[tuple[type[BaseException], ...]] = contextvars.ContextVar(
    "personal_external_fatal", default=()
)


@contextlib.contextmanager
def fatal_cancellations(types: tuple[type[BaseException], ...]) -> Iterator[None]:
    token = _external_fatal.set(types)
    try:
        yield
    finally:
        _external_fatal.reset(token)


def cancellation_from(exc: BaseException) -> RuntimeCancelled | None:
    if isinstance(exc, RuntimeCancelled):
        return exc
    if isinstance(exc, _external_fatal.get()):
        return RuntimeCancelled("personal execution interrupted by legacy soft deadline")
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            found = cancellation_from(nested)
            if found is not None:
                return found
    return None


def propagate_fatal(exc: BaseException) -> None:
    fatal = cancellation_from(exc)
    if fatal is not None:
        raise fatal from exc
