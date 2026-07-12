"""EIA energy provider client using stdlib urllib/json transport."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import EnergyObservation, EnergyProvider, EnergySeries, ensure_utc

UrlOpenCallable = Callable[[str | Request], Any]

DEFAULT_SERIES_ENDPOINT = "https://api.eia.gov/v2/seriesid/{series_id}"


class EIAClient(EnergyProvider):
    """Small EIA series client with injectable transport."""

    def __init__(
        self,
        *,
        api_key: str,
        urlopen: UrlOpenCallable | None = None,
        series_endpoint: str = DEFAULT_SERIES_ENDPOINT,
    ) -> None:
        if not api_key.strip():
            msg = "EIA api_key is required"
            raise ValueError(msg)
        self._api_key = api_key
        self._urlopen = urlopen or stdlib_urlopen
        self._series_endpoint = series_endpoint

    def fetch_series(self, series_id: str) -> EnergySeries:
        payload = self._fetch_payload(series_id, {})
        return _parse_series(series_id, payload)

    def fetch_series_observations(
        self,
        series_id: str,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> list[EnergyObservation]:
        if limit is not None and limit < 1:
            msg = "limit must be positive"
            raise ValueError(msg)
        params: dict[str, str | int] = {
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
        }
        if start_at is not None:
            params["start"] = ensure_utc(start_at).date().isoformat()
        if end_at is not None:
            params["end"] = ensure_utc(end_at).date().isoformat()
        if limit is not None:
            params["length"] = limit

        payload = self._fetch_payload(series_id, params)
        observations = [
            _parse_observation(series_id, record) for record in _observation_records(payload)
        ]
        if start_at is not None:
            start_at = ensure_utc(start_at)
            observations = [
                observation for observation in observations if observation.observed_at >= start_at
            ]
        if end_at is not None:
            end_at = ensure_utc(end_at)
            observations = [
                observation for observation in observations if observation.observed_at <= end_at
            ]
        if limit is not None:
            observations = observations[:limit]
        return observations

    def _fetch_payload(self, series_id: str, extra_params: Mapping[str, str | int]) -> Mapping[str, Any]:
        if not series_id.strip():
            msg = "series_id must not be empty"
            raise ValueError(msg)
        endpoint = self._series_endpoint.format(series_id=quote(series_id, safe=""))
        params: dict[str, str | int] = {"api_key": self._api_key, **dict(extra_params)}
        return _read_json(self._urlopen, _build_url(endpoint, params))


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
        msg = "EIA response must be a JSON object"
        raise ValueError(msg)
    return payload


def _parse_series(requested_series_id: str, payload: Mapping[str, Any]) -> EnergySeries:
    response = payload.get("response")
    if isinstance(response, Mapping):
        data = response.get("data")
        first = data[0] if isinstance(data, list) and data else {}
        if not isinstance(first, Mapping):
            first = {}
        series_id = _text(first.get("series") or first.get("series_id") or requested_series_id)
        return EnergySeries(
            series_id=series_id,
            name=_text(response.get("description") or first.get("series-description") or series_id),
            units=_text(response.get("units") or first.get("units")),
            frequency=_text(response.get("frequency") or first.get("frequency")),
            geography=_text(first.get("area-name") or first.get("stateDescription")),
            source_refs=(f"eia:series:{series_id}",),
            evidence_refs=(f"eia:series:{series_id}",),
            metadata=payload,
        )

    series = payload.get("series")
    first_series = series[0] if isinstance(series, list) and series else {}
    if not isinstance(first_series, Mapping):
        first_series = {}
    series_id = _text(first_series.get("series_id") or requested_series_id)
    return EnergySeries(
        series_id=series_id,
        name=_text(first_series.get("name") or series_id),
        units=_text(first_series.get("units")),
        frequency=_text(first_series.get("f") or first_series.get("frequency")),
        source_refs=(f"eia:series:{series_id}",),
        evidence_refs=(f"eia:series:{series_id}",),
        metadata=payload,
    )


def _observation_records(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    response = payload.get("response")
    if isinstance(response, Mapping):
        data = response.get("data", [])
        if isinstance(data, list):
            return [record for record in data if isinstance(record, Mapping)]

    series = payload.get("series")
    first_series = series[0] if isinstance(series, list) and series else {}
    if isinstance(first_series, Mapping):
        rows = first_series.get("data", [])
        if isinstance(rows, list):
            return [
                {
                    "period": row[0],
                    "value": row[1],
                    "series_id": first_series.get("series_id"),
                    "units": first_series.get("units"),
                }
                for row in rows
                if isinstance(row, list | tuple) and len(row) >= 2
            ]
    return []


def _parse_observation(
    requested_series_id: str, record: Mapping[str, Any]
) -> EnergyObservation:
    series_id = _text(record.get("series") or record.get("series_id") or requested_series_id)
    observed_at = _parse_period(record.get("period") or record.get("date"))
    return EnergyObservation(
        series_id=series_id,
        observed_at=observed_at,
        value=_parse_value(record.get("value")),
        units=_text(record.get("units")),
        geography=_text(record.get("area-name") or record.get("stateDescription")),
        source_refs=(f"eia:{series_id}",),
        evidence_refs=(f"eia:{series_id}:{observed_at.date().isoformat()}",),
        metadata=record,
    )


def _parse_period(value: Any) -> datetime.datetime:
    text = _text(value)
    if len(text) == 4 and text.isdigit():
        return datetime.datetime(int(text), 1, 1, tzinfo=datetime.UTC)
    if len(text) == 7 and text[4] == "-":
        year, month = text.split("-")
        return datetime.datetime(int(year), int(month), 1, tzinfo=datetime.UTC)
    if len(text) == 6 and text.isdigit():
        return datetime.datetime(int(text[:4]), int(text[4:6]), 1, tzinfo=datetime.UTC)
    if len(text) == 6 and text[4].upper() == "Q":
        return datetime.datetime(int(text[:4]), ((int(text[5]) - 1) * 3) + 1, 1, tzinfo=datetime.UTC)
    try:
        date = datetime.date.fromisoformat(text)
    except ValueError:
        date = datetime.date(1970, 1, 1)
    return datetime.datetime.combine(date, datetime.time(tzinfo=datetime.UTC))


def _parse_value(value: Any) -> float | None:
    if value in (None, "", "NA"):
        return None
    return float(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
