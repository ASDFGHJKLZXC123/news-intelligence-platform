"""Celery application configuration.

This file owns every operational Celery setting required by the build plan's job
contract: broker, result backend, queue names, retry/backoff defaults, task time
limits, and Celery Beat schedule ownership. No product/business logic lives here.
"""

from __future__ import annotations

from celery import Celery, Task
from kombu import Queue

from packages.config.settings import get_settings

settings = get_settings()

# Queue names. Stage 1 only needs a default queue, but ingestion/pipeline queues are
# declared up front so later stages route tasks without reconfiguring the broker.
QUEUE_DEFAULT = "default"
QUEUE_INGESTION = "ingestion"
QUEUE_PIPELINE = "pipeline"
TASK_MAX_RETRIES = 3
TASK_RETRY_BACKOFF_MAX = 600


class Stage1Task(Task):
    """Base task carrying Stage 1 retry/backoff defaults on real task objects."""

    abstract = True
    autoretry_for = (Exception,)
    max_retries = TASK_MAX_RETRIES
    retry_backoff = True
    retry_backoff_max = TASK_RETRY_BACKOFF_MAX
    retry_jitter = True
    retry_kwargs = {"max_retries": TASK_MAX_RETRIES}


celery_app = Celery(
    "news_intelligence",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["workers.tasks", "workers.ingestion_tasks", "workers.provider_data_tasks"],
)

celery_app.conf.update(
    # --- Serialization / safety -------------------------------------------------
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # --- Queues -----------------------------------------------------------------
    task_default_queue=QUEUE_DEFAULT,
    task_queues=(
        Queue(QUEUE_DEFAULT),
        Queue(QUEUE_INGESTION),
        Queue(QUEUE_PIPELINE),
    ),
    # --- Retry / backoff defaults (job contract) --------------------------------
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_default_retry_delay=10,  # seconds before first retry
    task_annotations={
        "*": {
            "max_retries": TASK_MAX_RETRIES,
            "retry_backoff": True,
            "retry_backoff_max": TASK_RETRY_BACKOFF_MAX,
            "retry_jitter": True,
        }
    },
    # --- Time limits ------------------------------------------------------------
    task_soft_time_limit=120,  # raises SoftTimeLimitExceeded for graceful cleanup
    task_time_limit=180,  # hard kill ceiling
    worker_prefetch_multiplier=1,
    # --- Result backend hygiene -------------------------------------------------
    result_expires=3600,
    # --- Beat schedule ownership ------------------------------------------------
    # Celery Beat owns scheduled jobs. Stage 1 registers only a no-op heartbeat to
    # prove the scheduler wiring without triggering any product logic.
    beat_schedule={
        "noop-heartbeat": {
            "task": "workers.tasks.scheduled_heartbeat",
            "schedule": 60.0,  # seconds
            "options": {"queue": QUEUE_DEFAULT},
        }
    },
)
