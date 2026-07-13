"""Wikidata identity enrichment of seeded/watchlisted entities (ADR 0006 item 2B).

Wikidata is precedence 3, the lowest of the three ADR 0006 sources: it fills identity fields
SEC (1) and GLEIF (2) left empty and never overwrites theirs. The query space is bounded by
construction — the provider only ever sees QIDs that a curated watchlist named explicitly or
that a CIK/LEI already seeded on an EntityProfile resolved to, so nothing here crawls Wikidata.

Alias semantics: an official name Wikidata dates with an end time is a ``former_name`` carrying
that interval; every other name is stored open (``valid_from``/``valid_to`` NULL, i.e. currently
valid). A brand is a ``brand_product`` alias of the entity that trades under it, never an
assertion that the two are the same legal entity — so a brand that is itself a tracked entity is
left as its own entity and no alias is written (ADR 0006: a brand is never aliased to its owner).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from db.models import EntityAlias, EntityIdentifier, EntityProfile, EntityRelationship
from packages.providers.base import (
    WikidataAlias,
    WikidataEntity,
    WikidataProvider,
    normalize_wikidata_qid,
)
from packages.providers.wikidata import PROPERTY_PARENT, PROPERTY_SUBSIDIARY
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
    retain_raw_item,
)

WIKIDATA_PROVIDER_NAME = "wikidata"
WIKIDATA_DATASET = "sparql"
QID_IDENTIFIER_TYPE = "wikidata_qid"
# Identity fields Wikidata may own, in the order they are written.
WIKIDATA_CLAIMABLE_FIELDS = ("canonical_name", "primary_cik", "primary_lei", "primary_ticker")
# Columns with a unique constraint: a value already bound to another profile is never stolen.
UNIQUE_IDENTITY_FIELDS = frozenset({"primary_cik", "primary_lei"})
PARENT_RELATIONSHIP_TYPE = "parent_of"
# The schema admits one alias row per (entity, normalized key, source), and Wikidata routinely
# reports the same key under several types (an altLabel that repeats a former name). The most
# authoritative type for a key wins: the current legal name first, then a dated former name -
# whose interval would otherwise be lost - and only then the undated variants.
ALIAS_TYPE_PRIORITY = ("legal_name", "former_name", "short_name", "ticker", "brand_product")
# ADR 0006: Wikidata quality varies, so its rows enter at a lower prior confidence than the
# registry sources and never auto-accept without a second signal.
WIKIDATA_CONFIDENCE = 0.75
# How many identifier values or QIDs one bounded provider query may carry.
DEFAULT_BATCH_SIZE = 100


def ingest_wikidata_identities(
    session: Any,
    provider: WikidataProvider,
    *,
    curated_qids: Sequence[str] = (),
    batch_size: int = DEFAULT_BATCH_SIZE,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = WIKIDATA_PROVIDER_NAME,
) -> IngestionResult:
    """Enrich SEC/GLEIF-seeded and watchlisted profiles from Wikidata, then link P749/P355."""

    curated = {normalize_wikidata_qid(qid) for qid in curated_qids if str(qid).strip()}
    counts: dict[str, int] = dict.fromkeys(
        (
            "aliases_inserted",
            "brands_skipped",
            "entities_ambiguous",
            "entities_unmatched",
            "identifiers_inserted",
            "profiles_enriched",
            "raw_inserted",
            "raw_skipped",
            "relationships_inserted",
            "relationships_skipped",
            "relationships_updated",
        ),
        0,
    )

    profiles = find_all(session, EntityProfile)
    qid_owner = _known_qids(session, profiles)
    matches = _resolve_qids(
        session,
        provider,
        counts,
        profiles=profiles,
        batch_size=batch_size,
        provider_run_id=provider_run_id,
        provider_name=provider_name,
    )
    for qid, (identifier_type, value) in sorted(matches.items()):
        if qid in qid_owner:
            continue
        profile = find_one(session, EntityProfile, **{f"primary_{identifier_type}": value})
        if profile is not None:
            qid_owner[qid] = profile

    wanted = sorted(set(qid_owner) | curated)
    fetched = 0
    inserted = 0
    claimed: set[uuid.UUID] = set()
    processed: list[tuple[WikidataEntity, EntityProfile]] = []

    for chunk in _chunks(wanted, batch_size):
        for entity in provider.fetch_entities(chunk):
            fetched += 1
            _retain(
                session,
                counts,
                provider_name=provider_name,
                item_type="entity",
                external_id=entity.qid,
                payload=entity,
                provider_run_id=provider_run_id,
                identity_parts=(entity.qid,),
            )
            profile, created = _resolve_profile(
                session, entity, qid_owner, curated, provider_name=provider_name
            )
            if profile is None:
                counts["entities_unmatched"] += 1
                continue
            if profile.id in claimed:
                # Two QIDs landing on one profile means Wikidata holds a duplicate item; the
                # first one (sorted order, so deterministic) owns the profile.
                counts["entities_ambiguous"] += 1
                continue
            claimed.add(profile.id)
            inserted += int(created)
            counts["profiles_enriched"] += int(not created)
            _enrich_profile(session, profile, entity, provider_name=provider_name)
            counts["identifiers_inserted"] += _upsert_identifiers(
                session, profile, entity, provider_name=provider_name
            )
            processed.append((entity, profile))

    # Aliases and edges run once every entity is bound to a profile, so a brand or a related QID
    # that a later entity introduces is still visible to an earlier one.
    edges: dict[tuple[uuid.UUID, uuid.UUID], set[str]] = {}
    for entity, profile in processed:
        counts["aliases_inserted"] += _upsert_aliases(
            session, profile, entity, qid_owner, counts, provider_name=provider_name
        )
        _collect_edges(edges, counts, entity, profile, qid_owner)

    for (parent_id, child_id), properties in sorted(edges.items()):
        outcome = _upsert_parent_relationship(
            session, parent_id, child_id, sorted(properties), provider_name=provider_name
        )
        counts[f"relationships_{outcome}"] += 1

    return IngestionResult(
        fetched=fetched,
        inserted=inserted,
        skipped=(
            counts["profiles_enriched"]
            + counts["entities_unmatched"]
            + counts["entities_ambiguous"]
        ),
        details={"qids_requested": len(wanted), **counts},
    )


def _known_qids(session: Any, profiles: Sequence[EntityProfile]) -> dict[str, EntityProfile]:
    """Profiles a previous run already bound to a QID, keyed by that QID."""

    profiles_by_id = {profile.id: profile for profile in profiles}
    known: dict[str, EntityProfile] = {}
    for identifier in find_all(session, EntityIdentifier, identifier_type=QID_IDENTIFIER_TYPE):
        profile = profiles_by_id.get(identifier.entity_profile_id)
        if profile is not None:
            known.setdefault(identifier.identifier_value, profile)
    return known


def _resolve_qids(
    session: Any,
    provider: WikidataProvider,
    counts: dict[str, int],
    *,
    profiles: Sequence[EntityProfile],
    batch_size: int,
    provider_run_id: uuid.UUID | None,
    provider_name: str,
) -> dict[str, tuple[str, str]]:
    """Map seeded identifier values onto QIDs: ``qid -> (identifier_type, value)``.

    Only CIK and LEI are used as resolution keys. Both are globally unique registry ids, whereas
    a ticker is exchange-scoped and reused across venues, so resolving on one would bind the
    wrong item. Tickers Wikidata reports are still ingested as identifiers and aliases.
    """

    bounded = [profile for profile in profiles if _is_bounded(profile)]
    ciks = sorted({profile.primary_cik for profile in bounded if profile.primary_cik})
    leis = sorted({profile.primary_lei for profile in bounded if profile.primary_lei})
    batches: list[dict[str, list[str]]] = [{"ciks": chunk} for chunk in _chunks(ciks, batch_size)]
    batches += [{"leis": chunk} for chunk in _chunks(leis, batch_size)]

    matches: dict[str, tuple[str, str]] = {}
    for kwargs in batches:
        for match in provider.resolve_qids(**kwargs):
            _retain(
                session,
                counts,
                provider_name=provider_name,
                item_type="qid_match",
                external_id=match.qid,
                payload=match,
                provider_run_id=provider_run_id,
                identity_parts=(match.qid, match.identifier_type, match.identifier_value),
            )
            if match.identifier_type in ("cik", "lei"):
                matches.setdefault(match.qid, (match.identifier_type, match.identifier_value))
    return matches


def _is_bounded(profile: EntityProfile) -> bool:
    """A profile is in scope once a prior identity ingestion seeded or claimed it."""

    return bool(profile.primary_cik) or bool(identity_sources(profile.profile_metadata))


def _resolve_profile(
    session: Any,
    entity: WikidataEntity,
    qid_owner: dict[str, EntityProfile],
    curated: set[str],
    *,
    provider_name: str,
) -> tuple[EntityProfile | None, bool]:
    """Map an item onto a bounded profile, creating one only for an explicitly curated QID."""

    profile = qid_owner.get(entity.qid)
    if profile is not None:
        return profile, False
    if entity.qid not in curated or not entity.label:
        return None, False

    # Only a curated QID may bring a new entity into the store: ADR 0006 leans on Wikidata for the
    # non-US and private companies the registry sources never list. Related QIDs never do this
    # (see _collect_edges), so no placeholder entity is ever minted from a parent/brand reference.
    profile = EntityProfile(
        id=uuid.uuid4(),
        canonical_name=entity.label,
        normalized_name=normalize_name(entity.label),
        entity_type="company",
        profile_metadata={"identity_sources": {"canonical_name": provider_name}},
    )
    add_entity_profile(session, profile)
    qid_owner[entity.qid] = profile
    return profile, True


def _enrich_profile(
    session: Any, profile: EntityProfile, entity: WikidataEntity, *, provider_name: str
) -> None:
    """Fill identity fields Wikidata owns or that no higher-precedence source has claimed."""

    metadata = dict(json_safe(profile.profile_metadata) or {})
    sources = identity_sources(metadata)
    # A field Wikidata reports more than one value for is ambiguous, so it claims nothing; the
    # values still land as identifiers below.
    values = {
        "canonical_name": entity.label or None,
        "primary_cik": _single(entity.ciks),
        "primary_lei": _single(entity.leis),
        "primary_ticker": _single(entity.tickers),
    }
    for field in WIKIDATA_CLAIMABLE_FIELDS:
        value = values[field]
        owned = sources.get(field) == provider_name
        if value is None or not can_claim_identity_field(metadata, field, source=provider_name):
            continue
        # SEC/GLEIF-owned values are blocked above; an unclaimed non-empty value is left alone.
        if getattr(profile, field) and not owned:
            continue
        if field in UNIQUE_IDENTITY_FIELDS and _bound_elsewhere(session, field, value, profile):
            continue
        setattr(profile, field, value)
        sources[field] = provider_name

    if sources.get("canonical_name") == provider_name:
        profile.normalized_name = normalize_name(profile.canonical_name)
    if not profile.entity_type:
        profile.entity_type = "company"

    metadata["identity_sources"] = sources
    # Industry (P452) has no first-class column, and ADR 0006 reserves sector/industry for NAICS/
    # SIC plus the custom taxonomy, so it is retained as provider metadata rather than owned.
    metadata[provider_name] = {
        "description": entity.description,
        "industries": [{"label": ref.label, "qid": ref.qid} for ref in entity.industries],
        "qid": entity.qid,
        **_source_ref(entity, provider_name=provider_name),
    }
    profile.profile_metadata = metadata


def _bound_elsewhere(session: Any, field: str, value: str, profile: EntityProfile) -> bool:
    """True when a unique identity column already holds this value for a different profile."""

    existing = find_one(session, EntityProfile, **{field: value})
    return existing is not None and existing.id != profile.id


def _single(values: Sequence[str]) -> str | None:
    return values[0] if len(values) == 1 else None


def _source_ref(entity: WikidataEntity, *, provider_name: str) -> dict[str, Any]:
    return {
        "dataset": WIKIDATA_DATASET,
        "evidence_refs": json_safe(entity.evidence_refs),
        "precedence": identity_precedence(provider_name),
        "schema_version": entity.schema_version,
        "source_refs": json_safe(entity.source_refs),
    }


def _upsert_identifiers(
    session: Any, profile: EntityProfile, entity: WikidataEntity, *, provider_name: str
) -> int:
    rows: list[tuple[str, str]] = [(QID_IDENTIFIER_TYPE, entity.qid)]
    rows += [("cik", value) for value in entity.ciks]
    rows += [("lei", value) for value in entity.leis]
    rows += [("ticker", value) for value in entity.tickers]

    source_ref = _source_ref(entity, provider_name=provider_name)
    inserted = 0
    for identifier_type, value in rows:
        existing = find_one(
            session,
            EntityIdentifier,
            entity_profile_id=profile.id,
            identifier_type=identifier_type,
            identifier_value=value,
            provider=provider_name,
        )
        if existing is not None:
            existing.confidence_score = WIKIDATA_CONFIDENCE
            existing.source_ref = source_ref
            continue
        add(
            session,
            EntityIdentifier(
                id=uuid.uuid4(),
                entity_profile_id=profile.id,
                identifier_type=identifier_type,
                identifier_value=value,
                provider=provider_name,
                confidence_score=WIKIDATA_CONFIDENCE,
                source_ref=source_ref,
            ),
        )
        inserted += 1
    return inserted


def _upsert_aliases(
    session: Any,
    profile: EntityProfile,
    entity: WikidataEntity,
    qid_owner: dict[str, EntityProfile],
    counts: dict[str, int],
    *,
    provider_name: str,
) -> int:
    inserted = 0
    for alias in sorted(entity.aliases, key=_alias_rank):
        if alias.alias_type == "brand_product" and _is_other_entity(qid_owner, alias, profile):
            # The brand is a tracked entity in its own right. Aliasing it onto the entity that
            # owns it would assert the two are one legal entity, which ADR 0006 forbids.
            counts["brands_skipped"] += 1
            continue
        inserted += int(_upsert_alias(session, profile, alias, provider_name=provider_name))
    return inserted


def _alias_rank(alias: WikidataAlias) -> tuple[int, str]:
    """Order aliases so the most authoritative type claims a normalized key it shares."""

    priority = (
        ALIAS_TYPE_PRIORITY.index(alias.alias_type)
        if alias.alias_type in ALIAS_TYPE_PRIORITY
        else len(ALIAS_TYPE_PRIORITY)
    )
    return priority, alias.value


def _is_other_entity(
    qid_owner: dict[str, EntityProfile], alias: WikidataAlias, profile: EntityProfile
) -> bool:
    owner = qid_owner.get(alias.qid) if alias.qid else None
    return owner is not None and owner.id != profile.id


def _upsert_alias(
    session: Any, profile: EntityProfile, alias: WikidataAlias, *, provider_name: str
) -> bool:
    """Merge one Wikidata alias; aliases never conflict across sources, they union."""

    raw_alias = alias.value.strip()
    normalized = normalize_alias(raw_alias)
    if not raw_alias or not normalized:
        return False

    # Matches uq_entity_aliases_entity_normalized_alias_source: one row per entity+key+source, so
    # re-running over a changed surface form of the same key inserts nothing new. Callers pass
    # aliases in _alias_rank order, so the row that stands for a shared key is the authoritative
    # one: never a dated former name where the current legal name shares its key.
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
            alias_type=alias.alias_type,
            source=provider_name,
            valid_from=alias.valid_from,
            valid_to=alias.valid_to,
        ),
    )
    return True


def _collect_edges(
    edges: dict[tuple[uuid.UUID, uuid.UUID], set[str]],
    counts: dict[str, int],
    entity: WikidataEntity,
    profile: EntityProfile,
    qid_owner: dict[str, EntityProfile],
) -> None:
    """Normalize P749/P355 into ``parent_of`` edges that always run parent -> child.

    P749 (parent organization) names the item's parent, so the related item is the parent and the
    item is the child; P355 (has subsidiary) is the mirror image. Both spellings can describe the
    same pair, so the properties that produced an edge are merged onto the single row the unique
    key admits.
    """

    references = [(ref, PROPERTY_PARENT, True) for ref in entity.parents]
    references += [(ref, PROPERTY_SUBSIDIARY, False) for ref in entity.subsidiaries]
    for ref, property_id, related_is_parent in references:
        related = qid_owner.get(ref.qid)
        if related is None:
            # An edge needs two resolved endpoints. An unknown QID is skipped, never created.
            counts["relationships_skipped"] += 1
            continue
        parent, child = (related, profile) if related_is_parent else (profile, related)
        if parent.id == child.id:
            counts["relationships_skipped"] += 1
            continue
        edges.setdefault((parent.id, child.id), set()).add(property_id)


def _upsert_parent_relationship(
    session: Any,
    parent_id: uuid.UUID,
    child_id: uuid.UUID,
    properties: Sequence[str],
    *,
    provider_name: str,
) -> str:
    """Persist one ``parent_of`` edge under the Wikidata provider.

    The unique key is (parent, child, type, provider), so this never touches the GLEIF edge for
    the same pair: the two sources' claims coexist with their own provenance and confidence.
    """

    values: dict[str, Any] = {
        "confidence_score": WIKIDATA_CONFIDENCE,
        "relationship_metadata": {
            "precedence": identity_precedence(provider_name),
            "provider": provider_name,
            "source_properties": list(properties),
        },
    }
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
            # P749/P355 truthy statements carry no interval, so the edge is stored open.
            valid_from=None,
            valid_to=None,
            **values,
        ),
    )
    return "inserted"


def _retain(
    session: Any,
    counts: dict[str, int],
    *,
    provider_name: str,
    item_type: str,
    external_id: str,
    payload: Any,
    provider_run_id: uuid.UUID | None,
    identity_parts: Sequence[Any],
) -> None:
    _, created = retain_raw_item(
        session,
        provider=provider_name,
        item_type=item_type,
        external_id=external_id,
        payload=payload,
        observed_at=None,
        provider_run_id=provider_run_id,
        identity_parts=identity_parts,
    )
    counts["raw_inserted"] += int(created)
    counts["raw_skipped"] += int(not created)


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    if size < 1:
        msg = "batch_size must be positive"
        raise ValueError(msg)
    return [list(values[start : start + size]) for start in range(0, len(values), size)]
