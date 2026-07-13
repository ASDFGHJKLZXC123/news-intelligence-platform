"""Shared helpers for deterministic provider-data ingestion services."""

from __future__ import annotations

import datetime
import decimal
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

from sqlalchemy import select

from db.models import RawIngestionItem


@dataclass(frozen=True)
class IngestionResult:
    """Small result object returned by provider ingestion services."""

    fetched: int
    inserted: int
    skipped: int
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "details": json_safe(self.details),
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def json_safe(value: Any) -> Any:
    """Return a deterministic JSON-compatible representation of provider values."""

    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: json_safe(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {
            str(key): json_safe(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, tuple | list):
        return [json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        items = [json_safe(item) for item in value]
        return sorted(items, key=canonical_json)
    if isinstance(value, bytes | bytearray):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def idempotency_key(provider: str, item_type: str, *parts: Any) -> str:
    """Build a stable raw-item key that fits the DB column and avoids live DB upserts."""

    digest = stable_hash({"item_type": item_type, "parts": parts, "provider": provider})
    prefix = f"{provider}:{item_type}:"
    if len(prefix) + len(digest) <= 192:
        return f"{prefix}{digest}"
    return f"{prefix[:127]}:{digest}"


def normalize_cik(cik: str) -> str:
    digits = "".join(character for character in str(cik) if character.isdigit())
    if not digits:
        msg = "CIK must contain digits"
        raise ValueError(msg)
    return digits.zfill(10)


def normalize_name(value: str) -> str:
    """Normalize an entity/name key for deterministic matching and idempotency."""
    return " ".join(value.casefold().strip().split())


# ADR 0006: the legal suffixes stripped from alias keys, casefolded.
LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "ag",
        "co",
        "corp",
        "corporation",
        "group",
        "holdings",
        "inc",
        "limited",
        "llc",
        "lp",
        "ltd",
        "nv",
        "plc",
        "sa",
    }
)

# Dropped outright so "U.S." -> "us" and "Macy's" -> "macys"; other punctuation becomes
# a separator so "Coca-Cola" -> "coca cola".
_ALIAS_DROPPED_PUNCTUATION = str.maketrans(dict.fromkeys(".'’`´"))


def normalize_alias(value: str) -> str:
    """Canonical alias key (ADR 0006).

    casefold -> strip punctuation -> strip repeated trailing legal suffixes -> collapse
    whitespace. The last token is never stripped, so an alias that is nothing but a legal
    suffix ("Group", or the ticker "CO") still normalizes to a non-empty key.
    """

    folded = value.casefold().translate(_ALIAS_DROPPED_PUNCTUATION)
    separated = "".join(
        character if character.isalnum() or character.isspace() else " " for character in folded
    )
    tokens = separated.split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


# ADR 0006 precedence on conflict for identity fields: SEC > GLEIF > Wikidata.
IDENTITY_SOURCE_PRECEDENCE: Mapping[str, int] = {
    "sec-edgar": 1,
    "gleif": 2,
    "wikidata": 3,
}
# An unranked source loses every conflict against a ranked one.
UNRANKED_IDENTITY_PRECEDENCE = 99


def identity_precedence(source: str | None) -> int:
    """Rank an identity source; lower wins."""

    if not source:
        return UNRANKED_IDENTITY_PRECEDENCE
    return IDENTITY_SOURCE_PRECEDENCE.get(source, UNRANKED_IDENTITY_PRECEDENCE)


def identity_sources(metadata: Any) -> dict[str, str]:
    """Read the per-field ownership map that ingestion records on ``profile_metadata``."""

    if not isinstance(metadata, Mapping):
        return {}
    sources = metadata.get("identity_sources")
    if not isinstance(sources, Mapping):
        return {}
    return {str(key): str(value) for key, value in sources.items()}


def can_claim_identity_field(metadata: Any, field: str, *, source: str) -> bool:
    """True when ``source`` outranks (or is) the source that already owns ``field``."""

    owner = identity_sources(metadata).get(field)
    if owner is None:
        return True
    return identity_precedence(source) <= identity_precedence(owner)


def date_from_datetime(value: datetime.datetime | None) -> datetime.date | None:
    return value.date() if value is not None else None


def parse_optional_date(value: Any) -> datetime.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def first_sequence_value(value: Any) -> Any:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value[0] if value else None
    return value


def to_float_or_none(value: Any) -> float | None:
    if value in (None, "", "."):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def flush_pending(session: Any) -> None:
    """Make rows this run already added visible to the next read.

    Every upsert in these services guards on a ``find_one``/``find_all`` lookup, which is
    only a real guard if it can see what the same run just added. A dict-backed fake session
    gives that for free; a real Session is built with ``autoflush=False``, so without this a
    second occurrence of the same row inside one run reads back empty and is inserted again —
    a duplicate that only surfaces as a unique-constraint violation at commit. The SEC seed
    hits this on its first run: a company with two share classes (Alphabet's GOOGL and GOOG)
    is two rows carrying one CIK and one legal name.
    """

    flush = getattr(session, "flush", None)
    if callable(flush):
        flush()


def _equality_query(model: type[Any], criteria: Mapping[str, Any]) -> Any:
    stmt = select(model)
    for name, expected in criteria.items():
        column = getattr(model, name)
        condition = column.is_(None) if expected is None else column == expected
        stmt = stmt.where(condition)
    return stmt


def find_one(session: Any, model: type[Any], **criteria: Any) -> Any | None:
    """Find one row by simple equality criteria using SQLAlchemy or a lightweight fake."""

    custom_find = getattr(session, "find_one", None)
    if callable(custom_find):
        return custom_find(model, **criteria)

    flush_pending(session)
    return session.execute(_equality_query(model, criteria)).scalars().first()


def find_all(session: Any, model: type[Any], **criteria: Any) -> list[Any]:
    """List rows by simple equality criteria using SQLAlchemy or a lightweight fake."""

    custom_find = getattr(session, "find_all", None)
    if callable(custom_find):
        return list(custom_find(model, **criteria))

    flush_pending(session)
    return list(session.execute(_equality_query(model, criteria)).scalars().all())


def add(session: Any, obj: Any) -> Any:
    session.add(obj)
    return obj


def add_entity_profile(session: Any, profile: Any) -> Any:
    """Add a new entity profile and flush it, so rows pointing at it have a live FK target.

    The identity models declare no ORM ``relationship()``, so SQLAlchemy's unit of work has no
    inter-mapper dependency to sort inserts by and falls back to mapper name: ``EntityAlias``
    and ``EntityIdentifier`` both sort before ``EntityProfile``. Adding a profile and its
    aliases in one flush therefore emits the child inserts first, and they fail the profile's
    foreign key. Flushing the profile on creation is what makes the parent exist first.
    """

    session.add(profile)
    flush_pending(session)
    return profile


def retain_raw_item(
    session: Any,
    *,
    provider: str,
    item_type: str,
    external_id: str | None,
    payload: Any,
    observed_at: datetime.datetime | None,
    provider_run_id: uuid.UUID | None = None,
    identity_parts: Sequence[Any] | None = None,
) -> tuple[RawIngestionItem, bool]:
    """Retain a raw payload once by deterministic idempotency key."""

    payload_value = json_safe(payload)
    key_parts = tuple(identity_parts or (external_id or stable_hash(payload_value),))
    raw_key = idempotency_key(provider, item_type, *key_parts)
    existing = find_one(session, RawIngestionItem, idempotency_key=raw_key)
    if existing is not None:
        return existing, False

    item = RawIngestionItem(
        id=uuid.uuid4(),
        provider_run_id=provider_run_id,
        provider=provider,
        item_type=item_type,
        external_id=external_id,
        idempotency_key=raw_key,
        payload_hash=stable_hash(payload_value),
        payload=payload_value,
        observed_at=observed_at,
    )
    add(session, item)
    return item, True
