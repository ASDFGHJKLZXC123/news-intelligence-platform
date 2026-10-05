"""Celery application configuration.

This file owns every operational Celery setting required by the build plan's job
contract: broker, result backend, queue names, retry/backoff defaults, task time
limits, and Celery Beat schedule ownership. No product/business logic lives here.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

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

# --- Daily pipeline task time budget ---------------------------------------------------
# These override the global `task_soft_time_limit`/`task_time_limit` below for exactly one task,
# `workers.pipeline_tasks.run_daily_pipeline`. Unlike every other task here, the daily coordinator
# runs all seven stages in-process -- including per-event LLM calls -- so the global 120s/180s
# ceiling would SIGKILL it mid-stage and no realistic date could ever reach `succeeded`. They are
# applied on that task's `@shared_task` decorator (the same way `queue=` is), so nothing else
# inherits the wider budget.
#
# Two relationships are load-bearing and must be preserved together:
#   * the 5-minute gap between soft and hard is the graceful window in which the run unwinds after
#     `SoftTimeLimitExceeded` and the task records a durable failure. That window is only enough
#     because both layers that catch broadly re-raise the signal instead of absorbing it -- the
#     per-item stage adapter (`workers.pipeline_stages`) and the per-stage coordinator, from the
#     single `workers.pipeline_tasks.PIPELINE_FATAL_EXCEPTIONS` definition. What it has to cover
#     is one in-flight provider call returning plus one `mark_failed` write, not another stage or
#     another fan-out item; and
#   * `services.pipeline.sqlalchemy_store.PIPELINE_RUNNING_LEASE` (35 min) must stay strictly
#     above the hard limit, so a still-running worker's RUNNING lease can never expire underneath
#     it and hand the date to a replacement attempt while the original is still writing.
PIPELINE_TASK_SOFT_TIME_LIMIT = 1500  # seconds (25 min); raises SoftTimeLimitExceeded
PIPELINE_TASK_TIME_LIMIT = 1800  # seconds (30 min); hard kill ceiling


class EasternDailyCrontab(crontab):
    """A ``crontab`` whose fields are evaluated in America/New_York, not the app timezone.

    ADR 0009 anchors the daily brief to 05:30 ET so the cutoff never drifts into the US
    trading day. Three non-options rule out the obvious approaches: Celery 5.6's ``crontab``
    evaluates its fields in the *app* timezone (UTC here, ``enable_utc=True``) and does **not**
    accept a ``timezone=`` argument -- passing one raises ``TypeError``; changing the global
    ``celery_app.conf.timezone`` would silently move every schedule already accepted above; and
    a fixed UTC hour would be an hour wrong for half the year.

    So the DST-awareness is per-schedule and minimal. This subclass pins the evaluation
    timezone to ET and forces every datetime the base scheduler reasons about into ET before
    the cron arithmetic runs. ``crontab``'s own ``ffwd`` then lands on 05:30 *ET wall-clock* --
    a :class:`~zoneinfo.ZoneInfo` instant whose UTC offset floats with DST -- which Celery
    converts back to real UTC seconds. Net effect: **10:30 UTC in winter (EST), 09:30 UTC in
    summer (EDT)**, and 05:30 ET on both transition days, fired exactly once each. 05:30 exists
    on every US calendar date -- it is in neither the spring-forward gap (02:00-03:00) nor the
    fall-back fold (01:00-02:00) -- so there is no missed or duplicated fire
    (see :mod:`services.reports.window`).

    Only the two seams the base class routes every datetime through are overridden, so the
    object stays pickle/``__eq__``/Beat compatible: the inherited ``__reduce__`` reconstructs it
    from its cron fields alone (no unpicklable ``nowfun`` is stored, and ``__init__`` re-pins the
    timezone on every unpickle), and equality still compares the cron spec.
    """

    #: ADR 0009's anchor timezone. Deliberately duplicated from
    #: ``services.reports.window.BRIEF_TIMEZONE`` rather than imported: this operational Celery
    #: module must stay free of the report-generation service (product logic) at import time.
    TIMEZONE = ZoneInfo("America/New_York")

    def __init__(
        self,
        minute: object = "*",
        hour: object = "*",
        day_of_week: object = "*",
        day_of_month: object = "*",
        month_of_year: object = "*",
        **kwargs: object,
    ) -> None:
        super().__init__(minute, hour, day_of_week, day_of_month, month_of_year, **kwargs)
        # Shadow ``BaseSchedule.tz`` (a kombu ``cached_property`` -- a data descriptor whose
        # ``__set__`` writes straight to ``__dict__``), so every ``maybe_make_aware``/``to_local``
        # the base scheduler performs evaluates in ET. Re-applied by ``__init__`` on every unpickle.
        self.tz = self.TIMEZONE

    def maybe_make_aware(
        self, dt: datetime.datetime, naive_as_utc: bool = True
    ) -> datetime.datetime:
        # The base method localizes *naive* datetimes to ``self.tz`` but leaves *aware* ones
        # (Beat stores ``last_run_at`` UTC-aware) untouched -- which would make the cron
        # arithmetic land on 05:30 UTC. Convert everything to ET so both ``last_run_at`` and
        # ``now`` are ET wall-clock before the fields are compared and ``ffwd`` is applied.
        return super().maybe_make_aware(dt, naive_as_utc=naive_as_utc).astimezone(self.tz)


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

# --- Alert lifecycle schedule (ADR 0010) -----------------------------------------------
# `run_alert_evaluation` is deliberately *not* scheduled here, for the same reason
# `run_event_entity_linking` isn't: it takes a batch of already-scored, already-owned
# observations as an argument, and no pipeline stage in this repo assembles those yet
# (see workers/alert_tasks.py's module docstring for the signal_fusion seam this is waiting
# on). A clock cannot invent the observations a clock-triggered call would need.
#
# `run_pending_alert_notification_sweep` has no such dependency: it reads the `alerts` table
# directly for rows a prior evaluation already decided owe a human a message but never got an
# acknowledged delivery, and redelivers. That is real, schedulable work today regardless of
# whether anything upstream calls `run_alert_evaluation` yet.
ALERT_BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    "alert-pending-notification-sweep": {
        "task": "workers.alert_tasks.run_pending_alert_notification_sweep",
        "schedule": 300.0,  # seconds; a retry net, not the evaluation cadence itself
        "options": {"queue": QUEUE_PIPELINE},
    },
}

# --- Daily brief schedule (ADR 0009) ---------------------------------------------------
# The one scheduled generation of the global daily brief: every calendar day at 05:30 ET (ADR
# 0009's cutoff), via the DST-aware crontab above. The task is referenced by its exact name
# (`generate_daily_brief`, matching ADR 0009) rather than imported, so this operational module
# does not depend on `workers.report_tasks`. The task fires with no args and derives its own
# `brief_date` from the ET cutoff at run time -- a static schedule cannot know "now".
REPORT_BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    "daily-brief-generation": {
        "task": "generate_daily_brief",
        "schedule": EasternDailyCrontab(minute=30, hour=5),
        "options": {"queue": QUEUE_PIPELINE},
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
    **ALERT_BEAT_SCHEDULE,
    **REPORT_BEAT_SCHEDULE,
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
        # ADR 0010 alert lifecycle. `run_alert_evaluation` is registered so a worker can execute
        # it on demand (see ALERT_BEAT_SCHEDULE above for why it has no Beat entry yet);
        # `run_pending_alert_notification_sweep` does have one.
        "workers.alert_tasks",
        # ADR 0004 embedding lifecycle (articles, events, historical episodes). Registered so a
        # worker can execute each on demand, and deliberately not in BEAT_SCHEDULE: neither ADR
        # 0004 nor the episode spec schedules an embedding run, and the trigger is what ingestion
        # or clustering just produced, not a clock.
        "workers.embedding_tasks",
        # Embedded-article clustering. Independently callable and deliberately not scheduled:
        # successful article embedding is its trigger, and the future pipeline coordinator owns
        # that ordering.
        "workers.clustering_tasks",
        # Historical-episode analogy rerank (ADR 0004 + the episode spec). Registered so a worker
        # can execute it per event, and deliberately not in BEAT_SCHEDULE for the same reason as
        # the embedding tasks it follows: an event is worth reranking once it has been embedded,
        # which is an event in the pipeline, not a time of day.
        "workers.analogy_tasks",
        # ADR 0009 daily brief. Registered so a worker can execute the `generate_daily_brief`
        # coordinator, and unlike the tasks above it *does* have a Beat entry
        # (REPORT_BEAT_SCHEDULE): the brief is a clock-triggered artifact, one per ET calendar day.
        "workers.report_tasks",
        # Manual non-crisis daily coordinator. Registered on the pipeline queue but deliberately
        # absent from BEAT_SCHEDULE: an authenticated operator/API request is its only trigger.
        "workers.pipeline_tasks",
        # Personal updates are API-delivered only. They are deliberately absent from Beat and
        # own retries in PostgreSQL rather than Celery autoretry.
        "workers.personal_tasks",
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
    # Fleet-wide defaults for single-purpose tasks. The one long-running exception, the daily
    # pipeline coordinator, carries PIPELINE_TASK_SOFT_TIME_LIMIT/PIPELINE_TASK_TIME_LIMIT on its
    # own decorator; see the comment on those constants above.
    task_soft_time_limit=120,  # raises SoftTimeLimitExceeded for graceful cleanup
    task_time_limit=180,  # hard kill ceiling
    worker_prefetch_multiplier=1,
    # --- Result backend hygiene -------------------------------------------------
    result_expires=3600,
    # --- Beat schedule ownership ------------------------------------------------
    beat_schedule=BEAT_SCHEDULE,
)
