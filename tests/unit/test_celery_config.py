"""Celery configuration and no-op task tests (no live broker required)."""

from __future__ import annotations

from packages.config import metrics
from workers import pipeline_tasks, tasks
from workers.celery_app import (
    PIPELINE_TASK_SOFT_TIME_LIMIT,
    PIPELINE_TASK_TIME_LIMIT,
    QUEUE_DEFAULT,
    QUEUE_INGESTION,
    QUEUE_PIPELINE,
    TASK_MAX_RETRIES,
    TASK_RETRY_BACKOFF_MAX,
    Stage1Task,
    celery_app,
)


def test_broker_and_backend_configured() -> None:
    assert celery_app.conf.broker_url
    assert celery_app.conf.result_backend


def test_queue_names_declared() -> None:
    names = {q.name for q in celery_app.conf.task_queues}
    assert {QUEUE_DEFAULT, QUEUE_INGESTION, QUEUE_PIPELINE} <= names


def test_retry_backoff_and_time_limits() -> None:
    star = celery_app.conf.task_annotations["*"]
    assert star["max_retries"] == TASK_MAX_RETRIES
    assert star["retry_backoff"] is True
    assert star["retry_backoff_max"] == TASK_RETRY_BACKOFF_MAX
    assert celery_app.conf.task_soft_time_limit < celery_app.conf.task_time_limit


def test_only_the_daily_pipeline_task_overrides_the_global_time_limits() -> None:
    pipeline_task = celery_app.tasks[pipeline_tasks.TASK_NAME]
    sample_other_task = celery_app.tasks["workers.tasks.noop"]

    assert PIPELINE_TASK_SOFT_TIME_LIMIT < PIPELINE_TASK_TIME_LIMIT
    assert PIPELINE_TASK_SOFT_TIME_LIMIT > celery_app.conf.task_soft_time_limit
    assert pipeline_task.soft_time_limit == PIPELINE_TASK_SOFT_TIME_LIMIT
    assert pipeline_task.time_limit == PIPELINE_TASK_TIME_LIMIT
    # `None` is how a task says "inherit the fleet-wide ceiling"; the override must not leak.
    assert sample_other_task.soft_time_limit is None
    assert sample_other_task.time_limit is None


def test_noop_tasks_carry_retry_backoff_defaults() -> None:
    noop_task = celery_app.tasks["workers.tasks.noop"]
    heartbeat_task = celery_app.tasks["workers.tasks.scheduled_heartbeat"]
    for task in (noop_task, heartbeat_task):
        assert isinstance(task, Stage1Task)
        assert task.max_retries == TASK_MAX_RETRIES
        assert task.retry_backoff is True
        assert task.retry_backoff_max == TASK_RETRY_BACKOFF_MAX
        assert task.retry_jitter is True


def test_beat_owns_noop_schedule_only() -> None:
    schedule = celery_app.conf.beat_schedule
    assert "noop-heartbeat" in schedule
    assert schedule["noop-heartbeat"]["task"] == "workers.tasks.scheduled_heartbeat"


def test_noop_tasks_registered() -> None:
    assert "workers.tasks.noop" in celery_app.tasks
    assert "workers.tasks.scheduled_heartbeat" in celery_app.tasks


def test_noop_runs_and_records_metrics() -> None:
    metrics.reset()
    result = tasks.noop({"hello": "world"})
    assert result["status"] == "ok"
    assert result["state"] == "succeeded"
    assert result["job_id"]
    assert result["job_key"].startswith("workers.tasks.noop:")
    assert metrics.get(metrics.JOB_STARTS) == 1
    assert metrics.get(metrics.JOB_SUCCESSES) == 1
    assert metrics.get(metrics.JOB_FAILURES) == 0


def test_scheduled_heartbeat_runs_with_job_contract() -> None:
    metrics.reset()
    result = tasks.scheduled_heartbeat()
    assert result["status"] == "ok"
    assert result["state"] == "succeeded"
    assert result["job_id"]
    assert result["job_key"].startswith("workers.tasks.scheduled_heartbeat:")
