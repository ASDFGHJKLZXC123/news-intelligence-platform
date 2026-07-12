"""GDELT provider client using stdlib urllib/json transport."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import GDELTArticle, GDELTEvent, GDELTProvider, ensure_utc

UrlOpenCallable = Callable[[str | Request], Any]

DEFAULT_DOC_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_EVENTS_ENDPOINT = "https://api.gdeltproject.org/api/v2/events/search"
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


class GDELTClient(GDELTProvider):
    """Small GDELT API client with injectable transport for deterministic tests."""

    def __init__(
        self,
        *,
        urlopen: UrlOpenCallable | None = None,
        doc_endpoint: str = DEFAULT_DOC_ENDPOINT,
        events_endpoint: str = DEFAULT_EVENTS_ENDPOINT,
    ) -> None:
        self._urlopen = urlopen or stdlib_urlopen
        self._doc_endpoint = doc_endpoint
        self._events_endpoint = events_endpoint

    def search_articles(
        self,
        query: str,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        max_records: int = 50,
    ) -> list[GDELTArticle]:
        _validate_search(query, max_records)
        params: dict[str, str | int] = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": max_records,
        }
        if start_at is not None:
            params["startdatetime"] = _format_gdelt_datetime(start_at)
        if end_at is not None:
            params["enddatetime"] = _format_gdelt_datetime(end_at)

        payload = _read_json(self._urlopen, _build_url(self._doc_endpoint, params))
        return [_parse_article(record) for record in _records(payload, "articles")]

    def search_events(
        self,
        query: str,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        max_records: int = 50,
    ) -> list[GDELTEvent]:
        _validate_search(query, max_records)
        params: dict[str, str | int] = {
            "query": query,
            "format": "json",
            "maxrecords": max_records,
        }
        if start_at is not None:
            params["startdatetime"] = _format_gdelt_datetime(start_at)
        if end_at is not None:
            params["enddatetime"] = _format_gdelt_datetime(end_at)

        payload = _read_json(self._urlopen, _build_url(self._events_endpoint, params))
        return [_parse_event(record) for record in _records(payload, "events", "results")]


def _validate_search(query: str, max_records: int) -> None:
    if not query.strip():
        msg = "query must not be empty"
        raise ValueError(msg)
    if max_records < 1:
        msg = "max_records must be positive"
        raise ValueError(msg)


def _build_url(endpoint: str, params: Mapping[str, str | int]) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(params)}"


def _read_json(urlopen: UrlOpenCallable, target: str | Request) -> Mapping[str, Any]:
    handle = urlopen(target)
    if hasattr(handle, "__enter__"):
        with handle as response:
            body = response.read()
    else:
        body = handle.read()
    if isinstance(body, bytes | bytearray):
        text = body.decode("utf-8")
    else:
        text = str(body)
    payload = json.loads(text)
    if not isinstance(payload, Mapping):
        msg = "GDELT response must be a JSON object"
        raise ValueError(msg)
    return payload


def _records(payload: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _parse_article(record: Mapping[str, Any]) -> GDELTArticle:
    url = _text(_pick(record, "url", "URL"))
    guid = _text(_pick(record, "guid", "id", "url", "URL")) or url
    return GDELTArticle(
        guid=guid,
        title=_text(_pick(record, "title", "Title")),
        url=url,
        seen_at=_parse_gdelt_datetime(_pick(record, "seendate", "seenDate", "date")),
        domain=_text(_pick(record, "domain", "Domain")),
        language=_text(_pick(record, "language", "Language")),
        source_country=_text(_pick(record, "sourcecountry", "sourceCountry", "SourceCountry")),
        source_refs=("gdelt:doc",),
        evidence_refs=(url,) if url else (),
        metadata=record,
    )


def _parse_event(record: Mapping[str, Any]) -> GDELTEvent:
    source_url = _text(_pick(record, "sourceurl", "sourceUrl", "SOURCEURL"))
    event_id = _text(_pick(record, "globaleventid", "GlobalEventID", "global_event_id"))
    return GDELTEvent(
        global_event_id=event_id,
        event_at=_parse_gdelt_datetime(
            _pick(record, "dateadded", "DATEADDED", "sqldate", "SQLDATE")
        ),
        event_code=_text(_pick(record, "eventcode", "EventCode", "EventBaseCode")),
        event_root_code=_text(_pick(record, "eventrootcode", "EventRootCode")),
        actor1_name=_text(_pick(record, "actor1name", "Actor1Name")),
        actor2_name=_text(_pick(record, "actor2name", "Actor2Name")),
        action_geo_country_code=_text(
            _pick(record, "actiongeocountrycode", "ActionGeo_CountryCode")
        ),
        quad_class=_optional_int(_pick(record, "quadclass", "QuadClass")),
        goldstein_scale=_optional_float(_pick(record, "goldsteinscale", "GoldsteinScale")),
        avg_tone=_optional_float(_pick(record, "avgtone", "AvgTone")),
        source_url=source_url,
        source_refs=("gdelt:events",),
        evidence_refs=(source_url,) if source_url else (),
        metadata=record,
    )


def _pick(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _format_gdelt_datetime(value: datetime.datetime) -> str:
    return ensure_utc(value).strftime("%Y%m%d%H%M%S")


def _parse_gdelt_datetime(value: Any) -> datetime.datetime:
    if value in (None, ""):
        return _EPOCH

    text = str(value).strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            parsed = datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue
        return parsed

    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)
