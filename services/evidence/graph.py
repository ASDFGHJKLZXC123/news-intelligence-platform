"""Deterministic evidence graph construction for provider facts.

The graph builder is intentionally DB-agnostic: callers can pass provider DTOs,
ORM-like rows, mappings, or explicit ``ProviderFact`` instances.  The output is a
stable set of nodes and edges that API layers can serialize directly.
"""

from __future__ import annotations

import datetime
import decimal
import hashlib
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from types import MappingProxyType
from typing import Any, Final

ATTRIBUTE_FIELDS: Final[tuple[str, ...]] = (
    "country_code",
    "country",
    "country_codes",
    "indicator_id",
    "observed_on",
    "observed_at",
    "value",
    "unit",
    "units",
    "programs",
    "sanctions_lists",
    "entity_type",
    "lei",
    "cik",
    "taxonomy",
    "concept",
    "accession_number",
    "form",
    "form_type",
    "disaster_types",
    "themes",
    "incident_type",
    "severity",
    "magnitude",
    "latitude",
    "longitude",
    "region",
    "geography",
    "commodity",
    "series_id",
    "price",
    "inventory",
    "production",
    "imports",
    "exports",
)

SUPPORTED_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    {"article", "event", "signal", "model_run", "historical_case"}
)

_MISSING = object()


@dataclass(frozen=True)
class EvidenceSubjectRef:
    """A non-provider subject node referenced by a provider fact."""

    kind: str
    id: str
    label: str = ""
    relationship: str = "about"
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = _compact(self.kind)
        id_value = str(self.id).strip()
        if not kind:
            msg = "subject kind must be a non-empty string"
            raise ValueError(msg)
        if not id_value:
            msg = "subject id must be a non-empty string"
            raise ValueError(msg)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "id", id_value)
        object.__setattr__(self, "label", self.label or id_value)
        object.__setattr__(self, "relationship", _compact(self.relationship) or "about")
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes))

    @property
    def node_id(self) -> str:
        return f"{self.kind}:{_stable_part(self.id)}"


@dataclass(frozen=True)
class ProviderFact:
    """Normalized provider fact before graph expansion."""

    kind: str
    fact_id: str
    label: str
    provider: str = "unknown"
    observed_at: datetime.datetime | datetime.date | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    subject_refs: tuple[EvidenceSubjectRef, ...] = field(default_factory=tuple)
    evidence_refs: tuple[Mapping[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        kind = _compact(self.kind)
        provider = _compact(self.provider) or "unknown"
        fact_id = str(self.fact_id).strip()
        if not kind:
            msg = "fact kind must be a non-empty string"
            raise ValueError(msg)
        if not fact_id:
            msg = "fact id must be a non-empty string"
            raise ValueError(msg)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "fact_id", fact_id)
        object.__setattr__(self, "label", self.label or fact_id)
        object.__setattr__(self, "observed_at", _coerce_temporal(self.observed_at))
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes))
        object.__setattr__(self, "subject_refs", tuple(self.subject_refs))
        refs = tuple(_coerce_evidence_ref(ref) for ref in self.evidence_refs)
        object.__setattr__(self, "evidence_refs", refs or (_default_evidence_ref(self),))

    @property
    def node_id(self) -> str:
        return canonical_fact_node_id(self.kind, self.provider, self.fact_id)


