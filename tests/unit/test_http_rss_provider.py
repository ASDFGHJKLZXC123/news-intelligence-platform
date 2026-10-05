"""Bounded RSS 2.0 adapter behavior used by personal capture."""

from __future__ import annotations

import datetime
import io
import xml.etree.ElementTree as ET
from collections.abc import Iterator

import pytest

from packages.providers.base import RSSItem
from services.ingestion.capture_bounds import (
    MAX_RSS_RESPONSE_BYTES,
    bound_rss_items,
    validate_http_url,
)
from services.ingestion.http_provider import HttpRSSProvider
from services.ingestion.normalize import url_hash


def _response(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: io.BytesIO(payload))


def test_valid_empty_rss_is_a_healthy_empty_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    _response(monkeypatch, b"<rss version='2.0'><channel><title>Empty</title></channel></rss>")
    assert HttpRSSProvider().fetch("https://offline.example/feed.xml") == []


@pytest.mark.parametrize(
    "payload",
    [
        b"<feed xmlns='http://www.w3.org/2005/Atom'><title>Atom</title></feed>",
        b"<html><body>Feed unavailable</body></html>",
        b"<rss version='2.0'></rss>",
    ],
)
def test_unsupported_or_invalid_xml_is_not_quiet_success(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    _response(monkeypatch, payload)
    with pytest.raises(ValueError, match="unsupported feed format|channel is missing"):
        HttpRSSProvider().fetch("https://offline.example/feed.xml")


def test_unknown_publication_time_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _response(
        monkeypatch,
        b"""<rss version='2.0'><channel><item>
        <title>Agency update</title><link>https://offline.example/story</link>
        <pubDate>not a date</pubDate></item></channel></rss>""",
    )
    [item] = HttpRSSProvider().fetch("https://offline.example/feed.xml")
    assert item.published_at is None


def test_response_body_and_parsed_item_count_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _response(monkeypatch, b"<rss><channel>" + b"x" * 80 + b"</channel></rss>")
    with pytest.raises(ValueError, match="response exceeds"):
        HttpRSSProvider(max_response_bytes=32).fetch("https://offline.example/feed.xml")

    payload = (
        b"<rss><channel>"
        b"<item><title>A</title><link>https://offline.example/a</link></item>"
        b"<item><title>B</title><link>https://offline.example/b</link></item>"
        b"</channel></rss>"
    )
    _response(monkeypatch, payload)
    with pytest.raises(ValueError, match="feed exceeds"):
        HttpRSSProvider(max_items=1).fetch("https://offline.example/feed.xml")


def _feed(entries: int, *, invalid_entries: int = 0) -> bytes:
    items = "".join(
        f"<item><title>Article {index}</title><link>https://offline.example/{index}</link></item>"
        for index in range(entries)
    )
    return ("<rss><channel>" + "<item />" * invalid_entries + items + "</channel></rss>").encode()


def test_501_entries_returns_500_with_an_explicit_unknown_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _feed(503)
    _response(monkeypatch, payload)
    capture = HttpRSSProvider().fetch_capture("https://offline.example/feed.xml")
    assert len(capture.items) == capture.entries_processed == 500
    assert capture.entries_observed == 501
    assert capture.total_entries_known is False
    assert capture.status == "entry_limit_reached"
    assert capture.bounds_reached == ("entry_limit",)
    assert capture.response_bytes == len(payload)
    assert capture.rejected_entries == 0


def test_exactly_500_entries_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    _response(monkeypatch, _feed(500))
    capture = HttpRSSProvider().fetch_capture("https://offline.example/feed.xml")
    assert len(capture.items) == capture.entries_observed == capture.entries_processed == 500
    assert capture.status == "complete"
    assert capture.total_entries_known
    assert capture.bounds_reached == ()


def test_invalid_entries_count_towards_the_entry_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _response(monkeypatch, _feed(1, invalid_entries=500))
    capture = HttpRSSProvider().fetch_capture("https://offline.example/feed.xml")
    assert capture.items == ()
    assert capture.entries_observed == 501
    assert capture.entries_processed == capture.rejected_entries == 500
    assert capture.status == "entry_limit_reached"


class _TrackedResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(payload)
        self.headers = headers or {}
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        assert size >= 0, "feed body reads must always be bounded"
        chunk = super().read(size)
        self.bytes_read += len(chunk)
        return chunk


@pytest.mark.parametrize("declared", [False, True])
def test_oversized_capture_reads_at_most_two_mib_and_retains_no_partial_xml(
    monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    payload = _feed(1) + b" " * MAX_RSS_RESPONSE_BYTES
    response = _TrackedResponse(
        payload, headers={"Content-Length": str(len(payload))} if declared else None
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)
    capture = HttpRSSProvider().fetch_capture("https://offline.example/feed.xml")
    assert response.bytes_read <= MAX_RSS_RESPONSE_BYTES
    assert capture.response_bytes == response.bytes_read
    assert capture.items == ()
    assert capture.entries_observed is None
    assert capture.entries_processed == 0
    assert capture.total_entries_known is False
    assert capture.bounds_reached == ("response_byte_limit",)
    assert capture.status == ("response_too_large" if declared else "response_byte_limit")


@pytest.mark.parametrize("declared", [False, True])
def test_exact_byte_ceiling_requires_framing_proof_to_be_complete(
    monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    payload = _feed(1)
    response = _TrackedResponse(
        payload, headers={"Content-Length": str(len(payload))} if declared else None
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)
    capture = HttpRSSProvider(max_response_bytes=len(payload)).fetch_capture(
        "https://offline.example/feed.xml"
    )
    assert response.bytes_read == len(payload)
    assert capture.status == ("complete" if declared else "response_byte_limit")
    assert len(capture.items) == int(declared)


def test_short_stream_reads_are_not_mistaken_for_the_end_of_a_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ShortReads(_TrackedResponse):
        def read(self, size: int = -1) -> bytes:
            return super().read(min(size, 7))

    payload = _feed(1)
    response = ShortReads(payload)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)
    capture = HttpRSSProvider().fetch_capture("https://offline.example/feed.xml")
    assert capture.status == "complete"
    assert capture.response_bytes == len(payload)
    assert len(capture.items) == 1


def test_truncated_transport_is_not_a_complete_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _feed(1)
    response = _TrackedResponse(payload, headers={"Content-Length": str(len(payload) + 1)})
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)
    with pytest.raises(ValueError, match="Content-Length"):
        HttpRSSProvider().fetch_capture("https://offline.example/feed.xml")


def test_malformed_xml_after_the_entry_bound_cannot_yield_unverified_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _response(monkeypatch, _feed(501) + b"<broken")
    with pytest.raises(ET.ParseError, match="junk after document element"):
        HttpRSSProvider().fetch_capture("https://offline.example/feed.xml")


def test_bounds_preserve_canonical_identity_original_url_and_field_truncations() -> None:
    original = "HTTPS://Example.com/article/?utm_source=rss&id=7#fragment"
    item = RSSItem(
        guid="g" * 5_000,
        title="T" * 600,
        url=original,
        published_at=None,
        summary="S" * 3_000,
    )
    [bounded] = bound_rss_items([item], summary_limit=700).items
    assert bounded.canonical_url == "https://example.com/article?id=7"
    assert bounded.url_hash == url_hash(original)
    assert bounded.original_url == bounded.item.url == original
    assert len(bounded.item.title) == 512
    assert len(bounded.item.summary) == 700
    assert len(bounded.item.guid) == 4_096
    assert bounded.field_truncations == ("guid", "title", "summary")
    assert bounded.item.published_at is None


def test_larger_source_permission_cannot_raise_the_application_snippet_bound() -> None:
    item = RSSItem(
        guid="a", title="a", url="https://offline.example/a", published_at=None, summary="s" * 3_000
    )
    [bounded] = bound_rss_items([item], summary_limit=9_000).items
    assert len(bounded.item.summary) == 2_000
    assert bounded.field_truncations == ("summary",)
    [restricted] = bound_rss_items([item], summary_limit=0).items
    assert restricted.item.summary == ""
    assert restricted.field_truncations == ("summary",)


def test_legacy_list_preserves_field_lengths_for_its_own_truncation_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        "<rss><channel><item><link>https://offline.example/a</link><title>"
        + "T" * 600
        + "</title><description>"
        + "S" * 3_000
        + "</description></item></channel></rss>"
    ).encode()
    _response(monkeypatch, payload)
    [legacy] = HttpRSSProvider().fetch("https://offline.example/feed.xml")
    assert len(legacy.title) == 600
    assert len(legacy.summary) == 3_000
    _response(monkeypatch, payload)
    [bounded] = HttpRSSProvider().fetch_capture("https://offline.example/feed.xml").items
    assert len(bounded.item.title) == 512
    assert len(bounded.item.summary) == 2_000
    assert bounded.field_truncations == ("title", "summary")


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://offline.example/a",
        "/relative/article",
        "https:///no-host",
        "https://user:secret@offline.example/article",
        "https://offline.example:bad/article",
        "https://offline.example:70000/article",
        "https://bad host.example/article",
        "https://bad..example/article",
        "https://offline.example/line\nbreak",
        "https://offline.example/path%xx",
        "https://offline.example/" + "a" * 4_096,
    ],
)
def test_invalid_urls_are_rejected_without_corrupting_their_identity(url: str) -> None:
    item = RSSItem(guid="a", title="a", url=url, published_at=None)
    capture = bound_rss_items([item])
    assert capture.items == ()
    assert capture.entries_processed == capture.entries_observed == capture.rejected_entries == 1
    with pytest.raises(ValueError):
        validate_http_url(url)


