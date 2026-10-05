"""Durable store, manual API, and Celery wiring for the daily pipeline."""

from __future__ import annotations

import datetime
import importlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from fastapi.testclient import TestClient

import apps.api.main as api_main
import apps.api.middleware as api_middleware
import apps.api.pipeline as pipeline_api
from db.base import Base
from db.models.core import Job
from db.models.pipeline import PipelineRunDetail
from packages.config import metrics
from packages.config.settings import Settings
from packages.jobs import JobState
from services.pipeline import (
    ORDERED_STAGES,
    DailyPipelineCoordinator,
    PipelineResultDeserializationError,
    PipelineRunResult,
    PipelineStage,
    PipelineState,
    StageContext,
    StageItemFailure,
    StageResult,
    daily_pipeline_identity,
    pipeline_run_result_from_dict,
)
from services.pipeline.sqlalchemy_store import (
    PIPELINE_JOB_TYPE,
    PIPELINE_QUEUE_LEASE,
    PIPELINE_RUNNING_LEASE,
    PipelineAlreadyRunningError,
    PipelineRetryConflictError,
    PipelineStaleDeliveryError,
    PipelineStoreError,
    SQLAlchemyPipelineLifecycleStore,
)
from workers import pipeline_tasks
from workers.celery_app import (
    PIPELINE_TASK_SOFT_TIME_LIMIT,
    PIPELINE_TASK_TIME_LIMIT,
    QUEUE_PIPELINE,
    Stage1Task,
    celery_app,
)

PROCESS_DATE = datetime.date(2026, 7, 29)
NOW = datetime.datetime(2026, 7, 29, 12, 0, tzinfo=datetime.UTC)


@dataclass
class MemoryDatabase:
    rows: dict[tuple[type[Any], Any], Any] = field(default_factory=dict)
    sessions: list[FakeSession] = field(default_factory=list)

    def session(self) -> FakeSession:
        session = FakeSession(self)
        self.sessions.append(session)
        return session


class FakeSession:
    def __init__(self, database: MemoryDatabase) -> None:
        self.database = database
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def get(
        self,
        model: type[Any],
        key: Any,
        *,
        with_for_update: bool = False,
    ) -> Any | None:
        del with_for_update
        return self.database.rows.get((model, key))

    def add(self, value: Any) -> None:
        if isinstance(value, Job):
            key = value.id
        elif isinstance(value, PipelineRunDetail):
            key = value.job_id
        else:
            raise AssertionError(f"unexpected fake row type {type(value).__name__}")
        self.database.rows[(type(value), key)] = value

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _store(database: MemoryDatabase | None = None) -> SQLAlchemyPipelineLifecycleStore:
    memory = database or MemoryDatabase()
    return SQLAlchemyPipelineLifecycleStore(
        session_factory=memory.session,
        now=lambda: NOW,
    )


def _runners(
    overrides: dict[PipelineStage, Callable[[StageContext], StageResult]] | None = None,
    calls: list[PipelineStage] | None = None,
) -> dict[PipelineStage, Callable[[StageContext], StageResult]]:
    def success(context: StageContext) -> StageResult:
        if calls is not None:
            calls.append(context.stage)
        return StageResult.succeeded(item_count=1, output={"stage": context.stage.value})

    runners = {stage: success for stage in ORDERED_STAGES}
    runners.update(overrides or {})
    return runners


def _result(
    *,
    state: PipelineState = PipelineState.SUCCEEDED,
) -> PipelineRunResult:
    if state is PipelineState.SUCCEEDED:
        runners = _runners()
    elif state is PipelineState.PARTIALLY_FAILED:

        def failed_entity(_context: StageContext) -> StageResult:
            return StageResult.failed(
                StageItemFailure(
                    item_key="__stage__",
                    code="entity_linking_failed",
                    message="identity index unavailable",
                    retryable=True,
                )
            )

        runners = _runners({PipelineStage.ENTITY_LINKING: failed_entity})
    else:

        def failed_clustering(_context: StageContext) -> StageResult:
            return StageResult.failed(
                StageItemFailure(
                    item_key="__stage__",
                    code="clustering_failed",
                    message="no vector space",
                    retryable=True,
                )
            )

        runners = _runners({PipelineStage.CLUSTERING: failed_clustering})
    result = DailyPipelineCoordinator(stage_runners=runners).run(PROCESS_DATE)
    assert result.state is state
    return result


