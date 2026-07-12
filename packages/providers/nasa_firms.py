"""NASA FIRMS fire incident provider client using stdlib urllib/json/csv transport."""

from __future__ import annotations

import csv
import datetime
import io
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import GeoIncident, GeoIncidentProvider, ensure_utc

UrlOpenCallable = Callable[[str | Request], Any]

DEFAULT_FIRMS_ENDPOINT = "https://firms.modaps.eosdis.nasa.gov/api/area/json"
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


class NASAFIRMSClient(GeoIncidentProvider):
    """Small NASA FIRMS client with injectable transport."""

    def __init__(
        self,
        *,
        map_key: str | None = None,
        urlopen: UrlOpenCallable | None = None,
        fires_endpoint: str = DEFAULT_FIRMS_ENDPOINT,
    ) -> None:
        self._map_key = map_key
        self._urlopen = urlopen or stdlib_urlopen
        self._fires_endpoint = fires_endpoint

    def search_incidents(
        self,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        region: str | None = None,
        limit: int | None = None,
        source: str = "VIIRS_SNPP_NRT",
        min_confidence: int | None = None,
    ) -> list[GeoIncident]:
        if limit is not None and limit < 1:
            msg = "limit must be positive"
            raise ValueError(msg)
        params: dict[str, str | int] = {"source": source}
        if self._map_key:
            params["map_key"] = self._map_key
        if region is not None:
            params["area"] = region
        if start_at is not None:
            params["start"] = ensure_utc(start_at).date().isoformat()
        if end_at is not None:
            params["end"] = ensure_utc(end_at).date().isoformat()
        if min_confidence is not None:
            params["min_confidence"] = min_confidence
        if limit is not None:
            params["limit"] = limit

        text = _read_text(self._urlopen, _build_url(self._fires_endpoint, params))
        incidents = [_parse_fire(record) for record in _records(text)]
        if limit is not None:
            incidents = incidents[:limit]
        return incidents


def _build_url(endpoint: str, params: Mapping[str, str | int]) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(params)}"


def _read_text(urlopen: UrlOpenCallable, target: str | Request) -> str:
    handle = urlopen(target)
    if hasattr(handle, "__enter__"):
        with handle as response:
            body = response.read()
    else:
        body = handle.read()
    if isinstance(body, bytes | bytearray):
        return body.decode("utf-8")
    return str(body)


def _records(text: str) -> list[Mapping[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("{") or stripped.startswith("["):
        payload = json.loads(stripped)
        if isinstance(payload, Mapping):
            data = payload.get("fires") or payload.get("data") or payload.get("results") or []
            if isinstance(data, list):
                return [record for record in data if isinstance(record, Mapping)]
            return []
        if isinstance(payload, list):
            return [record for record in payload if isinstance(record, Mapping)]
        return []

    reader = csv.DictReader(io.StringIO(stripped))
    return [record for record in reader]


def _parse_fire(record: Mapping[str, Any]) -> GeoIncident:
    latitude = _optional_float(_pick(record, "latitude", "lat")) or 0.0
    longitude = _optional_float(_pick(record, "longitude", "lon", "lng")) or 0.0
    occurred_at = _parse_observed_at(record)
    confidence = _text(_pick(record, "confidence", "confidence_level"))
    fire_id = _text(_pick(record, "id", "fire_id")) or (
        f"nasa-firms:{latitude}:{longitude}:{occurred_at.isoformat()}"
    )
    frp = _optional_float(_pick(record, "frp", "fire_radiative_power"))
    satellite = _text(_pick(record, "satellite"))
    instrument = _text(_pick(record, "instrument"))
    return GeoIncident(
        incident_id=fire_id,
        incident_type="fire",
        title=f"Fire detection {fire_id}",
        occurred_at=occurred_at,
        latitude=latitude,
        longitude=longitude,
        magnitude=frp,
        severity=confidence,
        place=_text(_pick(record, "area", "country_id")),
        provider_name="nasa-firms",
        source_refs=("nasa-firms:fires",),
        evidence_refs=(fire_id,),
        metadata={
            **dict(record),
            "satellite": satellite,
            "instrument": instrument,
        },
    )


def _parse_observed_at(record: Mapping[str, Any]) -> datetime.datetime:
    timestamp = _pick(record, "timestamp", "acq_datetime")
    if timestamp not in (None, ""):
        parsed = _parse_datetime(timestamp)
        if parsed is not None:
            return parsed
    date_text = _text(_pick(record, "acq_date", "date"))
    time_text = _text(_pick(record, "acq_time", "time")).zfill(4)
    if date_text:
        try:
            date = datetime.date.fromisoformat(date_text)
        except ValueError:
            return _EPOCH
        hour = int(time_text[:2] or "0")
        minute = int(time_text[2:4] or "0")
        return datetime.datetime.combine(date, datetime.time(hour, minute, tzinfo=datetime.UTC))
    return _EPOCH


def _parse_datetime(value: Any) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _pick(record: Mapping[str, Any], *keys: str) -> Any:
    lowercase = {str(key).lower(): value for key, value in record.items()}
    for key in keys:
        if key in record:
            return record[key]
        value = lowercase.get(key.lower())
        if value is not None:
            return value
    return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
