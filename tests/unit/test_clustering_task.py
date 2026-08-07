"""The independently callable Celery boundary around article clustering."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from packages.config import metrics
from services.nlp.cluster_service import ClusterResult
from services.nlp.clustering import DEFAULT_CLUSTERING_THRESHOLD
from workers import clustering_tasks
from workers.celery_app import QUEUE_PIPELINE, Stage1Task, celery_app

MODEL = "configured-embedding-model"
MODEL_VERSION = "configured-version"


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: ClusterResult | None = None,
    failure: Exception | None = None,
) -> tuple[FakeSession, list[dict[str, Any]]]:
    session = FakeSession()
    calls: list[dict[str, Any]] = []
    successful_result = result if result is not None else ClusterResult(2, 5, 2)

    def _resolve(
        provider: object | None = None,
        embedding_model: str | None = None,
        embedding_model_version: str | None = None,
    ) -> tuple[str, str]:
        assert provider is None
        return embedding_model or MODEL, embedding_model_version or MODEL_VERSION

    def _cluster(session_arg: Any, **kwargs: Any) -> ClusterResult:
        calls.append({"session": session_arg, **kwargs})
        if failure is not None:
            raise failure
        return successful_result

    monkeypatch.setattr(clustering_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(clustering_tasks, "resolve_embedding_identity", _resolve)
    monkeypatch.setattr(clustering_tasks, "cluster_unclustered_articles", _cluster)
    metrics.reset()
    return session, calls


def test_task_is_registered_on_the_pipeline_queue_with_retry_defaults() -> None:
    task = celery_app.tasks[clustering_tasks.TASK_NAME]

    assert isinstance(task, Stage1Task)
    assert task.queue == QUEUE_PIPELINE
    assert task.max_retries == 3
    assert task.retry_backoff is True


def test_task_is_not_beat_scheduled() -> None:
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}

    assert clustering_tasks.TASK_NAME not in scheduled


def test_success_pins_the_vector_space_commits_once_and_returns_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, calls = _install(monkeypatch)

    payload = clustering_tasks.run_article_clustering()

    assert calls == [
        {
            "session": session,
            "threshold": DEFAULT_CLUSTERING_THRESHOLD,
            "embedding_model": MODEL,
            "embedding_model_version": MODEL_VERSION,
        }
    ]
    assert (session.commits, session.rollbacks) == (1, 0)
    assert session.closed is True
    assert payload["status"] == "ok"
    assert payload["state"] == "succeeded"
    assert payload["events_created"] == 2
    assert payload["articles_clustered"] == 5
    assert payload["features_emitted"] == 2
    assert payload["event_ids"] == []
    assert payload["embedding_model"] == MODEL
    assert payload["embedding_model_version"] == MODEL_VERSION
    assert metrics.get(metrics.JOB_STARTS) == 1
    assert metrics.get(metrics.JOB_SUCCESSES) == 1
    assert metrics.get(metrics.JOB_FAILURES) == 0


def test_explicit_parameters_reach_the_service_and_define_stable_job_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session, calls = _install(monkeypatch, result=ClusterResult(0, 0, 0))

    first = clustering_tasks.run_article_clustering(0.91, "model-x", "version-x")
    second = clustering_tasks.run_article_clustering(0.91, "model-x", "version-x")

    assert len(calls) == 2
    assert all(call["threshold"] == 0.91 for call in calls)
    assert all(call["embedding_model"] == "model-x" for call in calls)
    assert all(call["embedding_model_version"] == "version-x" for call in calls)
    assert first["job_id"] == second["job_id"]
    assert first["job_key"] == second["job_key"]
    assert first["events_created"] == second["events_created"] == 0


def test_failure_rolls_back_closes_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    session, _calls = _install(monkeypatch, failure=RuntimeError("clustering failed"))

    with pytest.raises(RuntimeError, match="clustering failed"):
        clustering_tasks.run_article_clustering()

    assert (session.commits, session.rollbacks) == (0, 1)
    assert session.closed is True
    assert metrics.get(metrics.JOB_STARTS) == 1
    assert metrics.get(metrics.JOB_SUCCESSES) == 0
    assert metrics.get(metrics.JOB_FAILURES) == 1


def test_pipeline_event_scope_is_unioned_inside_the_clustering_transaction() -> None:
    prior = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
    created = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
    job_id = uuid.UUID("00000000-0000-4000-8000-0000000000c3")
    run_token = uuid.UUID("00000000-0000-4000-8000-0000000000d4")
    detail = SimpleNamespace(event_ids=[str(prior)], run_token=run_token)
    calls: list[tuple[Any, Any, bool]] = []

    class ScopeSession:
        @staticmethod
        def get(model: Any, key: Any, *, with_for_update: bool = False) -> Any:
            calls.append((model, key, with_for_update))
            return detail

    clustering_tasks._persist_pipeline_event_scope(
        ScopeSession(),
        pipeline_job_id=job_id,
        event_ids=(created, prior),
        run_token=str(run_token),
    )

    assert calls == [(clustering_tasks.PipelineRunDetail, job_id, True)]
    assert detail.event_ids == [str(prior), str(created)]


def test_pipeline_event_scope_write_requires_the_owning_run_token() -> None:
    prior = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
    job_id = uuid.UUID("00000000-0000-4000-8000-0000000000c3")
    owner_token = uuid.UUID("00000000-0000-4000-8000-0000000000d4")
    stale_token = uuid.UUID("00000000-0000-4000-8000-0000000000e5")
    detail = SimpleNamespace(event_ids=[str(prior)], run_token=owner_token)

    class ScopeSession:
        @staticmethod
        def get(model: Any, key: Any, *, with_for_update: bool = False) -> Any:
            return detail

    with pytest.raises(RuntimeError, match="require the RUNNING-attempt run token"):
        clustering_tasks._persist_pipeline_event_scope(
            ScopeSession(),
            pipeline_job_id=job_id,
            event_ids=(),
            run_token=None,
        )

    with pytest.raises(RuntimeError, match="owned by another attempt"):
        clustering_tasks._persist_pipeline_event_scope(
            ScopeSession(),
            pipeline_job_id=job_id,
            event_ids=(),
            run_token=str(stale_token),
        )

    assert detail.event_ids == [str(prior)]
