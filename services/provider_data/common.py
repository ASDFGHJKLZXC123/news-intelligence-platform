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


def find_one(session: Any, model: type[Any], **criteria: Any) -> Any | None:
    """Find one row by simple equality criteria using SQLAlchemy or a lightweight fake."""

    custom_find = getattr(session, "find_one", None)
    if callable(custom_find):
        return custom_find(model, **criteria)

    stmt = select(model)
    for name, expected in criteria.items():
        column = getattr(model, name)
        condition = column.is_(None) if expected is None else column == expected
        stmt = stmt.where(condition)
    return session.execute(stmt).scalars().first()


def add(session: Any, obj: Any) -> Any:
    session.add(obj)
    return obj


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
