"""Minimal in-process metrics counters.

Stage 1 only needs the four counters called out in the build plan: HTTP requests,
job starts, job successes, and job failures. This is a deliberately tiny, dependency
free implementation; a Prometheus/OpenTelemetry exporter can replace it later without
changing call sites.
"""

from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()
_counters: dict[str, int] = defaultdict(int)

# Canonical counter names.
REQUESTS = "requests_total"
JOB_STARTS = "job_starts_total"
JOB_SUCCESSES = "job_successes_total"
JOB_FAILURES = "job_failures_total"


def increment(name: str, amount: int = 1) -> None:
    with _lock:
        _counters[name] += amount


def get(name: str) -> int:
    with _lock:
        return _counters[name]


def snapshot() -> dict[str, int]:
    """Return a copy of all counters (always includes the four canonical ones)."""
    with _lock:
        result = {REQUESTS: 0, JOB_STARTS: 0, JOB_SUCCESSES: 0, JOB_FAILURES: 0}
        result.update(_counters)
        return result


def reset() -> None:
    """Clear all counters. Intended for tests only."""
    with _lock:
        _counters.clear()