def _rows(database: MemoryDatabase) -> tuple[Job, PipelineRunDetail]:
    identity = daily_pipeline_identity(PROCESS_DATE)
    return (
        database.rows[(Job, identity.process_id)],
        database.rows[(PipelineRunDetail, identity.process_id)],
    )


def test_pipeline_detail_model_and_migration_are_one_to_one_without_error_payload() -> None:
    table = Base.metadata.tables["pipeline_run_details"]
    assert {column.name for column in table.columns} == {
        "job_id",
        "process_date",
        "celery_task_id",
        "delivery_token",
        "run_token",
        "lease_expires_at",
        "event_ids",
        "result",
        "queued_at",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    }
    assert table.c.job_id.primary_key is True
    assert next(iter(table.c.job_id.foreign_keys)).target_fullname == "jobs.id"
    assert "error" not in table.columns

    migration = importlib.import_module("db.migrations.versions.0018_pipeline_run_lifecycle")
    assert migration.revision == "0018"
    assert migration.down_revision == "0017"


def test_queue_is_deterministic_and_duplicate_active_date_is_not_reenqueued() -> None:
    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)

    first = store.queue(identity)
    duplicate = store.queue(identity)
    job, detail = _rows(database)

    assert first.should_enqueue is True
    assert duplicate.should_enqueue is False
    assert duplicate.idempotent is True
    assert first.snapshot.job_id == identity.process_id
    assert first.snapshot.job_key == identity.process_key
    assert job.id == identity.process_id
    assert job.job_key == identity.process_key
    assert job.job_type == PIPELINE_JOB_TYPE
    assert job.state == JobState.QUEUED.value
    assert job.related_ids is None
    assert job.error is None
    assert detail.process_date == PROCESS_DATE
    assert detail.result is None
    assert len(database.rows) == 2


def test_succeeded_result_is_stored_in_detail_and_replayed_idempotently() -> None:
    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)
    terminal = _result()

    store.queue(identity)
    assert store.begin_or_get_succeeded(identity) is None
    store.save_terminal(terminal)
    replay = store.begin_or_get_succeeded(identity)
    decision = store.queue(identity)
    job, detail = _rows(database)

    assert replay == terminal
    assert decision.should_enqueue is False
    assert decision.snapshot.result == terminal.to_dict()
    assert job.state == JobState.SUCCEEDED.value
    assert job.error is None
    assert job.related_ids is None
    assert detail.result == terminal.to_dict()
    assert detail.started_at == NOW
    assert detail.completed_at == NOW


@pytest.mark.parametrize(
    ("terminal_state", "job_state", "error_code"),
    [
        (
            PipelineState.PARTIALLY_FAILED,
            JobState.PARTIALLY_FAILED,
            "pipeline_partially_failed",
        ),
        (PipelineState.FAILED, JobState.FAILED, "pipeline_failed"),
    ],
)
def test_failed_and_partial_terminal_results_use_job_error_and_detail_result(
    terminal_state: PipelineState,
    job_state: JobState,
    error_code: str,
) -> None:
    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)
    terminal = _result(state=terminal_state)

    store.queue(identity)
    store.begin_or_get_succeeded(identity)
    store.save_terminal(terminal)
    job, detail = _rows(database)

    assert job.state == job_state.value
    assert job.error["code"] == error_code
    assert "stages" in job.error["details"]
    assert job.related_ids is None
    assert detail.result == terminal.to_dict()
    assert "error" not in PipelineRunDetail.__table__.columns


def test_stored_succeeded_result_is_deserialized_fail_closed() -> None:
    payload = _result().to_dict()
    assert pipeline_run_result_from_dict(payload).to_dict() == payload

    payload["identity"]["process_id"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(
        PipelineResultDeserializationError,
        match="do not match process_date",
    ):
        pipeline_run_result_from_dict(payload)

    wrong_state = _result(state=PipelineState.FAILED).to_dict()
    wrong_state["state"] = "succeeded"
    with pytest.raises(
        PipelineResultDeserializationError,
        match="disagrees with its stage results",
    ):
        pipeline_run_result_from_dict(wrong_state)

    wrong_graph = _result().to_dict()
    wrong_graph["stages"][2]["required_dependencies"] = ["ingestion"]
    with pytest.raises(
        PipelineResultDeserializationError,
        match="dependencies drifted",
    ):
        pipeline_run_result_from_dict(wrong_graph)

    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)
    store.queue(identity)
    job, detail = _rows(database)
    job.state = JobState.SUCCEEDED.value
    detail.result = {"not": "a pipeline result"}
    with pytest.raises(PipelineResultDeserializationError):
        store.begin_or_get_succeeded(identity)


