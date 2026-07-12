"""FRED provider client using stdlib urllib/json transport."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import FREDObservation, FREDProvider

UrlOpenCallable = Callable[[str | Request], Any]

DEFAULT_OBSERVATIONS_ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"


class FREDClient(FREDProvider):
    """Small FRED observations client with injectable transport."""

    def __init__(
        self,
        *,
        api_key: str,
        urlopen: UrlOpenCallable | None = None,
        observations_endpoint: str = DEFAULT_OBSERVATIONS_ENDPOINT,
    ) -> None:
        if not api_key:
            msg = "FRED api_key is required"
            raise ValueError(msg)
        self._api_key = api_key
        self._urlopen = urlopen or stdlib_urlopen
        self._observations_endpoint = observations_endpoint

    def fetch_series_observations(
        self,
        series_id: str,
        *,
        observation_start: datetime.date | None = None,
        observation_end: datetime.date | None = None,
        limit: int | None = None,
    ) -> list[FREDObservation]:
        if not series_id.strip():
            msg = "series_id must not be empty"
            raise ValueError(msg)
        params: dict[str, str | int] = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
        }
        if observation_start is not None:
            params["observation_start"] = observation_start.isoformat()
        if observation_end is not None:
            params["observation_end"] = observation_end.isoformat()
        if limit is not None:
            if limit < 1:
                msg = "limit must be positive"
                raise ValueError(msg)
            params["limit"] = limit

        payload = _read_json(self._urlopen, _build_url(self._observations_endpoint, params))
        observations = payload.get("observations", [])
        if not isinstance(observations, list):
            msg = "FRED observations response must contain an observations list"
            raise ValueError(msg)
        return [
            _parse_observation(series_id, observation)
            for observation in observations
            if isinstance(observation, Mapping)
        ]


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
        msg = "FRED response must be a JSON object"
        raise ValueError(msg)
    return payload


def _parse_observation(series_id: str, record: Mapping[str, Any]) -> FREDObservation:
    observed_on = _parse_date(record.get("date"))
    return FREDObservation(
        series_id=series_id,
        observed_at=_date_to_utc(observed_on),
        value=_parse_value(record.get("value")),
        realtime_start=_parse_optional_date(record.get("realtime_start")),
        realtime_end=_parse_optional_date(record.get("realtime_end")),
        source_refs=(f"fred:{series_id}",),
        evidence_refs=(f"fred:{series_id}:{observed_on.isoformat()}",),
        metadata=record,
    )


def _parse_value(value: Any) -> float | None:
    if value in (None, "", "."):
        return None
    return float(value)


def _parse_optional_date(value: Any) -> datetime.date | None:
    if value in (None, ""):
        return None
    return _parse_date(value)


def _parse_date(value: Any) -> datetime.date:
    if value in (None, ""):
        msg = "date value is required"
        raise ValueError(msg)
    return datetime.date.fromisoformat(str(value))


def _date_to_utc(value: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(value, datetime.time(tzinfo=datetime.UTC))
