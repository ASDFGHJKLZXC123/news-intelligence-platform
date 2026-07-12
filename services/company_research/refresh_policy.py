"""Refresh policy helpers for company research jobs."""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any

REFRESH_INTERVALS: dict[str, datetime.timedelta] = {
    "sec_submissions": datetime.timedelta(days=1),
    "sec_company_facts": datetime.timedelta(days=1),
    "sec_bulk_backfill": datetime.timedelta(days=7),
    "market_quotes": datetime.timedelta(minutes=60),
    "valuation_snapshots": datetime.timedelta(days=1),
    "fred_macro": datetime.timedelta(days=1),
    "gdelt_company_news": datetime.timedelta(hours=1),
    "eia_energy": datetime.timedelta(days=1),
    "filing_text_extraction": datetime.timedelta(days=1),
}

LLM_RERUN_REASONS = frozenset(
    {
        "new_annual_filing",
        "section_hash_changed",
        "watchlist_entry",
        "stale_profile_opened",
        "material_event_risk_change",
        "prompt_version_changed",
        "model_version_changed",
    }
)


def due_refresh_jobs(
    last_runs: Mapping[str, datetime.datetime | None],
    *,
    now: datetime.datetime,
) -> list[str]:
    """Return refresh jobs that are due at ``now``."""

    due: list[str] = []
    for job_name, interval in REFRESH_INTERVALS.items():
        last_run = last_runs.get(job_name)
        if last_run is None or now - _ensure_aware(last_run, now) >= interval:
            due.append(job_name)
    return due


def refresh_reasons_for_profile(profile: Mapping[str, Any], *, opened_by_user: bool = False) -> list[str]:
    """Return rerun reasons for a profile without calling providers."""

    reasons: list[str] = []
    stale_fields = profile.get("staleFields")
    if opened_by_user and isinstance(stale_fields, Sequence) and len(stale_fields) > 0:
        reasons.append("stale_profile_opened")
    identity = profile.get("identity") if isinstance(profile.get("identity"), Mapping) else {}
    if identity.get("resolution_status") == "ambiguous_ticker":
        reasons.append("identity_review_required")
    return reasons


def should_schedule_llm_refresh(reasons: Sequence[str]) -> bool:
    """Return true only when a reason is approved for LLM reruns."""

    return any(reason in LLM_RERUN_REASONS for reason in reasons)


def _ensure_aware(value: datetime.datetime, reference: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    return value
