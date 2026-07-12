"""ReliefWeb humanitarian provider client using stdlib urllib/json transport."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import HumanitarianProvider, HumanitarianReport

UrlOpenCallable = Callable[[str | Request], Any]

DEFAULT_REPORTS_ENDPOINT = "https://api.reliefweb.int/v1/reports"


class ReliefWebClient(HumanitarianProvider):
    """Small ReliefWeb reports client with injectable transport."""

    def __init__(
        self,
        *,
        app_name: str = "news-intelligence-platform",
        urlopen: UrlOpenCallable | None = None,
        reports_endpoint: str = DEFAULT_REPORTS_ENDPOINT,
    ) -> None:
        if not app_name.strip():
            msg = "app_name must not be empty"
            raise ValueError(msg)
        self._app_name = app_name
        self._urlopen = urlopen or stdlib_urlopen
        self._reports_endpoint = reports_endpoint

    def search_reports(
        self,
        query: str,
        *,
        country_code: str | None = None,
        disaster_type: str | None = None,
        limit: int = 20,
    ) -> list[HumanitarianReport]:
        if not query.strip():
            msg = "query must not be empty"
            raise ValueError(msg)
        if limit < 1:
            msg = "limit must be positive"
            raise ValueError(msg)
        params: dict[str, str | int] = {
            "appname": self._app_name,
            "profile": "full",
            "query[value]": query,
            "limit": limit,
        }
        if country_code is not None:
            params["filter[country.iso3]"] = country_code
        if disaster_type is not None:
            params["filter[disaster_type.name]"] = disaster_type

        payload = _read_json(self._urlopen, _build_url(self._reports_endpoint, params))
        reports = [_parse_report(record) for record in _data_records(payload)]
        if country_code is not None:
            reports = [report for report in reports if country_code in report.country_codes]
        if disaster_type is not None:
            reports = [report for report in reports if disaster_type in report.disaster_types]
        return reports[:limit]


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
        msg = "ReliefWeb response must be a JSON object"
        raise ValueError(msg)
    return payload


def _data_records(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data", [])
    if isinstance(data, list):
        return [record for record in data if isinstance(record, Mapping)]
    return []


def _parse_report(record: Mapping[str, Any]) -> HumanitarianReport:
    fields = record.get("fields", record)
    if not isinstance(fields, Mapping):
        fields = {}
    report_id = _text(record.get("id") or fields.get("id"))
    url = _text(fields.get("url") or record.get("href"))
    created = _field_datetime(fields, "date.created", "date.original", "created")
    changed = _field_datetime(fields, "date.changed", "changed")
    source_names = _names(fields.get("source"))
    return HumanitarianReport(
        report_id=report_id,
        title=_text(fields.get("title")),
        url=url,
        published_at=created or datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC),
        source=", ".join(source_names),
        summary=_text(fields.get("body") or fields.get("summary")),
        country_codes=_country_codes(fields.get("country")),
        disaster_types=_names(fields.get("disaster_type")),
        themes=_names(fields.get("theme")),
        updated_at=changed,
        source_refs=("reliefweb:reports",),
        evidence_refs=(url,) if url else (),
        metadata=record,
    )


def _field_datetime(fields: Mapping[str, Any], *keys: str) -> datetime.datetime | None:
    for key in keys:
        value = fields.get(key)
        if value not in (None, ""):
            return _parse_datetime(value)
    return None


def _country_codes(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(code for item in value for code in [_country_code(item)] if code)
    if isinstance(value, Mapping):
        code = _country_code(value)
        return (code,) if code else ()
    text = _text(value)
    return (text,) if text else ()


def _country_code(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("iso3") or value.get("shortname") or value.get("name"))
    return _text(value)


def _names(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(
            _text(item.get("name") if isinstance(item, Mapping) else item)
            for item in value
            if _text(item.get("name") if isinstance(item, Mapping) else item)
        )
    if isinstance(value, Mapping):
        name = _text(value.get("name"))
        return (name,) if name else ()
    text = _text(value)
    return (text,) if text else ()


def _parse_datetime(value: Any) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            date = datetime.date.fromisoformat(text)
        except ValueError:
            return None
        return datetime.datetime.combine(date, datetime.time(tzinfo=datetime.UTC))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
