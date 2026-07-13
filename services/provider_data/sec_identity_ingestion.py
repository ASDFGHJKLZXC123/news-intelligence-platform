"""SEC company_tickers identity seeding into canonical entity tables (ADR 0006 item 1)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from db.models import EntityAlias, EntityIdentifier, EntityProfile
from packages.providers.base import SECCompanyTicker, SECCompanyTickerProvider
from services.provider_data.common import (
    IngestionResult,
    add,
    add_entity_profile,
    find_one,
    json_safe,
    normalize_alias,
    normalize_name,
    retain_raw_item,
)

SEC_PROVIDER_NAME = "sec-edgar"
SEC_DATASET = "company_tickers.json"
# ADR 0006 precedence on conflict: SEC (1) > GLEIF (2) > Wikidata (3) for identity fields.
SEC_PRECEDENCE = 1
# Fields SEC owns once seeded; recorded so lower-precedence sources cannot later overwrite them.
SEC_AUTHORITATIVE_FIELDS = ("canonical_name", "primary_cik", "primary_ticker")


def ingest_sec_company_tickers(
    session: Any,
    provider: SECCompanyTickerProvider,
    *,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = SEC_PROVIDER_NAME,
) -> IngestionResult:
    """Seed entity profiles, identifiers, and aliases from the SEC company_tickers seed.

    Deterministic and idempotent: re-running over the same seed inserts nothing new.
    """

    records = provider.fetch_company_tickers()
    inserted = 0
    skipped = 0
    aliases_inserted = 0
    identifiers_inserted = 0
    raw_inserted = 0
    raw_skipped = 0

    for record in records:
        _, raw_created = retain_raw_item(
            session,
            provider=provider_name,
            item_type="company_ticker",
            external_id=record.cik,
            payload=record,
            observed_at=None,
            provider_run_id=provider_run_id,
            identity_parts=(record.cik, record.ticker),
        )
        raw_inserted += int(raw_created)
        raw_skipped += int(not raw_created)

        profile, created = _upsert_profile(session, record, provider_name=provider_name)
        inserted += int(created)
        skipped += int(not created)

        for identifier_type, value in (("cik", record.cik), ("ticker", record.ticker)):
            identifiers_inserted += int(
                _upsert_identifier(
                    session,
                    profile,
                    identifier_type=identifier_type,
                    value=value,
                    record=record,
                    provider_name=provider_name,
                )
            )

        for alias, alias_type in ((record.title, "legal_name"), (record.ticker, "ticker")):
            aliases_inserted += int(
                _upsert_alias(
                    session,
                    profile,
                    alias=alias,
                    alias_type=alias_type,
                    provider_name=provider_name,
                )
            )

    return IngestionResult(
        fetched=len(records),
        inserted=inserted,
        skipped=skipped,
        details={
            "aliases_inserted": aliases_inserted,
            "identifiers_inserted": identifiers_inserted,
            "raw_inserted": raw_inserted,
            "raw_skipped": raw_skipped,
        },
    )


def _upsert_profile(
    session: Any, record: SECCompanyTicker, *, provider_name: str
) -> tuple[EntityProfile, bool]:
    canonical_name = record.title or f"CIK {record.cik}"
    existing = find_one(session, EntityProfile, primary_cik=record.cik)
    if existing is None:
        profile = EntityProfile(
            id=uuid.uuid4(),
            canonical_name=canonical_name,
            normalized_name=normalize_name(canonical_name),
            entity_type="company",
            primary_cik=record.cik,
            primary_ticker=record.ticker,
            profile_metadata=_profile_metadata(None, record, provider_name),
        )
        add_entity_profile(session, profile)
        return profile, True

    # SEC is the top-precedence source, so it refreshes the name it owns. A second ticker for
    # the same CIK (e.g. GOOG after GOOGL) never displaces the primary: seed order decides.
    existing.canonical_name = canonical_name
    existing.normalized_name = normalize_name(canonical_name)
    if not existing.primary_ticker:
        existing.primary_ticker = record.ticker
    if not existing.entity_type:
        existing.entity_type = "company"
    existing.profile_metadata = _profile_metadata(existing.profile_metadata, record, provider_name)
    return existing, False


def _profile_metadata(
    existing: Any, record: SECCompanyTicker, provider_name: str
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if isinstance(existing, Mapping):
        metadata = dict(json_safe(existing))

    identity_sources = dict(metadata.get("identity_sources") or {})
    identity_sources.update(dict.fromkeys(SEC_AUTHORITATIVE_FIELDS, provider_name))
    known_tickers = set(metadata.get("sec_tickers") or ()) | {record.ticker}

    metadata.update(
        {
            "identity_sources": identity_sources,
            "sec_tickers": sorted(known_tickers),
            "source": {
                "dataset": SEC_DATASET,
                "evidence_refs": json_safe(record.evidence_refs),
                "precedence": SEC_PRECEDENCE,
                "provider": provider_name,
                "schema_version": record.schema_version,
                "source_refs": json_safe(record.source_refs),
            },
        }
    )
    return metadata


def _upsert_identifier(
    session: Any,
    profile: EntityProfile,
    *,
    identifier_type: str,
    value: str,
    record: SECCompanyTicker,
    provider_name: str,
) -> bool:
    if not value:
        return False
    source_ref = {
        "dataset": SEC_DATASET,
        "evidence_refs": json_safe(record.evidence_refs),
        "precedence": SEC_PRECEDENCE,
        "source_refs": json_safe(record.source_refs),
    }
    existing = find_one(
        session,
        EntityIdentifier,
        entity_profile_id=profile.id,
        identifier_type=identifier_type,
        identifier_value=value,
        provider=provider_name,
    )
    if existing is not None:
        existing.confidence_score = 1.0
        existing.source_ref = source_ref
        return False

    add(
        session,
        EntityIdentifier(
            id=uuid.uuid4(),
            entity_profile_id=profile.id,
            identifier_type=identifier_type,
            identifier_value=value,
            provider=provider_name,
            confidence_score=1.0,
            source_ref=source_ref,
        ),
    )
    return True


def _upsert_alias(
    session: Any,
    profile: EntityProfile,
    *,
    alias: str,
    alias_type: str,
    provider_name: str,
) -> bool:
    raw_alias = alias.strip()
    normalized = normalize_alias(raw_alias)
    if not raw_alias or not normalized:
        return False

    # Matches uq_entity_aliases_entity_normalized_alias_source: two raw aliases that share a
    # normalized key for one entity+source are the same alias, so the first one stored wins.
    existing = find_one(
        session,
        EntityAlias,
        entity_id=profile.id,
        normalized_alias=normalized,
        source=provider_name,
    )
    if existing is not None:
        return False

    add(
        session,
        EntityAlias(
            id=uuid.uuid4(),
            entity_id=profile.id,
            alias=raw_alias,
            normalized_alias=normalized,
            alias_type=alias_type,
            source=provider_name,
        ),
    )
    return True
