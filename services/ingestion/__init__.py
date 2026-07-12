"""News ingestion service (Stage P3): RSS -> normalized, deduplicated articles."""

from __future__ import annotations

from services.ingestion.normalize import content_hash, normalize_url, url_hash
from services.ingestion.service import (
    IngestResult,
    ensure_source,
    ingest_source,
    item_to_article_values,
    partition_new_items,
)

__all__ = [
    "IngestResult",
    "content_hash",
    "ensure_source",
    "ingest_source",
    "item_to_article_values",
    "normalize_url",
    "partition_new_items",
    "url_hash",
]
