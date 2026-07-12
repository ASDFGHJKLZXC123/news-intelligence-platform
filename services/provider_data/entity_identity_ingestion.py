"""GLEIF/entity identity ingestion into canonical entity storage."""

from __future__ import annotations

import uuid
from typing import Any

from db.models import EntityIdentifier, EntityProfile, EntityRelationship
from packages.providers.base import EntityIdentityProvider, LEIRecord
from services.provider_data.common import (
    IngestionResult,
    add,
    find_one,
    json_safe,
    normalize_name,
    parse_optional_date,
    retain_raw_item,
)


def ingest_entity_identity_records(
    session: Any,
    provider: EntityIdentityProvider,
    *,
    queries: list[str],
    country_code: str | None = None,
    limit: int = 20,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = "gleif",
) -> IngestionResult:
    """Search LEI records, upsert entity profiles, identifiers, and relationships."""

    fetched = 0
    inserted = 0
    skipped = 0
    identifiers_inserted = 0
    relationships_inserted = 0
    raw_inserted = 0
    raw_skipped = 0

    for query in queries:
        if not query.strip():
            continue
        records = provider.search_records(query, country_code=country_code, limit=limit)
        fetched += len(records)
        for record in records:
            _, raw_created = retain_raw_item(
                session,
                provider=provider_name,
                item_type="lei_record",
                external_id=record.lei,
                payload=record,
                observed_at=record.last_updated_at,
                provider_run_id=provider_run_id,
                identity_parts=(record.lei,),
            )
            raw_inserted += int(raw_created)
            raw_skipped += int(not raw_created)

            profile, created = _upsert_profile(session, record, provider_name=provider_name)
            inserted += int(created)
            skipped += int(not created)
            identifiers_inserted += int(_upsert_lei_identifier(session, profile, record, provider_name))

            for relationship in provider.fetch_relationships(record.lei, limit=limit):
                _, rel_raw_created = retain_raw_item(
                    session,
                    provider=provider_name,
                    item_type="lei_relationship",
                    external_id=relationship.relationship_id,
                    payload=relationship,
                    observed_at=relationship.start_at,
                    provider_run_id=provider_run_id,
                    identity_parts=(relationship.relationship_id,),
                )
                raw_inserted += int(rel_raw_created)
                raw_skipped += int(not rel_raw_created)
                relationships_inserted += int(
                    _upsert_relationship(session, relationship, provider_name=provider_name)
                )

    return IngestionResult(
        fetched=fetched,
        inserted=inserted,
        skipped=skipped,
        details={
            "identifiers_inserted": identifiers_inserted,
            "relationships_inserted": relationships_inserted,
            "raw_inserted": raw_inserted,
            "raw_skipped": raw_skipped,
        },
    )


def _upsert_profile(
    session: Any, record: LEIRecord, *, provider_name: str
) -> tuple[EntityProfile, bool]:
    existing = find_one(session, EntityProfile, primary_lei=record.lei)
    values = {
        "canonical_name": record.legal_name,
        "normalized_name": normalize_name(record.legal_name),
        "entity_type": record.entity_status or None,
        "country": record.country_code or None,
        "primary_lei": record.lei,
        "profile_metadata": {
            **json_safe(record.metadata),
            "jurisdiction": record.jurisdiction,
            "legal_form": record.legal_form,
            "provider": provider_name,
            "registration_status": record.registration_status,
        },
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return existing, False

    profile = EntityProfile(id=uuid.uuid4(), **values)
    add(session, profile)
    return profile, True


def _upsert_lei_identifier(
    session: Any, profile: EntityProfile, record: LEIRecord, provider_name: str
) -> bool:
    existing = find_one(
        session,
        EntityIdentifier,
        entity_profile_id=profile.id,
        identifier_type="lei",
        identifier_value=record.lei,
        provider=provider_name,
    )
    if existing is not None:
        existing.confidence_score = 1.0
        existing.source_ref = json_safe(record.evidence_refs)
        return False
    add(
        session,
        EntityIdentifier(
            id=uuid.uuid4(),
            entity_profile_id=profile.id,
            identifier_type="lei",
            identifier_value=record.lei,
            provider=provider_name,
            confidence_score=1.0,
            source_ref=json_safe(record.evidence_refs),
        ),
    )
    return True


def _upsert_relationship(session: Any, relationship: Any, *, provider_name: str) -> bool:
    parent = _ensure_placeholder_profile(session, relationship.related_lei, provider_name)
    child = _ensure_placeholder_profile(session, relationship.lei, provider_name)
    existing = find_one(
        session,
        EntityRelationship,
        parent_entity_id=parent.id,
        child_entity_id=child.id,
        relationship_type=relationship.relationship_type,
        provider=provider_name,
    )
    values = {
        "confidence_score": 1.0 if relationship.status.upper() == "ACTIVE" else 0.75,
        "valid_from": parse_optional_date(relationship.start_at),
        "valid_to": parse_optional_date(relationship.end_at),
        "relationship_metadata": json_safe(relationship),
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return False
    add(
        session,
        EntityRelationship(
            id=uuid.uuid4(),
            parent_entity_id=parent.id,
            child_entity_id=child.id,
            relationship_type=relationship.relationship_type,
            provider=provider_name,
            **values,
        ),
    )
    return True


def _ensure_placeholder_profile(
    session: Any, lei: str, provider_name: str
) -> EntityProfile:
    existing = find_one(session, EntityProfile, primary_lei=lei)
    if existing is not None:
        return existing
    profile = EntityProfile(
        id=uuid.uuid4(),
        canonical_name=f"LEI {lei}",
        normalized_name=normalize_name(f"LEI {lei}"),
        primary_lei=lei,
        profile_metadata={"placeholder": True, "provider": provider_name},
    )
    add(session, profile)
    return profile