def test_running_job_refuses_a_second_concurrent_claim() -> None:
    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)
    store.queue(identity)
    assert store.begin_or_get_succeeded(identity) is None

    with pytest.raises(PipelineAlreadyRunningError, match="already running"):
        store.begin_or_get_succeeded(identity)

    job, detail = _rows(database)
    assert job.state == JobState.RUNNING.value
    assert detail.result is None


def test_abandoned_queue_delivery_is_bounded_and_reissued_with_a_new_token() -> None:
    database = MemoryDatabase()
    clock = [NOW]
    store = SQLAlchemyPipelineLifecycleStore(
        session_factory=database.session,
        now=lambda: clock[0],
    )
    identity = daily_pipeline_identity(PROCESS_DATE)

    first = store.queue(identity)
    duplicate = store.queue(identity)
    clock[0] = NOW + PIPELINE_QUEUE_LEASE + datetime.timedelta(seconds=1)
    recovered = store.queue(identity)

    assert first.should_enqueue is True
    assert duplicate.should_enqueue is False
    assert recovered.should_enqueue is True
    assert recovered.snapshot.attempt == 2
    assert recovered.delivery_token is not None
    assert recovered.delivery_token != first.delivery_token
    assert recovered.snapshot.result is None


def test_expired_running_lease_rotates_owner_and_rejects_late_terminal_write() -> None:
    database = MemoryDatabase()
    clock = [NOW]
    api_store = SQLAlchemyPipelineLifecycleStore(
        session_factory=database.session,
        now=lambda: clock[0],
    )
    identity = daily_pipeline_identity(PROCESS_DATE)
    queued = api_store.queue(identity)
    assert queued.delivery_token is not None

    first_worker = SQLAlchemyPipelineLifecycleStore(
        session_factory=database.session,
        now=lambda: clock[0],
        delivery_token=queued.delivery_token,
    )
    assert first_worker.begin_or_get_succeeded(identity) is None
    clock[0] = NOW + PIPELINE_RUNNING_LEASE + datetime.timedelta(seconds=1)
    replacement_worker = SQLAlchemyPipelineLifecycleStore(
        session_factory=database.session,
        now=lambda: clock[0],
        delivery_token=queued.delivery_token,
    )
    assert replacement_worker.begin_or_get_succeeded(identity) is None

    with pytest.raises(PipelineStaleDeliveryError, match="terminal-write lease"):
        first_worker.save_terminal(_result())
    replacement_worker.save_terminal(_result())
    snapshot = replacement_worker.get_snapshot(identity)
    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.attempt == 2


def test_terminal_fault_stays_inspectable_until_explicit_retry_and_keeps_event_scope() -> None:
    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)
    queued = store.queue(identity)
    assert queued.delivery_token is not None
    assert store.begin_or_get_succeeded(identity) is None
    event_id = "00000000-0000-4000-8000-0000000000e1"
    store.add_event_ids(identity, (event_id,))
    terminal = _result(state=PipelineState.PARTIALLY_FAILED)
    store.save_terminal(terminal)

    inspected = store.queue(identity)
    assert inspected.should_enqueue is False
    assert inspected.snapshot.state is JobState.PARTIALLY_FAILED
    assert inspected.snapshot.result == terminal.to_dict()
    assert inspected.snapshot.event_ids == (uuid.UUID(event_id),)

    retried = store.retry(identity)
    assert retried.should_enqueue is True
    assert retried.snapshot.state is JobState.QUEUED
    assert retried.snapshot.attempt == 2
    assert retried.snapshot.result == terminal.to_dict()
    assert retried.snapshot.event_ids == (uuid.UUID(event_id),)

    with pytest.raises(PipelineRetryConflictError, match="not eligible"):
        store.retry(identity)


def test_late_task_error_cannot_erase_a_complete_partial_terminal_result() -> None:
    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)
    store.queue(identity)
    store.begin_or_get_succeeded(identity)
    terminal = _result(state=PipelineState.PARTIALLY_FAILED)
    store.save_terminal(terminal)

    snapshot = store.mark_failed(identity, RuntimeError("response backend unavailable"))

    assert snapshot.state is JobState.PARTIALLY_FAILED
    assert snapshot.result == terminal.to_dict()
    assert snapshot.error is not None
    assert snapshot.error["code"] == "pipeline_partially_failed"


