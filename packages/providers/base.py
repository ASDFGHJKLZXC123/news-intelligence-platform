"""Typed provider interfaces for all external/non-deterministic dependencies.

Every external call (RSS feeds, embeddings, LLM, wall-clock time) sits behind one of
these Protocols so production code depends on the interface and tests inject a fake.
Stage 1 only defines the contracts and DTOs; no real network implementations exist
yet (those are gated to later stages).
"""

from __future__ import annotations

import datetime
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

# A Wikidata item id: "Q" followed by a positive integer. Nothing else is accepted anywhere
# a QID is taken from a caller or a payload, so no caller-supplied text reaches a SPARQL query.
WIKIDATA_QID_PATTERN = re.compile(r"^Q[1-9][0-9]*$")


def ensure_finite_vector(values: Iterable[Any]) -> tuple[float, ...]:
    """Coerce an embedding to floats, rejecting non-numeric and non-finite components.

    A NaN or an infinity is not a degraded vector, it is a corrupt one, and the damage is silent:
    pgvector's cosine distance against a NaN component is NaN, `NaN >= threshold` is False, and the
    row simply never matches anything ever again. An infinity poisons the index instead. Neither
    may reach a table whose whole purpose is to be compared against. Booleans are rejected too --
    a JSON `true` would otherwise coerce to a perfectly plausible 1.0.
    """
    vector: list[float] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, int | float):
            msg = f"embedding value at index {index} is not a number: {value!r}"
            raise ValueError(msg)
        number = float(value)
        if not math.isfinite(number):
            msg = f"embedding value at index {index} is not finite: {value!r}"
            raise ValueError(msg)
        vector.append(number)
    return tuple(vector)


def ensure_utc(value: datetime.datetime) -> datetime.datetime:
    """Return a UTC-normalized aware datetime or raise for naive/non-UTC values."""
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "datetime must be timezone-aware UTC"
        raise ValueError(msg)
    if value.utcoffset() != datetime.timedelta(0):
        msg = "datetime must use UTC offset"
        raise ValueError(msg)
    return value.astimezone(datetime.UTC)


def ensure_optional_utc(value: datetime.datetime | None) -> datetime.datetime | None:
    """Return a UTC-normalized aware datetime when a value is present."""
    if value is None:
        return None
    return ensure_utc(value)


def normalize_sec_cik(value: Any) -> str:
    """Return an SEC CIK as the zero-padded 10-digit string SEC endpoints expect."""
    digits = "".join(character for character in str(value) if character.isdigit())
    if not digits:
        msg = "CIK must contain digits"
        raise ValueError(msg)
    return digits.zfill(10)


def normalize_wikidata_qid(value: Any) -> str:
    """Return a validated Wikidata QID (``Q312``) or raise for anything that is not one."""
    qid = str(value).strip().upper()
    if not WIKIDATA_QID_PATTERN.match(qid):
        msg = f"invalid Wikidata QID: {value!r}"
        raise ValueError(msg)
    return qid


