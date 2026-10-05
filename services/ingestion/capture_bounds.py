"""Pure bounds and receipt metadata for retained RSS candidates.

This is an additive interface: ordinary RSS consumers still receive ``RSSItem`` values,
while the personal pending queue can retain canonical identities and truncation evidence.
No body fetch, clock fallback, or admission decision happens here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from ipaddress import IPv6Address
from urllib.parse import urlsplit

from packages.providers.base import RSSItem
from services.ingestion.normalize import normalize_url, url_hash

MAX_RSS_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RSS_ITEMS = 500
MAX_TITLE_CHARS = 512
MAX_RSS_SUMMARY_CHARS = 2_000
MAX_URL_CHARS = 4_096
MAX_GUID_CHARS = 4_096
MAX_PROVIDER_CHARS = 64
MAX_SCHEMA_CHARS = 64
MAX_REFERENCE_COUNT = 32


@dataclass(frozen=True)
class BoundedRSSItem:
    """An RSS item whose URL identity was validated without truncating that identity."""

    item: RSSItem
    canonical_url: str
    url_hash: str
    original_url: str
    field_truncations: tuple[str, ...] = ()


@dataclass(frozen=True)
class RSSCaptureResult:
    """Observed bounded input, never an invented count of all remote feed records.

    ``entries_observed`` is a lower bound when ``total_entries_known`` is false. An
    oversized/unverified response has no observed entry count. ``entries_processed``
    includes rejected records and never exceeds the configured entry ceiling.
    """

    items: tuple[BoundedRSSItem, ...]
    status: str
    entries_observed: int | None
    entries_processed: int
    rejected_entries: int
    response_bytes: int | None
    bounds_reached: tuple[str, ...]
    total_entries_known: bool


def validate_http_url(value: str) -> str:
    """Accept a complete HTTP(S) URL or reject it; never create identity by slicing.

    The production canonicalizer deliberately tolerates incomplete strings for its older
    callers. Capture adds this validation before invoking that same canonicalizer/hash.
    """
    if not isinstance(value, str):
        raise ValueError("RSS URL must be text")
    value = value.strip()
    if not value or len(value) > MAX_URL_CHARS:
        raise ValueError("RSS URL is missing or exceeds its storage bound")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("RSS URL contains whitespace or control characters")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("RSS URL is invalid") from exc
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.netloc
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or "\\" in value
        or "%" in hostname
        or (port is not None and port < 1)
    ):
        raise ValueError("RSS URL must be an absolute HTTP(S) URL without credentials")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("RSS URL contains invalid percent encoding")
    try:
        if ":" in hostname:
            IPv6Address(hostname)
        else:
            domain = hostname.rstrip(".").encode("idna").decode("ascii")
            if len(domain) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in domain.split(".")
            ):
                raise ValueError("invalid hostname")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("RSS URL hostname is invalid") from exc
    return value


def _limit(value: int, *, name: str, maximum: int, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer at least {minimum}")
    return min(value, maximum)


def bound_rss_item(item: RSSItem, *, summary_limit: int = MAX_RSS_SUMMARY_CHARS) -> BoundedRSSItem:
    """Bound one retained item and preserve exactly which fields were cut."""
    summary_limit = _limit(
        summary_limit, name="summary_limit", maximum=MAX_RSS_SUMMARY_CHARS, allow_zero=True
    )
    original_url = validate_http_url(item.url)
    canonical_url = validate_http_url(normalize_url(original_url))
    truncations: list[str] = []

    def text(value: str, maximum: int, field_name: str) -> str:
        if len(value) > maximum:
            truncations.append(field_name)
        return value[:maximum]

    def refs(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
        if len(values) > MAX_REFERENCE_COUNT or any(len(value) > MAX_URL_CHARS for value in values):
            truncations.append(field_name)
        return tuple(value[:MAX_URL_CHARS] for value in values[:MAX_REFERENCE_COUNT])

    bounded = replace(
        item,
        guid=text(item.guid, MAX_GUID_CHARS, "guid"),
        title=text(item.title, MAX_TITLE_CHARS, "title"),
        url=original_url,
        summary=text(item.summary or "", summary_limit, "summary"),
        source=text(item.source, MAX_URL_CHARS, "source"),
        provider_name=text(item.provider_name, MAX_PROVIDER_CHARS, "provider_name"),
        output_schema_version=text(item.schema_version, MAX_SCHEMA_CHARS, "schema_version"),
        source_refs=refs(item.source_refs, "source_refs"),
        evidence_refs=refs(item.evidence_refs, "evidence_refs"),
    )
    return BoundedRSSItem(
        item=bounded,
        canonical_url=canonical_url,
        url_hash=url_hash(canonical_url),
        original_url=original_url,
        field_truncations=tuple(truncations),
    )


def bound_rss_items(
    items: Iterable[RSSItem],
    *,
    summary_limit: int = MAX_RSS_SUMMARY_CHARS,
    max_items: int = MAX_RSS_ITEMS,
    response_bytes: int | None = None,
) -> RSSCaptureResult:
    """Adapt an injected provider without silently accepting over-cap or invalid input.

    One sentinel record establishes that the entry bound was reached. Its fields are not
    processed, and the iterator is never drained to claim an unavailable total count.
    """
    max_items = _limit(max_items, name="max_items", maximum=MAX_RSS_ITEMS)
    summary_limit = _limit(
        summary_limit, name="summary_limit", maximum=MAX_RSS_SUMMARY_CHARS, allow_zero=True
    )
    retained: list[BoundedRSSItem] = []
    observed = 0
    processed = 0
    rejected = 0
    entry_limit_reached = False
    for item in items:
        observed += 1
        if processed == max_items:
            entry_limit_reached = True
            break
        processed += 1
        try:
            retained.append(bound_rss_item(item, summary_limit=summary_limit))
        except ValueError:
            rejected += 1
    return RSSCaptureResult(
        items=tuple(retained),
        status="entry_limit_reached" if entry_limit_reached else "complete",
        entries_observed=observed,
        entries_processed=processed,
        rejected_entries=rejected,
        response_bytes=response_bytes,
        bounds_reached=("entry_limit",) if entry_limit_reached else (),
        total_entries_known=not entry_limit_reached,
    )
