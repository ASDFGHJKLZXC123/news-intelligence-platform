"""The daily-brief Beat schedule: one entry, DST-correct at 05:30 ET, and Beat-serializable.

No live broker is required. The DST behaviour is proved by reconstructing the exact instant the
scheduler would fire at (``start + delta`` from ``remaining_delta``) and asserting it lands on
05:30 America/New_York -- 10:30 UTC in winter, 09:30 UTC in summer -- across both transitions,
once per day, without shifting any earlier schedule or the global app timezone.
"""

from __future__ import annotations

import datetime
import pickle
from zoneinfo import ZoneInfo

from celery.schedules import crontab

# Registering the task is import-time work (the ``@shared_task`` decorator runs on import). A plain
# Celery app does not eagerly import its ``include=`` modules under pytest, so import the task module
# here for its registration side effect -- otherwise this module fails standalone when it looks the
# task up by name. Test-only: production eager-loading (worker startup) is unchanged.
import workers.report_tasks  # noqa: F401  -- imported for its @shared_task registration side effect
from workers.celery_app import (
    QUEUE_PIPELINE,
    REPORT_BEAT_SCHEDULE,
    EasternDailyCrontab,
    celery_app,
)

ET = ZoneInfo("America/New_York")
UTC = datetime.UTC

#: The exact schedule object Beat owns, used so the tests exercise what production runs.
SCHEDULE: EasternDailyCrontab = REPORT_BEAT_SCHEDULE["daily-brief-generation"]["schedule"]  # type: ignore[assignment]


def _next_fire_utc(sched: EasternDailyCrontab, reference: datetime.datetime) -> datetime.datetime:
    """The UTC instant the scheduler fires at next, strictly after ``reference``.

    This is exactly how Celery's crontab computes it: apply the cron ``ffwd`` to the localized
    ``last_run_at`` to get the next scheduled wall-clock datetime, then read it as a UTC instant.
    Deterministic in ``reference`` alone (the ``now`` component is not used here).
    """

    start, delta, _now = sched.remaining_delta(reference)
    return (start + delta).astimezone(UTC)


def _is_0530_et(fire: datetime.datetime) -> bool:
    local = fire.astimezone(ET)
    return (local.hour, local.minute, local.second) == (5, 30, 0)


# --- registration ----------------------------------------------------------------------
def test_task_registered_under_exact_name_on_pipeline_queue() -> None:
    from workers.celery_app import Stage1Task

    task = celery_app.tasks["generate_daily_brief"]
    assert isinstance(task, Stage1Task)
    assert task.queue == QUEUE_PIPELINE
    assert task.max_retries == 3
    assert task.retry_backoff is True


def test_beat_owns_exactly_one_daily_brief_entry() -> None:
    schedule = celery_app.conf.beat_schedule
    matching = [key for key, entry in schedule.items() if entry["task"] == "generate_daily_brief"]
    assert matching == ["daily-brief-generation"]

    entry = schedule["daily-brief-generation"]
    assert entry["options"] == {"queue": QUEUE_PIPELINE}
    assert isinstance(entry["schedule"], EasternDailyCrontab)
    assert entry["schedule"].hour == {5}
    assert entry["schedule"].minute == {30}
    assert entry["schedule"].tz == ET


def test_the_daily_brief_task_has_no_second_or_unregistered_schedule() -> None:
    tasks = [entry["task"] for entry in celery_app.conf.beat_schedule.values()]
    assert tasks.count("generate_daily_brief") == 1


# --- the global config is untouched: earlier schedules are not shifted -----------------
def test_global_timezone_stays_utc() -> None:
    # ADR 0009's DST-awareness is per-schedule; changing the app timezone would silently move
    # every earlier crontab (identity refreshes, alert sweep) that was accepted against UTC.
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.enable_utc is True


def test_earlier_schedules_are_unchanged() -> None:
    schedule = celery_app.conf.beat_schedule
    # A representative earlier entry from each pre-existing group, still exactly as configured.
    assert schedule["noop-heartbeat"]["task"] == "workers.tasks.scheduled_heartbeat"
    assert schedule["noop-heartbeat"]["schedule"] == 60.0
    assert schedule["sec-company-identity-weekly"]["schedule"] == crontab(
        minute=0, hour=6, day_of_week="monday"
    )
    assert schedule["alert-pending-notification-sweep"]["schedule"] == 300.0


