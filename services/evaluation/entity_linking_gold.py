"""The entity-linking gold v1 contract: pure, offline schema, loaders and validators.

Stage 2 (``services.entities.news_linking``) links a news mention to a canonical entity with a
hand-tuned weighted score. Those weights are, in the ADR's words, "initial values, to be
recalibrated against the labeled-mentions gold set" -- this module is the contract for that gold
set. Nothing here touches a database, a network, spaCy, or an LLM: a gold dataset is committed as
JSON under ``evaluation/gold/entity_linking/v1/`` and this module loads it, rejects everything it
can before a tuning run ever starts, and converts a labelled mention into the exact Stage-2 input
DTOs (``EntityMention`` + ``ArticleLinkingContext``) on demand.

The dataset is five physical files: ``manifest.json`` (identifiers, split map, provenance),
``targets.json`` (the entity catalog every label points at), and one file per split
(``train.json`` 100, ``development.json`` 50, ``final_holdout.json`` 50, total 200). The catalog and
each mention carry only original, synthetic prose, so nothing here reproduces a copyrighted article.

Two rules that a human eye slides over are enforced mechanically, because getting them wrong makes a
recalibration lie about itself:

* **The holdout is gated.** The tuning loader (:func:`load_tuning_corpus`) never returns the final
  holdout, and :func:`load_split` refuses to load it without an explicit opt-in. Fitting a threshold
  on data you then report a "held-out" number against is the one failure a gold set exists to
  prevent.
* **No leakage across splits.** A ``leakage_group`` (or an identical document) may not straddle two
  splits, so the same story cannot sit in both what you tune on and what you evaluate on.

Every fixture UUID is derived from :data:`TARGET_UUID_NAMESPACE` and the target id, so the catalog
can be turned into ``EntityProfile``/``EntityAlias``/``EntityRelationship`` rows deterministically
(:meth:`GoldTarget.profile_row` and friends) without writing anything.
"""

from __future__ import annotations

import datetime
import json
import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from services.entities.news_linking import (
    COMPANY_ENTITY_TYPES,
    COMPANY_MENTION_LABELS,
    LINKABLE_ALIAS_TYPES,
    LOCATION_ENTITY_TYPES,
    ArticleLinkingContext,
)
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import EntityLabel, EntityMention, SentenceContext
from services.provider_data.common import normalize_alias, normalize_name, stable_hash

# --- identifiers (exact v1) ----------------------------------------------------------------
DATASET_ID = "entity_linking_gold"
SCHEMA_VERSION = "v1"
LANGUAGE = "en"
MANIFEST_FILE = "manifest.json"
CATALOG_FILE = "targets.json"

SPLIT_TRAIN = "train"
SPLIT_DEVELOPMENT = "development"
SPLIT_FINAL_HOLDOUT = "final_holdout"
#: Physical, deterministic order: what tuning may see comes first, the holdout last.
SPLIT_ORDER = (SPLIT_TRAIN, SPLIT_DEVELOPMENT, SPLIT_FINAL_HOLDOUT)
SPLIT_FILES = {SPLIT_TRAIN: "train.json", SPLIT_DEVELOPMENT: "development.json", SPLIT_FINAL_HOLDOUT: "final_holdout.json"}
SPLIT_COUNTS = {SPLIT_TRAIN: 100, SPLIT_DEVELOPMENT: 50, SPLIT_FINAL_HOLDOUT: 50}
TOTAL_COUNT = 200
#: The splits a default/tuning consumer may hold; the holdout is deliberately absent.
TUNING_SPLITS = (SPLIT_TRAIN, SPLIT_DEVELOPMENT)

#: The one exported namespace every fixture UUID is derived from. Frozen: a target's UUID is a
#: function of its id, so the catalog reproduces the same identity every load, in tests and in seeds.
TARGET_UUID_NAMESPACE = uuid.UUID("6d4f1c2e-8a37-5b90-9c1d-2e7f4a6b8c05")
_ALIAS_UUID_TAG = "alias"
_RELATIONSHIP_UUID_TAG = "relationship"

GOLD_ROOT = Path(__file__).resolve().parents[2] / "evaluation" / "gold" / "entity_linking" / "v1"

# --- controlled vocabularies ---------------------------------------------------------------
#: Entity types the catalog may carry, borrowed from the linker so the two never drift apart.
TARGET_ENTITY_TYPES = COMPANY_ENTITY_TYPES | LOCATION_ENTITY_TYPES | frozenset({"person", "product"})
ALIAS_SOURCES = frozenset({"sec-edgar", "gleif", "wikidata", "internal"})
RELATIONSHIP_TYPES = frozenset({"subsidiary", "division", "brand", "affiliate", "ownership"})
REF_KINDS = frozenset(
    {"regulatory_filing", "registry", "company_disclosure", "reference_work", "news_article"}
)
EXPECTED_LABELS = frozenset({"link", "nil"})
PROVENANCE_KIND = "original_synthetic"

#: Every case the recalibration must be able to see, so a missing bucket fails the audit rather than
#: silently biasing the tuned thresholds.
CASE_TAGS = (
    "legal_name",
    "short_name",
    "former_name",
    "colloquial",
    "brand_product",
    "ticker",
    "transliteration",
    "acronym",
    "ambiguous",
    "subsidiary_parent",
    "geographic_disambiguation",
    "hard_negative",
    "nil",
)
CASE_TAG_SET = frozenset(CASE_TAGS)
#: Modest, achievable minima -- curation stays practical while every bucket is guaranteed present.
MIN_MENTIONS_PER_CASE_TAG = 1
MIN_TARGETS = 12
MIN_COUNTRIES = 8

#: The positive labels: a mention that resolves to an entity is an organization or a product, never
#: a person or a place (the identity store holds companies).
POSITIVE_LABELS = COMPANY_MENTION_LABELS

MAX_DOCUMENT_CHARS = 2000
MAX_QUOTE_CHARS = 240
FAKE_URL_HOSTS = frozenset(
    {"example.com", "example.org", "example.net", "example.edu", "test", "localhost", "invalid",
     "fake.com", "placeholder.com"}
)

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_LEI_PATTERN = re.compile(r"^[A-Z0-9]{20}$")
_QUOTE_RUN = re.compile(r'"([^"]*)"')