def freeze_value(value: Any) -> Any:
    """Recursively freeze JSON-like metadata so DTOs are immutable after creation."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_value(item) for key, item in value.items()})
    if isinstance(value, tuple):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(freeze_value(item) for item in value)
    return value


# --- Data transfer objects -----------------------------------------------------
@dataclass(frozen=True)
class RSSItem:
    """A normalized RSS item plus source provenance for later evidence storage."""

    guid: str
    title: str
    url: str
    published_at: datetime.datetime | None
    summary: str = ""
    source: str = ""
    provider_name: str = "unknown"
    output_schema_version: str = "rss-item.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "published_at", ensure_optional_utc(self.published_at))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class EmbeddingResult:
    """An embedding vector plus the metadata needed for future re-embedding."""

    vector: tuple[float, ...]
    provider_name: str
    model_name: str
    model_version: str
    dimension: int
    model_run_id: str
    output_schema_version: str = "embedding-result.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Every provider's vector passes through here, so this is the one place a NaN/Inf can be
        # stopped for all of them rather than in each adapter.
        vector = ensure_finite_vector(self.vector)
        object.__setattr__(self, "vector", vector)
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if len(vector) != self.dimension:
            msg = "embedding dimension must match vector length"
            raise ValueError(msg)

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class LLMResponse:
    """A structured LLM response with provenance for the evidence/audit contract."""

    text: str
    provider_name: str
    model_name: str
    model_version: str
    model_run_id: str
    prompt_name: str
    prompt_version: str
    output_schema_version: str = "llm-response.v1"
    input_tokens: int = 0
    output_tokens: int = 0
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class GDELTArticle:
    """A normalized GDELT article search result with source provenance."""

    guid: str
    title: str
    url: str
    seen_at: datetime.datetime
    domain: str = ""
    language: str = ""
    source_country: str = ""
    provider_name: str = "gdelt"
    output_schema_version: str = "gdelt-article.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "seen_at", ensure_utc(self.seen_at))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class GDELTEvent:
    """A normalized GDELT event result with CAMEO-style event metadata."""

    global_event_id: str
    event_at: datetime.datetime
    event_code: str = ""
    event_root_code: str = ""
    actor1_name: str = ""
    actor2_name: str = ""
    action_geo_country_code: str = ""
    quad_class: int | None = None
    goldstein_scale: float | None = None
    avg_tone: float | None = None
    source_url: str = ""
    provider_name: str = "gdelt"
    output_schema_version: str = "gdelt-event.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_at", ensure_utc(self.event_at))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class FREDObservation:
    """A normalized FRED time-series observation."""

    series_id: str
    observed_at: datetime.datetime
    value: float | None
    realtime_start: datetime.date | None = None
    realtime_end: datetime.date | None = None
    provider_name: str = "fred"
    output_schema_version: str = "fred-observation.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        if self.value is not None:
            object.__setattr__(self, "value", float(self.value))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def date(self) -> datetime.date:
        return self.observed_at.date()

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class SECSubmission:
    """A normalized SEC EDGAR submission filing row."""

    cik: str
    accession_number: str
    form: str
    filed_at: datetime.datetime
    report_at: datetime.datetime | None = None
    company_name: str = ""
    primary_document: str = ""
    primary_doc_description: str = ""
    items: str = ""
    provider_name: str = "sec-edgar"
    output_schema_version: str = "sec-submission.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "filed_at", ensure_utc(self.filed_at))
        object.__setattr__(self, "report_at", ensure_optional_utc(self.report_at))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class SECCompanyFact:
    """A normalized SEC EDGAR XBRL company fact row."""

    cik: str
    taxonomy: str
    concept: str
    unit: str
    value: int | float | str | None
    accession_number: str
    filed_at: datetime.datetime | None = None
    period_end_at: datetime.datetime | None = None
    form: str = ""
    fiscal_year: int | None = None
    fiscal_period: str = ""
    frame: str = ""
    label: str = ""
    description: str = ""
    provider_name: str = "sec-edgar"
    output_schema_version: str = "sec-company-fact.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "filed_at", ensure_optional_utc(self.filed_at))
        object.__setattr__(self, "period_end_at", ensure_optional_utc(self.period_end_at))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class SECCompanyTicker:
    """A normalized row of the SEC ``company_tickers.json`` identity seed (ADR 0006)."""

    cik: str
    ticker: str
    title: str
    provider_name: str = "sec-edgar"
    output_schema_version: str = "sec-company-ticker.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cik", normalize_sec_cik(self.cik))
        object.__setattr__(self, "ticker", self.ticker.strip().upper())
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class SanctionsAlias:
    """A normalized sanctions alias/name variant."""

    name: str
    alias_type: str = ""
    quality: str = ""
    category: str = ""
    output_schema_version: str = "sanctions-alias.v1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class SanctionsIdentifier:
    """A normalized sanctions identifier such as a passport, tax id, or vessel id."""

    identifier_type: str
    value: str
    country: str = ""
    issuer: str = ""
    output_schema_version: str = "sanctions-identifier.v1"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class SanctionsEntity:
    """A normalized sanctioned party with aliases, identifiers, and provenance."""

    entity_id: str
    name: str
    entity_type: str = ""
    programs: tuple[str, ...] = field(default_factory=tuple)
    sanctions_lists: tuple[str, ...] = field(default_factory=tuple)
    aliases: tuple[SanctionsAlias, ...] = field(default_factory=tuple)
    identifiers: tuple[SanctionsIdentifier, ...] = field(default_factory=tuple)
    countries: tuple[str, ...] = field(default_factory=tuple)
    listed_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    provider_name: str = "ofac"
    output_schema_version: str = "sanctions-entity.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "programs", tuple(self.programs))
        object.__setattr__(self, "sanctions_lists", tuple(self.sanctions_lists))
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "identifiers", tuple(self.identifiers))
        object.__setattr__(self, "countries", tuple(self.countries))
        object.__setattr__(self, "listed_at", ensure_optional_utc(self.listed_at))
        object.__setattr__(self, "updated_at", ensure_optional_utc(self.updated_at))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class SanctionsDelta:
    """A normalized sanctions-list change event for downstream diff processing."""

    delta_id: str
    entity_id: str
    change_type: str
    changed_at: datetime.datetime
    entity: SanctionsEntity | None = None
    provider_name: str = "ofac"
    output_schema_version: str = "sanctions-delta.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_at", ensure_utc(self.changed_at))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class LEIRecord:
    """A normalized GLEIF LEI record for entity identity resolution."""

    lei: str
    legal_name: str
    entity_status: str = ""
    registration_status: str = ""
    country_code: str = ""
    jurisdiction: str = ""
    legal_form: str = ""
    last_updated_at: datetime.datetime | None = None
    next_renewal_at: datetime.datetime | None = None
    provider_name: str = "gleif"
    output_schema_version: str = "lei-record.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "last_updated_at", ensure_optional_utc(self.last_updated_at))
        object.__setattr__(self, "next_renewal_at", ensure_optional_utc(self.next_renewal_at))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class LEIRelationship:
    """A normalized GLEIF relationship between two LEI records.

    Direction follows GLEIF Level 2: ``lei`` is the start node (the consolidated entity, i.e.
    the child) and ``related_lei`` is the end node (the entity consolidating it, i.e. the
    parent) for the ``IS_*_CONSOLIDATED_BY`` / direct- and ultimate-parent types.
    """

    relationship_id: str
    lei: str
    related_lei: str
    relationship_type: str
    status: str = ""
    start_at: datetime.datetime | None = None
    end_at: datetime.datetime | None = None
    provider_name: str = "gleif"
    output_schema_version: str = "lei-relationship.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_at", ensure_optional_utc(self.start_at))
        object.__setattr__(self, "end_at", ensure_optional_utc(self.end_at))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class WikidataItemRef:
    """A reference to a related Wikidata item (parent, subsidiary, industry, or brand)."""

    qid: str
    label: str = ""
    output_schema_version: str = "wikidata-item-ref.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "qid", normalize_wikidata_qid(self.qid))
        object.__setattr__(self, "label", self.label.strip())

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class WikidataAlias:
    """One name variant of a Wikidata item, typed to the ADR 0006 ``alias_type`` enum.

    ``valid_from``/``valid_to`` carry the statement's start/end qualifiers when Wikidata dates
    the name (a former name has an end date); an undated name is an open interval and leaves
    both as ``None``. ``qid`` is set only when the alias is itself an item (a brand), so the
    consumer can tell a bare surface form from a reference to another entity.
    """

    value: str
    alias_type: str
    language: str = "en"
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = None
    qid: str = ""
    output_schema_version: str = "wikidata-alias.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", self.value.strip())
        if self.qid:
            object.__setattr__(self, "qid", normalize_wikidata_qid(self.qid))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class WikidataEntity:
    """A normalized Wikidata identity record for one item (ADR 0006 source 3)."""

    qid: str
    label: str
    description: str = ""
    aliases: tuple[WikidataAlias, ...] = field(default_factory=tuple)
    tickers: tuple[str, ...] = field(default_factory=tuple)
    leis: tuple[str, ...] = field(default_factory=tuple)
    ciks: tuple[str, ...] = field(default_factory=tuple)
    parents: tuple[WikidataItemRef, ...] = field(default_factory=tuple)
    subsidiaries: tuple[WikidataItemRef, ...] = field(default_factory=tuple)
    industries: tuple[WikidataItemRef, ...] = field(default_factory=tuple)
    provider_name: str = "wikidata"
    output_schema_version: str = "wikidata-entity.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "qid", normalize_wikidata_qid(self.qid))
        object.__setattr__(self, "label", self.label.strip())
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "tickers", tuple(self.tickers))
        object.__setattr__(self, "leis", tuple(self.leis))
        object.__setattr__(self, "ciks", tuple(self.ciks))
        object.__setattr__(self, "parents", tuple(self.parents))
        object.__setattr__(self, "subsidiaries", tuple(self.subsidiaries))
        object.__setattr__(self, "industries", tuple(self.industries))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class WikidataQidMatch:
    """A QID resolved from an identifier value that an entity profile already carries."""

    qid: str
    identifier_type: str
    identifier_value: str
    provider_name: str = "wikidata"
    output_schema_version: str = "wikidata-qid-match.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "qid", normalize_wikidata_qid(self.qid))
        object.__setattr__(self, "identifier_type", self.identifier_type.strip().casefold())
        object.__setattr__(self, "identifier_value", self.identifier_value.strip())

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class CountryIndicator:
    """A normalized country-level indicator definition."""

    indicator_id: str
    name: str
    source: str = ""
    unit: str = ""
    frequency: str = ""
    topics: tuple[str, ...] = field(default_factory=tuple)
    provider_name: str = "world-bank"
    output_schema_version: str = "country-indicator.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "topics", tuple(self.topics))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class CountryIndicatorObservation:
    """A normalized country-level indicator observation."""

    country_code: str
    indicator_id: str
    observed_at: datetime.datetime
    value: float | None
    country_name: str = ""
    unit: str = ""
    provider_name: str = "world-bank"
    output_schema_version: str = "country-indicator-observation.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        if self.value is not None:
            object.__setattr__(self, "value", float(self.value))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def date(self) -> datetime.date:
        return self.observed_at.date()

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class HumanitarianReport:
    """A normalized humanitarian report or situation update."""

    report_id: str
    title: str
    url: str
    published_at: datetime.datetime
    source: str = ""
    summary: str = ""
    country_codes: tuple[str, ...] = field(default_factory=tuple)
    disaster_types: tuple[str, ...] = field(default_factory=tuple)
    themes: tuple[str, ...] = field(default_factory=tuple)
    updated_at: datetime.datetime | None = None
    provider_name: str = "reliefweb"
    output_schema_version: str = "humanitarian-report.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "published_at", ensure_utc(self.published_at))
        object.__setattr__(self, "updated_at", ensure_optional_utc(self.updated_at))
        object.__setattr__(self, "country_codes", tuple(self.country_codes))
        object.__setattr__(self, "disaster_types", tuple(self.disaster_types))
        object.__setattr__(self, "themes", tuple(self.themes))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class GeoIncident:
    """A normalized geospatial incident such as an earthquake or fire detection."""

    incident_id: str
    incident_type: str
    title: str
    occurred_at: datetime.datetime
    latitude: float
    longitude: float
    magnitude: float | None = None
    depth_km: float | None = None
    severity: str = ""
    country_code: str = ""
    place: str = ""
    url: str = ""
    provider_name: str = "unknown"
    output_schema_version: str = "geo-incident.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "latitude", float(self.latitude))
        object.__setattr__(self, "longitude", float(self.longitude))
        if self.magnitude is not None:
            object.__setattr__(self, "magnitude", float(self.magnitude))
        if self.depth_km is not None:
            object.__setattr__(self, "depth_km", float(self.depth_km))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class EnergyObservation:
    """A normalized energy time-series observation."""

    series_id: str
    observed_at: datetime.datetime
    value: float | None
    units: str = ""
    geography: str = ""
    provider_name: str = "eia"
    output_schema_version: str = "energy-observation.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        if self.value is not None:
            object.__setattr__(self, "value", float(self.value))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def date(self) -> datetime.date:
        return self.observed_at.date()

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


@dataclass(frozen=True)
class EnergySeries:
    """A normalized energy time-series definition."""

    series_id: str
    name: str
    units: str = ""
    frequency: str = ""
    geography: str = ""
    provider_name: str = "eia"
    output_schema_version: str = "energy-series.v1"
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_value(self.metadata))

    @property
    def schema_version(self) -> str:
        return self.output_schema_version


# --- Provider Protocols --------------------------------------------------------
@runtime_checkable
class RSSProvider(Protocol):
    """Fetch normalized items from an RSS feed URL."""

    def fetch(self, feed_url: str) -> list[RSSItem]: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Produce embedding vectors for a batch of texts."""

    model_name: str
    model_version: str

    def embed(self, texts: list[str]) -> list[EmbeddingResult]: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Run a named/versioned prompt and return a structured response."""

    def complete(
        self, prompt_name: str, prompt_version: str, prompt: str
    ) -> LLMResponse: ...


@runtime_checkable
class GDELTProvider(Protocol):
    """Search GDELT article and event data."""

    def search_articles(
        self,
        query: str,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        max_records: int = 50,
    ) -> list[GDELTArticle]: ...

    def search_events(
        self,
        query: str,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        max_records: int = 50,
    ) -> list[GDELTEvent]: ...


@runtime_checkable
class FREDProvider(Protocol):
    """Fetch FRED time-series observations."""

    def fetch_series_observations(
        self,
        series_id: str,
        *,
        observation_start: datetime.date | None = None,
        observation_end: datetime.date | None = None,
        limit: int | None = None,
    ) -> list[FREDObservation]: ...


@runtime_checkable
class SECEdgarProvider(Protocol):
    """Fetch SEC EDGAR submissions and company facts."""

    def fetch_submissions(self, cik: str) -> list[SECSubmission]: ...

    def fetch_company_facts(self, cik: str) -> list[SECCompanyFact]: ...


@runtime_checkable
class SECCompanyTickerProvider(Protocol):
    """Fetch the SEC ``company_tickers.json`` name/ticker/CIK identity seed."""

    def fetch_company_tickers(self) -> list[SECCompanyTicker]: ...


@runtime_checkable
class SanctionsProvider(Protocol):
    """Fetch sanctioned entities and sanctions-list changes."""

    def fetch_entities(
        self,
        *,
        program: str | None = None,
        updated_since: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> list[SanctionsEntity]: ...

    def fetch_deltas(
        self,
        *,
        since: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> list[SanctionsDelta]: ...


@runtime_checkable
class EntityIdentityProvider(Protocol):
    """Search entity identity records and ownership relationships."""

    def search_records(
        self,
        query: str,
        *,
        country_code: str | None = None,
        limit: int = 20,
    ) -> list[LEIRecord]: ...

    def fetch_relationships(
        self,
        lei: str,
        *,
        relationship_type: str | None = None,
        limit: int = 100,
    ) -> list[LEIRelationship]: ...


@runtime_checkable
class WikidataProvider(Protocol):
    """Fetch bounded Wikidata identity payloads.

    Both calls are bounded by construction: the caller supplies the exact QIDs or the exact
    identifier values it already holds, and the implementation pins them into the query. There
    is deliberately no free-text search entry point — Wikidata enrichment is restricted to
    entities the other ADR 0006 sources already seeded plus a curated QID watchlist. ``limit``
    caps the rows a single query may return.
    """

    def resolve_qids(
        self,
        *,
        ciks: Sequence[str] = (),
        leis: Sequence[str] = (),
        limit: int = 10_000,
    ) -> list[WikidataQidMatch]: ...

    def fetch_entities(
        self, qids: Sequence[str], *, limit: int = 10_000
    ) -> list[WikidataEntity]: ...


@runtime_checkable
class CountryIndicatorProvider(Protocol):
    """Fetch country-level indicator metadata and observations."""

    def fetch_indicator_metadata(self, indicator_id: str) -> CountryIndicator | None: ...

    def fetch_indicator_observations(
        self,
        country_code: str,
        indicator_id: str,
        *,
        start_year: int | None = None,
        end_year: int | None = None,
        limit: int | None = None,
    ) -> list[CountryIndicatorObservation]: ...


@runtime_checkable
class HumanitarianProvider(Protocol):
    """Search humanitarian reports and situation updates."""

    def search_reports(
        self,
        query: str,
        *,
        country_code: str | None = None,
        disaster_type: str | None = None,
        limit: int = 20,
    ) -> list[HumanitarianReport]: ...


@runtime_checkable
class GeoIncidentProvider(Protocol):
    """Search normalized geospatial incidents."""

    def search_incidents(
        self,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        region: str | None = None,
        limit: int | None = None,
    ) -> list[GeoIncident]: ...


@runtime_checkable
class EnergyProvider(Protocol):
    """Fetch energy time-series definitions and observations."""

    def fetch_series(self, series_id: str) -> EnergySeries: ...

    def fetch_series_observations(
        self,
        series_id: str,
        *,
        start_at: datetime.datetime | None = None,
        end_at: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> list[EnergyObservation]: ...


@runtime_checkable
class Clock(Protocol):
    """Abstract wall-clock access so time-dependent logic is testable."""

    def now(self) -> datetime.datetime: ...


class SystemClock(Clock):
    """UTC wall-clock implementation for production code that needs current time."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)