@dataclass(frozen=True)
class EvidenceNode:
    """A node in the derived evidence graph."""

    id: str
    kind: str
    label: str
    provider: str = "internal"
    observed_at: datetime.datetime | datetime.date | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[Mapping[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _compact(self.kind))
        object.__setattr__(self, "provider", _compact(self.provider) or "internal")
        object.__setattr__(self, "observed_at", _coerce_temporal(self.observed_at))
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes))
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(_coerce_evidence_ref(ref) for ref in self.evidence_refs),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "provider": self.provider,
            "observed_at": _json_safe(self.observed_at),
            "attributes": _json_safe(self.attributes),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class EvidenceEdge:
    """A directed relationship between evidence graph nodes."""

    id: str
    source_id: str
    target_id: str
    relationship: str
    weight: float = 1.0
    attributes: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[Mapping[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        weight = float(self.weight)
        if not math.isfinite(weight):
            msg = "edge weight must be finite"
            raise ValueError(msg)
        object.__setattr__(self, "relationship", _compact(self.relationship) or "related_to")
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes))
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(_coerce_evidence_ref(ref) for ref in self.evidence_refs),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relationship": self.relationship,
            "weight": self.weight,
            "attributes": _json_safe(self.attributes),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class EvidenceGraph:
    """Stable graph payload derived from provider facts."""

    nodes: tuple[EvidenceNode, ...]
    edges: tuple[EvidenceEdge, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "nodes": [node.as_payload() for node in self.nodes],
            "edges": [edge.as_payload() for edge in self.edges],
        }

    def node(self, node_id: str) -> EvidenceNode | None:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None


def canonical_fact_node_id(kind: str, provider: str, fact_id: object) -> str:
    """Build a stable readable node id for one provider fact."""
    return ":".join((_compact(kind), _compact(provider) or "unknown", _stable_part(fact_id)))


def build_evidence_graph(facts: Sequence[ProviderFact | Mapping[str, Any] | object]) -> EvidenceGraph:
    """Expand provider facts into deterministic graph nodes and subject edges."""
    nodes: dict[str, EvidenceNode] = {}
    edges: dict[str, EvidenceEdge] = {}

    for fact in sorted((_coerce_provider_fact(item) for item in facts), key=lambda item: item.node_id):
        fact_node = EvidenceNode(
            id=fact.node_id,
            kind=fact.kind,
            label=fact.label,
            provider=fact.provider,
            observed_at=fact.observed_at,
            attributes=fact.attributes,
            evidence_refs=fact.evidence_refs,
        )
        nodes.setdefault(fact_node.id, fact_node)

        for subject in sorted(fact.subject_refs, key=lambda item: (item.kind, item.id, item.relationship)):
            subject_node = EvidenceNode(
                id=subject.node_id,
                kind=subject.kind,
                label=subject.label,
                attributes=subject.attributes,
            )
            nodes.setdefault(subject_node.id, subject_node)
            relationship = subject.relationship or _relationship_for(fact.kind, subject.kind)
            edge = EvidenceEdge(
                id=_edge_id(fact_node.id, subject_node.id, relationship),
                source_id=fact_node.id,
                target_id=subject_node.id,
                relationship=relationship,
                evidence_refs=fact.evidence_refs,
            )
            edges.setdefault(edge.id, edge)

    return EvidenceGraph(
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        edges=tuple(edges[key] for key in sorted(edges)),
    )


def provider_fact_from_record(record: ProviderFact | Mapping[str, Any] | object) -> ProviderFact:
    """Public coercion helper for callers that want to inspect normalized facts."""
    return _coerce_provider_fact(record)


def evidence_ref_for_provider_fact(record: ProviderFact | Mapping[str, Any] | object) -> Mapping[str, str]:
    """Return the primary contract-compatible evidence ref for a provider fact."""
    fact = _coerce_provider_fact(record)
    return fact.evidence_refs[0]


def _coerce_provider_fact(record: ProviderFact | Mapping[str, Any] | object) -> ProviderFact:
    if isinstance(record, ProviderFact):
        return record

    explicit_kind = _get(record, "kind")
    kind = _compact(explicit_kind) if explicit_kind else _infer_kind(record)
    provider = _provider(record)
    fact_id = _infer_fact_id(record, kind)
    label = _infer_label(record, fact_id)
    attributes = _extract_attributes(record)
    subject_refs = _extract_subject_refs(record, kind)
    evidence_refs = _extract_evidence_refs(record, kind, provider, fact_id)

    return ProviderFact(
        kind=kind,
        provider=provider,
        fact_id=fact_id,
        label=label,
        observed_at=_infer_observed_at(record),
        attributes=attributes,
        subject_refs=subject_refs,
        evidence_refs=evidence_refs,
    )


