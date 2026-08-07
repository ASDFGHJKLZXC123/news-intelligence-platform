#!/usr/bin/env python3
"""Run bounded, non-destructive degraded-path drills and emit a secret-safe report."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DRILL_SCHEMA: Final = "stage10-degraded-path-drills.v1"
DEFAULT_TIMEOUT_SECONDS: Final = 20.0


@dataclass(frozen=True)
class DrillCommand:
    name: str
    argv: tuple[str, ...]
    environment: Mapping[str, str]


CommandExecutor = Callable[[DrillCommand, float], bool]


def _base_environment() -> dict[str, str]:
    # Pass only process-runtime essentials. In particular, do not copy provider API keys or the
    # developer's live database/broker credentials into drill subprocesses that do not need them.
    environment = {
        name: os.environ[name]
        for name in ("LANG", "LC_ALL", "PATH", "PYTHONHOME", "TMPDIR", "VIRTUAL_ENV")
        if name in os.environ
    }
    environment["APP_ENV"] = "test"
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
    return environment


def _health_command(name: str, check_name: str, **environment_updates: str) -> DrillCommand:
    code = (
        f"from apps.api.deps import {check_name}; "
        f"status={check_name}(); "
        "raise SystemExit(0 if status.ok is False else 1)"
    )
    environment = _base_environment()
    environment.update(environment_updates)
    return DrillCommand(name, (sys.executable, "-c", code), environment)


def build_drill_commands() -> tuple[DrillCommand, ...]:
    """Return the fixed drill inventory; every socket target is loopback port 1."""

    commands = [
        _health_command(
            "postgresql_loss_readiness",
            "check_database",
            DATABASE_URL="postgresql+psycopg2://drill:drill@127.0.0.1:1/drill",
        ),
        _health_command(
            "redis_loss_readiness",
            "check_redis",
            REDIS_URL="redis://127.0.0.1:1/0",
        ),
        _health_command(
            "worker_broker_loss_readiness",
            "check_worker_config",
            CELERY_BROKER_URL="redis://127.0.0.1:1/1",
            CELERY_RESULT_BACKEND="redis://127.0.0.1:1/2",
        ),
    ]
    test_ids = (
        "tests/unit/test_pipeline_runtime.py::test_broker_failure_is_durably_failed_and_returns_service_unavailable",
        "tests/unit/test_pipeline_runtime.py::test_abandoned_queue_delivery_is_bounded_and_reissued_with_a_new_token",
        "tests/unit/test_pipeline_runtime.py::test_expired_running_lease_rotates_owner_and_rejects_late_terminal_write",
        "tests/unit/test_pipeline_runtime.py::test_soft_time_limit_raised_by_a_stage_runner_is_durably_failed_and_stops_the_run",
        "tests/unit/test_pipeline_stages.py::test_a_fatal_nested_with_cleanup_failures_propagates_out_of_run_one",
    )
    commands.append(
        DrillCommand(
            "broker_lease_recovery_and_shutdown",
            (sys.executable, "-m", "pytest", "-q", *test_ids),
            _base_environment(),
        )
    )
    return tuple(commands)


def _execute(command: DrillCommand, timeout_seconds: float) -> bool:
    try:
        completed = subprocess.run(
            command.argv,
            cwd=REPOSITORY_ROOT,
            env=dict(command.environment),
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def run_drills(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    executor: CommandExecutor = _execute,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    results = [
        {"name": command.name, "passed": executor(command, timeout_seconds)}
        for command in build_drill_commands()
    ]
    passed = all(result["passed"] for result in results)
    return {
        "schema": DRILL_SCHEMA,
        "decision": "pass" if passed else "hold",
        "timeout_seconds_per_drill": timeout_seconds,
        "drills": results,
        "safety": {
            "external_network": False,
            "loopback_refusal_probes": True,
            "database_writes": False,
            "redis_writes": False,
            "files_written": False,
            "subprocess_output_serialized": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_drills(timeout_seconds=args.timeout_seconds)
    print(
        json.dumps(
            report,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report["decision"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