# --- DST correctness -------------------------------------------------------------------
def test_winter_fire_is_1030_utc() -> None:
    fire = _next_fire_utc(SCHEDULE, datetime.datetime(2026, 1, 15, 0, 0, tzinfo=ET))
    assert fire == datetime.datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
    assert _is_0530_et(fire)


def test_summer_fire_is_0930_utc() -> None:
    fire = _next_fire_utc(SCHEDULE, datetime.datetime(2026, 7, 15, 0, 0, tzinfo=ET))
    assert fire == datetime.datetime(2026, 7, 15, 9, 30, tzinfo=UTC)
    assert _is_0530_et(fire)


def test_a_utc_aware_reference_still_fires_at_0530_et() -> None:
    # Beat stores ``last_run_at`` UTC-aware; the schedule must convert it to ET, not read 05:30 UTC.
    fire = _next_fire_utc(SCHEDULE, datetime.datetime(2026, 1, 15, 4, 0, tzinfo=UTC))
    assert fire == datetime.datetime(2026, 1, 15, 10, 30, tzinfo=UTC)


def test_spring_forward_day_stays_0530_et() -> None:
    # 2026-03-08: clocks jump 02:00 EST -> 03:00 EDT. 05:30 exists (it is not in the gap).
    fire = _next_fire_utc(SCHEDULE, datetime.datetime(2026, 3, 7, 12, 0, tzinfo=ET))
    assert fire == datetime.datetime(2026, 3, 8, 9, 30, tzinfo=UTC)
    assert _is_0530_et(fire)


def test_fall_back_day_stays_0530_et() -> None:
    # 2026-11-01: clocks fall 02:00 EDT -> 01:00 EST. 05:30 exists exactly once (not in the fold).
    fire = _next_fire_utc(SCHEDULE, datetime.datetime(2026, 10, 31, 12, 0, tzinfo=ET))
    assert fire == datetime.datetime(2026, 11, 1, 10, 30, tzinfo=UTC)
    assert _is_0530_et(fire)


def _fire_sequence(reference: datetime.datetime, count: int) -> list[datetime.datetime]:
    fires: list[datetime.datetime] = []
    cursor = reference
    for _ in range(count):
        fire = _next_fire_utc(SCHEDULE, cursor)
        fires.append(fire)
        cursor = fire + datetime.timedelta(minutes=1)  # step just past it; UTC add is DST-safe
    return fires


def test_every_day_across_spring_forward_is_0530_et_and_never_duplicated() -> None:
    fires = _fire_sequence(datetime.datetime(2026, 3, 5, 12, 0, tzinfo=ET), 6)

    assert all(_is_0530_et(f) for f in fires)
    assert len(set(fires)) == len(fires)  # no duplicate fire
    gaps = [(fires[i + 1] - fires[i]).total_seconds() / 3600 for i in range(len(fires) - 1)]
    # The spring-forward day is one hour shorter; every gap is a whole calendar day.
    assert 23.0 in gaps
    assert all(gap in (23.0, 24.0, 25.0) for gap in gaps)


def test_every_day_across_fall_back_is_0530_et_and_never_duplicated() -> None:
    fires = _fire_sequence(datetime.datetime(2026, 10, 30, 12, 0, tzinfo=ET), 6)

    assert all(_is_0530_et(f) for f in fires)
    assert len(set(fires)) == len(fires)
    gaps = [(fires[i + 1] - fires[i]).total_seconds() / 3600 for i in range(len(fires) - 1)]
    # The fall-back day is one hour longer.
    assert 25.0 in gaps
    assert all(gap in (23.0, 24.0, 25.0) for gap in gaps)


# --- Beat compatibility: pickling and equality -----------------------------------------
def test_schedule_survives_pickle_roundtrip_with_tz_and_cron_fields() -> None:
    sched = EasternDailyCrontab(minute=30, hour=5)
    restored = pickle.loads(pickle.dumps(sched))

    assert isinstance(restored, EasternDailyCrontab)
    assert restored.tz == ET  # re-pinned by __init__ on unpickle, not carried in __dict__
    assert (restored.hour, restored.minute) == ({5}, {30})
    assert restored == sched


def test_equality_tracks_the_cron_spec() -> None:
    assert EasternDailyCrontab(minute=30, hour=5) == EasternDailyCrontab(minute=30, hour=5)
    assert EasternDailyCrontab(minute=30, hour=5) != EasternDailyCrontab(minute=0, hour=5)
