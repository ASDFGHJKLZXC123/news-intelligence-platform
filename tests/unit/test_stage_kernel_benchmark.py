"""Contract tests for the fixed production-stage kernel benchmark."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark-stage-kernels.py"
    spec = importlib.util.spec_from_file_location("stage_kernel_benchmark", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_stage_workloads_pass_their_structural_amplification_gates() -> None:
    module = _module()

    report = module.run_benchmark(iterations=2, warmup=1)

    assert report["decision"] == "pass"
    assert report["timing_gate_enforced"] is False
    assert all(check["passed"] for check in report["structural_checks"])
    assert report["workloads"]["embedding"]["structure"]["provider_calls"] == 4
    assert report["workloads"]["clustering"]["structure"]["pair_comparisons"] == 276
    assert report["workloads"]["pipeline_adapters"]["structure"]["total_task_calls"] == 15
    assert report["external_io"] == {
        "network": False,
        "database": False,
        "redis": False,
        "broker": False,
        "files_written": False,
    }


def test_cli_emits_machine_readable_report_and_success_exit(capsys) -> None:
    module = _module()

    assert module.main(["--iterations", "1", "--warmup", "0"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["schema"] == module.SCHEMA
    assert report["workload_version"] == module.WORKLOAD_VERSION
    assert report["decision"] == "pass"