def test_canonical_url_expansion_cannot_be_silently_sliced() -> None:
    # URL encoding can lengthen a query even when its original string fits the bound.
    original = "https://offline.example/a?search=" + "é" * 1_000
    capture = bound_rss_items([RSSItem(guid="a", title="a", url=original, published_at=None)])
    assert capture.items == ()
    assert capture.rejected_entries == 1


def test_injected_provider_iterator_is_not_drained_past_one_sentinel() -> None:
    visited: list[int] = []

    def items() -> Iterator[RSSItem]:
        for index in range(10_000):
            visited.append(index)
            yield RSSItem(
                guid=str(index),
                title="a",
                url=f"https://offline.example/{index}",
                published_at=None,
            )

    result = bound_rss_items(items())
    assert visited == list(range(501))
    assert len(result.items) == 500
    assert result.response_bytes is None
    assert result.entries_observed == 501
    assert not result.total_entries_known


def test_valid_non_utc_publisher_time_is_normalized_not_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _response(
        monkeypatch,
        b"""<rss><channel><item><link>https://offline.example/a</link>
        <pubDate>Sun, 20 Sep 2026 01:15:00 -0700</pubDate></item></channel></rss>""",
    )
    [bounded] = HttpRSSProvider().fetch_capture("https://offline.example/feed.xml").items
    assert bounded.item.published_at == datetime.datetime(2026, 9, 20, 8, 15, tzinfo=datetime.UTC)


@pytest.mark.parametrize("value", ["", "not a date", "Sun, 99 Sep 2026 01:15:00 GMT"])
def test_missing_or_invalid_publisher_dates_stay_unknown(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _response(
        monkeypatch,
        f"<rss><channel><item><link>https://offline.example/a</link><pubDate>{value}</pubDate></item></channel></rss>".encode(),
    )
    [bounded] = HttpRSSProvider().fetch_capture("https://offline.example/feed.xml").items
    assert bounded.item.published_at is None


@pytest.mark.parametrize(
    "options",
    [
        {"max_items": 501},
        {"max_items": 0},
        {"max_items": True},
        {"max_response_bytes": MAX_RSS_RESPONSE_BYTES + 1},
        {"max_response_bytes": -1},
        {"timeout": float("inf")},
        {"timeout": float("nan")},
        {"timeout": 0},
    ],
)
def test_invalid_capture_bounds_cannot_disable_limits(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        HttpRSSProvider(**options)
