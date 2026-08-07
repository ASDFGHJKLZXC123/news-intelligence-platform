"""Contract tests for the bounded Stage 10 degraded-path drill runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run-degraded-path-drills.py"
    spec = importlib.util.spec_from_file_location("degraded_path_drills", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_drill_inventory_is_fixed_bounded_and_loopback_only() -> None:
    module = _module()

    commands = module.build_drill_commands()

    assert [command.name for command in commands] == [
        "postgresql_loss_readiness",
        "redis_loss_readiness",
        "worker_broker_loss_readiness",
        "broker_lease_recovery_and_shutdown",
    ]
    serialized = repr(
        [
            {
                key: value
                for key, value in command.environment.items()
                if key in {"DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL"}
            }
            for command in commands
        ]
    )
    assert "127.0.0.1:1" in serialized
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert all(command.argv[0] == module.sys.executable for command in commands)


def test_report_contains_only_aggregate_outcomes_not_subprocess_output() -> None:
    module = _module()
    seen: list[tuple[str, float]] = []

    def executor(command, timeout_seconds):
        seen.append((command.name, timeout_seconds))
        return command.name != "redis_loss_readiness"

    report = module.run_drills(timeout_seconds=3.0, executor=executor)

    assert report["decision"] == "hold"
    assert len(seen) == 4
    assert all(timeout == 3.0 for _name, timeout in seen)
    assert report["safety"]["subprocess_output_serialized"] is False
    assert set(report) == {
        "schema",
        "decision",
        "timeout_seconds_per_drill",
        "drills",
        "safety",
    }
