#!/usr/bin/env python3
"""Benchmark the deterministic daily-pipeline coordinator without external services.

This is a control-plane baseline, not an ingestion, database, broker, or model benchmark.
Every stage is represented by a deterministic in-memory runner so the command is safe to run
locally and in CI without credentials or network access.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Make direct execution from ``scripts/`` behave like the repository's module entry points.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from services.pipeline import (  # noqa: E402
    ORDERED_STAGES,
    DailyPipelineCoordinator,
    PipelineStage,
    PipelineState,
    StageContext,
    StageResult,
)

SCHEMA = "daily-pipeline-coordinator-benchmark.v1"
DEFAULT_PROCESS_DATE = "2026-07-29"
DEFAULT_ITERATIONS = 1_000
DEFAULT_WARMUP_ITERATIONS = 100

# The counts describe a small, fixed daily workload. The in-memory runners do not perform the
# underlying I/O; they return the same shape of stage metadata on every invocation.
STAGE_ITEM_COUNTS: dict[PipelineStage, int] = {
    PipelineStage.INGESTION: 5,
    PipelineStage.ARTICLE_EMBEDDINGS: 50,
    PipelineStage.CLUSTERING: 8,
    PipelineStage.ENTITY_LINKING: 8,
    PipelineStage.EVENT_EMBEDDINGS: 8,
    PipelineStage.ANALOGIES: 8,
    PipelineStage.DAILY_BRIEF: 1,
}


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _nonnegative_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure deterministic in-memory daily-pipeline coordinator overhead and emit JSON."
        )
    )
    parser.add_argument(
        "--iterations",
        type=_positive_integer,
        default=DEFAULT_ITERATIONS,
        help=f"measured runs (default: {DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "--warmup",
        type=_nonnegative_integer,
        default=DEFAULT_WARMUP_ITERATIONS,
        help=f"unreported warmup runs (default: {DEFAULT_WARMUP_ITERATIONS})",
    )
    parser.add_argument(
        "--process-date",
        default=DEFAULT_PROCESS_DATE,
        help=f"fixed YYYY-MM-DD coordinator identity (default: {DEFAULT_PROCESS_DATE})",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent the JSON report for human inspection",
    )
    return parser


def _percentile(samples: list[int], percentile: float) -> int:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def _timing_summary(samples: list[int]) -> dict[str, int | float]:
    if not samples:
        raise ValueError("cannot summarize an empty timing sample")
    total = sum(samples)
    return {
        "samples": len(samples),
        "total_ns": total,
        "min_ns": min(samples),
        "median_ns": int(statistics.median(samples)),
        "p95_ns": _percentile(samples, 0.95),
        "max_ns": max(samples),
        "mean_ns": total / len(samples),
    }


class _StageTimer:
    """Collect timings around deterministic stage stubs."""

    def __init__(self) -> None:
        self.samples: dict[PipelineStage, list[int]] = {stage: [] for stage in ORDERED_STAGES}

    def clear(self) -> None:
        for samples in self.samples.values():
            samples.clear()

    def runner(
        self,
        stage: PipelineStage,
    ) -> Callable[[StageContext], StageResult]:
        item_count = STAGE_ITEM_COUNTS[stage]

        def run(context: StageContext) -> StageResult:
            started = time.perf_counter_ns()
            output: dict[str, Any] = {
                "benchmark_stage": stage.value,
                "process_key": context.identity.process_key,
                "input_items": item_count,
            }
            if stage is PipelineStage.CLUSTERING:
                output["event_ids"] = [
                    f"benchmark-event-{index:02d}" for index in range(item_count)
                ]
            result = StageResult.succeeded(item_count=item_count, output=output)
            self.samples[stage].append(time.perf_counter_ns() - started)
            return result

        return run


def run_benchmark(
    *,
    iterations: int,
    warmup: int,
    process_date: str,
) -> dict[str, Any]:
    """Return a JSON-safe report for a deterministic coordinator-only workload."""

    timer = _StageTimer()
    coordinator = DailyPipelineCoordinator(
        stage_runners={stage: timer.runner(stage) for stage in ORDERED_STAGES}
    )

    for _ in range(warmup):
        warmup_result = coordinator.run(process_date)
        if warmup_result.state is not PipelineState.SUCCEEDED:
            raise RuntimeError("warmup pipeline did not succeed")
    timer.clear()

    coordinator_samples: list[int] = []
    final_result = None
    for _ in range(iterations):
        started = time.perf_counter_ns()
        final_result = coordinator.run(process_date)
        coordinator_samples.append(time.perf_counter_ns() - started)
        if final_result.state is not PipelineState.SUCCEEDED:
            raise RuntimeError("measured pipeline did not succeed")

    assert final_result is not None
    total_seconds = sum(coordinator_samples) / 1_000_000_000
    return {
        "schema": SCHEMA,
        "scope": "coordinator_control_plane_only",
        "deterministic_workload": True,
        "external_io": {
            "database": False,
            "broker": False,
            "model_provider": False,
            "network": False,
        },
        "hard_budget_enforced": False,
        "process_date": final_result.identity.process_date.isoformat(),
        "process_key": final_result.identity.process_key,
        "pipeline_state": final_result.state.value,
        "iterations": iterations,
        "warmup_iterations": warmup,
        "stage_item_counts": {stage.value: STAGE_ITEM_COUNTS[stage] for stage in ORDERED_STAGES},
        "coordinator_timing": {
            **_timing_summary(coordinator_samples),
            "runs_per_second": iterations / total_seconds,
        },
        "stage_timings": {
            stage.value: _timing_summary(timer.samples[stage]) for stage in ORDERED_STAGES
        },
        "interpretation": (
            "Compare reports from equivalent hosts as a trend baseline; this command does not "
            "pass or fail on timing and does not measure production stage latency."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_benchmark(
        iterations=args.iterations,
        warmup=args.warmup,
        process_date=args.process_date,
    )
    print(
        json.dumps(
            report,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
