"""GLEIF entity identity provider client using stdlib urllib/json transport."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import EntityIdentityProvider, LEIRecord, LEIRelationship

UrlOpenCallable = Callable[[str | Request], Any]

DEFAULT_LEI_RECORDS_ENDPOINT = "https://api.gleif.org/api/v1/lei-records"
DEFAULT_RELATIONSHIPS_ENDPOINT = "https://api.gleif.org/api/v1/relationship-records"


class GLEIFClient(EntityIdentityProvider):
    """Small GLEIF API client with injectable transport."""

    def __init__(
        self,
        *,
        urlopen: UrlOpenCallable | None = None,
        records_endpoint: str = DEFAULT_LEI_RECORDS_ENDPOINT,
        relationships_endpoint: str = DEFAULT_RELATIONSHIPS_ENDPOINT,
    ) -> None:
        self._urlopen = urlopen or stdlib_urlopen
        self._records_endpoint = records_endpoint
        self._relationships_endpoint = relationships_endpoint

    def search_records(
        self,
        query: str,
        *,
        country_code: str | None = None,
        limit: int = 20,
    ) -> list[LEIRecord]:
        if not query.strip():
            msg = "query must not be empty"
            raise ValueError(msg)
        if limit < 1:
            msg = "limit must be positive"
            raise ValueError(msg)
        params: dict[str, str | int] = {
            "filter[entity.legalName]": query,
            "page[size]": limit,
        }
        if country_code is not None:
            params["filter[entity.legalAddress.country]"] = country_code
        payload = _read_json(self._urlopen, _build_url(self._records_endpoint, params))
        return [_parse_record(record) for record in _data_records(payload)]

    def fetch_relationships(
        self,
        lei: str,
        *,
        relationship_type: str | None = None,
        limit: int = 100,
    ) -> list[LEIRelationship]:
        if not lei.strip():
            msg = "lei must not be empty"
            raise ValueError(msg)
        if limit < 1:
            msg = "limit must be positive"
            raise ValueError(msg)
        params: dict[str, str | int] = {
            "filter[relationship.startNode.nodeID]": lei,
            "page[size]": limit,
        }
        if relationship_type is not None:
            params["filter[relationship.type]"] = relationship_type
        payload = _read_json(self._urlopen, _build_url(self._relationships_endpoint, params))
        return [_parse_relationship(lei, record) for record in _data_records(payload)]


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
        msg = "GLEIF response must be a JSON object"
        raise ValueError(msg)
    return payload


def _data_records(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data", [])
    if isinstance(data, Mapping):
        return [data]
    if isinstance(data, list):
        return [record for record in data if isinstance(record, Mapping)]
    return []


def _parse_record(record: Mapping[str, Any]) -> LEIRecord:
    attrs = _attrs(record)
    entity = _mapping(attrs.get("entity"))
    registration = _mapping(attrs.get("registration"))
    legal_address = _mapping(entity.get("legalAddress"))
    headquarters_address = _mapping(entity.get("headquartersAddress"))
    links = _mapping(record.get("links"))
    lei = _text(record.get("id") or attrs.get("lei"))
    legal_name = _legal_name(entity.get("legalName")) or _text(attrs.get("legalName"))
    return LEIRecord(
        lei=lei,
        legal_name=legal_name,
        entity_status=_text(entity.get("status") or attrs.get("entityStatus")),
        registration_status=_text(registration.get("status") or attrs.get("registrationStatus")),
        country_code=_text(
            legal_address.get("country")
            or headquarters_address.get("country")
            or attrs.get("countryCode")
        ),
        jurisdiction=_text(entity.get("jurisdiction") or attrs.get("jurisdiction")),
        legal_form=_legal_form(entity.get("legalForm")),
        last_updated_at=_parse_datetime(
            registration.get("lastUpdateDate") or attrs.get("lastUpdateDate")
        ),
        next_renewal_at=_parse_datetime(
            registration.get("nextRenewalDate") or attrs.get("nextRenewalDate")
        ),
        source_refs=("gleif:lei-records",),
        evidence_refs=(_text(links.get("self")) or f"gleif:lei:{lei}",),
        metadata=record,
    )


def _parse_relationship(lei: str, record: Mapping[str, Any]) -> LEIRelationship:
    attrs = _attrs(record)
    relationship = _mapping(attrs.get("relationship"))
    start_node = _mapping(relationship.get("startNode") or attrs.get("startNode"))
    end_node = _mapping(relationship.get("endNode") or attrs.get("endNode"))
    periods = attrs.get("periods") or relationship.get("periods") or []
    first_period = periods[0] if isinstance(periods, list) and periods else {}
    if not isinstance(first_period, Mapping):
        first_period = {}
    start_lei = _text(start_node.get("nodeID") or attrs.get("startNodeID"))
    end_lei = _text(end_node.get("nodeID") or attrs.get("endNodeID"))
    related_lei = end_lei if start_lei == lei else start_lei
    links = _mapping(record.get("links"))
    relationship_id = _text(record.get("id") or attrs.get("id"))
    return LEIRelationship(
        relationship_id=relationship_id,
        lei=lei,
        related_lei=related_lei,
        relationship_type=_text(relationship.get("type") or attrs.get("relationshipType")),
        status=_text(attrs.get("status") or relationship.get("status")),
        start_at=_parse_datetime(first_period.get("startDate") or attrs.get("startDate")),
        end_at=_parse_datetime(first_period.get("endDate") or attrs.get("endDate")),
        source_refs=("gleif:relationship-records",),
        evidence_refs=(_text(links.get("self")) or f"gleif:relationship:{relationship_id}",),
        metadata=record,
    )


def _attrs(record: Mapping[str, Any]) -> Mapping[str, Any]:
    attrs = record.get("attributes", record)
    return attrs if isinstance(attrs, Mapping) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _legal_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("name") or value.get("value"))
    return _text(value)


def _legal_form(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("id") or value.get("other"))
    return _text(value)


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
