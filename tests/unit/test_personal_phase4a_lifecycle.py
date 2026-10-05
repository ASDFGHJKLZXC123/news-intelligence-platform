"""Server-derived retry eligibility at fixed boundaries, without runtime services."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from services.personal.runs import retry_status

NOW = dt.datetime(2026, 10, 4, 12, tzinfo=dt.UTC)


@pytest.mark.parametrize("state", ["queued", "running"])
def test_retry_is_available_at_expiry_but_never_before(state):
    run = SimpleNamespace(state=state, attempt=1, max_attempts=3, lease_expires_at=NOW)
    assert retry_status(run, now=NOW - dt.timedelta(microseconds=1))[0] is False
    assert retry_status(run, now=NOW) == (True, "ownership lease expired")


@pytest.mark.parametrize("state", ["failed", "partially_failed", "queued", "running"])
def test_three_attempt_ceiling_wins_over_terminal_or_expiry_eligibility(state):
    run = SimpleNamespace(state=state, attempt=3, max_attempts=3, lease_expires_at=NOW)
    assert retry_status(run, now=NOW + dt.timedelta(days=100)) == (
        False,
        "maximum attempts reached",
    )


def test_success_never_becomes_retryable_after_lease_or_calendar_changes():
    run = SimpleNamespace(state="succeeded", attempt=1, max_attempts=3, lease_expires_at=None)
    assert retry_status(run, now=NOW + dt.timedelta(days=100))[0] is False


def test_historical_one_time_live_profile_cannot_receive_ordinary_retry():
    run = SimpleNamespace(state="failed", attempt=1, max_attempts=3, lease_expires_at=None)
    profile = SimpleNamespace(
        execution_profile="assisted",
        schema_revision="personal-profile.v1",
        settings={"model_route": {"mode": "live"}},
    )
    eligible, reason = retry_status(run, now=NOW, profile=profile)
    assert eligible is False and "separately authorized" in reason
