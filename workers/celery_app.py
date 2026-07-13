"""Celery application configuration.

This file owns every operational Celery setting required by the build plan's job
contract: broker, result backend, queue names, retry/backoff defaults, task time
limits, and Celery Beat schedule ownership. No product/business logic lives here.
"""

from __future__ import annotations

from celery import Celery, Task
from celery.schedules import crontab
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

# --- Entity identity refresh schedule (ADR 0006) --------------------------------------
# Cadence and ordering come straight from the ADR: SEC (precedence 1) weekly, GLEIF (2) and
# Wikidata (3) monthly. The three are deliberately separate Beat entries, not a chain: each
# is independently callable and idempotent, and a SEC or GLEIF failure must never be reported
# as success for the sources behind it.
#
# The coupling between them is persisted state, not run order. GLEIF and Wikidata read the
# EntityProfiles that earlier SEC runs seeded, so they work off whatever the store already
# holds regardless of whether a SEC run happened that morning. The hour stagger on the 1st is
# what makes the happy path clean when the 1st *is* a Monday: seed at 06:00, fill from GLEIF
# at 07:00, enrich from Wikidata at 08:00 — the ADR precedence order, in one pass.
IDENTITY_BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    "sec-company-identity-weekly": {
        "task": "workers.provider_data_tasks.run_sec_identity_refresh",
        "schedule": crontab(minute=0, hour=6, day_of_week="monday"),
        "options": {"queue": QUEUE_INGESTION},
    },
    "gleif-entity-identity-monthly": {
        "task": "workers.provider_data_tasks.run_entity_identity_ingestion",
        "schedule": crontab(minute=0, hour=7, day_of_month="1"),
        "options": {"queue": QUEUE_INGESTION},
    },
    "wikidata-entity-identity-monthly": {
        "task": "workers.provider_data_tasks.run_wikidata_identity_ingestion",
        "schedule": crontab(minute=0, hour=8, day_of_month="1"),
        "options": {"queue": QUEUE_INGESTION},
    },
}

# Celery Beat owns scheduled jobs, and this dict is their single definition: one entry per
# schedule, no task scheduled twice. Stage 1's no-op heartbeat proves the scheduler wiring.
BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    "noop-heartbeat": {
        "task": "workers.tasks.scheduled_heartbeat",
        "schedule": 60.0,  # seconds
        "options": {"queue": QUEUE_DEFAULT},
    },
    **IDENTITY_BEAT_SCHEDULE,
}


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
    include=[
        "workers.tasks",
        "workers.ingestion_tasks",
        "workers.provider_data_tasks",
        # ADR 0005 entity linking. Registered so a worker can execute it, and deliberately not
        # in BEAT_SCHEDULE: the ADR schedules no linking run, and an event is linked when it has
        # articles, not when a clock fires.
        "workers.entity_linking_tasks",
    ],
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
    beat_schedule=BEAT_SCHEDULE,
)