def _infer_kind(record: Mapping[str, Any] | object) -> str:
    class_name = record.__class__.__name__.casefold()
    if _has(record, "report_id") or (
        _has(record, "external_id") and (_has(record, "disaster_types") or _has(record, "themes"))
    ):
        return "humanitarian_report"
    if _has(record, "incident_id") or (
        _has(record, "external_id") and (_has(record, "incident_type") or _has(record, "latitude"))
    ):
        return "geo_incident"
    if _has(record, "region") and _has(record, "commodity") and _has(record, "snapshot_date"):
        return "energy_snapshot"
    if _has(record, "series_id") and (_has(record, "geography") or _has(record, "units")):
        return "energy_observation"
    if _has(record, "indicator_id") or "countryindicatorobservation" in class_name:
        return "country_indicator"
    if _has(record, "lei") and _has(record, "legal_name"):
        return "lei_record"
    if _has(record, "entity_uid") or (
        _has(record, "entity_id") and (_has(record, "programs") or _has(record, "sanctions_lists"))
    ):
        return "sanctions_record"
    if _has(record, "accession_number") and (_has(record, "form") or _has(record, "form_type")):
        return "filing"
    if _has(record, "taxonomy") and _has(record, "concept"):
        return "company_fact"
    if _has(record, "global_event_id"):
        return "event"
    if _has(record, "guid") and _has(record, "url"):
        return "article"
    return "provider_fact"


def _infer_fact_id(record: Mapping[str, Any] | object, kind: str) -> str:
    if explicit := _get(record, "fact_id"):
        return str(explicit)
    candidates = (
        "entity_id",
        "entity_uid",
        "lei",
        "report_id",
        "incident_id",
        "external_id",
        "global_event_id",
        "guid",
        "accession_number",
    )
    for name in candidates:
        value = _get(record, name, _MISSING)
        if value is not _MISSING and value not in (None, ""):
            return str(value)

    if kind == "country_indicator":
        country = _get(record, "country_code", "unknown")
        indicator = _get(record, "indicator_id", _get(record, "series_id", "unknown"))
        date_value = _get(record, "date", _get(record, "observed_on", _get(record, "observed_at", "")))
        return f"{country}:{indicator}:{_json_safe(date_value)}"
    if kind in {"energy_observation", "energy_snapshot"}:
        series = _get(record, "series_id", _get(record, "commodity", "unknown"))
        region = _get(record, "geography", _get(record, "region", "global"))
        date_value = _get(record, "date", _get(record, "snapshot_date", _get(record, "observed_at", "")))
        return f"{region}:{series}:{_json_safe(date_value)}"

    value = _get(record, "id", _MISSING)
    if value is not _MISSING and value not in (None, ""):
        return str(value)
    return _short_hash(_json_safe(record))


def _infer_label(record: Mapping[str, Any] | object, fallback: str) -> str:
    for name in (
        "label",
        "title",
        "name",
        "legal_name",
        "primary_name",
        "description",
        "concept",
        "indicator_id",
        "series_id",
        "commodity",
    ):
        value = _get(record, name, _MISSING)
        if value is not _MISSING and value not in (None, ""):
            return str(value)
    return fallback


def _provider(record: Mapping[str, Any] | object) -> str:
    return str(_get(record, "provider_name", _get(record, "provider", "unknown")) or "unknown")


def _infer_observed_at(record: Mapping[str, Any] | object) -> datetime.datetime | datetime.date | None:
    for name in (
        "observed_at",
        "observed_on",
        "published_at",
        "occurred_at",
        "seen_at",
        "filed_at",
        "snapshot_date",
        "updated_at",
        "last_seen_at",
        "listed_at",
    ):
        value = _get(record, name, _MISSING)
        if value is not _MISSING and value not in (None, ""):
            return _coerce_temporal(value)
    return None


def _extract_attributes(record: Mapping[str, Any] | object) -> Mapping[str, Any]:
    metadata = _metadata(record)
    attributes: dict[str, Any] = {}
    if metadata:
        attributes["metadata"] = _json_safe(metadata)
    for name in ATTRIBUTE_FIELDS:
        value = _get(record, name, _MISSING)
        if value is not _MISSING and value not in (None, ""):
            attributes[name] = _json_safe(value)
    return attributes


