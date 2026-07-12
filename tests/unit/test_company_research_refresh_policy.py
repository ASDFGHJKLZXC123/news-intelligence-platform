"""Refresh policy tests for company research jobs."""

from __future__ import annotations

import datetime

from services.company_research import (
    due_refresh_jobs,
    refresh_reasons_for_profile,
    should_schedule_llm_refresh,
)


def test_due_refresh_jobs_returns_overdue_and_never_run_jobs() -> None:
    now = datetime.datetime(2026, 6, 20, 12, 0, tzinfo=datetime.UTC)
    due = due_refresh_jobs(
        {
            "market_quotes": now - datetime.timedelta(minutes=30),
            "gdelt_company_news": now - datetime.timedelta(hours=2),
            "sec_submissions": now - datetime.timedelta(days=1, minutes=1),
        },
        now=now,
    )

    assert "market_quotes" not in due
    assert "gdelt_company_news" in due
    assert "sec_submissions" in due
    assert "sec_company_facts" in due


def test_refresh_reasons_for_profile_marks_stale_opened_profiles() -> None:
    profile = {
        "identity": {"ticker": "AAPL"},
        "staleFields": [{"category": "valuation", "key": "share_price"}],
    }

    assert refresh_reasons_for_profile(profile, opened_by_user=False) == []
    assert refresh_reasons_for_profile(profile, opened_by_user=True) == ["stale_profile_opened"]
    assert should_schedule_llm_refresh(["stale_profile_opened"]) is True


def test_identity_review_reason_does_not_schedule_llm_refresh() -> None:
    profile = {
        "identity": {"ticker": "DUP", "resolution_status": "ambiguous_ticker"},
        "staleFields": [],
    }

    reasons = refresh_reasons_for_profile(profile, opened_by_user=True)

    assert reasons == ["identity_review_required"]
    assert should_schedule_llm_refresh(reasons) is False