# --- error ---------------------------------------------------------------------------------
class EntityGoldValidationError(ValueError):
    """The committed gold set (or a candidate) is not usable. Carries every problem, not the first.

    Raised before any tuning run: a bad gold set must never recalibrate a live threshold. Each
    problem names the file and record it came from, so a curator can find it without a debugger.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        listed = "\n  - ".join(self.problems)
        super().__init__(f"{len(self.problems)} entity-linking gold problem(s):\n  - {listed}")


# --- fixture identity ----------------------------------------------------------------------
def fixture_target_uuid(target_id: str) -> uuid.UUID:
    """The deterministic profile UUID for a target id: reproducible from the namespace alone."""
    return uuid.uuid5(TARGET_UUID_NAMESPACE, target_id)


def _alias_uuid(target_id: str, normalized: str, source: str) -> uuid.UUID:
    return uuid.uuid5(TARGET_UUID_NAMESPACE, f"{target_id}|{_ALIAS_UUID_TAG}|{normalized}|{source}")


def _relationship_uuid(child_id: str, parent_id: str, relationship_type: str) -> uuid.UUID:
    return uuid.uuid5(
        TARGET_UUID_NAMESPACE, f"{parent_id}|{_RELATIONSHIP_UUID_TAG}|{child_id}|{relationship_type}"
    )


def document_fingerprint(text: str) -> str:
    """A stable fingerprint of a document's *normalized* text, for cross-split leakage checks."""
    return stable_hash(normalize_name(text))


# --- dataclasses ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GoldAlias:
    """One alternate surface for a target, in the ADR 0006 alias vocabulary."""

    value: str
    normalized: str
    alias_type: str
    source: str
    valid_from: datetime.date | None
    valid_to: datetime.date | None


@dataclass(frozen=True, slots=True)
class GoldParent:
    """A target's parent, resolved by target id -- a subsidiary/brand/division relationship."""

    target_id: str
    relationship_type: str


@dataclass(frozen=True, slots=True)
class AuthoritativeRef:
    """A citation that makes a target auditable: registry, filing, or reference work."""

    id: str
    title: str
    publisher: str
    url: str
    accessed: datetime.date
    kind: str


@dataclass(frozen=True, slots=True)
class GoldTarget:
    """One catalog entity every label may point at, plus its ORM-compatible fixture rows."""

    target_id: str
    fixture_uuid: uuid.UUID
    canonical_name: str
    normalized_name: str
    entity_type: str
    country: str
    ticker: str | None
    cik: str | None
    lei: str | None
    website: str | None
    aliases: tuple[GoldAlias, ...]
    parent: GoldParent | None
    refs: tuple[AuthoritativeRef, ...]

    def profile_row(self) -> dict[str, Any]:
        """A ``dict`` that constructs an ``EntityProfile`` (``EntityProfile(**row)``). No write."""
        return {
            "id": self.fixture_uuid,
            "canonical_name": self.canonical_name,
            "normalized_name": self.normalized_name,
            "entity_type": self.entity_type,
            "country": self.country,
            "primary_ticker": self.ticker,
            "primary_cik": self.cik,
            "primary_lei": self.lei,
            "website": self.website,
            "profile_metadata": None,
        }

    def alias_rows(self) -> tuple[dict[str, Any], ...]:
        """One ``EntityAlias``-shaped ``dict`` per alias, with deterministic ids. No write."""
        return tuple(
            {
                "id": _alias_uuid(self.target_id, alias.normalized, alias.source),
                "entity_id": self.fixture_uuid,
                "alias": alias.value,
                "normalized_alias": alias.normalized,
                "alias_type": alias.alias_type,
                "source": alias.source,
                "valid_from": alias.valid_from,
                "valid_to": alias.valid_to,
            }
            for alias in self.aliases
        )

    def relationship_row(self) -> dict[str, Any] | None:
        """An ``EntityRelationship``-shaped ``dict`` when the target has a parent. No write."""
        if self.parent is None:
            return None
        return {
            "id": _relationship_uuid(self.target_id, self.parent.target_id, self.parent.relationship_type),
            "parent_entity_id": fixture_target_uuid(self.parent.target_id),
            "child_entity_id": self.fixture_uuid,
            "relationship_type": self.parent.relationship_type,
        }


@dataclass(frozen=True, slots=True)
class TargetCatalog:
    """The validated target catalog, in deterministic target-id order."""

    targets: tuple[GoldTarget, ...]

    @property
    def by_id(self) -> dict[str, GoldTarget]:
        return {target.target_id: target for target in self.targets}

    @property
    def countries(self) -> frozenset[str]:
        return frozenset(target.country for target in self.targets)


@dataclass(frozen=True, slots=True)
class LinkingContextSpec:
    """The explicit Stage-2 context inputs a mention declares -- nothing is read from an article."""

    source_category: str | None
    industry_terms: tuple[str, ...]
    location_terms: tuple[str, ...]
    co_mention_surfaces: tuple[str, ...]
    linked_target_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoldSentence:
    """The mention's sentence and its neighbours, sufficient to rebuild a ``SentenceContext``."""

    index: int
    text: str
    start: int
    end: int
    previous_text: str | None
    next_text: str | None


@dataclass(frozen=True, slots=True)
class GoldMention:
    """One labelled mention: the exact surface, its document, its context, and its answer."""

    mention_id: str
    split: str
    language: str
    article_key: str
    document_text: str
    sentence: GoldSentence
    mention_text: str
    start: int
    end: int
    label: EntityLabel
    assertion_status: AssertionStatus
    published_on: datetime.date
    article_url: str | None
    context: LinkingContextSpec
    expected_target_id: str | None
    expected_label: str
    case_tags: tuple[str, ...]
    leakage_group: str
    note: str
    provenance_kind: str
    provenance_created_on: datetime.date

    @property
    def is_link(self) -> bool:
        return self.expected_label == "link"

    @property
    def fingerprint(self) -> str:
        return document_fingerprint(self.document_text)

    def to_stage2_inputs(
        self, catalog: TargetCatalog
    ) -> tuple[EntityMention, ArticleLinkingContext]:
        """Convert to the exact Stage-2 DTOs, resolving linked target refs to fixture UUIDs.

        Pure and deterministic: no spaCy, no LLM, no database. The ``SentenceContext`` is
        reconstructed field for field, so a linked candidate cannot support itself and the linker
        scores the mention exactly as it would in production.
        """
        sentence = SentenceContext(
            index=self.sentence.index,
            text=self.sentence.text,
            start_char=self.sentence.start,
            end_char=self.sentence.end,
            previous_text=self.sentence.previous_text,
            next_text=self.sentence.next_text,
        )
        mention = EntityMention(
            article_key=self.article_key,
            text=self.mention_text,
            label=self.label,
            start_char=self.start,
            end_char=self.end,
            sentence=sentence,
            assertion_status=self.assertion_status,
        )
        context = ArticleLinkingContext(
            article_key=self.article_key,
            published_on=self.published_on,
            url=self.article_url,
            source_category=self.context.source_category,
            text=self.document_text,
            co_mention_surfaces=self.context.co_mention_surfaces,
            linked_entity_ids=tuple(
                fixture_target_uuid(target_id) for target_id in self.context.linked_target_ids
            ),
            industry_terms=self.context.industry_terms,
            location_terms=self.context.location_terms,
        )
        return mention, context


