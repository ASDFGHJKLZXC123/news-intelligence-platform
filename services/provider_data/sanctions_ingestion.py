"""Sanctions provider ingestion into sanctions storage tables."""

from __future__ import annotations

import uuid
from typing import Any

from db.models import (
    SanctionsAlias as SanctionsAliasModel,
)
from db.models import (
    SanctionsEntity as SanctionsEntityModel,
)
from db.models import (
    SanctionsIdentifier as SanctionsIdentifierModel,
)
from db.models import SanctionsList
from packages.providers.base import SanctionsEntity, SanctionsProvider
from services.provider_data.common import (
    IngestionResult,
    add,
    find_one,
    json_safe,
    normalize_name,
    retain_raw_item,
)


def ingest_sanctions_entities(
    session: Any,
    provider: SanctionsProvider,
    *,
    program: str | None = None,
    limit: int | None = None,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = "ofac",
) -> IngestionResult:
    """Fetch sanctioned entities and persist entity, alias, and identifier rows."""

    entities = provider.fetch_entities(program=program, limit=limit)
    inserted = 0
    skipped = 0
    raw_inserted = 0
    raw_skipped = 0
    aliases_inserted = 0
    identifiers_inserted = 0

    for entity in entities:
        _, raw_created = retain_raw_item(
            session,
            provider=provider_name,
            item_type="sanctions_entity",
            external_id=entity.entity_id,
            payload=entity,
            observed_at=entity.updated_at or entity.listed_at,
            provider_run_id=provider_run_id,
            identity_parts=(entity.entity_id, entity.sanctions_lists),
        )
        raw_inserted += int(raw_created)
        raw_skipped += int(not raw_created)

        persisted, created = _upsert_entity(session, entity, provider_name=provider_name)
        inserted += int(created)
        skipped += int(not created)
        for alias in entity.aliases:
            aliases_inserted += int(_upsert_alias(session, persisted, alias))
        for identifier in entity.identifiers:
            identifiers_inserted += int(_upsert_identifier(session, persisted, identifier))

    return IngestionResult(
        fetched=len(entities),
        inserted=inserted,
        skipped=skipped,
        details={
            "aliases_inserted": aliases_inserted,
            "identifiers_inserted": identifiers_inserted,
            "raw_inserted": raw_inserted,
            "raw_skipped": raw_skipped,
        },
    )


def _upsert_entity(
    session: Any,
    entity: SanctionsEntity,
    *,
    provider_name: str,
) -> tuple[SanctionsEntityModel, bool]:
    list_code = entity.sanctions_lists[0] if entity.sanctions_lists else "SDN"
    sanctions_list = _ensure_list(session, provider_name=provider_name, list_code=list_code)
    existing = find_one(
        session,
        SanctionsEntityModel,
        provider=provider_name,
        list_code=list_code,
        entity_uid=entity.entity_id,
    )
    values = {
        "sanctions_list_id": sanctions_list.id,
        "entity_type": entity.entity_type or None,
        "primary_name": entity.name,
        "normalized_name": normalize_name(entity.name),
        "country": entity.countries[0] if entity.countries else None,
        "programs": json_safe(entity.programs),
        "remarks": _remarks(entity),
        "source_payload": json_safe(entity),
        "first_seen_at": entity.listed_at,
        "last_seen_at": entity.updated_at or entity.listed_at,
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return existing, False

    row = SanctionsEntityModel(
        id=uuid.uuid4(),
        provider=provider_name,
        list_code=list_code,
        entity_uid=entity.entity_id,
        **values,
    )
    add(session, row)
    return row, True


def _ensure_list(session: Any, *, provider_name: str, list_code: str) -> SanctionsList:
    existing = find_one(session, SanctionsList, provider=provider_name, list_code=list_code)
    if existing is not None:
        return existing
    row = SanctionsList(
        id=uuid.uuid4(),
        provider=provider_name,
        list_code=list_code,
        list_name=f"{provider_name.upper()} {list_code}",
    )
    add(session, row)
    return row


def _upsert_alias(
    session: Any, entity: SanctionsEntityModel, alias: Any
) -> bool:
    normalized = normalize_name(alias.name)
    existing = find_one(
        session,
        SanctionsAliasModel,
        sanctions_entity_id=entity.id,
        normalized_alias=normalized,
        alias_type=alias.alias_type or None,
    )
    if existing is not None:
        existing.alias_name = alias.name
        existing.quality = alias.quality or None
        return False
    add(
        session,
        SanctionsAliasModel(
            id=uuid.uuid4(),
            sanctions_entity_id=entity.id,
            alias_name=alias.name,
            normalized_alias=normalized,
            alias_type=alias.alias_type or None,
            quality=alias.quality or None,
        ),
    )
    return True


def _upsert_identifier(
    session: Any, entity: SanctionsEntityModel, identifier: Any
) -> bool:
    existing = find_one(
        session,
        SanctionsIdentifierModel,
        sanctions_entity_id=entity.id,
        identifier_type=identifier.identifier_type,
        identifier_value=identifier.value,
    )
    if existing is not None:
        existing.country = identifier.country or None
        return False
    add(
        session,
        SanctionsIdentifierModel(
            id=uuid.uuid4(),
            sanctions_entity_id=entity.id,
            identifier_type=identifier.identifier_type,
            identifier_value=identifier.value,
            country=identifier.country or None,
        ),
    )
    return True


def _remarks(entity: SanctionsEntity) -> str | None:
    remarks = entity.metadata.get("remarks") if hasattr(entity.metadata, "get") else None
    return str(remarks) if remarks else None