def _extract_subject_refs(record: Mapping[str, Any] | object, kind: str) -> tuple[EvidenceSubjectRef, ...]:
    explicit = _get(record, "subject_refs", _MISSING)
    if explicit is not _MISSING and explicit not in (None, ""):
        return tuple(_coerce_subject_ref(item) for item in _as_sequence(explicit))

    refs: dict[tuple[str, str], EvidenceSubjectRef] = {}

    for country in _country_values(record):
        _add_subject(refs, "country", country, relationship=_relationship_for(kind, "country"))

    entity_id = _first_present(
        _get(record, "entity_uid", _MISSING),
        _get(record, "entity_id", _MISSING),
        _get(record, "cik", _MISSING),
        _get(record, "lei", _MISSING),
        _get(record, "primary_lei", _MISSING),
        _get(record, "primary_cik", _MISSING),
    )
    if entity_id is not _MISSING and entity_id not in (None, ""):
        label = _first_present(
            _get(record, "legal_name", _MISSING),
            _get(record, "primary_name", _MISSING),
            _get(record, "company_name", _MISSING),
            _get(record, "name", _MISSING),
        )
        _add_subject(
            refs,
            "entity",
            entity_id,
            label="" if label is _MISSING else str(label),
            relationship=_relationship_for(kind, "entity"),
        )

    region = _first_present(_get(record, "region", _MISSING), _get(record, "geography", _MISSING))
    if region is not _MISSING and region not in (None, ""):
        _add_subject(refs, "region", region, relationship=_relationship_for(kind, "region"))

    commodity = _first_present(
        _get(record, "commodity", _MISSING),
        _metadata(record).get("commodity", _MISSING) if isinstance(_metadata(record), Mapping) else _MISSING,
    )
    if commodity is not _MISSING and commodity not in (None, ""):
        _add_subject(refs, "commodity", commodity, relationship=_relationship_for(kind, "commodity"))

    indicator = _get(record, "indicator_id", _MISSING)
    if indicator is not _MISSING and indicator not in (None, ""):
        _add_subject(refs, "indicator", indicator, relationship="measures_indicator")

    series_id = _get(record, "series_id", _MISSING)
    if series_id is not _MISSING and series_id not in (None, ""):
        _add_subject(refs, "energy_series", series_id, relationship="uses_energy_series")

    return tuple(refs[key] for key in sorted(refs))


def _country_values(record: Mapping[str, Any] | object) -> tuple[str, ...]:
    raw_values = (
        _get(record, "country_code", _MISSING),
        _get(record, "country", _MISSING),
        _get(record, "country_codes", _MISSING),
        _get(record, "countries", _MISSING),
        _get(record, "source_country", _MISSING),
        _get(record, "action_geo_country_code", _MISSING),
    )
    values: list[str] = []
    for raw_value in raw_values:
        if raw_value is _MISSING or raw_value in (None, ""):
            continue
        for item in _as_sequence(raw_value):
            value = str(item).strip()
            if value:
                values.append(value.upper())
    return tuple(dict.fromkeys(values))


def _extract_evidence_refs(
    record: Mapping[str, Any] | object,
    kind: str,
    provider: str,
    fact_id: str,
) -> tuple[Mapping[str, str], ...]:
    explicit = _get(record, "evidence_refs", _MISSING)
    if explicit is not _MISSING and explicit:
        return tuple(_coerce_evidence_ref(ref) for ref in _as_sequence(explicit))

    fact = ProviderFact(kind=kind, provider=provider, fact_id=fact_id, label=str(fact_id))
    return (_default_evidence_ref(fact),)


def _coerce_subject_ref(value: EvidenceSubjectRef | Mapping[str, Any] | object) -> EvidenceSubjectRef:
    if isinstance(value, EvidenceSubjectRef):
        return value
    return EvidenceSubjectRef(
        kind=str(_get(value, "kind")),
        id=str(_get(value, "id")),
        label=str(_get(value, "label", "") or ""),
        relationship=str(_get(value, "relationship", "about") or "about"),
        attributes=_get(value, "attributes", {}) or {},
    )


