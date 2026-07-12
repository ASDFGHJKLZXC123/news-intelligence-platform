"""DB-free unit tests for ingestion normalization and idempotency logic."""

from __future__ import annotations

import datetime

from packages.providers.base import RSSItem
from services.ingestion import (
    content_hash,
    item_to_article_values,
    normalize_url,
    partition_new_items,
    url_hash,
)

_PUBLISHED = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)


def _item(url: str, title: str = "t", summary: str = "s") -> RSSItem:
    return RSSItem(guid=url, title=title, url=url, published_at=_PUBLISHED, summary=summary)


def test_normalize_url_strips_tracking_fragment_and_trailing_slash() -> None:
    a = normalize_url("HTTPS://Example.com/news/1/?utm_source=x&id=7#frag")
    b = normalize_url("https://example.com/news/1?id=7")
    assert a == b == "https://example.com/news/1?id=7"


def test_url_hash_is_stable_and_canonical() -> None:
    assert url_hash("https://example.com/a/") == url_hash("https://example.com/a")
    assert url_hash("https://example.com/a?utm_medium=rss") == url_hash("https://example.com/a")
    assert url_hash("https://example.com/a") != url_hash("https://example.com/b")


def test_content_hash_changes_with_text() -> None:
    assert content_hash("title", "body") == content_hash("title", "body")
    assert content_hash("title", "body") != content_hash("title", "other")


def test_partition_new_items_dedups_against_existing_and_within_batch() -> None:
    items = [
        _item("https://example.com/1"),
        _item("https://example.com/1/?utm_source=x"),  # in-batch dup of #1
        _item("https://example.com/2"),
    ]
    existing = {url_hash("https://example.com/2")}  # already ingested
    new, dups = partition_new_items(items, existing)
    assert [i.url for i in new] == ["https://example.com/1"]
    assert len(dups) == 2


def test_item_to_article_values_carries_idempotency_and_provenance() -> None:
    values = item_to_article_values(_item("https://example.com/x?utm_source=rss"), source_id="S")
    assert values["url_hash"] == url_hash("https://example.com/x")
    assert values["source_id"] == "S"
    assert values["raw_payload"]["guid"] == "https://example.com/x?utm_source=rss"
