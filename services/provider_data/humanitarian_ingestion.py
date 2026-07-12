"""Humanitarian report ingestion into provider-data storage."""

from __future__ import annotations

import uuid
from typing import Any

from db.models import HumanitarianReport as HumanitarianReportModel
from packages.providers.base import HumanitarianProvider
from services.provider_data.common import (
    IngestionResult,
    add,
    find_one,
    json_safe,
    retain_raw_item,
)


def ingest_humanitarian_reports(
    session: Any,
    provider: HumanitarianProvider,
    *,
    query: str,
    country_code: str | None = None,
    disaster_type: str | None = None,
    limit: int = 20,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = "reliefweb",
) -> IngestionResult:
    """Search humanitarian reports and persist metadata plus retained raw payloads."""

    reports = provider.search_reports(
        query,
        country_code=country_code,
        disaster_type=disaster_type,
        limit=limit,
    )
    inserted = 0
    skipped = 0
    raw_inserted = 0
    raw_skipped = 0

    for report in reports:
        _, raw_created = retain_raw_item(
            session,
            provider=provider_name,
            item_type="humanitarian_report",
            external_id=report.report_id,
            payload=report,
            observed_at=report.updated_at or report.published_at,
            provider_run_id=provider_run_id,
            identity_parts=(report.report_id,),
        )
        raw_inserted += int(raw_created)
        raw_skipped += int(not raw_created)
        was_inserted = _upsert_report(session, report, provider_name=provider_name)
        inserted += int(was_inserted)
        skipped += int(not was_inserted)

    return IngestionResult(
        fetched=len(reports),
        inserted=inserted,
        skipped=skipped,
        details={"raw_inserted": raw_inserted, "raw_skipped": raw_skipped},
    )


def _upsert_report(session: Any, report: Any, *, provider_name: str) -> bool:
    existing = find_one(
        session,
        HumanitarianReportModel,
        provider=provider_name,
        external_id=report.report_id,
    )
    values = {
        "title": report.title,
        "url": report.url or None,
        "published_at": report.published_at,
        "country_codes": json_safe(report.country_codes),
        "disaster_types": json_safe(report.disaster_types),
        "organizations": [report.source] if report.source else None,
        "themes": json_safe(report.themes),
        "summary": report.summary or None,
        "body_excerpt": _body_excerpt(report.summary),
        "source_payload": json_safe(report),
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return False
    add(
        session,
        HumanitarianReportModel(
            id=uuid.uuid4(),
            provider=provider_name,
            external_id=report.report_id,
            **values,
        ),
    )
    return True


def _body_excerpt(value: str) -> str | None:
    if not value:
        return None
    return value[:500]
