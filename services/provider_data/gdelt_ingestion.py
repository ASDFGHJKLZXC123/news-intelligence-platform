"""GDELT provider ingestion into retained raw provider-data storage."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from packages.providers.base import GDELTArticle, GDELTEvent, GDELTProvider
from services.provider_data.common import IngestionResult, retain_raw_item


def ingest_gdelt_raw_items(
    session: Any,
    provider: GDELTProvider,
    *,
    query: str,
    start_at: datetime.datetime | None = None,
    end_at: datetime.datetime | None = None,
    max_records: int = 50,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = "gdelt",
) -> IngestionResult:
    """Search GDELT articles/events and retain normalized DTO payloads idempotently."""

    articles = provider.search_articles(
        query,
        start_at=start_at,
        end_at=end_at,
        max_records=max_records,
    )
    events = provider.search_events(
        query,
        start_at=start_at,
        end_at=end_at,
        max_records=max_records,
    )

    inserted = 0
    skipped = 0
    for article in articles:
        was_inserted = _retain_article(
            session,
            article,
            provider_name=provider_name,
            provider_run_id=provider_run_id,
        )
        inserted += int(was_inserted)
        skipped += int(not was_inserted)

    for event in events:
        was_inserted = _retain_event(
            session,
            event,
            provider_name=provider_name,
            provider_run_id=provider_run_id,
        )
        inserted += int(was_inserted)
        skipped += int(not was_inserted)

    return IngestionResult(
        fetched=len(articles) + len(events),
        inserted=inserted,
        skipped=skipped,
        details={
            "articles_fetched": len(articles),
            "events_fetched": len(events),
            "raw_inserted": inserted,
            "raw_skipped": skipped,
        },
    )


def _retain_article(
    session: Any,
    article: GDELTArticle,
    *,
    provider_name: str,
    provider_run_id: uuid.UUID | None,
) -> bool:
    external_id = article.guid or article.url
    _, inserted = retain_raw_item(
        session,
        provider=provider_name,
        item_type="article",
        external_id=external_id,
        payload=article,
        observed_at=article.seen_at,
        provider_run_id=provider_run_id,
        identity_parts=(external_id,),
    )
    return inserted


def _retain_event(
    session: Any,
    event: GDELTEvent,
    *,
    provider_name: str,
    provider_run_id: uuid.UUID | None,
) -> bool:
    external_id = event.global_event_id or event.source_url
    _, inserted = retain_raw_item(
        session,
        provider=provider_name,
        item_type="event",
        external_id=external_id,
        payload=event,
        observed_at=event.event_at,
        provider_run_id=provider_run_id,
        identity_parts=(external_id,),
    )
    return inserted


ingest_gdelt = ingest_gdelt_raw_items
