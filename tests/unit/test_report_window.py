"""The daily brief's window: DST, boundaries, and the developing marker (ADR 0009).

DB-free. Every assertion is about an *instant*, which is the whole difficulty: 2026's US
transitions are 08 Mar (spring forward) and 01 Nov (fall back).
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from services.reports.window import (
    BRIEF_TIMEZONE,
    BriefWindow,
    NaiveDatetimeError,
    cutoff_for,
    window_for,
    window_for_date,
)

UTC = datetime.UTC

SPRING_FORWARD = datetime.date(2026, 3, 8)
FALL_BACK = datetime.date(2026, 11, 1)
ORDINARY = datetime.date(2026, 7, 14)


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=BRIEF_TIMEZONE)


# --------------------------------------------------------------------------------------
# Aware-datetime validation
# --------------------------------------------------------------------------------------


def test_window_for_rejects_a_naive_now() -> None:
    with pytest.raises(NaiveDatetimeError):
        window_for(datetime.datetime(2026, 7, 14, 5, 30))


def test_contains_rejects_a_naive_moment() -> None:
    window = window_for_date(ORDINARY)
    with pytest.raises(NaiveDatetimeError):
        window.contains(datetime.datetime(2026, 7, 14, 4, 0))


def test_is_developing_rejects_a_naive_start_time() -> None:
    window = window_for_date(ORDINARY)
    with pytest.raises(NaiveDatetimeError):
        window.is_developing(datetime.datetime(2026, 7, 13, 4, 0))


# --------------------------------------------------------------------------------------
# The cutoff and the brief_date
# --------------------------------------------------------------------------------------


def test_cutoff_is_0530_et_on_both_sides_of_a_dst_transition() -> None:
    # The same wall-clock cutoff, two different UTC offsets. 05:30 is chosen precisely
    # because it is never in the gap (02:00-03:00) or the fold (01:00-02:00).
    before = cutoff_for(datetime.date(2026, 3, 7))
    after = cutoff_for(SPRING_FORWARD)
    assert (before.hour, before.minute) == (5, 30)
    assert (after.hour, after.minute) == (5, 30)
    assert before.utcoffset() == datetime.timedelta(hours=-5)
    assert after.utcoffset() == datetime.timedelta(hours=-4)


@pytest.mark.parametrize(
    ("now", "expected_brief_date"),
    [
        # Exactly at the cutoff: the cutoff is inclusive, so it closes *today's* window.
        (_et(2026, 7, 14, 5, 30), datetime.date(2026, 7, 14)),
        # One second before: still inside yesterday's.
        (_et(2026, 7, 14, 5, 29) + datetime.timedelta(seconds=59), datetime.date(2026, 7, 13)),
        (_et(2026, 7, 14, 5, 31), datetime.date(2026, 7, 14)),
        (_et(2026, 7, 14, 0, 1), datetime.date(2026, 7, 13)),
        (_et(2026, 7, 14, 23, 59), datetime.date(2026, 7, 14)),
    ],
)
def test_brief_date_is_the_et_date_of_the_cutoff(
    now: datetime.datetime, expected_brief_date: datetime.date
) -> None:
    assert window_for(now).brief_date == expected_brief_date


def test_brief_date_follows_et_not_utc() -> None:
    # 03:00 UTC on the 14th is 23:00 ET on the 13th: an ET-anchored brief says the 13th.
    now = datetime.datetime(2026, 7, 14, 3, 0, tzinfo=UTC)
    assert window_for(now).brief_date == datetime.date(2026, 7, 13)


def test_window_for_accepts_any_aware_zone() -> None:
    tokyo = datetime.datetime(2026, 7, 14, 18, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert tokyo.astimezone(BRIEF_TIMEZONE).hour == 5  # 05:30 ET
    assert window_for(tokyo).brief_date == datetime.date(2026, 7, 14)


# --------------------------------------------------------------------------------------
# DST: the window's length is what moves, not the cutoff
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("brief_date", "expected_hours"),
    [
        (ORDINARY, 24),
        # Spring forward: the window loses the hour the clocks skipped.
        (SPRING_FORWARD, 23),
        # Fall back: it gains the hour the clocks repeated.
        (FALL_BACK, 25),
    ],
)
def test_dst_changes_the_window_length_not_the_cutoff(
    brief_date: datetime.date, expected_hours: int
) -> None:
    window = window_for_date(brief_date)
    assert window.duration == datetime.timedelta(hours=expected_hours)
    # Both ends are still 05:30 ET.
    assert (window.start.hour, window.start.minute) == (5, 30)
    assert (window.end.hour, window.end.minute) == (5, 30)


def test_consecutive_windows_tile_the_timeline_without_gap_or_overlap() -> None:
    # Across the fall-back transition, where an hour of ET wall clock happens twice.
    first = window_for_date(FALL_BACK)
    second = window_for_date(FALL_BACK + datetime.timedelta(days=1))
    assert first.end == second.start
    # The shared instant belongs to the earlier window (closed at its end), and to it alone.
    assert first.contains(first.end)
    assert not second.contains(second.start)


def test_the_repeated_fall_back_hour_lands_in_one_window_only() -> None:
    # 01:30 ET occurs twice on 01 Nov 2026: fold=0 is EDT, fold=1 is EST, an hour apart.
    window = window_for_date(FALL_BACK)
    first_pass = datetime.datetime(2026, 11, 1, 1, 30, tzinfo=BRIEF_TIMEZONE, fold=0)
    second_pass = datetime.datetime(2026, 11, 1, 1, 30, tzinfo=BRIEF_TIMEZONE, fold=1)
    # Distinct instants, both before the 05:30 cutoff, so both are in this window.
    assert first_pass.astimezone(UTC) != second_pass.astimezone(UTC)
    assert window.contains(first_pass)
    assert window.contains(second_pass)


# --------------------------------------------------------------------------------------
# The qualifying predicate: (start, end]
# --------------------------------------------------------------------------------------


def test_window_is_open_at_the_start_and_closed_at_the_end() -> None:
    window = window_for_date(ORDINARY)
    tick = datetime.timedelta(microseconds=1)
    assert not window.contains(window.start)  # belongs to the previous brief
    assert window.contains(window.start + tick)
    assert window.contains(window.end)  # the cutoff is inclusive
    assert not window.contains(window.end + tick)


def test_containment_is_by_instant_not_by_wall_clock() -> None:
    # The same instant expressed in UTC must qualify exactly as the ET one does.
    window = window_for_date(ORDINARY)
    assert window.contains(window.end.astimezone(UTC))
    assert not window.contains(window.start.astimezone(UTC))


# --------------------------------------------------------------------------------------
# The developing marker
# --------------------------------------------------------------------------------------


def test_event_that_began_before_the_window_is_developing() -> None:
    window = window_for_date(ORDINARY)
    assert window.is_developing(window.start - datetime.timedelta(hours=6))


def test_event_that_began_at_the_previous_cutoff_is_developing() -> None:
    # The previous window is closed at its end, so an event beginning on that instant began
    # inside it -- and is straddling by the time it updates into this one.
    window = window_for_date(ORDINARY)
    assert window.is_developing(window.start)


def test_event_that_began_inside_the_window_is_not_developing() -> None:
    window = window_for_date(ORDINARY)
    assert not window.is_developing(window.start + datetime.timedelta(microseconds=1))
    assert not window.is_developing(window.end)


def test_missing_start_time_is_not_treated_as_developing() -> None:
    # An absent first_seen_at is evidence of nothing; it must not mark a story as breaking.
    assert not window_for_date(ORDINARY).is_developing(None)


# --------------------------------------------------------------------------------------
# Reproducibility
# --------------------------------------------------------------------------------------


def test_window_is_frozen_and_reproducible() -> None:
    window = window_for_date(ORDINARY)
    assert window == window_for_date(ORDINARY)
    assert isinstance(window, BriefWindow)
    with pytest.raises(AttributeError):
        window.end = window.start  # type: ignore[misc]