@dataclass(frozen=True, slots=True)
class GoldManifest:
    """The dataset descriptor: identifiers, the split map, and provenance/labeling/license."""

    dataset_id: str
    schema_version: str
    language: str
    target_catalog: str
    splits: tuple[tuple[str, str, int], ...]  # (name, file, count) in SPLIT_ORDER
    total_count: int
    metadata: Mapping[str, Any]
    labeling: Mapping[str, Any]
    license: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GoldQuotas:
    """The audit the contract asks for. Reported on every full load, JSON-serializable."""

    total: int
    links: int
    nils: int
    targets: int
    countries: int
    by_label: dict[str, int]
    by_case_tag: dict[str, int]
    by_assertion: dict[str, int]
    positive_labels: dict[str, int]
    nil_labels: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "links": self.links,
            "nils": self.nils,
            "targets": self.targets,
            "countries": self.countries,
            "by_label": dict(sorted(self.by_label.items())),
            "by_case_tag": dict(sorted(self.by_case_tag.items())),
            "by_assertion": dict(sorted(self.by_assertion.items())),
            "positive_labels": dict(sorted(self.positive_labels.items())),
            "nil_labels": dict(sorted(self.nil_labels.items())),
        }


@dataclass(frozen=True, slots=True)
class EntityLinkingGoldCorpus:
    """A validated dataset (or a validated tuning slice), in deterministic order."""

    manifest: GoldManifest
    catalog: TargetCatalog
    mentions: tuple[GoldMention, ...]
    quotas: GoldQuotas
    includes_holdout: bool

    def mentions_for(self, split: str) -> tuple[GoldMention, ...]:
        return tuple(mention for mention in self.mentions if mention.split == split)

    def report(self) -> dict[str, Any]:
        return {
            "dataset_id": self.manifest.dataset_id,
            "schema_version": self.manifest.schema_version,
            "includes_holdout": self.includes_holdout,
            "quotas": self.quotas.as_dict(),
        }


# --- primitive validators ------------------------------------------------------------------
def _keys(raw: Any, where: str, required: set[str], optional: set[str], problems: list[str]) -> bool:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: expected an object, got {type(raw).__name__}")
        return False
    present = set(raw)
    unknown = present - required - optional
    missing = required - present
    if unknown:
        problems.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    if missing:
        problems.append(f"{where}: missing field(s) {', '.join(sorted(missing))}")
    return not missing


def _str(raw: Mapping[str, Any], key: str, where: str, problems: list[str], *, min_len: int = 1) -> str | None:
    value = raw.get(key)
    if not isinstance(value, str) or len(value.strip()) < min_len:
        problems.append(f"{where}.{key}: must be a non-empty string of at least {min_len} char(s)")
        return None
    return value


def _int(raw: Mapping[str, Any], key: str, where: str, problems: list[str]) -> int | None:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        problems.append(f"{where}.{key}: must be a non-negative integer")
        return None
    return value


def _date(value: Any, where: str, problems: list[str]) -> datetime.date | None:
    if not isinstance(value, str):
        problems.append(f"{where}: expected an ISO date string, got {type(value).__name__}")
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        problems.append(f"{where}: {value!r} is not an ISO date")
        return None