def test_worker_task_is_registered_on_pipeline_queue_and_has_no_beat_schedule() -> None:
    task = celery_app.tasks[pipeline_tasks.TASK_NAME]
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}

    assert isinstance(task, Stage1Task)
    assert task.queue == QUEUE_PIPELINE
    assert pipeline_tasks.TASK_NAME not in scheduled


def test_running_lease_outlives_the_pipeline_task_hard_time_limit() -> None:
    # The RUNNING lease exists to hand an abandoned date to a replacement worker. If it could
    # expire while the original worker is still inside its own time budget, a healthy in-flight
    # run would be taken over mid-stage, so the two numbers must always move together.
    hard_limit = datetime.timedelta(seconds=PIPELINE_TASK_TIME_LIMIT)

    assert PIPELINE_RUNNING_LEASE > hard_limit
    assert datetime.timedelta(seconds=PIPELINE_TASK_SOFT_TIME_LIMIT) < hard_limit
    assert PIPELINE_QUEUE_LEASE < PIPELINE_RUNNING_LEASE


def test_soft_time_limit_raised_by_a_stage_runner_is_durably_failed_and_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Injected at the coordinator/runner boundary. The adapter-level case -- the limit landing
    # inside a task body, which is where a real run spends its wall time -- is covered by
    # tests/unit/test_pipeline_stages.py.
    database = MemoryDatabase()
    store = _store(database)
    calls: list[PipelineStage] = []

    def timing_out(context: StageContext) -> StageResult:
        calls.append(context.stage)
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(
        pipeline_tasks,
        "build_lifecycle_store",
        lambda _delivery_token=None: store,
    )
    monkeypatch.setattr(
        pipeline_tasks,
        "build_stage_runners",
        lambda _store: _runners({PipelineStage.CLUSTERING: timing_out}, calls=calls),
    )
    metrics.reset()

    with pytest.raises(SoftTimeLimitExceeded):
        pipeline_tasks.run_daily_pipeline_task(PROCESS_DATE.isoformat())

    # The signal is not absorbed as a clustering failure: no later stage starts, and the run is
    # recorded by the task's generic handler as a retryable failure rather than a terminal result.
    assert calls == [
        PipelineStage.INGESTION,
        PipelineStage.ARTICLE_EMBEDDINGS,
        PipelineStage.CLUSTERING,
    ]
    assert metrics.get(metrics.JOB_FAILURES) == 1
    job, detail = _rows(database)
    assert job.state == JobState.FAILED.value
    assert job.error is not None
    assert job.error["code"] == "pipeline_execution_error"
    assert job.error["retryable"] is True
    assert detail.result is None


def test_the_stage_adapter_is_built_with_the_coordinators_own_fatal_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One definition for both layers. The soft limit lands inside a task body far more often than
    # between two stages, so the adapter that invokes those bodies has to know the same types the
    # coordinator is given; naming them in only one place leaves the other free to swallow it.
    captured: dict[str, Any] = {}

    class _RecordingStages:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def runners(self) -> dict[PipelineStage, Callable[[StageContext], StageResult]]:
            return {}

    monkeypatch.setattr(pipeline_tasks, "ProductionPipelineStages", _RecordingStages)
    store = _store()

    assert pipeline_tasks.build_stage_runners(store) == {}
    assert captured["event_scope_store"] is store
    assert captured["fatal_exceptions"] == pipeline_tasks.PIPELINE_FATAL_EXCEPTIONS
    assert SoftTimeLimitExceeded in pipeline_tasks.PIPELINE_FATAL_EXCEPTIONS


def test_worker_runs_durable_lifecycle_and_completed_date_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = MemoryDatabase()
    store = _store(database)
    calls: list[PipelineStage] = []
    monkeypatch.setattr(
        pipeline_tasks,
        "build_lifecycle_store",
        lambda _delivery_token=None: store,
    )
    monkeypatch.setattr(
        pipeline_tasks,
        "build_stage_runners",
        lambda _store: _runners(calls=calls),
    )
    metrics.reset()

    first = pipeline_tasks.run_daily_pipeline_task(PROCESS_DATE.isoformat())
    calls_after_first = list(calls)
    second = pipeline_tasks.run_daily_pipeline_task(PROCESS_DATE.isoformat())

    assert first["status"] == "succeeded"
    assert first["state"] == "succeeded"
    assert first["job_id"] == str(daily_pipeline_identity(PROCESS_DATE).process_id)
    assert calls_after_first == list(ORDERED_STAGES)
    assert calls == calls_after_first
    assert second["idempotent"] is True
    assert metrics.get(metrics.JOB_STARTS) == 2
    assert metrics.get(metrics.JOB_SUCCESSES) == 2
    job, detail = _rows(database)
    assert job.state == JobState.SUCCEEDED.value
    assert job.error is None
    assert detail.result == first["result"]


