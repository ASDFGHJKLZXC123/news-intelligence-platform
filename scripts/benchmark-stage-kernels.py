#!/usr/bin/env python3
"""Benchmark fixed deterministic ingestion, embedding, clustering, and stage-adapter workloads.

This complements the coordinator-only benchmark.  It executes production service functions and
production pipeline adapters with deterministic in-process providers, but deliberately excludes
database, broker, and network latency.  CI gates structural work amplification (batch/call/pair
counts), never host-specific timing.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from packages.providers.base import EmbeddingResult, RSSItem  # noqa: E402
from services.ingestion import item_to_article_values, partition_new_items, url_hash  # noqa: E402
from services.nlp.clustering import ClusterItem, cluster_by_similarity  # noqa: E402
from services.nlp.embeddings import embed_texts  # noqa: E402
from services.pipeline import DailyPipelineCoordinator, PipelineState  # noqa: E402
from workers.pipeline_stages import ProductionPipelineStages  # noqa: E402

SCHEMA: Final = "production-stage-kernel-benchmark.v1"
WORKLOAD_VERSION: Final = "stage10-fixed-small.v1"
DEFAULT_ITERATIONS: Final = 10
DEFAULT_WARMUP_ITERATIONS: Final = 2
EMBEDDING_DIMENSION: Final = 1536
_PUBLISHED: Final = datetime.datetime(2026, 8, 1, 12, tzinfo=datetime.UTC)
_PROCESS_DATE: Final = "2026-08-01"


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=_positive_integer, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmup", type=_nonnegative_integer, default=DEFAULT_WARMUP_ITERATIONS)
    parser.add_argument("--pretty", action="store_true")
    return parser


def _percentile(samples: Sequence[int], percentile: float) -> int:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def _timing(samples: Sequence[int], *, units_per_run: int) -> dict[str, int | float]:
    total_ns = sum(samples)
    return {
        "samples": len(samples),
        "p50_ns": int(statistics.median(samples)),
        "p95_ns": _percentile(samples, 0.95),
        "min_ns": min(samples),
        "max_ns": max(samples),
        "mean_ns": total_ns / len(samples),
        "units_per_second": (units_per_run * len(samples)) / (total_ns / 1_000_000_000),
    }


def _measure(
    workload: Callable[[], dict[str, Any]],
    *,
    iterations: int,
    warmup: int,
    units_per_run: int,
) -> tuple[dict[str, Any], dict[str, int | float]]:
    for _ in range(warmup):
        workload()
    samples: list[int] = []
    first_result: dict[str, Any] | None = None
    for _ in range(iterations):
        started = time.perf_counter_ns()
        result = workload()
        samples.append(time.perf_counter_ns() - started)
        if first_result is None:
            first_result = result
        elif result != first_result:
            raise RuntimeError("deterministic benchmark workload changed its structural result")
    assert first_result is not None
    return first_result, _timing(samples, units_per_run=units_per_run)


_INGESTION_ITEMS: Final = tuple(
    RSSItem(
        guid=f"benchmark-{index}",
        title=f"Benchmark article {index}",
        url=f"https://news.example.test/items/{index // 2}?utm_source=benchmark&copy={index % 2}",
        published_at=_PUBLISHED,
        summary="A fixed synthetic article used only for local structural performance checks.",
        source="benchmark",
        provider_name="deterministic",
    )
    for index in range(200)
)


def _ingestion_workload() -> dict[str, Any]:
    existing = {url_hash(_INGESTION_ITEMS[index].url) for index in range(0, 20, 2)}
    new, duplicates = partition_new_items(_INGESTION_ITEMS, existing)
    values = [item_to_article_values(item, "benchmark-source") for item in new]
    return {
        "input_items": len(_INGESTION_ITEMS),
        "new_items": len(values),
        "duplicate_items": len(duplicates),
        "mapped_rows": len(values),
    }


class _DeterministicEmbeddingProvider:
    provider_name = "deterministic"
    model_name = "text-embedding-3-small"
    model_version = "benchmark-fixed"

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed(self, texts: Sequence[str]) -> Sequence[EmbeddingResult]:
        self.batch_sizes.append(len(texts))
        return tuple(
            EmbeddingResult(
                vector=tuple([float((index % 7) + 1) / 8.0] * EMBEDDING_DIMENSION),
                provider_name=self.provider_name,
                model_name=self.model_name,
                model_version=self.model_version,
                dimension=EMBEDDING_DIMENSION,
                model_run_id=f"deterministic-{index}",
            )
            for index, _text in enumerate(texts)
        )


_EMBEDDING_TEXTS: Final = tuple(
    f"Benchmark article {index}\n\nFixed deterministic embedding input {index}."
    for index in range(64)
)


def _embedding_workload() -> dict[str, Any]:
    provider = _DeterministicEmbeddingProvider()
    results = embed_texts(
        provider,
        _EMBEDDING_TEXTS,
        model=provider.model_name,
        model_version=provider.model_version,
        batch_size=16,
    )
    return {
        "input_texts": len(_EMBEDDING_TEXTS),
        "vectors": len(results),
        "dimension": results[0].dimension,
        "provider_calls": len(provider.batch_sizes),
        "batch_sizes": provider.batch_sizes,
    }


def _cluster_vector(group: int) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSION
    values[group] = 1.0
    return tuple(values)


_CLUSTER_ITEMS: Final = tuple(
    ClusterItem(
        key=f"article-{index:02d}",
        vector=_cluster_vector(index // 6),
        content_hash=f"content-{index:02d}",
    )
    for index in range(24)
)


def _clustering_workload() -> dict[str, Any]:
    groups = cluster_by_similarity(_CLUSTER_ITEMS)
    return {
        "input_items": len(_CLUSTER_ITEMS),
        "pair_comparisons": len(_CLUSTER_ITEMS) * (len(_CLUSTER_ITEMS) - 1) // 2,
        "cluster_count": len(groups),
        "cluster_sizes": [len(group) for group in groups],
    }


class _Scalars:
    def __init__(self, values: Sequence[uuid.UUID]) -> None:
        self._values = tuple(values)

    def all(self) -> list[uuid.UUID]:
        return list(self._values)


class _Session:
    def __init__(self, source_ids: Sequence[uuid.UUID]) -> None:
        self._source_ids = tuple(source_ids)

    def scalars(self, _statement: object) -> _Scalars:
        return _Scalars(self._source_ids)

    def close(self) -> None:
        return None


class _Task:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = dict(payload)
        self.calls = 0

    def run(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        self.calls += 1
        return dict(self._payload)


_SOURCE_IDS: Final = tuple(uuid.UUID(int=index + 1) for index in range(3))
_EVENT_IDS: Final = tuple(str(uuid.UUID(int=100 + index)) for index in range(4))


def _pipeline_adapter_workload() -> dict[str, Any]:
    tasks = {
        "ingestion": _Task({"status": "ok", "inserted": 1}),
        "article_embeddings": _Task({"status": "ok", "articles_embedded": 64}),
        "clustering": _Task({"status": "ok", "event_ids": list(_EVENT_IDS)}),
        "entity_linking": _Task({"status": "ok", "links_persisted": 1}),
        "event_embeddings": _Task({"status": "ok", "events_embedded": 4}),
        "analogies": _Task({"status": "ok", "matches_persisted": 1}),
        "daily_brief": _Task({"status": "published", "report_id": "benchmark-report"}),
    }
    stages = ProductionPipelineStages(
        session_factory=lambda: _Session(_SOURCE_IDS),
        ingestion_task=tasks["ingestion"],
        article_embedding_task=tasks["article_embeddings"],
        clustering_task=tasks["clustering"],
        entity_linking_task=tasks["entity_linking"],
        event_embedding_task=tasks["event_embeddings"],
        analogy_task=tasks["analogies"],
        daily_brief_task=tasks["daily_brief"],
    )
    result = DailyPipelineCoordinator(stage_runners=stages.runners()).run(_PROCESS_DATE)
    if result.state is not PipelineState.SUCCEEDED:
        raise RuntimeError("production stage-adapter workload did not succeed")
    return {
        "pipeline_state": result.state.value,
        "stage_count": len(result.stages),
        "task_calls": {name: task.calls for name, task in tasks.items()},
        "total_task_calls": sum(task.calls for task in tasks.values()),
    }


_EXPECTED_STRUCTURES: Final[Mapping[str, Mapping[str, Any]]] = {
    "ingestion": {
        "input_items": 200,
        "new_items": 190,
        "duplicate_items": 10,
        "mapped_rows": 190,
    },
    "embedding": {
        "input_texts": 64,
        "vectors": 64,
        "dimension": EMBEDDING_DIMENSION,
        "provider_calls": 4,
        "batch_sizes": [16, 16, 16, 16],
    },
    "clustering": {
        "input_items": 24,
        "pair_comparisons": 276,
        "cluster_count": 4,
        "cluster_sizes": [6, 6, 6, 6],
    },
    "pipeline_adapters": {
        "pipeline_state": "succeeded",
        "stage_count": 7,
        "task_calls": {
            "ingestion": 3,
            "article_embeddings": 1,
            "clustering": 1,
            "entity_linking": 4,
            "event_embeddings": 1,
            "analogies": 4,
            "daily_brief": 1,
        },
        "total_task_calls": 15,
    },
}


def run_benchmark(*, iterations: int, warmup: int) -> dict[str, Any]:
    workloads = {
        "ingestion": (_ingestion_workload, 200),
        "embedding": (_embedding_workload, 64),
        "clustering": (_clustering_workload, 24),
        "pipeline_adapters": (_pipeline_adapter_workload, 15),
    }
    results: dict[str, Any] = {}
    structural_checks: list[dict[str, Any]] = []
    for name, (workload, units_per_run) in workloads.items():
        observed, timing = _measure(
            workload,
            iterations=iterations,
            warmup=warmup,
            units_per_run=units_per_run,
        )
        passed = observed == _EXPECTED_STRUCTURES[name]
        structural_checks.append({"workload": name, "passed": passed})
        results[name] = {"structure": observed, "timing": timing}
    return {
        "schema": SCHEMA,
        "workload_version": WORKLOAD_VERSION,
        "iterations": iterations,
        "warmup_iterations": warmup,
        "external_io": {
            "network": False,
            "database": False,
            "redis": False,
            "broker": False,
            "files_written": False,
        },
        "timing_gate_enforced": False,
        "structural_checks": structural_checks,
        "workloads": results,
        "decision": "pass" if all(check["passed"] for check in structural_checks) else "hold",
        "interpretation": (
            "Structural work amplification is gated; compare p50/p95 only on equivalent hosts. "
            "Production database/network latency requires an environment-specific SLO run."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_benchmark(iterations=args.iterations, warmup=args.warmup)
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
