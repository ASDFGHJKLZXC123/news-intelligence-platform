"""World Bank country indicator provider client using stdlib urllib/json transport."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import (
    CountryIndicator,
    CountryIndicatorObservation,
    CountryIndicatorProvider,
)

UrlOpenCallable = Callable[[str | Request], Any]

DEFAULT_INDICATOR_ENDPOINT = "https://api.worldbank.org/v2/indicator/{indicator_id}"
DEFAULT_OBSERVATIONS_ENDPOINT = (
    "https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator_id}"
)


class WorldBankClient(CountryIndicatorProvider):
    """Small World Bank API client with injectable transport."""

    def __init__(
        self,
        *,
        urlopen: UrlOpenCallable | None = None,
        indicator_endpoint: str = DEFAULT_INDICATOR_ENDPOINT,
        observations_endpoint: str = DEFAULT_OBSERVATIONS_ENDPOINT,
    ) -> None:
        self._urlopen = urlopen or stdlib_urlopen
        self._indicator_endpoint = indicator_endpoint
        self._observations_endpoint = observations_endpoint

    def fetch_indicator_metadata(self, indicator_id: str) -> CountryIndicator | None:
        if not indicator_id.strip():
            msg = "indicator_id must not be empty"
            raise ValueError(msg)
        endpoint = self._indicator_endpoint.format(indicator_id=quote(indicator_id, safe=""))
        payload = _read_json(self._urlopen, _build_url(endpoint, {"format": "json"}))
        records = _world_bank_records(payload)
        if not records:
            return None
        return _parse_indicator(records[0])

    def fetch_indicator_observations(
        self,
        country_code: str,
        indicator_id: str,
        *,
        start_year: int | None = None,
        end_year: int | None = None,
        limit: int | None = None,
    ) -> list[CountryIndicatorObservation]:
        if not country_code.strip():
            msg = "country_code must not be empty"
            raise ValueError(msg)
        if not indicator_id.strip():
            msg = "indicator_id must not be empty"
            raise ValueError(msg)
        if limit is not None and limit < 1:
            msg = "limit must be positive"
            raise ValueError(msg)
        params: dict[str, str | int] = {"format": "json"}
        if limit is not None:
            params["per_page"] = limit
        if start_year is not None or end_year is not None:
            start = start_year if start_year is not None else ""
            end = end_year if end_year is not None else ""
            params["date"] = f"{start}:{end}"
        endpoint = self._observations_endpoint.format(
            country_code=quote(country_code, safe=""),
            indicator_id=quote(indicator_id, safe=""),
        )
        payload = _read_json(self._urlopen, _build_url(endpoint, params))
        observations = [
            _parse_observation(country_code, indicator_id, record)
            for record in _world_bank_records(payload)
        ]
        if start_year is not None:
            observations = [
                observation for observation in observations if observation.date.year >= start_year
            ]
        if end_year is not None:
            observations = [
                observation for observation in observations if observation.date.year <= end_year
            ]
        if limit is not None:
            observations = observations[:limit]
        return observations


def _build_url(endpoint: str, params: Mapping[str, str | int]) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(params)}"


def _read_json(urlopen: UrlOpenCallable, target: str | Request) -> Any:
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
    return json.loads(text)


def _world_bank_records(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list):
        return [record for record in payload[1] if isinstance(record, Mapping)]
    if isinstance(payload, Mapping):
        data = payload.get("data") or payload.get("results") or []
        if isinstance(data, list):
            return [record for record in data if isinstance(record, Mapping)]
    return []


def _parse_indicator(record: Mapping[str, Any]) -> CountryIndicator:
    indicator_id = _text(record.get("id") or record.get("indicator_id"))
    source = record.get("source")
    source_name = _text(source.get("value")) if isinstance(source, Mapping) else _text(source)
    return CountryIndicator(
        indicator_id=indicator_id,
        name=_text(record.get("name")),
        source=source_name,
        unit=_text(record.get("unit")),
        frequency=_text(record.get("periodicity") or record.get("frequency")),
        topics=_topics(record.get("topics") or record.get("topic")),
        source_refs=(f"world-bank:indicator:{indicator_id}",),
        evidence_refs=(f"world-bank:indicator:{indicator_id}",),
        metadata=record,
    )


def _parse_observation(
    requested_country_code: str, requested_indicator_id: str, record: Mapping[str, Any]
) -> CountryIndicatorObservation:
    country = record.get("country")
    indicator = record.get("indicator")
    country_code = _text(record.get("countryiso3code") or requested_country_code)
    indicator_id = _text(
        indicator.get("id") if isinstance(indicator, Mapping) else None
    ) or requested_indicator_id
    observed_at = _parse_period(record.get("date"))
    return CountryIndicatorObservation(
        country_code=country_code,
        indicator_id=indicator_id,
        observed_at=observed_at,
        value=_parse_value(record.get("value")),
        country_name=_text(country.get("value")) if isinstance(country, Mapping) else _text(country),
        unit=_text(record.get("unit")),
        source_refs=(f"world-bank:{country_code}:{indicator_id}",),
        evidence_refs=(f"world-bank:{country_code}:{indicator_id}:{observed_at.date().year}",),
        metadata=record,
    )


def _topics(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(
            _text(item.get("value") if isinstance(item, Mapping) else item)
            for item in value
            if _text(item.get("value") if isinstance(item, Mapping) else item)
        )
    text = _text(value)
    return (text,) if text else ()


def _parse_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _parse_period(value: Any) -> datetime.datetime:
    text = _text(value)
    if len(text) == 4 and text.isdigit():
        return datetime.datetime(int(text), 1, 1, tzinfo=datetime.UTC)
    try:
        date = datetime.date.fromisoformat(text)
    except ValueError:
        date = datetime.date(1970, 1, 1)
    return datetime.datetime.combine(date, datetime.time(tzinfo=datetime.UTC))


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
