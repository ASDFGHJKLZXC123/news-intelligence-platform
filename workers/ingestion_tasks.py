"""Celery task that ingests one source's feed (Stage P3).

Opens a session, loads the source, runs the idempotent ingestion service, and records the
job outcome via the metrics/logging job contract. Safe to re-run (idempotent on URL hash).
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

from db.base import SessionLocal
from db.models import Source
from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.jobs import Stage1Job
from services.ingestion import ingest_source
from services.ingestion.http_provider import HttpRSSProvider
from services.writer_mode import require_legacy_writer_mode
from workers.celery_app import Stage1Task

logger = get_logger("workers.ingestion_tasks")


@shared_task(name="workers.ingestion_tasks.ingest_source_feed", base=Stage1Task)
def ingest_source_feed(source_id: str) -> dict[str, Any]:
    """Ingest a single source by id. Idempotent; returns insert/skip counts."""
    job = Stage1Job.create(
        "workers.ingestion_tasks.ingest_source_feed", {"source_id": source_id}
    ).mark_running()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)
    session = SessionLocal()
    try:
        require_legacy_writer_mode(session, lock=True)
        source = session.get(Source, source_id)
        if source is None:
            msg = f"unknown source_id: {source_id}"
            raise ValueError(msg)
        result = ingest_source(session, source, HttpRSSProvider())
        session.commit()
        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        logger.info(
            "ingested source",
            extra={
                "source_id": source_id,
                "inserted": result.inserted,
                "skipped_duplicates": result.skipped_duplicates,
            },
        )
        return {
            "status": "ok",
            "job_id": completed.job_id,
            "fetched": result.fetched,
            "inserted": result.inserted,
            "skipped_duplicates": result.skipped_duplicates,
        }
    except Exception:
        session.rollback()
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("ingestion failed")
        raise
    finally:
        session.close()
        set_job_id(None)
