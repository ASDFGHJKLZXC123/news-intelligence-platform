"""Celery task for clustering embedded articles into events.

The task is independently callable and belongs on the pipeline queue, but it is not clock
scheduled: article embedding is the event that makes clustering useful. It pins one configured
embedding vector space before creating the job identity, then delegates all selection and
persistence to :func:`services.nlp.cluster_service.cluster_unclustered_articles`.

The service selects only articles that do not yet belong to an event. Together with this task's
single transaction, that makes successful reruns no-ops and failed attempts rollback-safe under
the retry/backoff policy inherited from :class:`workers.celery_app.Stage1Task`.
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task

from db.base import SessionLocal
from db.models.pipeline import PipelineRunDetail
from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.jobs import Stage1Job
from services.nlp.cluster_service import ClusterResult, cluster_unclustered_articles
from services.nlp.clustering import DEFAULT_CLUSTERING_THRESHOLD
from services.nlp.embeddings import resolve_embedding_identity
from services.writer_mode import require_legacy_writer_mode
from workers.celery_app import QUEUE_PIPELINE, Stage1Task

logger = get_logger("workers.clustering_tasks")

TASK_NAME = "workers.clustering_tasks.run_article_clustering"


@shared_task(name=TASK_NAME, base=Stage1Task, queue=QUEUE_PIPELINE)
def run_article_clustering(
    threshold: float = DEFAULT_CLUSTERING_THRESHOLD,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
    pipeline_job_id: str | None = None,
    pipeline_run_token: str | None = None,
) -> dict[str, Any]:
    """Cluster every unclustered article in one explicitly pinned embedding space."""

    model, model_version = resolve_embedding_identity(
        embedding_model=embedding_model,
        embedding_model_version=embedding_model_version,
    )
    payload = {
        "threshold": threshold,
        "embedding_model": model,
        "embedding_model_version": model_version,
    }
    job = Stage1Job.create(TASK_NAME, payload).mark_running()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)

    session = SessionLocal()
    try:
        require_legacy_writer_mode(session, lock=True)
        result: ClusterResult = cluster_unclustered_articles(
            session,
            threshold=threshold,
            embedding_model=model,
            embedding_model_version=model_version,
        )
        if pipeline_job_id is not None:
            _persist_pipeline_event_scope(
                session,
                pipeline_job_id=uuid.UUID(pipeline_job_id),
                event_ids=result.event_ids,
                run_token=pipeline_run_token,
            )
        session.commit()
        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        logger.info(
            "article clustering completed",
            extra={
                "events_created": result.events_created,
                "articles_clustered": result.articles_clustered,
                "features_emitted": result.features_emitted,
                **payload,
            },
        )
        return {
            "status": "ok",
            "job_id": completed.job_id,
            "job_key": completed.job_key,
            "state": completed.state.value,
            "events_created": result.events_created,
            "articles_clustered": result.articles_clustered,
            "features_emitted": result.features_emitted,
            "event_ids": [str(event_id) for event_id in result.event_ids],
            **payload,
        }
    except Exception:
        session.rollback()
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("article clustering failed", extra=payload)
        raise
    finally:
        session.close()
        set_job_id(None)


def _persist_pipeline_event_scope(
    session: Any,
    *,
    pipeline_job_id: uuid.UUID,
    event_ids: tuple[uuid.UUID, ...],
    run_token: str | None,
) -> None:
    """Union created events into the coordinator sidecar in the clustering transaction.

    The write is guarded by the caller's RUNNING-attempt ``run_token`` so a superseded
    (zombie) worker cannot mutate the event scope of a date whose lease a newer attempt now
    owns; on mismatch the whole clustering transaction rolls back with it.
    """

    if run_token is None:
        raise RuntimeError("pipeline event scope writes require the RUNNING-attempt run token")
    detail = session.get(PipelineRunDetail, pipeline_job_id, with_for_update=True)
    if detail is None:
        raise RuntimeError(f"pipeline lifecycle detail {pipeline_job_id} does not exist")
    if detail.run_token is None or str(detail.run_token) != run_token:
        raise RuntimeError(
            "pipeline event scope write refused: the RUNNING lease is owned by another attempt"
        )
    existing = detail.event_ids
    if existing is None:
        existing = []
    if not isinstance(existing, list) or not all(isinstance(value, str) for value in existing):
        raise RuntimeError("pipeline lifecycle event_ids are malformed")
    detail.event_ids = sorted({*existing, *(str(event_id) for event_id in event_ids)})
