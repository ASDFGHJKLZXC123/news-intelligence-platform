"""Celery tasks for the ADR 0004 embedding lifecycle: articles, events, historical episodes.

Three independently callable tasks on the pipeline queue rather than one chained task: an event
is embeddable only once clustering has produced it, an episode refresh is a curation/migration
concern, and a failure in any one of them must not be reported as a failure of the others.

Nothing is built, connected, or configured at import: the HTTP client and the session are created
inside the task. Each task owns its transaction -- one commit on success, one rollback on failure --
and the writes underneath are gap-filling inserts keyed by ``(subject, model, model_version)``, so
the Stage1Task retry that follows a provider fault re-derives the same rows instead of duplicating
them.

No Beat entry: neither ADR 0004 nor the episode spec schedules an embedding run, and a clock is
the wrong trigger for work whose input is "whatever ingestion or clustering just produced".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from celery import shared_task
from sqlalchemy.orm import Session

from db.base import SessionLocal
from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.config.settings import get_settings
from packages.jobs import Stage1Job
from packages.providers.openai_embeddings import MAX_EMBEDDING_BATCH_SIZE, OpenAIEmbeddingProvider
from services.nlp.embeddings import (
    build_embedding_provider,
    embed_unembedded_articles,
    embed_unembedded_events,
)
from services.nlp.episodes import refresh_episode_embeddings
from workers.celery_app import QUEUE_PIPELINE, Stage1Task

logger = get_logger("workers.embedding_tasks")

TASK_ARTICLE_EMBEDDING = "workers.embedding_tasks.run_article_embedding"
TASK_EVENT_EMBEDDING = "workers.embedding_tasks.run_event_embedding"
TASK_EPISODE_EMBEDDING_REFRESH = "workers.embedding_tasks.run_episode_embedding_refresh"


def build_provider() -> OpenAIEmbeddingProvider:
    """The production adapter, built per task run. Unit tests replace this hook with a fake."""
    return build_embedding_provider(get_settings())


def _run(
    task_name: str,
    count_key: str,
    work: Callable[[Session, OpenAIEmbeddingProvider], int],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Job contract, one transaction, and guaranteed cleanup of both the session and the client."""
    job = Stage1Job.create(task_name, payload).mark_running()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)

    session = SessionLocal()
    provider: OpenAIEmbeddingProvider | None = None
    try:
        provider = build_provider()
        count = work(session, provider)
        session.commit()
        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        logger.info("embedding task completed", extra={"task": task_name, count_key: count})
        return {
            "status": "ok",
            "job_id": completed.job_id,
            "job_key": completed.job_key,
            "state": completed.state.value,
            count_key: count,
        }
    except Exception:
        # Includes a provider fault raised mid-batch: nothing is left half-written, and the
        # Stage1Task retry re-selects whatever is still missing a vector.
        session.rollback()
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("embedding task failed", extra={"task": task_name})
        raise
    finally:
        if provider is not None:
            provider.close()
        session.close()
        set_job_id(None)


@shared_task(name=TASK_ARTICLE_EMBEDDING, base=Stage1Task, queue=QUEUE_PIPELINE)
def run_article_embedding(
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE, limit: int | None = None
) -> dict[str, Any]:
    """Embed every article missing a vector in the configured model space."""
    return _run(
        TASK_ARTICLE_EMBEDDING,
        "articles_embedded",
        lambda session, provider: embed_unembedded_articles(
            session, provider, batch_size=batch_size, limit=limit
        ),
        {"batch_size": batch_size, "limit": limit},
    )


@shared_task(name=TASK_EVENT_EMBEDDING, base=Stage1Task, queue=QUEUE_PIPELINE)
def run_event_embedding(
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE, limit: int | None = None
) -> dict[str, Any]:
    """Embed every event missing a vector in the configured model space."""
    return _run(
        TASK_EVENT_EMBEDDING,
        "events_embedded",
        lambda session, provider: embed_unembedded_events(
            session, provider, batch_size=batch_size, limit=limit
        ),
        {"batch_size": batch_size, "limit": limit},
    )


@shared_task(name=TASK_EPISODE_EMBEDDING_REFRESH, base=Stage1Task, queue=QUEUE_PIPELINE)
def run_episode_embedding_refresh(
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE, limit: int | None = None
) -> dict[str, Any]:
    """Carry curated episodes whose onset vector sits outside the configured space across to it."""
    return _run(
        TASK_EPISODE_EMBEDDING_REFRESH,
        "episodes_embedded",
        lambda session, provider: refresh_episode_embeddings(
            session, provider, batch_size=batch_size, limit=limit
        ),
        {"batch_size": batch_size, "limit": limit},
    )
