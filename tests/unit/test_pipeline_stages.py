"""Adapters from existing task bodies into the pure daily-pipeline stage contract."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from services.pipeline import (
    DailyPipelineCoordinator,
    PipelineConfigurationError,
    PipelineIdentity,
    PipelineRunResult,
    PipelineStage,
    PipelineState,
    StageStatus,
)
from workers.pipeline_stages import ProductionPipelineStages, _run_one
from workers.pipeline_tasks import PIPELINE_FATAL_EXCEPTIONS

PROCESS_DATE = datetime.date(2026, 7, 29)
EVENT_A = str(uuid.UUID("00000000-0000-0000-0000-0000000000a1"))
EVENT_B = str(uuid.UUID("00000000-0000-0000-0000-0000000000b2"))


class WorkerShutdown(Exception):
    """Stand-in for a runtime teardown signal such as Celery's ``SoftTimeLimitExceeded``."""


class FakeTask:
    def __init__(
        self,
        payload: Mapping[str, Any] | None = None,
        failures: Mapping[str, Exception] | None = None,
    ) -> None:
        self.payload = dict(payload or {"status": "ok"})
        self.failures = dict(failures or {})
        self.calls: list[tuple[Any, ...]] = []

    def run(self, *args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(args)
        key = str(args[0]) if args else "__stage__"
        if key in self.failures:
            raise self.failures[key]
        return dict(self.payload)


class FakeScalars:
    def __init__(self, values: list[uuid.UUID]) -> None:
        self._values = values

    def all(self) -> list[uuid.UUID]:
        return self._values


class FakeSession:
    def __init__(self, source_ids: list[uuid.UUID]) -> None:
        self.source_ids = source_ids
        self.closed = False

    def scalars(self, _statement: Any) -> FakeScalars:
        return FakeScalars(self.source_ids)

    def close(self) -> None:
        self.closed = True


class MemoryEventScope:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, ...]] = {}
        self.active_run_token: uuid.UUID | None = None

    def add_event_ids(
        self,
        identity: PipelineIdentity,
        event_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        combined = tuple(sorted({*self.event_ids(identity), *event_ids}))
        self.values[identity.process_key] = combined
        return combined

    def event_ids(self, identity: PipelineIdentity) -> tuple[str, ...]:
        return self.values.get(identity.process_key, ())


class RecordingStore:
    """Lifecycle double that records whether a terminal result was ever written."""

    def __init__(self) -> None:
        self.saved: list[PipelineRunResult] = []

    def begin_or_get_succeeded(self, identity: PipelineIdentity) -> PipelineRunResult | None:
        del identity
        return None

    def save_terminal(self, result: PipelineRunResult) -> None:
        self.saved.append(result)


def _stages(
    *,
    source_ids: list[uuid.UUID] | None = None,
    ingestion: FakeTask | None = None,
    entity: FakeTask | None = None,
    analogy: FakeTask | None = None,
    brief_status: str = "published",
    event_scope_store: MemoryEventScope | None = None,
    fatal_exceptions: tuple[type[BaseException], ...] = (),
) -> tuple[ProductionPipelineStages, dict[str, FakeTask], FakeSession]:
    session = FakeSession(source_ids or [])
    tasks = {
        "ingestion": ingestion or FakeTask({"status": "ok", "inserted": 1}),
        "article": FakeTask({"status": "ok", "articles_embedded": 2}),
        "clustering": FakeTask(
            {
                "status": "ok",
                "events_created": 2,
                "event_ids": [EVENT_A, EVENT_B],
            }
        ),
        "entity": entity or FakeTask({"status": "ok", "links_persisted": 1}),
        "event": FakeTask({"status": "ok", "events_embedded": 2}),
        "analogy": analogy or FakeTask({"status": "ok", "matches_persisted": 1}),
        "brief": FakeTask({"status": brief_status, "report_id": "report-1"}),
    }
    stages = ProductionPipelineStages(
        session_factory=lambda: session,
        ingestion_task=tasks["ingestion"],
        article_embedding_task=tasks["article"],
        clustering_task=tasks["clustering"],
        entity_linking_task=tasks["entity"],
        event_embedding_task=tasks["event"],
        analogy_task=tasks["analogy"],
        daily_brief_task=tasks["brief"],
        event_scope_store=event_scope_store,
        fatal_exceptions=fatal_exceptions,
    )
    return stages, tasks, session


def test_production_adapters_run_existing_tasks_in_pipeline_order() -> None:
    source_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
    source_b = uuid.UUID("00000000-0000-0000-0000-000000000002")
    stages, tasks, session = _stages(source_ids=[source_a, source_b])

    result = DailyPipelineCoordinator(stage_runners=stages.runners()).run(PROCESS_DATE)

    assert result.state is PipelineState.SUCCEEDED
    assert tasks["ingestion"].calls == [(str(source_a),), (str(source_b),)]
    assert tasks["article"].calls == [()]
    assert tasks["clustering"].calls == [()]
    assert tasks["entity"].calls == [(EVENT_A,), (EVENT_B,)]
    assert tasks["event"].calls == [()]
    assert tasks["analogy"].calls == [(EVENT_A,), (EVENT_B,)]
    assert tasks["brief"].calls == [("2026-07-29",)]
    assert session.closed is True


def test_one_entity_failure_is_partial_and_does_not_block_analogy_or_brief() -> None:
    entity = FakeTask(
        {"status": "ok"},
        failures={EVENT_B: RuntimeError("entity provider unavailable")},
    )
    stages, tasks, _session = _stages(entity=entity)

    result = DailyPipelineCoordinator(stage_runners=stages.runners()).run(PROCESS_DATE)

    assert result.state is PipelineState.PARTIALLY_FAILED
    entity_result = result.execution_for(PipelineStage.ENTITY_LINKING).result
    assert entity_result.status is StageStatus.PARTIALLY_FAILED
    assert entity_result.succeeded_count == 1
    assert entity_result.failures[0].item_key == EVENT_B
    assert tasks["analogy"].calls == [(EVENT_A,), (EVENT_B,)]
    assert tasks["brief"].calls == [("2026-07-29",)]


def test_all_analogy_failures_are_recorded_but_descriptive_brief_still_runs() -> None:
    analogy = FakeTask(
        failures={
            EVENT_A: RuntimeError("reranker unavailable"),
            EVENT_B: RuntimeError("reranker unavailable"),
        }
    )
    stages, tasks, _session = _stages(analogy=analogy)

    result = DailyPipelineCoordinator(stage_runners=stages.runners()).run(PROCESS_DATE)

    assert result.state is PipelineState.PARTIALLY_FAILED
    assert result.execution_for(PipelineStage.ANALOGIES).result.status is StageStatus.FAILED
    assert tasks["brief"].calls == [("2026-07-29",)]


def test_blocked_daily_brief_makes_pipeline_failed_not_succeeded() -> None:
    stages, _tasks, _session = _stages(brief_status="blocked")

    result = DailyPipelineCoordinator(stage_runners=stages.runners()).run(PROCESS_DATE)

    brief = result.execution_for(PipelineStage.DAILY_BRIEF).result
    assert brief.status is StageStatus.FAILED
    assert brief.failures[0].code == "daily_brief_not_published"
    assert result.state is PipelineState.FAILED


def test_no_new_events_is_a_successful_zero_item_fanout() -> None:
    stages, tasks, _session = _stages()
    tasks["clustering"].payload["event_ids"] = []

    result = DailyPipelineCoordinator(stage_runners=stages.runners()).run(PROCESS_DATE)

    assert result.state is PipelineState.SUCCEEDED
    assert tasks["entity"].calls == []
    assert tasks["analogy"].calls == []
    assert result.execution_for(PipelineStage.ENTITY_LINKING).result.attempted_count == 0


def test_retry_reloads_prior_event_scope_when_clustering_creates_nothing() -> None:
    scope = MemoryEventScope()
    first_entity = FakeTask(
        {"status": "ok"},
        failures={EVENT_B: RuntimeError("entity provider unavailable")},
    )
    first_stages, _first_tasks, _session = _stages(
        entity=first_entity,
        event_scope_store=scope,
    )
    first = DailyPipelineCoordinator(stage_runners=first_stages.runners()).run(PROCESS_DATE)
    assert first.state is PipelineState.PARTIALLY_FAILED
    assert scope.event_ids(first.identity) == (EVENT_A, EVENT_B)

    second_stages, second_tasks, _session = _stages(event_scope_store=scope)
    second_tasks["clustering"].payload["event_ids"] = []
    second = DailyPipelineCoordinator(stage_runners=second_stages.runners()).run(PROCESS_DATE)

    assert second.state is PipelineState.SUCCEEDED
    assert second_tasks["entity"].calls == [(EVENT_A,), (EVENT_B,)]
    assert second_tasks["analogy"].calls == [(EVENT_A,), (EVENT_B,)]


# --- Runtime teardown inside a task body ------------------------------------------------
# Practically all of a real run's wall time is spent inside `task.run`, so a Celery soft time
# limit lands *here* rather than between two stages. These adapters must therefore re-raise the
# declared fatal types instead of laundering them into per-item pipeline state.
def test_a_fatal_task_exception_propagates_out_of_run_one() -> None:
    task = FakeTask(failures={EVENT_A: WorkerShutdown("soft time limit exceeded")})

    with pytest.raises(WorkerShutdown, match="soft time limit"):
        _run_one(task, EVENT_A, (WorkerShutdown,), EVENT_A)


def test_a_fatal_nested_with_cleanup_failures_propagates_out_of_run_one() -> None:
    grouped = ExceptionGroup(
        "task and cleanup both failed",
        [
            RuntimeError("rollback failed"),
            ExceptionGroup("cleanup", [SoftTimeLimitExceeded(), RuntimeError("close failed")]),
        ],
    )
    task = FakeTask(failures={EVENT_A: grouped})

    with pytest.raises(ExceptionGroup) as captured:
        _run_one(task, EVENT_A, PIPELINE_FATAL_EXCEPTIONS, EVENT_A)

    assert captured.value is grouped


def test_an_ordinary_task_exception_is_still_an_item_failure_with_fatal_types_set() -> None:
    task = FakeTask(failures={EVENT_A: RuntimeError("entity provider unavailable")})

    payload, failure = _run_one(task, EVENT_A, (WorkerShutdown,), EVENT_A)

    assert payload is None
    assert failure is not None
    assert failure.item_key == EVENT_A
    assert failure.code == "task_failed"
    assert failure.error_type == "RuntimeError"


def test_without_declared_fatal_types_every_task_exception_stays_an_item_failure() -> None:
    # The default is an empty tuple: callers that never opt in keep the pre-existing behaviour.
    task = FakeTask(failures={EVENT_A: WorkerShutdown("soft time limit exceeded")})

    payload, failure = _run_one(task, EVENT_A, (), EVENT_A)

    assert payload is None
    assert failure is not None
    assert failure.code == "task_failed"
    assert failure.error_type == "WorkerShutdown"


def test_a_fatal_task_exception_aborts_the_fanout_and_leaves_later_items_unattempted() -> None:
    entity = FakeTask(failures={EVENT_A: WorkerShutdown("soft time limit exceeded")})
    stages, tasks, _session = _stages(entity=entity, fatal_exceptions=(WorkerShutdown,))
    store = RecordingStore()

    with pytest.raises(WorkerShutdown, match="soft time limit"):
        DailyPipelineCoordinator(
            stage_runners=stages.runners(),
            lifecycle_store=store,
            fatal_exceptions=(WorkerShutdown,),
        ).run(PROCESS_DATE)

    # The loop stopped at the item that raised -- EVENT_B was never attempted -- and no later
    # stage started. Nothing was recorded as a stage failure or a terminal result: the run did
    # not end, it was cut off, and the task's own handler owns that disposition.
    assert entity.calls == [(EVENT_A,)]
    assert tasks["analogy"].calls == []
    assert tasks["brief"].calls == []
    assert store.saved == []


def test_a_fatal_exception_late_in_a_fanout_is_not_laundered_into_a_partial_result() -> None:
    entity = FakeTask(failures={EVENT_B: WorkerShutdown("soft time limit exceeded")})
    stages, _tasks, _session = _stages(entity=entity, fatal_exceptions=(WorkerShutdown,))
    store = RecordingStore()

    with pytest.raises(WorkerShutdown, match="soft time limit"):
        DailyPipelineCoordinator(
            stage_runners=stages.runners(),
            lifecycle_store=store,
            fatal_exceptions=(WorkerShutdown,),
        ).run(PROCESS_DATE)

    assert entity.calls == [(EVENT_A,), (EVENT_B,)]
    assert store.saved == []  # not a `partially_failed` stage with one success


def test_a_fatal_task_exception_in_a_single_item_stage_is_not_a_stage_failure() -> None:
    stages, tasks, _session = _stages(fatal_exceptions=(WorkerShutdown,))
    tasks["clustering"].failures["__stage__"] = WorkerShutdown("soft time limit exceeded")
    store = RecordingStore()

    with pytest.raises(WorkerShutdown, match="soft time limit"):
        DailyPipelineCoordinator(
            stage_runners=stages.runners(),
            lifecycle_store=store,
            fatal_exceptions=(WorkerShutdown,),
        ).run(PROCESS_DATE)

    # Without the re-raise this returns normally with clustering marked failed, every dependent
    # stage skipped, and a terminal `failed` result written for a worker that is about to be
    # SIGKILLed anyway.
    assert tasks["entity"].calls == []
    assert store.saved == []


def test_the_production_fatal_set_stops_a_fanout_on_a_celery_soft_time_limit() -> None:
    # The same wiring `workers.pipeline_tasks` uses, with the real Celery exception type.
    entity = FakeTask(failures={EVENT_A: SoftTimeLimitExceeded()})
    stages, tasks, _session = _stages(entity=entity, fatal_exceptions=PIPELINE_FATAL_EXCEPTIONS)

    with pytest.raises(SoftTimeLimitExceeded):
        DailyPipelineCoordinator(
            stage_runners=stages.runners(),
            fatal_exceptions=PIPELINE_FATAL_EXCEPTIONS,
        ).run(PROCESS_DATE)

    assert entity.calls == [(EVENT_A,)]
    assert tasks["brief"].calls == []


def test_the_stage_adapter_rejects_fatal_types_that_are_not_exception_classes() -> None:
    # Validated at construction with the coordinator's own normalizer, so a misconfiguration
    # cannot surface later as a `TypeError` from the `except` clause, i.e. as a fake item failure.
    not_a_class: Any = ("SoftTimeLimitExceeded",)
    not_a_tuple: Any = SoftTimeLimitExceeded

    with pytest.raises(PipelineConfigurationError, match="not an exception class"):
        ProductionPipelineStages(fatal_exceptions=not_a_class)

    with pytest.raises(PipelineConfigurationError, match="must be a tuple"):
        ProductionPipelineStages(fatal_exceptions=not_a_tuple)