class _AsyncResult:
    id = "celery-task-123"


def _api_client(
    monkeypatch: pytest.MonkeyPatch,
    store: SQLAlchemyPipelineLifecycleStore,
) -> TestClient:
    settings = Settings(app_env="prod", api_key="pipeline-secret")
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(api_middleware, "get_settings", lambda: settings)
    app = api_main.create_app()
    app.dependency_overrides[pipeline_api.get_pipeline_lifecycle_store] = lambda: store
    return TestClient(app, client=("127.0.0.1", 50000))


def test_manual_api_is_protected_enqueues_exact_queue_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = MemoryDatabase()
    store = _store(database)
    client = _api_client(monkeypatch, store)
    deliveries: list[dict[str, Any]] = []

    def enqueue(
        process_date: datetime.date,
        delivery_token: Any,
        celery_task_id: str,
    ) -> _AsyncResult:
        deliveries.append(
            {
                "process_date": process_date.isoformat(),
                "delivery_token": str(delivery_token),
                "celery_task_id": celery_task_id,
            }
        )
        return _AsyncResult()

    monkeypatch.setattr(pipeline_api, "enqueue_pipeline_task", enqueue)
    body = {"process_date": PROCESS_DATE.isoformat()}

    unauthorized = client.post("/api/v1/internal/jobs/process", json=body)
    first = client.post(
        "/api/v1/internal/jobs/process",
        json=body,
        headers={"X-API-Key": "pipeline-secret"},
    )
    duplicate = client.post(
        "/api/v1/internal/jobs/process",
        json=body,
        headers={"X-API-Key": "pipeline-secret"},
    )

    assert unauthorized.status_code == 401
    assert first.status_code == 202
    assert first.json() == {
        "job_id": str(daily_pipeline_identity(PROCESS_DATE).process_id),
        "job_key": daily_pipeline_identity(PROCESS_DATE).process_key,
        "process_date": PROCESS_DATE.isoformat(),
        "state": "queued",
        "attempt": 1,
        "celery_task_id": first.json()["celery_task_id"],
        "event_ids": [],
        "lease_expires_at": "2026-07-29T12:02:00Z",
        "enqueued": True,
        "idempotent": False,
        "result": None,
        "error": None,
    }
    assert duplicate.status_code == 202
    assert duplicate.json()["enqueued"] is False
    assert duplicate.json()["idempotent"] is True
    assert len(deliveries) == 1
    assert deliveries[0]["process_date"] == PROCESS_DATE.isoformat()
    assert deliveries[0]["celery_task_id"] == first.json()["celery_task_id"]
    assert deliveries[0]["delivery_token"]


def test_api_delivery_helper_uses_exact_pipeline_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deliveries: list[dict[str, Any]] = []

    class FakeTask:
        @staticmethod
        def apply_async(*, args: list[str], queue: str, task_id: str) -> _AsyncResult:
            deliveries.append({"args": args, "queue": queue, "task_id": task_id})
            return _AsyncResult()

    monkeypatch.setattr(pipeline_tasks, "run_daily_pipeline_task", FakeTask())
    delivery_token = uuid.uuid4()
    result = pipeline_api.enqueue_pipeline_task(
        PROCESS_DATE,
        delivery_token,
        "celery-task-123",
    )

    assert result.id == "celery-task-123"
    assert deliveries == [
        {
            "args": [PROCESS_DATE.isoformat(), str(delivery_token)],
            "queue": QUEUE_PIPELINE,
            "task_id": "celery-task-123",
        }
    ]


