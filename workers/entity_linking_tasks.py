"""Celery task for ADR 0005 entity linking over one event.

Independently callable by ``event_id`` and routed to the pipeline queue. There is deliberately
no Beat entry: the ADR schedules nothing here, and clustering is what decides when an event has
articles worth linking, so the caller is whatever produced the event -- not a clock.

Nothing is loaded, connected, or configured at import. The spaCy pipeline is loaded on the first
article batch that has text, the LLM providers and the Redis cache are built inside the task, and
the session is opened there too. Unit tests replace ``SessionLocal`` and the two build hooks with
fakes, so importing this module costs nothing and reaches nothing.

The task owns the transaction: one commit on success, one rollback on failure. Every write the
service makes is an idempotent upsert keyed by the mention or the (event, entity) pair, so the
Stage1Task retry that follows a provider failure re-derives exactly the same rows rather than
duplicating them.
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task

from db.base import SessionLocal
from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.config.settings import get_settings
from packages.jobs import Stage1Job
from services.entities.adjudication import MentionAdjudicator
from services.entities.event_linking import (
    EventLinkingResult,
    MentionExtractor,
    link_event_entities,
)
from services.llm.runtime import build_production_orchestrator
from services.nlp.mentions import ArticleText, extract_mentions
from workers.celery_app import QUEUE_PIPELINE, Stage1Task

logger = get_logger("workers.entity_linking_tasks")

TASK_NAME = "workers.entity_linking_tasks.run_event_entity_linking"


def build_mention_extractor() -> MentionExtractor:
    """The real stage-1 extractor: one batched ``nlp.pipe`` call, model loaded on first use."""

    def extract(articles: tuple[ArticleText, ...]) -> Any:
        return extract_mentions(articles)

    return extract


def build_mention_adjudicator(session: Any) -> MentionAdjudicator:
    """The real stage-3 adjudicator, on the settings-backed orchestrator (providers, cache, limiter).

    It shares this task's session and does *not* commit on its own: one event is one transaction,
    so a provider failure on the fourth mention cannot leave the first three committed while the
    task reports a failure and retries the whole event.
    """

    return MentionAdjudicator(
        build_production_orchestrator(
            settings=get_settings(),
            session=session,
            redis_client=_redis_client(),
            commit_on_write=False,
        )
    )


def _redis_client() -> Any:
    # Imported and constructed here, never at module import: `from_url` builds a lazy connection
    # pool, so no socket is opened until the orchestrator actually reads the prompt cache.
    import redis

    return redis.Redis.from_url(get_settings().redis_url)


@shared_task(name=TASK_NAME, base=Stage1Task, queue=QUEUE_PIPELINE)
def run_event_entity_linking(event_id: str) -> dict[str, Any]:
    """Extract, link, adjudicate, and persist the entities of one event (ADR 0005)."""

    identifier = uuid.UUID(str(event_id))
    job = Stage1Job.create(TASK_NAME, {"event_id": str(identifier)}).mark_running()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)

    session = SessionLocal()
    try:
        result = link_event_entities(
            session,
            identifier,
            extractor=build_mention_extractor(),
            adjudicator=build_mention_adjudicator(session),
        )
        session.commit()
        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        logger.info("event entity linking completed", extra=_log_fields(result))
        return {
            "status": "ok",
            "job_id": completed.job_id,
            "job_key": completed.job_key,
            "state": completed.state.value,
            **result.as_dict(),
        }
    except Exception:
        # Includes an infrastructure/provider failure raised out of the adjudicator: the event is
        # left exactly as it was found, and Stage1Task retries with backoff. A mention is never
        # reported as linked because the call that would have linked it did not happen.
        session.rollback()
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("event entity linking failed", extra={"event_id": str(identifier)})
        raise
    finally:
        session.close()
        set_job_id(None)


def _log_fields(result: EventLinkingResult) -> dict[str, Any]:
    return {
        "event_id": str(result.event_id),
        "articles_processed": result.articles_processed,
        "articles_skipped": len(result.skipped_articles),
        "mentions_total": len(result.mentions),
        "mentions_accepted": result.accepted_count,
        "mentions_adjudicated": result.adjudicated_count,
        "links_persisted": result.linked_count,
    }
