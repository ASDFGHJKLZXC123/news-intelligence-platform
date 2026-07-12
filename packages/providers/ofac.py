"""OFAC sanctions provider client using stdlib urllib/json/XML transport."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen
from xml.etree import ElementTree

from packages.providers.base import (
    SanctionsAlias,
    SanctionsDelta,
    SanctionsEntity,
    SanctionsIdentifier,
    SanctionsProvider,
    ensure_utc,
)

UrlOpenCallable = Callable[[str | Request], Any]

DEFAULT_OFAC_ENTITIES_ENDPOINT = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.JSON"
)
DEFAULT_OFAC_DELTAS_ENDPOINT = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_DELTA.JSON"
)
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


class OFACClient(SanctionsProvider):
    """Small OFAC client with injectable transport for deterministic tests."""

    def __init__(
        self,
        *,
        urlopen: UrlOpenCallable | None = None,
        entities_endpoint: str = DEFAULT_OFAC_ENTITIES_ENDPOINT,
        deltas_endpoint: str = DEFAULT_OFAC_DELTAS_ENDPOINT,
    ) -> None:
        self._urlopen = urlopen or stdlib_urlopen
        self._entities_endpoint = entities_endpoint
        self._deltas_endpoint = deltas_endpoint

    def fetch_entities(
        self,
        *,
        program: str | None = None,
        updated_since: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> list[SanctionsEntity]:
        params = _pagination_params(limit)
        if program is not None:
            params["program"] = program
        if updated_since is not None:
            params["updated_since"] = ensure_utc(updated_since).isoformat()

        entities = _parse_entities(_read_text(self._urlopen, _build_url(self._entities_endpoint, params)))
        if program is not None:
            entities = [entity for entity in entities if program in entity.programs]
        if updated_since is not None:
            updated_since = ensure_utc(updated_since)
            entities = [
                entity
                for entity in entities
                if entity.updated_at is not None and entity.updated_at >= updated_since
            ]
        if limit is not None:
            entities = entities[:limit]
        return entities

    def fetch_deltas(
        self,
        *,
        since: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> list[SanctionsDelta]:
        params = _pagination_params(limit)
        if since is not None:
            params["since"] = ensure_utc(since).isoformat()

        deltas = _parse_deltas(_read_text(self._urlopen, _build_url(self._deltas_endpoint, params)))
        if since is not None:
            since = ensure_utc(since)
            deltas = [delta for delta in deltas if delta.changed_at >= since]
        if limit is not None:
            deltas = deltas[:limit]
        return deltas


def _pagination_params(limit: int | None) -> dict[str, str | int]:
    if limit is not None and limit < 1:
        msg = "limit must be positive"
        raise ValueError(msg)
    return {"limit": limit} if limit is not None else {}


def _build_url(endpoint: str, params: Mapping[str, str | int]) -> str:
    clean_params = {key: value for key, value in params.items() if value not in (None, "")}
    if not clean_params:
        return endpoint
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(clean_params)}"


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


def _parse_entities(text: str) -> list[SanctionsEntity]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("<"):
        return _parse_xml_entities(stripped)
    payload = json.loads(stripped)
    return [_parse_json_entity(record) for record in _extract_records(payload, "entities", "sdnEntry")]


def _parse_deltas(text: str) -> list[SanctionsDelta]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("<"):
        return _parse_xml_deltas(stripped)

    payload = json.loads(stripped)
    records = _extract_records(payload, "deltas", "changes", "delta", "change")
    deltas: list[SanctionsDelta] = []
    for index, record in enumerate(records):
        entity_record = _pick(record, "entity", "sdnEntry")
        entity = _parse_json_entity(entity_record) if isinstance(entity_record, Mapping) else None
        entity_id = _text(
            _pick(record, "entity_id", "entityId", "uid", "sdnId", "id")
        ) or (entity.entity_id if entity else "")
        changed_at = _parse_datetime(_pick(record, "changed_at", "changedAt", "date")) or _EPOCH
        deltas.append(
            SanctionsDelta(
                delta_id=_text(_pick(record, "delta_id", "deltaId", "id"))
                or f"ofac-delta:{index}:{entity_id}",
                entity_id=entity_id,
                change_type=_text(_pick(record, "change_type", "changeType", "action")),
                changed_at=changed_at,
                entity=entity,
                source_refs=("ofac:delta",),
                evidence_refs=(f"ofac:delta:{entity_id}",) if entity_id else (),
                metadata=record,
            )
        )
    return deltas


def _extract_records(payload: Any, *preferred_keys: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []

    for key in (*preferred_keys, "results", "data", "items", "SDN"):
        value = _pick(payload, key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            nested = _extract_records(value, *preferred_keys)
            if nested:
                return nested

    for value in payload.values():
        nested = _extract_records(value, *preferred_keys)
        if nested:
            return nested
    return []


def _parse_json_entity(record: Mapping[str, Any]) -> SanctionsEntity:
    entity_id = _text(_pick(record, "entity_id", "entityId", "uid", "sdnId", "id"))
    name = _text(_pick(record, "name", "sdnName", "title")) or _join_name(record)
    programs = _text_tuple(_pick(record, "programs", "programList", "program"))
    sanctions_lists = _text_tuple(_pick(record, "sanctions_lists", "sanctionsList", "list"))
    aliases = tuple(_parse_aliases(_pick(record, "aliases", "akaList", "aka")))
    identifiers = tuple(_parse_identifiers(_pick(record, "identifiers", "idList", "id")))
    countries = tuple(
        sorted(
            {
                country
                for country in (
                    *_text_tuple(_pick(record, "countries", "country")),
                    *_countries_from_addresses(_pick(record, "addresses", "addressList", "address")),
                )
                if country
            }
        )
    )
    return SanctionsEntity(
        entity_id=entity_id or name,
        name=name,
        entity_type=_text(_pick(record, "entity_type", "entityType", "sdnType", "type")),
        programs=programs,
        sanctions_lists=sanctions_lists or ("SDN",),
        aliases=aliases,
        identifiers=identifiers,
        countries=countries,
        listed_at=_parse_datetime(_pick(record, "listed_at", "listedAt", "publicationDate")),
        updated_at=_parse_datetime(_pick(record, "updated_at", "updatedAt", "lastUpdated")),
        source_refs=("ofac:sdn",),
        evidence_refs=(f"ofac:sdn:{entity_id}",) if entity_id else (),
        metadata=record,
    )


def _parse_aliases(value: Any) -> Iterable[SanctionsAlias]:
    for record in _recordish(value, "aka"):
        if isinstance(record, Mapping):
            name = _text(_pick(record, "name", "alias", "akaName")) or _join_name(record)
            yield SanctionsAlias(
                name=name,
                alias_type=_text(_pick(record, "type", "aliasType", "category")),
                quality=_text(_pick(record, "quality", "qualityType")),
                category=_text(_pick(record, "category")),
                metadata=record,
            )
        else:
            yield SanctionsAlias(name=_text(record))


def _parse_identifiers(value: Any) -> Iterable[SanctionsIdentifier]:
    for record in _recordish(value, "id"):
        if isinstance(record, Mapping):
            identifier_type = _text(_pick(record, "type", "idType", "identifierType"))
            identifier_value = _text(_pick(record, "value", "number", "idNumber"))
            if identifier_value:
                yield SanctionsIdentifier(
                    identifier_type=identifier_type,
                    value=identifier_value,
                    country=_text(_pick(record, "country", "idCountry")),
                    issuer=_text(_pick(record, "issuer", "issuedBy")),
                    metadata=record,
                )
        else:
            identifier_value = _text(record)
            if identifier_value:
                yield SanctionsIdentifier(identifier_type="unknown", value=identifier_value)


def _recordish(value: Any, nested_key: str) -> list[Any]:
    if isinstance(value, Mapping):
        nested = _pick(value, nested_key)
        if isinstance(nested, list):
            return nested
        if nested is not None:
            return [nested]
        return [value]
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _countries_from_addresses(value: Any) -> Iterable[str]:
    for record in _recordish(value, "address"):
        if isinstance(record, Mapping):
            country = _text(_pick(record, "country", "countryCode"))
            if country:
                yield country


def _parse_xml_entities(text: str) -> list[SanctionsEntity]:
    root = ElementTree.fromstring(text)
    entries = [node for node in root.iter() if _local_name(node.tag) in {"sdnEntry", "entity"}]
    if _local_name(root.tag) in {"sdnEntry", "entity"}:
        entries = [root]
    entities: list[SanctionsEntity] = []
    for entry in entries:
        entity_id = _xml_text(entry, "uid", "id", "sdnId")
        aliases = tuple(
            SanctionsAlias(
                name=_xml_text(alias, "name") or _join_xml_name(alias),
                alias_type=_xml_text(alias, "type", "category"),
                quality=_xml_text(alias, "quality"),
            )
            for alias in entry.iter()
            if _local_name(alias.tag) == "aka"
        )
        identifiers = tuple(
            SanctionsIdentifier(
                identifier_type=_xml_text(identifier, "idType", "type"),
                value=_xml_text(identifier, "idNumber", "value", "number"),
                country=_xml_text(identifier, "idCountry", "country"),
            )
            for identifier in entry.iter()
            if _local_name(identifier.tag) == "id"
            and _xml_text(identifier, "idNumber", "value", "number")
        )
        entities.append(
            SanctionsEntity(
                entity_id=entity_id or _xml_text(entry, "sdnName", "name"),
                name=_xml_text(entry, "sdnName", "name") or _join_xml_name(entry),
                entity_type=_xml_text(entry, "sdnType", "type"),
                programs=tuple(_xml_values(entry, "program")),
                sanctions_lists=("SDN",),
                aliases=aliases,
                identifiers=identifiers,
                countries=tuple(sorted(set(_xml_values(entry, "country")))),
                source_refs=("ofac:sdn",),
                evidence_refs=(f"ofac:sdn:{entity_id}",) if entity_id else (),
                metadata={"xml_tag": _local_name(entry.tag)},
            )
        )
    return entities


def _parse_xml_deltas(text: str) -> list[SanctionsDelta]:
    root = ElementTree.fromstring(text)
    deltas: list[SanctionsDelta] = []
    for index, node in enumerate(
        item for item in root.iter() if _local_name(item.tag) in {"delta", "change"}
    ):
        entity_id = _xml_text(node, "entityId", "uid", "sdnId")
        deltas.append(
            SanctionsDelta(
                delta_id=_xml_text(node, "deltaId", "id") or f"ofac-delta:{index}:{entity_id}",
                entity_id=entity_id,
                change_type=_xml_text(node, "changeType", "action"),
                changed_at=_parse_datetime(_xml_text(node, "changedAt", "date")) or _EPOCH,
                source_refs=("ofac:delta",),
                evidence_refs=(f"ofac:delta:{entity_id}",) if entity_id else (),
                metadata={"xml_tag": _local_name(node.tag)},
            )
        )
    return deltas


def _pick(record: Mapping[str, Any], *keys: str) -> Any:
    lowercase = {str(key).lower(): value for key, value in record.items()}
    for key in keys:
        if key in record:
            return record[key]
        value = lowercase.get(key.lower())
        if value is not None:
            return value
    return None


def _text_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        for key in ("program", "value", "name", "list"):
            nested = _pick(value, key)
            if nested is not None:
                return _text_tuple(nested)
        return tuple(_text(item) for item in value.values() if _text(item))
    if isinstance(value, list | tuple | set | frozenset):
        return tuple(_text(item) for item in value if _text(item))
    text = _text(value)
    return (text,) if text else ()


def _join_name(record: Mapping[str, Any]) -> str:
    parts = [
        _text(_pick(record, "firstName", "first_name")),
        _text(_pick(record, "lastName", "last_name")),
    ]
    return " ".join(part for part in parts if part)


def _join_xml_name(node: ElementTree.Element) -> str:
    parts = [_xml_text(node, "firstName"), _xml_text(node, "lastName")]
    return " ".join(part for part in parts if part)


def _parse_datetime(value: Any) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y"):
        try:
            parsed_date = datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return datetime.datetime.combine(parsed_date, datetime.time(tzinfo=datetime.UTC))
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _xml_text(node: ElementTree.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in node.iter():
        if child is not node and _local_name(child.tag).lower() in wanted:
            return _node_text(child)
    return ""


def _xml_values(node: ElementTree.Element, *names: str) -> list[str]:
    wanted = {name.lower() for name in names}
    return [
        text
        for child in node.iter()
        if _local_name(child.tag).lower() in wanted
        for text in [_node_text(child)]
        if text
    ]


def _node_text(node: ElementTree.Element) -> str:
    return (node.text or "").strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
