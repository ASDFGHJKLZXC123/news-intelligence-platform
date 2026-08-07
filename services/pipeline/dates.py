"""Calendar safety for the manually triggered daily pipeline."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from services.pipeline.contracts import PipelineIdentity

PIPELINE_CALENDAR_TIMEZONE = ZoneInfo("America/New_York")


class FuturePipelineDateError(ValueError):
    """A manual run cannot publish a brief for a date that has not started yet."""


def current_pipeline_date() -> datetime.date:
    """Return the report calendar date used by the existing daily-brief contract."""

    return datetime.datetime.now(datetime.UTC).astimezone(PIPELINE_CALENDAR_TIMEZONE).date()


def validate_pipeline_date(
    identity: PipelineIdentity,
    *,
    today: datetime.date | None = None,
) -> None:
    """Reject future logical dates while allowing explicit historical brief regeneration.

    The date keys lifecycle and scopes the final daily brief. Upstream ingestion/NLP stages are
    intentionally catch-up reconciliation over outstanding rows, not a historical feed replay.
    """

    resolved_today = current_pipeline_date() if today is None else today
    if identity.process_date > resolved_today:
        raise FuturePipelineDateError(
            f"process_date {identity.process_date.isoformat()} is in the future; "
            f"current pipeline date is {resolved_today.isoformat()}"
        )


__all__ = [
    "PIPELINE_CALENDAR_TIMEZONE",
    "FuturePipelineDateError",
    "current_pipeline_date",
    "validate_pipeline_date",
]
