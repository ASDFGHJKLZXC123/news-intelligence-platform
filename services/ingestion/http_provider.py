"""Production HTTP RSS provider (RSS 2.0) using only the standard library.

Implements the ``RSSProvider`` protocol with ``urllib`` + ``xml.etree``. This is the live
network connector; tests use ``packages.providers.fakes.FakeRSSProvider`` instead, so the
unit/CI suites never touch the network.
"""

from __future__ import annotations

import datetime
import math
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from email.utils import parsedate_to_datetime

from packages.providers.base import RSSItem, ensure_utc
from services.ingestion.capture_bounds import (
    MAX_RSS_ITEMS,
    MAX_RSS_RESPONSE_BYTES,
    MAX_RSS_SUMMARY_CHARS,
    RSSCaptureResult,
    bound_rss_items,
    validate_http_url,
)
from services.ingestion.normalize import normalize_url


def _parse_date(value: str | None) -> datetime.datetime | None:
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.UTC)
            return ensure_utc(parsed.astimezone(datetime.UTC))
        except (TypeError, ValueError, OverflowError):
            pass
    return None


class HttpRSSProvider:
    """Fetch and parse an RSS 2.0 feed over HTTP (live connector)."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        user_agent: str = "SIGNAL-ingest/0.1",
        max_response_bytes: int = MAX_RSS_RESPONSE_BYTES,
        max_items: int = MAX_RSS_ITEMS,
    ) -> None:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("RSS timeout must be finite and positive")
        for name, value, ceiling in (
            ("max_response_bytes", max_response_bytes, MAX_RSS_RESPONSE_BYTES),
            ("max_items", max_items, MAX_RSS_ITEMS),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be an integer between 1 and {ceiling}")
        self._timeout = timeout
        self._user_agent = user_agent
        self._max_response_bytes = max_response_bytes
        self._max_items = max_items

    def _open_response(self, request: urllib.request.Request):
        """Preserve the legacy transport while allowing bounded personal assembly."""
        return urllib.request.urlopen(request, timeout=self._timeout)  # noqa: S310

    def fetch(self, feed_url: str) -> list[RSSItem]:
        """Keep the legacy list interface fail-closed when a feed is incomplete.

        Pending-queue callers use ``fetch_capture`` to retain the bounded, parseable prefix
        and its explicit receipt. Live-smoke callers must still reject an incomplete feed.
        """
        # Preserve original field lengths for legacy consumers that record truncation at
        # their own storage boundary. Returning bounded fields through a metadata-free list
        # would hide that truncation when the live-smoke adapter forwards this list.
        original_items: list[RSSItem] = []
        result = self._fetch_capture(
            feed_url, summary_limit=MAX_RSS_SUMMARY_CHARS, original_items=original_items
        )
        if "response_byte_limit" in result.bounds_reached:
            raise ValueError(
                f"RSS response exceeds or reaches the {self._max_response_bytes}-byte capture bound"
            )
        if "entry_limit" in result.bounds_reached:
            raise ValueError(f"RSS feed exceeds the {self._max_items}-item capture bound")
        return original_items

    def fetch_capture(
        self, feed_url: str, *, summary_limit: int = MAX_RSS_SUMMARY_CHARS
    ) -> RSSCaptureResult:
        return self._fetch_capture(feed_url, summary_limit=summary_limit)

    def _fetch_capture(
        self,
        feed_url: str,
        *,
        summary_limit: int,
        original_items: list[RSSItem] | None = None,
    ) -> RSSCaptureResult:
        """Read no more than the byte ceiling and expose verified capture bounds.

        Without HTTP framing that proves an exact-size body, reaching the byte ceiling is
        conservatively incomplete. No extra overflow byte is read, and no partial XML is
        admitted. The response byte count is bytes actually read, not a remote size guess.
        """
        feed_url = validate_http_url(feed_url)
        request = urllib.request.Request(feed_url, headers={"User-Agent": self._user_agent})
        with self._open_response(request) as response:
            headers = getattr(response, "headers", {})
            declared_length: int | None = None
            if not headers.get("Transfer-Encoding"):
                try:
                    declared_length = int(headers.get("Content-Length", ""))
                except (ValueError, TypeError):
                    pass
                if declared_length is not None and declared_length < 0:
                    declared_length = None
            if declared_length is not None and declared_length > self._max_response_bytes:
                return _unverified_response("response_too_large", 0)
            raw = bytearray()
            while len(raw) < self._max_response_bytes:
                chunk = response.read(self._max_response_bytes - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) == self._max_response_bytes and declared_length != len(raw):
                return _unverified_response("response_byte_limit", len(raw))
            if declared_length is not None and len(raw) != declared_length:
                raise ValueError("RSS response ended before its declared Content-Length")
        root = ET.fromstring(raw)
        if root.tag != "rss":
            raise ValueError("unsupported feed format: expected an RSS 2.0 document")
        channel = root.find("channel")
        if channel is None:
            raise ValueError("invalid RSS 2.0 document: channel is missing")

        def items() -> Iterator[RSSItem]:
            for index, node in enumerate(channel.iterfind("item")):
                # A sentinel exposes that an additional entry exists without reading any
                # fields beyond the entry ceiling. Invalid nodes consume an entry slot too.
                if index == self._max_items:
                    yield RSSItem(guid="", title="", url="", published_at=None)
                    return
                link = (node.findtext("link") or "").strip()
                title = (node.findtext("title") or link).strip()
                guid = (node.findtext("guid") or link).strip()
                summary = (node.findtext("description") or "").strip()
                item = RSSItem(
                    guid=guid or link,
                    title=title,
                    url=link,
                    published_at=_parse_date(node.findtext("pubDate")),
                    summary=summary,
                    source=feed_url,
                    provider_name="http-rss",
                )
                if original_items is not None:
                    try:
                        validate_http_url(link)
                        validate_http_url(normalize_url(link))
                    except ValueError:
                        pass
                    else:
                        original_items.append(item)
                yield item

        return bound_rss_items(
            items(),
            summary_limit=summary_limit,
            max_items=self._max_items,
            response_bytes=len(raw),
        )


def _unverified_response(status: str, response_bytes: int) -> RSSCaptureResult:
    return RSSCaptureResult(
        items=(),
        status=status,
        entries_observed=None,
        entries_processed=0,
        rejected_entries=0,
        response_bytes=response_bytes,
        bounds_reached=("response_byte_limit",),
        total_entries_known=False,
    )