def test_manual_api_replays_succeeded_date_without_broker_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)
    terminal = _result()
    store.queue(identity)
    store.begin_or_get_succeeded(identity)
    store.save_terminal(terminal)
    client = _api_client(monkeypatch, store)
    deliveries: list[Any] = []
    monkeypatch.setattr(
        pipeline_api,
        "enqueue_pipeline_task",
        lambda process_date: deliveries.append(process_date),
    )

    response = client.post(
        "/api/v1/internal/jobs/process",
        json={"process_date": PROCESS_DATE.isoformat()},
        headers={"X-API-Key": "pipeline-secret"},
    )

    assert response.status_code == 202
    assert response.json()["state"] == "succeeded"
    assert response.json()["enqueued"] is False
    assert response.json()["idempotent"] is True
    assert response.json()["result"] == terminal.to_dict()
    assert response.json()["error"] is None
    assert deliveries == []


def test_status_is_protected_and_terminal_retry_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)
    store.queue(identity)
    store.begin_or_get_succeeded(identity)
    terminal = _result(state=PipelineState.PARTIALLY_FAILED)
    store.save_terminal(terminal)
    client = _api_client(monkeypatch, store)
    deliveries: list[tuple[datetime.date, Any, str]] = []

    def enqueue(
        process_date: datetime.date,
        delivery_token: Any,
        celery_task_id: str,
    ) -> _AsyncResult:
        deliveries.append((process_date, delivery_token, celery_task_id))
        return _AsyncResult()

    monkeypatch.setattr(pipeline_api, "enqueue_pipeline_task", enqueue)
    path = f"/api/v1/internal/jobs/process/{PROCESS_DATE.isoformat()}"

    assert client.get(path).status_code == 401
    inspected = client.get(path, headers={"X-API-Key": "pipeline-secret"})
    duplicate_trigger = client.post(
        "/api/v1/internal/jobs/process",
        json={"process_date": PROCESS_DATE.isoformat()},
        headers={"X-API-Key": "pipeline-secret"},
    )

    assert inspected.status_code == 200
    assert inspected.json()["state"] == "partially_failed"
    assert inspected.json()["result"] == terminal.to_dict()
    assert duplicate_trigger.status_code == 202
    assert duplicate_trigger.json()["enqueued"] is False
    assert duplicate_trigger.json()["result"] == terminal.to_dict()
    assert deliveries == []

    retried = client.post(
        f"{path}/retry",
        headers={"X-API-Key": "pipeline-secret"},
    )
    assert retried.status_code == 202
    assert retried.json()["state"] == "queued"
    assert retried.json()["attempt"] == 2
    assert retried.json()["result"] == terminal.to_dict()
    assert len(deliveries) == 1


def test_manual_api_rejects_a_future_process_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = MemoryDatabase()
    store = _store(database)
    client = _api_client(monkeypatch, store)
    monkeypatch.setattr(
        pipeline_api,
        "current_pipeline_date",
        lambda: PROCESS_DATE,
    )
    future = PROCESS_DATE + datetime.timedelta(days=1)

    response = client.post(
        "/api/v1/internal/jobs/process",
        json={"process_date": future.isoformat()},
        headers={"X-API-Key": "pipeline-secret"},
    )

    assert response.status_code == 422
    assert "is in the future" in response.json()["error"]["message"]
    assert database.rows == {}


def test_broker_failure_is_durably_failed_and_returns_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = MemoryDatabase()
    store = _store(database)
    client = _api_client(monkeypatch, store)

    def fail_delivery(
        _process_date: datetime.date,
        _delivery_token: Any,
        _celery_task_id: str,
    ) -> None:
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(
        pipeline_api,
        "enqueue_pipeline_task",
        fail_delivery,
    )
    response = client.post(
        "/api/v1/internal/jobs/process",
        json={"process_date": PROCESS_DATE.isoformat()},
        headers={"X-API-Key": "pipeline-secret"},
    )
    job, detail = _rows(database)

    assert response.status_code == 503
    assert response.json()["error"]["message"] == (
        "pipeline job could not be delivered to the worker"
    )
    assert job.state == JobState.FAILED.value
    assert job.error == {
        "code": "broker_enqueue_failed",
        "message": "broker unavailable",
        "retryable": True,
        "details": {"error_type": "ConnectionError"},
    }
    assert job.related_ids is None
    assert detail.result is None
    assert detail.completed_at == NOW


def test_store_refuses_mismatched_deterministic_rows() -> None:
    database = MemoryDatabase()
    store = _store(database)
    identity = daily_pipeline_identity(PROCESS_DATE)
    store.queue(identity)
    job, _detail = _rows(database)
    job.job_key = "wrong"

    with pytest.raises(PipelineStoreError, match="identity is inconsistent"):
        store.queue(identity)