def _coerce_evidence_ref(value: Mapping[str, Any] | object) -> Mapping[str, str]:
    if isinstance(value, str):
        id_text = value.strip()
        if not id_text:
            msg = "evidence ref id must be a non-empty string"
            raise ValueError(msg)
        return {"kind": "signal", "id": id_text}
    kind = str(_get(value, "kind", "signal") or "signal")
    if kind not in SUPPORTED_EVIDENCE_KINDS:
        kind = "signal"
    id_value = str(_get(value, "id", "") or "").strip()
    if not id_value:
        msg = "evidence ref id must be a non-empty string"
        raise ValueError(msg)
    return {"kind": kind, "id": id_value}


def _default_evidence_ref(fact: ProviderFact) -> Mapping[str, str]:
    if fact.kind == "article":
        return {"kind": "article", "id": fact.fact_id}
    if fact.kind in {"event", "geo_incident"}:
        return {"kind": "event", "id": fact.node_id}
    return {"kind": "signal", "id": fact.node_id}


def _relationship_for(fact_kind: str, subject_kind: str) -> str:
    if subject_kind == "country":
        return {
            "country_indicator": "measures_country",
            "humanitarian_report": "reports_country",
            "geo_incident": "located_in",
            "event": "occurred_in",
        }.get(fact_kind, "mentions_country")
    if subject_kind == "entity":
        return {
            "lei_record": "identifies_entity",
            "sanctions_record": "sanctions_subject",
            "filing": "discloses_entity",
            "company_fact": "measures_entity",
        }.get(fact_kind, "mentions_entity")
    if subject_kind == "region":
        return "tracks_region" if fact_kind.startswith("energy") else "mentions_region"
    if subject_kind == "commodity":
        return "tracks_commodity"
    return f"references_{subject_kind}"


def _edge_id(source_id: str, target_id: str, relationship: str) -> str:
    value = f"{source_id}|{relationship}|{target_id}"
    if len(value) <= 192:
        return value
    return f"edge:{_short_hash(value, length=24)}"


def _add_subject(
    refs: dict[tuple[str, str], EvidenceSubjectRef],
    kind: str,
    id_value: object,
    *,
    label: str = "",
    relationship: str,
) -> None:
    text = str(id_value).strip()
    if not text:
        return
    subject = EvidenceSubjectRef(kind=kind, id=text, label=label or text, relationship=relationship)
    refs[(subject.kind, subject.id)] = subject


def _get(source: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _has(source: Mapping[str, Any] | object, name: str) -> bool:
    return _get(source, name, _MISSING) is not _MISSING


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not _MISSING and value not in (None, ""):
            return value
    return _MISSING


def _metadata(record: Mapping[str, Any] | object) -> Mapping[str, Any]:
    for name in (
        "metadata",
        "observation_metadata",
        "snapshot_metadata",
        "source_payload",
        "fact_metadata",
        "filing_metadata",
    ):
        value = _get(record, name, _MISSING)
        if isinstance(value, Mapping):
            return value
    return {}


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value is _MISSING or value is None:
        return ()
    if isinstance(value, str | bytes | bytearray):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(value)
    if isinstance(value, set | frozenset):
        return tuple(sorted(value, key=lambda item: str(item)))
    return (value,)


def _coerce_temporal(value: Any) -> datetime.datetime | datetime.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime | datetime.date):
        return value
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        try:
            return datetime.date.fromisoformat(str(value))
        except ValueError:
            return None


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _json_safe(item) for key, item in sorted(value.items())})


def _json_safe(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in sorted(value.items())}
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_safe(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_json_safe(item) for item in value), key=lambda item: str(item))
    if value is _MISSING:
        return None
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _compact(value: object) -> str:
    return "_".join(str(value).casefold().strip().replace("/", "_").split())


def _stable_part(value: object) -> str:
    text = str(value).strip().replace(" ", "_")
    if len(text) <= 96:
        return text
    return f"{text[:64]}:{_short_hash(text)}"


def _short_hash(value: Any, *, length: int = 16) -> str:
    raw = repr(_json_safe(value)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:length]
