"""Production HTTP RSS provider (RSS 2.0) using only the standard library.

Implements the ``RSSProvider`` protocol with ``urllib`` + ``xml.etree``. This is the live
network connector; tests use ``packages.providers.fakes.FakeRSSProvider`` instead, so the
unit/CI suites never touch the network.
"""

from __future__ import annotations

import datetime
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from packages.providers.base import RSSItem, ensure_utc


def _parse_date(value: str | None) -> datetime.datetime:
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.UTC)
            return ensure_utc(parsed)
        except (TypeError, ValueError):
            pass
    return datetime.datetime.now(datetime.UTC)


class HttpRSSProvider:
    """Fetch and parse an RSS 2.0 feed over HTTP (live connector)."""

    def __init__(self, *, timeout: float = 15.0, user_agent: str = "SIGNAL-ingest/0.1") -> None:
        self._timeout = timeout
        self._user_agent = user_agent

    def fetch(self, feed_url: str) -> list[RSSItem]:
        request = urllib.request.Request(feed_url, headers={"User-Agent": self._user_agent})
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            raw = response.read()
        root = ET.fromstring(raw)
        items: list[RSSItem] = []
        for node in root.iter("item"):
            link = (node.findtext("link") or "").strip()
            if not link:
                continue
            title = (node.findtext("title") or link).strip()
            guid = (node.findtext("guid") or link).strip()
            summary = (node.findtext("description") or "").strip()
            items.append(
                RSSItem(
                    guid=guid or link,
                    title=title,
                    url=link,
                    published_at=_parse_date(node.findtext("pubDate")),
                    summary=summary,
                    source=feed_url,
                    provider_name="http-rss",
                )
            )
        return items