def _str_list(
    value: Any, where: str, problems: list[str], vocabulary: frozenset[str] | None = None
) -> tuple[str, ...]:
    """A sorted, duplicate-free list of strings (optionally from a vocabulary)."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.append(f"{where}: must be a list of strings")
        return ()
    if len(set(value)) != len(value):
        problems.append(f"{where}: contains duplicate value(s)")
    if list(value) != sorted(value):
        problems.append(f"{where}: must be in sorted order")
    if vocabulary is not None:
        unknown = [item for item in value if item not in vocabulary]
        if unknown:
            problems.append(f"{where}: unknown value(s) {', '.join(sorted(set(unknown)))}")
    return tuple(value)


def _https(url: Any, where: str, problems: list[str]) -> str | None:
    if not isinstance(url, str):
        problems.append(f"{where}: url must be a string")
        return None
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if parsed.scheme != "https":
        problems.append(f"{where}: url must be https, got {url!r}")
    elif not host:
        problems.append(f"{where}: url has no host, got {url!r}")
    elif any(host == fake or host.endswith(f".{fake}") for fake in FAKE_URL_HOSTS):
        problems.append(f"{where}: {host!r} is a placeholder/fake host")
    return url


def _read_json(path: Path, problems: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"{path.name}: unreadable ({exc})")
        return None


# --- manifest ------------------------------------------------------------------------------
_MANIFEST_KEYS = {
    "dataset_id", "schema_version", "language", "target_catalog", "splits", "total_count",
    "metadata", "labeling", "license",
}


def load_manifest(root: Path = GOLD_ROOT) -> GoldManifest:
    """Load and validate ``manifest.json``: exact identifiers, split map, and provenance."""
    problems: list[str] = []
    raw = _read_json(root / MANIFEST_FILE, problems)
    if raw is None or not _keys(raw, MANIFEST_FILE, _MANIFEST_KEYS, set(), problems):
        raise EntityGoldValidationError(problems or [f"{MANIFEST_FILE}: unreadable"])

    if raw.get("dataset_id") != DATASET_ID:
        problems.append(f"{MANIFEST_FILE}: dataset_id must be {DATASET_ID!r}")
    if raw.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"{MANIFEST_FILE}: schema_version must be {SCHEMA_VERSION!r}")
    if raw.get("language") != LANGUAGE:
        problems.append(f"{MANIFEST_FILE}: language must be {LANGUAGE!r} (en-only)")
    if raw.get("target_catalog") != CATALOG_FILE:
        problems.append(f"{MANIFEST_FILE}: target_catalog must be {CATALOG_FILE!r}")

    splits = _manifest_splits(raw.get("splits"), problems)
    total = raw.get("total_count")
    if total != TOTAL_COUNT:
        problems.append(f"{MANIFEST_FILE}: total_count must be {TOTAL_COUNT}")
    if splits and sum(count for _, _, count in splits) != TOTAL_COUNT:
        problems.append(f"{MANIFEST_FILE}: split counts must sum to {TOTAL_COUNT}")

    _require_block(raw.get("metadata"), "metadata", {"title", "description", "created_on"}, problems)
    _require_block(raw.get("labeling"), "labeling", {"guidelines", "labelers", "adjudication"}, problems)
    _require_block(raw.get("license"), "license", {"id", "note"}, problems)
    if isinstance(raw.get("metadata"), Mapping):
        _date(raw["metadata"].get("created_on"), f"{MANIFEST_FILE}.metadata.created_on", problems)
    if isinstance(raw.get("labeling"), Mapping):
        labelers = raw["labeling"].get("labelers")
        if not isinstance(labelers, list) or not labelers:
            problems.append(f"{MANIFEST_FILE}.labeling.labelers: must be a non-empty list")

    if problems:
        raise EntityGoldValidationError(problems)
    return GoldManifest(
        dataset_id=DATASET_ID,
        schema_version=SCHEMA_VERSION,
        language=LANGUAGE,
        target_catalog=CATALOG_FILE,
        splits=splits,
        total_count=TOTAL_COUNT,
        metadata=dict(raw["metadata"]),
        labeling=dict(raw["labeling"]),
        license=dict(raw["license"]),
    )


def _manifest_splits(raw: Any, problems: list[str]) -> tuple[tuple[str, str, int], ...]:
    if not isinstance(raw, list):
        problems.append(f"{MANIFEST_FILE}.splits: must be a list")
        return ()
    names = [item.get("name") if isinstance(item, Mapping) else None for item in raw]
    if names != list(SPLIT_ORDER):
        problems.append(f"{MANIFEST_FILE}.splits: must be exactly {list(SPLIT_ORDER)} in order")
    parsed: list[tuple[str, str, int]] = []
    for item in raw:
        where = f"{MANIFEST_FILE}.splits"
        if not _keys(item, where, {"name", "file", "count"}, set(), problems):
            continue
        name = item["name"]
        if item.get("file") != SPLIT_FILES.get(name):
            problems.append(f"{where}[{name}]: file must be {SPLIT_FILES.get(name)!r}")
        if item.get("count") != SPLIT_COUNTS.get(name):
            problems.append(f"{where}[{name}]: count must be {SPLIT_COUNTS.get(name)}")
        parsed.append((name, SPLIT_FILES.get(name, ""), SPLIT_COUNTS.get(name, 0)))
    return tuple(parsed)


def _require_block(raw: Any, name: str, required: set[str], problems: list[str]) -> None:
    if not isinstance(raw, Mapping):
        problems.append(f"{MANIFEST_FILE}.{name}: must be an object")
        return
    missing = required - set(raw)
    if missing:
        problems.append(f"{MANIFEST_FILE}.{name}: missing {', '.join(sorted(missing))}")


# --- target catalog ------------------------------------------------------------------------
_TARGET_REQUIRED = {"target_id", "fixture_uuid", "canonical_name", "normalized_name", "entity_type",
                    "country", "aliases", "authoritative_refs"}
_TARGET_OPTIONAL = {"ticker", "cik", "lei", "website", "parent"}
_ALIAS_REQUIRED = {"value", "normalized", "alias_type", "source"}
_ALIAS_OPTIONAL = {"valid_from", "valid_to"}
_REF_KEYS = {"id", "title", "publisher", "url", "accessed", "kind"}
_PARENT_KEYS = {"target_id", "relationship_type"}


def load_target_catalog(root: Path = GOLD_ROOT) -> TargetCatalog:
    """Load and validate ``targets.json``: identifiers, normalization, ordering, and parents."""
    problems: list[str] = []
    raw = _read_json(root / CATALOG_FILE, problems)
    if raw is None or not _keys(raw, CATALOG_FILE, {"schema_version", "targets"}, set(), problems):
        raise EntityGoldValidationError(problems or [f"{CATALOG_FILE}: unreadable"])
    if raw.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"{CATALOG_FILE}: schema_version must be {SCHEMA_VERSION!r}")

    entries = raw.get("targets")
    if not isinstance(entries, list):
        raise EntityGoldValidationError(problems + [f"{CATALOG_FILE}.targets: must be a list"])

    targets: list[GoldTarget] = []
    ids = [item.get("target_id") if isinstance(item, Mapping) else None for item in entries]
    if ids != sorted(str(i) for i in ids):
        problems.append(f"{CATALOG_FILE}.targets: must be sorted by target_id")
    seen: set[str] = set()
    for item in entries:
        target = _check_target(item, problems)
        if target is None:
            continue
        if target.target_id in seen:
            problems.append(f"{CATALOG_FILE}:{target.target_id}: duplicate target_id")
        seen.add(target.target_id)
        targets.append(target)

    _check_parents(targets, problems)
    if problems:
        raise EntityGoldValidationError(problems)
    return TargetCatalog(targets=tuple(targets))


def _check_target(raw: Any, problems: list[str]) -> GoldTarget | None:
    target_id = raw.get("target_id") if isinstance(raw, Mapping) else None
    where = f"{CATALOG_FILE}:{target_id or '<no id>'}"
    if not _keys(raw, where, _TARGET_REQUIRED, _TARGET_OPTIONAL, problems):
        return None
    if not isinstance(target_id, str) or not _ID_PATTERN.match(target_id):
        problems.append(f"{where}: target_id must match {_ID_PATTERN.pattern}")
        return None

    fixture = fixture_target_uuid(target_id)
    if raw.get("fixture_uuid") != str(fixture):
        problems.append(f"{where}: fixture_uuid must be {fixture} (uuid5 of the exported namespace)")

    canonical = _str(raw, "canonical_name", where, problems)
    normalized = _str(raw, "normalized_name", where, problems)
    if canonical is not None and normalized is not None and normalize_name(canonical) != normalized:
        problems.append(f"{where}: normalized_name must equal normalize_name(canonical_name)")

    if raw.get("entity_type") not in TARGET_ENTITY_TYPES:
        problems.append(f"{where}: unknown entity_type {raw.get('entity_type')!r}")
    country = raw.get("country")
    if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
        problems.append(f"{where}: country must be a 2-letter uppercase code")

    ticker = _check_optional_identifier(raw, "ticker", _TICKER_PATTERN, where, problems)
    lei = _check_optional_identifier(raw, "lei", _LEI_PATTERN, where, problems)
    cik = raw.get("cik")
    if cik is not None and (not isinstance(cik, str) or not cik.isdigit() or len(cik) > 10):
        problems.append(f"{where}: cik must be up to 10 digits")
        cik = None
    website = _https(raw["website"], f"{where}.website", problems) if raw.get("website") else None

    aliases = _check_aliases(raw.get("aliases"), where, problems)
    refs = _check_refs(raw.get("authoritative_refs"), where, problems)
    parent = _check_parent(raw.get("parent"), target_id, where, problems)

    if canonical is None or normalized is None:
        return None
    return GoldTarget(
        target_id=target_id,
        fixture_uuid=fixture,
        canonical_name=canonical,
        normalized_name=normalized,
        entity_type=str(raw.get("entity_type")),
        country=str(country),
        ticker=ticker,
        cik=cik if isinstance(cik, str) else None,
        lei=lei,
        website=website,
        aliases=aliases,
        parent=parent,
        refs=refs,
    )


def _check_optional_identifier(
    raw: Mapping[str, Any], key: str, pattern: re.Pattern[str], where: str, problems: list[str]
) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.match(value):
        problems.append(f"{where}.{key}: {value!r} does not match {pattern.pattern}")
        return None
    return value


def _check_aliases(raw: Any, where: str, problems: list[str]) -> tuple[GoldAlias, ...]:
    if not isinstance(raw, list):
        problems.append(f"{where}.aliases: must be a list")
        return ()
    aliases: list[GoldAlias] = []
    for index, item in enumerate(raw):
        spot = f"{where}.aliases[{index}]"
        if not _keys(item, spot, _ALIAS_REQUIRED, _ALIAS_OPTIONAL, problems):
            continue
        value = _str(item, "value", spot, problems)
        normalized = item.get("normalized")
        if value is not None and normalize_alias(value) != normalized:
            problems.append(f"{spot}: normalized must equal normalize_alias(value)")
        if item.get("alias_type") not in LINKABLE_ALIAS_TYPES:
            problems.append(f"{spot}: alias_type {item.get('alias_type')!r} is not an ADR alias type")
        if item.get("source") not in ALIAS_SOURCES:
            problems.append(f"{spot}: unknown source {item.get('source')!r}")
        valid_from = _date(item["valid_from"], f"{spot}.valid_from", problems) if item.get("valid_from") else None
        valid_to = _date(item["valid_to"], f"{spot}.valid_to", problems) if item.get("valid_to") else None
        if valid_from is not None and valid_to is not None and valid_from > valid_to:
            problems.append(f"{spot}: valid_from {valid_from} follows valid_to {valid_to}")
        if value is not None and isinstance(normalized, str):
            aliases.append(
                GoldAlias(value, normalized, str(item.get("alias_type")), str(item.get("source")),
                          valid_from, valid_to)
            )
    keys = [(alias.alias_type, alias.normalized, alias.source) for alias in aliases]
    if keys != sorted(keys):
        problems.append(f"{where}.aliases: must be sorted by (alias_type, normalized, source)")
    if len(set(keys)) != len(keys):
        problems.append(f"{where}.aliases: duplicate (alias_type, normalized, source)")
    return tuple(aliases)


def _check_refs(raw: Any, where: str, problems: list[str]) -> tuple[AuthoritativeRef, ...]:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where}.authoritative_refs: must be a non-empty list")
        return ()
    refs: list[AuthoritativeRef] = []
    ids: list[str] = []
    for index, item in enumerate(raw):
        spot = f"{where}.authoritative_refs[{index}]"
        if not _keys(item, spot, _REF_KEYS, set(), problems):
            continue
        ref_id = _str(item, "id", spot, problems)
        title = _str(item, "title", spot, problems)
        publisher = _str(item, "publisher", spot, problems)
        _https(item.get("url"), spot, problems)
        accessed = _date(item.get("accessed"), f"{spot}.accessed", problems)
        if item.get("kind") not in REF_KINDS:
            problems.append(f"{spot}: unknown kind {item.get('kind')!r}")
        if None not in (ref_id, title, publisher, accessed):
            ids.append(ref_id)  # type: ignore[arg-type]
            refs.append(AuthoritativeRef(ref_id, title, publisher, str(item["url"]), accessed, str(item["kind"])))  # type: ignore[arg-type]
    if ids != sorted(ids):
        problems.append(f"{where}.authoritative_refs: must be sorted by id")
    if len(set(ids)) != len(ids):
        problems.append(f"{where}.authoritative_refs: duplicate ref id")
    return tuple(refs)


def _check_parent(raw: Any, target_id: str, where: str, problems: list[str]) -> GoldParent | None:
    if raw is None:
        return None
    if not _keys(raw, f"{where}.parent", _PARENT_KEYS, set(), problems):
        return None
    parent_id = raw.get("target_id")
    if not isinstance(parent_id, str) or not _ID_PATTERN.match(parent_id):
        problems.append(f"{where}.parent: target_id must match {_ID_PATTERN.pattern}")
        return None
    if parent_id == target_id:
        problems.append(f"{where}.parent: a target cannot be its own parent")
        return None
    if raw.get("relationship_type") not in RELATIONSHIP_TYPES:
        problems.append(f"{where}.parent: unknown relationship_type {raw.get('relationship_type')!r}")
    return GoldParent(parent_id, str(raw.get("relationship_type")))


def _check_parents(targets: list[GoldTarget], problems: list[str]) -> None:
    """Every parent exists, no target is its own ancestor, and no cycle closes."""
    by_id = {target.target_id: target for target in targets}
    for target in targets:
        if target.parent is None:
            continue
        if target.parent.target_id not in by_id:
            problems.append(f"{CATALOG_FILE}:{target.target_id}: parent {target.parent.target_id!r} is not in the catalog")
            continue
        seen = {target.target_id}
        cursor: GoldTarget | None = by_id.get(target.parent.target_id)
        while cursor is not None:
            if cursor.target_id in seen:
                problems.append(f"{CATALOG_FILE}:{target.target_id}: parent chain forms a cycle")
                break
            seen.add(cursor.target_id)
            cursor = by_id.get(cursor.parent.target_id) if cursor.parent else None


# --- mentions / splits ---------------------------------------------------------------------
_MENTION_REQUIRED = {
    "mention_id", "split", "language", "article_key", "document_text", "sentence", "mention_text",
    "start", "end", "label", "assertion_status", "published_on", "article_url", "context",
    "expected_target_id", "expected_label", "case_tags", "leakage_group", "note", "provenance",
}
_SENTENCE_KEYS = {"index", "text", "start", "end", "previous_text", "next_text"}
_CONTEXT_KEYS = {"source_category", "industry_terms", "location_terms", "co_mention_surfaces", "linked_target_ids"}
_PROVENANCE_KEYS = {"kind", "created_on"}


def load_split(
    root: Path = GOLD_ROOT,
    split: str = SPLIT_TRAIN,
    *,
    catalog: TargetCatalog | None = None,
    allow_holdout: bool = False,
) -> tuple[GoldMention, ...]:
    """Load and validate one named split against the catalog. The holdout is gated.

    ``final_holdout`` refuses to load unless ``allow_holdout`` is set: a tuning consumer must not
    reach it by accident, so opening it is always a deliberate, visible act.
    """
    if split not in SPLIT_ORDER:
        raise EntityGoldValidationError([f"unknown split {split!r}; expected one of {list(SPLIT_ORDER)}"])
    if split == SPLIT_FINAL_HOLDOUT and not allow_holdout:
        raise EntityGoldValidationError(
            [f"{split} is gated: pass allow_holdout=True to load the held-out evaluation split"]
        )
    catalog = catalog or load_target_catalog(root)
    problems: list[str] = []
    mentions = _load_split_mentions(root, split, catalog, problems)
    if problems:
        raise EntityGoldValidationError(problems)
    return mentions


def _load_split_mentions(
    root: Path, split: str, catalog: TargetCatalog, problems: list[str]
) -> tuple[GoldMention, ...]:
    raw = _read_json(root / SPLIT_FILES[split], problems)
    file = SPLIT_FILES[split]
    if raw is None or not _keys(raw, file, {"schema_version", "split", "mentions"}, set(), problems):
        return ()
    if raw.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"{file}: schema_version must be {SCHEMA_VERSION!r}")
    if raw.get("split") != split:
        problems.append(f"{file}: split must be {split!r}")
    entries = raw.get("mentions")
    if not isinstance(entries, list):
        problems.append(f"{file}.mentions: must be a list")
        return ()

    known = catalog.by_id
    mentions: list[GoldMention] = []
    for item in entries:
        mention = _check_mention(item, split, file, known, problems)
        if mention is not None:
            mentions.append(mention)
    order = [mention.mention_id for mention in mentions]
    if order != sorted(order):
        problems.append(f"{file}.mentions: must be sorted by mention_id")
    return tuple(mentions)


def _check_mention(
    raw: Any, split: str, file: str, known: Mapping[str, GoldTarget], problems: list[str]
) -> GoldMention | None:
    mention_id = raw.get("mention_id") if isinstance(raw, Mapping) else None
    where = f"{file}:{mention_id or '<no id>'}"
    if not _keys(raw, where, _MENTION_REQUIRED, set(), problems):
        return None
    if not isinstance(mention_id, str) or not _ID_PATTERN.match(mention_id):
        problems.append(f"{where}: mention_id must match {_ID_PATTERN.pattern}")
        return None
    if raw.get("split") != split:
        problems.append(f"{where}: split must be {split!r}")
    if raw.get("language") != LANGUAGE:
        problems.append(f"{where}: language must be {LANGUAGE!r} (en-only)")

    article_key = _str(raw, "article_key", where, problems)
    document = _str(raw, "document_text", where, problems)
    label = _enum(raw.get("label"), EntityLabel, f"{where}.label", problems)
    assertion = _enum(raw.get("assertion_status"), AssertionStatus, f"{where}.assertion_status", problems)
    published = _date(raw.get("published_on"), f"{where}.published_on", problems)

    sentence = _check_sentence(raw.get("sentence"), document, where, problems)
    span = _check_span(raw, document, sentence, where, problems)
    if isinstance(document, str):
        _check_original_text(document, where, problems)
    article_url = raw.get("article_url")
    if article_url is not None:
        _https(article_url, f"{where}.article_url", problems)

    context = _check_context(raw.get("context"), known, where, problems)
    expected_label, expected_target = _check_expectation(raw, label, known, where, problems)
    case_tags = _str_list(raw.get("case_tags"), f"{where}.case_tags", problems, CASE_TAG_SET)
    leakage = _str(raw, "leakage_group", where, problems)
    note = _str(raw, "note", where, problems, min_len=1)
    created_on = _check_provenance(raw.get("provenance"), where, problems)

    if None in (article_key, document, label, assertion, published, sentence, span, leakage, note, created_on):
        return None
    surface, start, end = span  # type: ignore[misc]
    return GoldMention(
        mention_id=mention_id,
        split=split,
        language=LANGUAGE,
        article_key=article_key,  # type: ignore[arg-type]
        document_text=document,  # type: ignore[arg-type]
        sentence=sentence,  # type: ignore[arg-type]
        mention_text=surface,
        start=start,
        end=end,
        label=label,  # type: ignore[arg-type]
        assertion_status=assertion,  # type: ignore[arg-type]
        published_on=published,  # type: ignore[arg-type]
        article_url=article_url if isinstance(article_url, str) else None,
        context=context,
        expected_target_id=expected_target,
        expected_label=expected_label,
        case_tags=case_tags,
        leakage_group=leakage,  # type: ignore[arg-type]
        note=note,  # type: ignore[arg-type]
        provenance_kind=PROVENANCE_KIND,
        provenance_created_on=created_on,  # type: ignore[arg-type]
    )


def _enum(value: Any, enum: type, where: str, problems: list[str]) -> Any:
    try:
        return enum(value)
    except ValueError:
        problems.append(f"{where}: {value!r} is not a valid {enum.__name__}")
        return None


def _check_sentence(raw: Any, document: str | None, where: str, problems: list[str]) -> GoldSentence | None:
    if not _keys(raw, f"{where}.sentence", _SENTENCE_KEYS, set(), problems):
        return None
    index = _int(raw, "index", f"{where}.sentence", problems)
    start = _int(raw, "start", f"{where}.sentence", problems)
    end = _int(raw, "end", f"{where}.sentence", problems)
    text = raw.get("text")
    if not isinstance(text, str) or not text:
        problems.append(f"{where}.sentence.text: must be a non-empty string")
        return None
    for name in ("previous_text", "next_text"):
        if raw.get(name) is not None and not isinstance(raw.get(name), str):
            problems.append(f"{where}.sentence.{name}: must be a string or null")
    if None in (index, start, end) or not isinstance(document, str):
        return None
    if not (0 <= start < end <= len(document)):
        problems.append(f"{where}.sentence: [{start}, {end}) is out of the document bounds")
        return None
    if document[start:end] != text:
        problems.append(f"{where}.sentence: text does not match document[{start}:{end}]")
    return GoldSentence(index, text, start, end, raw.get("previous_text"), raw.get("next_text"))


def _check_span(
    raw: Mapping[str, Any], document: str | None, sentence: GoldSentence | None, where: str, problems: list[str]
) -> tuple[str, int, int] | None:
    start, end = raw.get("start"), raw.get("end")
    surface = raw.get("mention_text")
    if not isinstance(surface, str) or not surface:
        problems.append(f"{where}.mention_text: must be a non-empty string")
        return None
    if not (isinstance(start, int) and isinstance(end, int)) or isinstance(start, bool) or isinstance(end, bool):
        problems.append(f"{where}: start/end must be integers")
        return None
    if not isinstance(document, str):
        return None
    if not (0 <= start < end <= len(document)):
        problems.append(f"{where}: mention span [{start}, {end}) is out of document bounds")
        return None
    if document[start:end] != surface:
        problems.append(f"{where}: document[{start}:{end}] is not the mention text {surface!r}")
        return None
    if sentence is not None and not (sentence.start <= start and end <= sentence.end):
        problems.append(f"{where}: mention span is not contained in its sentence")
    return surface, start, end


def _check_original_text(document: str, where: str, problems: list[str]) -> None:
    if len(document) > MAX_DOCUMENT_CHARS:
        problems.append(f"{where}.document_text: {len(document)} chars exceeds the {MAX_DOCUMENT_CHARS} bound")
    for quote in _QUOTE_RUN.findall(document):
        if len(quote) > MAX_QUOTE_CHARS:
            problems.append(f"{where}.document_text: quoted run of {len(quote)} chars exceeds {MAX_QUOTE_CHARS}")
            break


def _check_context(raw: Any, known: Mapping[str, GoldTarget], where: str, problems: list[str]) -> LinkingContextSpec:
    if not _keys(raw, f"{where}.context", _CONTEXT_KEYS, set(), problems):
        return LinkingContextSpec(None, (), (), (), ())
    source_category = raw.get("source_category")
    if source_category is not None and (not isinstance(source_category, str) or not source_category):
        problems.append(f"{where}.context.source_category: must be a non-empty string or null")
    industry = _str_list(raw.get("industry_terms"), f"{where}.context.industry_terms", problems)
    location = _str_list(raw.get("location_terms"), f"{where}.context.location_terms", problems)
    co_mentions = _str_list(raw.get("co_mention_surfaces"), f"{where}.context.co_mention_surfaces", problems)
    linked = _str_list(raw.get("linked_target_ids"), f"{where}.context.linked_target_ids", problems)
    for target_id in linked:
        if target_id not in known:
            problems.append(f"{where}.context.linked_target_ids: {target_id!r} is not in the catalog")
    return LinkingContextSpec(
        source_category=source_category if isinstance(source_category, str) else None,
        industry_terms=industry,
        location_terms=location,
        co_mention_surfaces=co_mentions,
        linked_target_ids=linked,
    )


def _check_expectation(
    raw: Mapping[str, Any], label: Any, known: Mapping[str, GoldTarget], where: str, problems: list[str]
) -> tuple[str, str | None]:
    expected_label = raw.get("expected_label")
    expected_target = raw.get("expected_target_id")
    if expected_label not in EXPECTED_LABELS:
        problems.append(f"{where}.expected_label: must be one of {sorted(EXPECTED_LABELS)}")
        return "nil", None
    if expected_label == "link":
        if not isinstance(expected_target, str) or expected_target not in known:
            problems.append(f"{where}: a link mention needs an expected_target_id in the catalog")
            expected_target = None
        if label is not None and label not in POSITIVE_LABELS:
            problems.append(f"{where}: a positive link must be labelled ORG or PRODUCT, not {label.value}")
    else:  # nil
        if expected_target is not None:
            problems.append(f"{where}: a NIL mention must have a null expected_target_id")
            expected_target = None
    return expected_label, expected_target if isinstance(expected_target, str) else None


def _check_provenance(raw: Any, where: str, problems: list[str]) -> datetime.date | None:
    if not _keys(raw, f"{where}.provenance", _PROVENANCE_KEYS, set(), problems):
        return None
    if raw.get("kind") != PROVENANCE_KIND:
        problems.append(f"{where}.provenance.kind: must be {PROVENANCE_KIND!r}")
    return _date(raw.get("created_on"), f"{where}.provenance.created_on", problems)


# --- corpus assembly -----------------------------------------------------------------------
def load_corpus(root: Path = GOLD_ROOT) -> EntityLinkingGoldCorpus:
    """Load and audit the whole 200-mention dataset, holdout included.

    This is the integrity audit -- exact counts, split order, unique ids and article keys, no
    cross-split leakage, and every quota. It reads the holdout because auditing the dataset's
    integrity requires it; :func:`load_tuning_corpus` is the loader a *consumer* uses.
    """
    return _assemble(root, SPLIT_ORDER, includes_holdout=True, enforce_quotas=True)


def load_tuning_corpus(root: Path = GOLD_ROOT) -> EntityLinkingGoldCorpus:
    """Load train + development for tuning. The final holdout is excluded by construction."""
    return _assemble(root, TUNING_SPLITS, includes_holdout=False, enforce_quotas=False)


def _assemble(
    root: Path, splits: Sequence[str], *, includes_holdout: bool, enforce_quotas: bool
) -> EntityLinkingGoldCorpus:
    problems: list[str] = []
    manifest = load_manifest(root)
    catalog = load_target_catalog(root)

    mentions: list[GoldMention] = []
    for split in splits:
        split_mentions = _load_split_mentions(root, split, catalog, problems)
        if len(split_mentions) != SPLIT_COUNTS[split]:
            problems.append(f"{SPLIT_FILES[split]}: holds {len(split_mentions)} mentions, expected {SPLIT_COUNTS[split]}")
        mentions.extend(split_mentions)

    _check_uniqueness(mentions, problems)
    _check_no_leakage(mentions, problems)
    if includes_holdout and len(mentions) != TOTAL_COUNT:
        problems.append(f"corpus holds {len(mentions)} mentions; expected {TOTAL_COUNT}")

    quotas = _quotas(mentions, catalog)
    if enforce_quotas:
        _check_quotas(quotas, problems)
    if problems:
        raise EntityGoldValidationError(problems)
    return EntityLinkingGoldCorpus(
        manifest=manifest,
        catalog=catalog,
        mentions=tuple(mentions),
        quotas=quotas,
        includes_holdout=includes_holdout,
    )


def _check_uniqueness(mentions: Sequence[GoldMention], problems: list[str]) -> None:
    for field_name, values in (
        ("mention_id", [m.mention_id for m in mentions]),
        ("article_key", [m.article_key for m in mentions]),
    ):
        duplicates = sorted({value for value, count in Counter(values).items() if count > 1})
        if duplicates:
            problems.append(f"corpus: duplicate {field_name}(s) {', '.join(duplicates)}")


def _check_no_leakage(mentions: Sequence[GoldMention], problems: list[str]) -> None:
    """No leakage_group and no identical document may straddle two splits."""
    groups: dict[str, set[str]] = {}
    fingerprints: dict[str, set[str]] = {}
    for mention in mentions:
        groups.setdefault(mention.leakage_group, set()).add(mention.split)
        fingerprints.setdefault(mention.fingerprint, set()).add(mention.split)
    for group, splits in sorted(groups.items()):
        if len(splits) > 1:
            problems.append(f"corpus: leakage_group {group!r} spans splits {sorted(splits)}")
    for fingerprint, splits in fingerprints.items():
        if len(splits) > 1:
            problems.append(f"corpus: an identical document appears in splits {sorted(splits)} (fingerprint {fingerprint[:12]})")


def _quotas(mentions: Sequence[GoldMention], catalog: TargetCatalog) -> GoldQuotas:
    links = [m for m in mentions if m.is_link]
    nils = [m for m in mentions if not m.is_link]
    return GoldQuotas(
        total=len(mentions),
        links=len(links),
        nils=len(nils),
        targets=len(catalog.targets),
        countries=len(catalog.countries),
        by_label=dict(Counter(m.label.value for m in mentions)),
        by_case_tag=dict(Counter(tag for m in mentions for tag in m.case_tags)),
        by_assertion=dict(Counter(m.assertion_status.value for m in mentions)),
        positive_labels=dict(Counter(m.label.value for m in links)),
        nil_labels=dict(Counter(m.label.value for m in nils)),
    )


def _check_quotas(quotas: GoldQuotas, problems: list[str]) -> None:
    if quotas.links == 0 or quotas.nils == 0:
        problems.append(f"corpus needs both link and NIL mentions (links={quotas.links}, nils={quotas.nils})")
    for label in (EntityLabel.ORG.value, EntityLabel.PRODUCT.value):
        if quotas.positive_labels.get(label, 0) == 0:
            problems.append(f"corpus needs at least one positive {label} link")
    for label in (EntityLabel.ORG.value, EntityLabel.PRODUCT.value, EntityLabel.PERSON.value, EntityLabel.GPE.value):
        if quotas.nil_labels.get(label, 0) == 0:
            problems.append(f"corpus needs at least one NIL {label} mention")
    for status in AssertionStatus:
        if quotas.by_assertion.get(status.value, 0) == 0:
            problems.append(f"corpus needs at least one {status.value} mention")
    for tag in CASE_TAGS:
        count = quotas.by_case_tag.get(tag, 0)
        if count < MIN_MENTIONS_PER_CASE_TAG:
            problems.append(f"corpus has {count} mention(s) tagged {tag!r}; at least {MIN_MENTIONS_PER_CASE_TAG} required")
    if quotas.targets < MIN_TARGETS:
        problems.append(f"catalog holds {quotas.targets} targets; at least {MIN_TARGETS} required")
    if quotas.countries < MIN_COUNTRIES:
        problems.append(f"catalog spans {quotas.countries} countries; at least {MIN_COUNTRIES} required")


__all__ = [
    "CASE_TAGS",
    "CATALOG_FILE",
    "DATASET_ID",
    "GOLD_ROOT",
    "LANGUAGE",
    "MANIFEST_FILE",
    "MIN_COUNTRIES",
    "MIN_TARGETS",
    "SCHEMA_VERSION",
    "SPLIT_COUNTS",
    "SPLIT_FILES",
    "SPLIT_FINAL_HOLDOUT",
    "SPLIT_ORDER",
    "TARGET_UUID_NAMESPACE",
    "TOTAL_COUNT",
    "TUNING_SPLITS",
    "AuthoritativeRef",
    "EntityGoldValidationError",
    "EntityLinkingGoldCorpus",
    "GoldAlias",
    "GoldManifest",
    "GoldMention",
    "GoldParent",
    "GoldQuotas",
    "GoldSentence",
    "GoldTarget",
    "LinkingContextSpec",
    "TargetCatalog",
    "document_fingerprint",
    "fixture_target_uuid",
    "load_corpus",
    "load_manifest",
    "load_split",
    "load_target_catalog",
    "load_tuning_corpus",
]
