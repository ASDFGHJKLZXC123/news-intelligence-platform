"""GLEIF LEI enrichment of seeded/watchlisted entities (ADR 0006 item 2A).

GLEIF is precedence 2: it fills identity fields the SEC seed (precedence 1) left empty and
never overwrites SEC-owned ones. The search space is bounded — callers pass a curated
watchlist, the rest of the candidates are discovered from SEC-seeded profiles, and records
whose legal name falls outside that set are retained raw but never persisted as entities.

Alias semantics: GLEIF Level 1 carries the entity's *current* legal name and no validity
interval for it, so the merged alias is stored open (``valid_from``/``valid_to`` NULL, i.e.
currently valid). Dated former names and colloquial aliases are Wikidata's source in
ADR 0006, not GLEIF's.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from db.models import EntityAlias, EntityIdentifier, EntityProfile, EntityRelationship
from packages.providers.base import EntityIdentityProvider, LEIRecord, LEIRelationship
from services.provider_data.common import (
    IngestionResult,
    add,
    add_entity_profile,
    can_claim_identity_field,
    find_all,
    find_one,
    identity_precedence,
    identity_sources,
    json_safe,
    normalize_alias,
    normalize_name,
    parse_optional_date,
    retain_raw_item,
)

GLEIF_PROVIDER_NAME = "gleif"
GLEIF_DATASET = "lei-records"
# Identity fields GLEIF may own, in the order they are written.
GLEIF_CLAIMABLE_FIELDS = ("canonical_name", "country", "primary_lei")
# Level-2 types where the related LEI is the parent of the record's LEI.
DIRECT_PARENT_TYPES = frozenset({"DIRECT_PARENT", "IS_DIRECTLY_CONSOLIDATED_BY"})
ULTIMATE_PARENT_TYPES = frozenset({"ULTIMATE_PARENT", "IS_ULTIMATELY_CONSOLIDATED_BY"})
PARENT_RELATIONSHIP_TYPE = "parent_of"
ACTIVE_CONFIDENCE = 1.0
INACTIVE_CONFIDENCE = 0.75


@dataclass
class _Candidate:
    """One bounded search input: a curated name and/or the profile it already resolves to."""

    key: str
    name: str
    profile: EntityProfile | None
    watchlisted: bool


def ingest_entity_identity_records(
    session: Any,
    provider: EntityIdentityProvider,
    *,
    curated_watchlist: Sequence[str] = (),
    country_code: str | None = None,
    limit: int = 20,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = GLEIF_PROVIDER_NAME,
) -> IngestionResult:
    """Enrich SEC-seeded and watchlisted profiles from GLEIF, then link Level-2 parents."""

    candidates = _candidates(session, curated_watchlist)
    counts: dict[str, int] = dict.fromkeys(
        (
            "aliases_inserted",
            "identifiers_inserted",
            "profiles_enriched",
            "raw_inserted",
            "raw_skipped",
            "records_ambiguous",
            "records_unmatched",
            "relationships_inserted",
            "relationships_skipped",
            "relationships_updated",
        ),
        0,
    )
    fetched = 0
    inserted = 0
    profiles_by_lei: dict[str, EntityProfile] = {}

    for candidate in sorted(candidates.values(), key=lambda item: item.key):
        records = provider.search_records(candidate.name, country_code=country_code, limit=limit)
        fetched += len(records)
        for record in records:
            _retain(
                session,
                counts,
                provider_name=provider_name,
                item_type="lei_record",
                external_id=record.lei,
                payload=record,
                observed_at=record.last_updated_at,
                provider_run_id=provider_run_id,
            )
            if not record.lei.strip():
                # A record with no LEI carries nothing GLEIF is authoritative for, and a blank
                # primary_lei would collide with every other blank one on the unique column.
                counts["records_unmatched"] += 1
                continue
            profile, created = _resolve_profile(
                session, record, candidates, provider_name=provider_name
            )
            if profile is None:
                counts["records_unmatched"] += 1
                continue
            if profile.primary_lei and profile.primary_lei != record.lei:
                # Search is fuzzy and alias keys are lossy, so a second, different LEI can land
                # on an already-bound profile ("Apple Inc." and "Apple Ltd" share the key
                # "apple"). The bound LEI stands; the loser is retained raw and counted.
                counts["records_ambiguous"] += 1
                continue

            inserted += int(created)
            counts["profiles_enriched"] += int(not created)
            _enrich_profile(profile, record, provider_name=provider_name)
            counts["identifiers_inserted"] += int(
                _upsert_lei_identifier(session, profile, record, provider_name=provider_name)
            )
            counts["aliases_inserted"] += int(
                _upsert_legal_name_alias(session, profile, record, provider_name=provider_name)
            )
            profiles_by_lei[record.lei] = profile

    edges = _parent_edges(
        session,
        provider,
        profiles_by_lei,
        counts,
        limit=limit,
        provider_run_id=provider_run_id,
        provider_name=provider_name,
    )
    for (parent_id, child_id), relationships in sorted(edges.items()):
        outcome = _upsert_parent_relationship(
            session,
            parent_id,
            child_id,
            _relationship_values(relationships, provider_name=provider_name),
            provider_name=provider_name,
        )
        counts[f"relationships_{outcome}"] += 1

    return IngestionResult(
        fetched=fetched,
        inserted=inserted,
        skipped=(
            counts["profiles_enriched"] + counts["records_unmatched"] + counts["records_ambiguous"]
        ),
        details={"candidates_searched": len(candidates), **counts},
    )


def _candidates(session: Any, curated_watchlist: Sequence[str]) -> dict[str, _Candidate]:
    """Build the bounded candidate set: curated names plus SEC-seeded profile names."""

    candidates: dict[str, _Candidate] = {}
    for name in curated_watchlist:
        cleaned = name.strip()
        key = normalize_alias(cleaned)
        if key and key not in candidates:
            candidates[key] = _Candidate(key=key, name=cleaned, profile=None, watchlisted=True)

    for profile in find_all(session, EntityProfile):
        if not _is_bounded(profile):
            continue
        key = normalize_alias(profile.canonical_name)
        if not key:
            continue
        existing = candidates.get(key)
        if existing is None:
            candidates[key] = _Candidate(
                key=key, name=profile.canonical_name, profile=profile, watchlisted=False
            )
        elif existing.profile is None:
            existing.profile = profile
    return candidates


def _is_bounded(profile: EntityProfile) -> bool:
    """A profile is in scope once a prior identity ingestion seeded or claimed it."""

    return bool(profile.primary_cik) or bool(identity_sources(profile.profile_metadata))


def _resolve_profile(
    session: Any,
    record: LEIRecord,
    candidates: dict[str, _Candidate],
    *,
    provider_name: str,
) -> tuple[EntityProfile | None, bool]:
    """Map an LEI record onto a bounded profile, creating one only for a curated name."""

    existing = find_one(session, EntityProfile, primary_lei=record.lei)
    if existing is not None:
        return existing, False

    candidate = candidates.get(normalize_alias(record.legal_name))
    if candidate is None:
        return None, False
    if candidate.profile is not None:
        return candidate.profile, False
    if not candidate.watchlisted:
        return None, False

    # Only an explicitly curated name may bring a new entity into the store: GLEIF covers
    # parents the SEC seed never lists. Related LEIs get no profile (see _resolve_related).
    profile = EntityProfile(
        id=uuid.uuid4(),
        canonical_name=record.legal_name,
        normalized_name=normalize_name(record.legal_name),
        entity_type="company",
        profile_metadata={"identity_sources": {"canonical_name": provider_name}},
    )
    add_entity_profile(session, profile)
    candidate.profile = profile
    return profile, True


def _enrich_profile(profile: EntityProfile, record: LEIRecord, *, provider_name: str) -> None:
    """Fill identity fields GLEIF owns or that no source has claimed yet."""

    metadata = dict(json_safe(profile.profile_metadata) or {})
    sources = identity_sources(metadata)
    values = {
        "canonical_name": record.legal_name,
        "country": record.country_code or None,
        "primary_lei": record.lei,
    }
    for field in GLEIF_CLAIMABLE_FIELDS:
        value = values[field]
        owned = sources.get(field) == provider_name
        if value is None or not can_claim_identity_field(metadata, field, source=provider_name):
            continue
        # SEC-owned values are blocked above; an unclaimed non-empty value is left alone.
        if getattr(profile, field) and not owned:
            continue
        setattr(profile, field, value)
        sources[field] = provider_name

    if sources.get("canonical_name") == provider_name:
        profile.normalized_name = normalize_name(profile.canonical_name)
    if not profile.entity_type:
        profile.entity_type = "company"

    metadata["identity_sources"] = sources
    # The legal metadata GLEIF owns outright: status, jurisdiction, and legal form have no
    # first-class column on entity_profiles, so they live under the provider's block.
    metadata[provider_name] = {
        "entity_status": record.entity_status,
        "jurisdiction": record.jurisdiction,
        "legal_form": record.legal_form,
        "lei": record.lei,
        "registration_status": record.registration_status,
        **_source_ref(record, provider_name=provider_name),
    }
    profile.profile_metadata = metadata


def _source_ref(record: LEIRecord, *, provider_name: str) -> dict[str, Any]:
    return {
        "dataset": GLEIF_DATASET,
        "evidence_refs": json_safe(record.evidence_refs),
        "precedence": identity_precedence(provider_name),
        "schema_version": record.schema_version,
        "source_refs": json_safe(record.source_refs),
    }


def _upsert_lei_identifier(
    session: Any, profile: EntityProfile, record: LEIRecord, *, provider_name: str
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
        existing.confidence_score = ACTIVE_CONFIDENCE
        existing.source_ref = _source_ref(record, provider_name=provider_name)
        return False

    add(
        session,
        EntityIdentifier(
            id=uuid.uuid4(),
            entity_profile_id=profile.id,
            identifier_type="lei",
            identifier_value=record.lei,
            provider=provider_name,
            confidence_score=ACTIVE_CONFIDENCE,
            source_ref=_source_ref(record, provider_name=provider_name),
        ),
    )
    return True


def _upsert_legal_name_alias(
    session: Any, profile: EntityProfile, record: LEIRecord, *, provider_name: str
) -> bool:
    """Merge the GLEIF legal name as a GLEIF-sourced alias; aliases never conflict, they union.

    The alias is stored as an open interval: GLEIF reports the legal name that is current now
    and dates no name interval, so ``valid_from``/``valid_to`` stay NULL (see module docstring).
    """

    alias = record.legal_name.strip()
    normalized = normalize_alias(alias)
    if not normalized:
        return False

    # Matches uq_entity_aliases_entity_normalized_alias_source: one row per entity+key+source,
    # so re-running over a changed surface form of the same key inserts nothing new.
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
            alias=alias,
            normalized_alias=normalized,
            alias_type="legal_name",
            source=provider_name,
            valid_from=None,
            valid_to=None,
        ),
    )
    return True


def _parent_edges(
    session: Any,
    provider: EntityIdentityProvider,
    profiles_by_lei: dict[str, EntityProfile],
    counts: dict[str, int],
    *,
    limit: int,
    provider_run_id: uuid.UUID | None,
    provider_name: str,
) -> dict[tuple[uuid.UUID, uuid.UUID], list[LEIRelationship]]:
    """Group Level-2 records by the ``(parent, child)`` pair they resolve to.

    Records are grouped rather than written one by one because GLEIF reports the direct and
    the ultimate parent as separate records and they are frequently the same entity, which
    the unique key (parent, child, type, provider) admits only once.
    """

    edges: dict[tuple[uuid.UUID, uuid.UUID], list[LEIRelationship]] = {}
    for lei in sorted(profiles_by_lei):
        for relationship in provider.fetch_relationships(lei, limit=limit):
            _retain(
                session,
                counts,
                provider_name=provider_name,
                item_type="lei_relationship",
                external_id=relationship.relationship_id,
                payload=relationship,
                observed_at=relationship.start_at,
                provider_run_id=provider_run_id,
            )
            if _parent_level(relationship.relationship_type) is None:
                counts["relationships_skipped"] += 1
                continue

            # Level-2 runs from the consolidated child to the entity that consolidates it.
            parent = _resolve_related(session, profiles_by_lei, relationship.related_lei)
            child = _resolve_related(session, profiles_by_lei, relationship.lei)
            if parent is None or child is None or parent.id == child.id:
                counts["relationships_skipped"] += 1
                continue
            edges.setdefault((parent.id, child.id), []).append(relationship)
    return edges


def _relationship_values(
    relationships: Sequence[LEIRelationship], *, provider_name: str
) -> dict[str, Any]:
    """Merge every Level-2 record for one ``(parent, child)`` pair into a single edge."""

    ordered = sorted(relationships, key=lambda item: item.relationship_id)
    starts = [parse_optional_date(item.start_at) for item in ordered]
    ends = [parse_optional_date(item.end_at) for item in ordered]
    known_starts = [value for value in starts if value is not None]
    known_ends: list[datetime.date] = [value for value in ends if value is not None]
    active = any(item.status.strip().upper() == "ACTIVE" for item in ordered)
    return {
        "confidence_score": ACTIVE_CONFIDENCE if active else INACTIVE_CONFIDENCE,
        # The edge has held since the earliest reported start, and stays open while any of the
        # merged records is open.
        "valid_from": min(known_starts) if known_starts else None,
        "valid_to": max(known_ends) if len(known_ends) == len(ends) and known_ends else None,
        "relationship_metadata": {
            "evidence_refs": sorted({ref for item in ordered for ref in item.evidence_refs}),
            "parent_levels": sorted(
                {_parent_level(item.relationship_type) or "" for item in ordered}
            ),
            "precedence": identity_precedence(provider_name),
            "provider": provider_name,
            "relationship_ids": [item.relationship_id for item in ordered],
            "source_relationship_types": sorted({item.relationship_type for item in ordered}),
        },
    }


def _upsert_parent_relationship(
    session: Any,
    parent_id: uuid.UUID,
    child_id: uuid.UUID,
    values: dict[str, Any],
    *,
    provider_name: str,
) -> str:
    """Persist one ``parent_of`` edge; both endpoints are already resolved profiles."""

    existing = find_one(
        session,
        EntityRelationship,
        parent_entity_id=parent_id,
        child_entity_id=child_id,
        relationship_type=PARENT_RELATIONSHIP_TYPE,
        provider=provider_name,
    )
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return "updated"

    add(
        session,
        EntityRelationship(
            id=uuid.uuid4(),
            parent_entity_id=parent_id,
            child_entity_id=child_id,
            relationship_type=PARENT_RELATIONSHIP_TYPE,
            provider=provider_name,
            **values,
        ),
    )
    return "inserted"


def _parent_level(relationship_type: str) -> str | None:
    kind = relationship_type.strip().upper().replace(" ", "_")
    if kind in DIRECT_PARENT_TYPES:
        return "direct"
    if kind in ULTIMATE_PARENT_TYPES:
        return "ultimate"
    return None


def _resolve_related(
    session: Any, profiles_by_lei: dict[str, EntityProfile], lei: str
) -> EntityProfile | None:
    """Resolve an LEI to a bounded profile; an unknown LEI never mints a placeholder."""

    profile = profiles_by_lei.get(lei)
    if profile is not None:
        return profile
    existing = find_one(session, EntityProfile, primary_lei=lei)
    if existing is None or not _is_bounded(existing):
        return None
    return existing


def _retain(
    session: Any,
    counts: dict[str, int],
    *,
    provider_name: str,
    item_type: str,
    external_id: str,
    payload: Any,
    observed_at: Any,
    provider_run_id: uuid.UUID | None,
) -> None:
    _, created = retain_raw_item(
        session,
        provider=provider_name,
        item_type=item_type,
        external_id=external_id,
        payload=payload,
        observed_at=observed_at,
        provider_run_id=provider_run_id,
        identity_parts=(external_id,),
    )
    counts["raw_inserted"] += int(created)
    counts["raw_skipped"] += int(not created)
