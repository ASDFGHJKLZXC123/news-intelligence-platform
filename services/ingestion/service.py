"""Idempotent RSS ingestion.

``ingest_source`` fetches items through an injected ``RSSProvider`` (fake in tests, HTTP
in production) and inserts only articles whose canonical URL has not been seen. The
new/duplicate decision is a pure function (``partition_new_items``) so it is unit-tested
without a database; persistence is integration-tested.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Article, Source
from db.models.enums import SourceType
from packages.providers.base import RSSItem, RSSProvider
from services.ingestion.normalize import content_hash, url_hash


@dataclass(frozen=True)
class IngestResult:
    """Outcome counts for one source ingestion run."""

    source_id: str
    fetched: int
    inserted: int
    skipped_duplicates: int


def item_to_article_values(item: RSSItem, source_id: Any) -> dict[str, Any]:
    """Map a normalized RSS item to ``Article`` column values (pure)."""
    return {
        "source_id": source_id,
        "url": item.url,
        "url_hash": url_hash(item.url),
        "title": item.title,
        "summary": item.summary or None,
        "published_at": item.published_at,
        "content_hash": content_hash(item.title, item.summary or ""),
        "raw_payload": {
            "guid": item.guid,
            "provider_name": item.provider_name,
            "source": item.source,
            "schema_version": item.schema_version,
        },
    }


def partition_new_items(
    items: Iterable[RSSItem], existing_hashes: Iterable[str]
) -> tuple[list[RSSItem], list[RSSItem]]:
    """Split items into (new, duplicate), de-duplicating within the batch too."""
    seen = set(existing_hashes)
    new: list[RSSItem] = []
    duplicates: list[RSSItem] = []
    for item in items:
        digest = url_hash(item.url)
        if digest in seen:
            duplicates.append(item)
        else:
            seen.add(digest)
            new.append(item)
    return new, duplicates


def ensure_source(
    session: Session,
    *,
    name: str,
    feed_url: str,
    source_type: SourceType = SourceType.RSS,
) -> Source:
    """Idempotently get-or-create a source by feed URL."""
    existing = session.scalar(select(Source).where(Source.feed_url == feed_url))
    if existing is not None:
        return existing
    source = Source(name=name, feed_url=feed_url, source_type=source_type.value)
    session.add(source)
    session.flush()
    return source


def ingest_source(session: Session, source: Source, provider: RSSProvider) -> IngestResult:
    """Fetch a source's feed and insert only unseen articles. Idempotent on re-run."""
    items: Sequence[RSSItem] = provider.fetch(source.feed_url)
    hashes = [url_hash(item.url) for item in items]
    existing: set[str] = set()
    if hashes:
        existing = set(
            session.scalars(select(Article.url_hash).where(Article.url_hash.in_(hashes))).all()
        )
    new_items, _ = partition_new_items(items, existing)
    for item in new_items:
        session.add(Article(**item_to_article_values(item, source.id)))
    session.flush()
    return IngestResult(
        source_id=str(source.id),
        fetched=len(items),
        inserted=len(new_items),
        skipped_duplicates=len(items) - len(new_items),
    )
