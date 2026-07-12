"""USGS earthquake provider client using stdlib urllib/json transport."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import GeoIncident, GeoIncidentProvider, ensure_utc

UrlOpenCallable = Callable[[str | Request], Any]

DEFAULT_EARTHQUAKE_ENDPOINT = "https://earthquake.usgs.gov/fdsnws/event/1/query"
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


class USGSEarthquakeClient(GeoIncidentProvider):
    """Small USGS earthquake query client with injectable transport."""

    def __init__(
        self,
        *,
        urlopen: UrlOpenCallable | None = None,
        earthquake_endpoint: str = DEFAULT_EARTHQUAKE_ENDPOINT,
    ) -> None:
        self._urlopen = urlopen or stdlib_urlopen
        self._earthquake_endpoint = earthquake_endpoint

    def search_incidents(
        self,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        region: str | None = None,
        limit: int | None = None,
        min_magnitude: float | None = None,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> list[GeoIncident]:
        if limit is not None and limit < 1:
            msg = "limit must be positive"
            raise ValueError(msg)
        params: dict[str, str | int | float] = {
            "format": "geojson",
            "eventtype": "earthquake",
            "orderby": "time",
        }
        if start_at is not None:
            params["starttime"] = ensure_utc(start_at).isoformat()
        if end_at is not None:
            params["endtime"] = ensure_utc(end_at).isoformat()
        if region is not None:
            params["region"] = region
        if min_magnitude is not None:
            params["minmagnitude"] = min_magnitude
        if limit is not None:
            params["limit"] = limit
        if bbox is not None:
            min_latitude, min_longitude, max_latitude, max_longitude = bbox
            params.update(
                {
                    "minlatitude": min_latitude,
                    "minlongitude": min_longitude,
                    "maxlatitude": max_latitude,
                    "maxlongitude": max_longitude,
                }
            )

        payload = _read_json(self._urlopen, _build_url(self._earthquake_endpoint, params))
        incidents = [_parse_feature(feature) for feature in _features(payload)]
        if limit is not None:
            incidents = incidents[:limit]
        return incidents


def _build_url(endpoint: str, params: Mapping[str, str | int | float]) -> str:
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
        msg = "USGS response must be a JSON object"
        raise ValueError(msg)
    return payload


def _features(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    features = payload.get("features", [])
    if isinstance(features, list):
        return [feature for feature in features if isinstance(feature, Mapping)]
    return []


def _parse_feature(feature: Mapping[str, Any]) -> GeoIncident:
    properties = feature.get("properties", {})
    geometry = feature.get("geometry", {})
    if not isinstance(properties, Mapping):
        properties = {}
    if not isinstance(geometry, Mapping):
        geometry = {}
    coordinates = geometry.get("coordinates", [])
    if not isinstance(coordinates, list):
        coordinates = []
    longitude = _float_at(coordinates, 0)
    latitude = _float_at(coordinates, 1)
    depth_km = _optional_float(_at(coordinates, 2))
    event_id = _text(feature.get("id") or properties.get("code"))
    url = _text(properties.get("url") or properties.get("detail"))
    occurred_at = _parse_epoch_millis(properties.get("time"))
    return GeoIncident(
        incident_id=event_id,
        incident_type="earthquake",
        title=_text(properties.get("title")) or _text(properties.get("place")),
        occurred_at=occurred_at,
        latitude=latitude,
        longitude=longitude,
        magnitude=_optional_float(properties.get("mag")),
        depth_km=depth_km,
        severity=_text(properties.get("alert") or properties.get("sig")),
        place=_text(properties.get("place")),
        url=url,
        provider_name="usgs",
        source_refs=("usgs:earthquakes",),
        evidence_refs=(url or f"usgs:event:{event_id}",),
        metadata=feature,
    )


def _parse_epoch_millis(value: Any) -> datetime.datetime:
    if value in (None, ""):
        return _EPOCH
    return datetime.datetime.fromtimestamp(float(value) / 1000.0, tz=datetime.UTC)


def _float_at(values: list[Any], index: int) -> float:
    value = _at(values, index)
    if value in (None, ""):
        return 0.0
    return float(value)


def _at(values: list[Any], index: int) -> Any:
    if index >= len(values):
        return None
    return values[index]


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
