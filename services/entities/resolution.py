"""Deterministic entity resolver for provider-data identity records."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from db.models import (
    EntityIdentifier,
    EntityProfile,
    EntityRelationship,
    EntityResolutionRun,
    SanctionsAlias,
    SanctionsEntity,
    SanctionsIdentifier,
    SECCompany,
)
from services.provider_data.common import (
    add,
    find_one,
    idempotency_key,
    json_safe,
    normalize_cik,
    normalize_name,
    stable_hash,
)

AUTO_ACCEPTED = "auto_accepted"
LIKELY_MATCH = "likely_match"
REVIEW_REQUIRED = "review_required"
NO_MATCH = "no_match"

_ATTACHABLE_BANDS = {AUTO_ACCEPTED, LIKELY_MATCH}
_IDENTIFIER_ALIASES = {
    "cik": "cik",
    "centralindexkey": "cik",
    "cikno": "cik",
    "seccik": "cik",
    "lei": "lei",
    "legalentityidentifier": "lei",
    "leino": "lei",
    "ticker": "ticker",
    "tickersymbol": "ticker",
    "symbol": "ticker",
}
_PROFILE_IDENTIFIER_ATTRS = {
    "cik": "primary_cik",
    "lei": "primary_lei",
    "ticker": "primary_ticker",
}
_METHOD_PRIORITY = {
    "identifier:cik": 0,
    "identifier:lei": 1,
    "identifier:ticker": 2,
    "identifier": 3,
    "sanctions_bridge": 4,
    "name:normalized": 10,
}


@dataclass(frozen=True)
class EntityResolutionRequest:
    """A deterministic entity-resolution request.

    Identifiers are passed as provider/native names, then canonicalized to CIK,
    ticker, LEI, or a stable generic identifier key.
    """

    target_type: str
    target_id: str | uuid.UUID | None = None
    names: Sequence[str] = ()
    identifiers: Mapping[str, Any] = field(default_factory=dict)
    country: str | None = None
    persist_run: bool = True


@dataclass(frozen=True)
class IdentifierInput:
    identifier_type: str
    raw_type: str
    raw_value: str
    normalized_value: str


@dataclass(frozen=True)
class ResolutionCandidate:
    entity_profile: EntityProfile
    confidence_score: float
    confidence_band: str
    match_method: str
    matched_value: str
    explanation: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SanctionsResolutionCandidate:
    sanctions_entity: SanctionsEntity
    confidence_score: float
    confidence_band: str
    match_method: str
    matched_value: str
    explanation: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RelatedEntity:
    entity_profile: EntityProfile
    relationship: EntityRelationship
    direction: str


@dataclass(frozen=True)
class EntityResolutionResult:
    target_type: str
    target_id: str
    matched_entity: EntityProfile | None
    confidence_score: float | None
    confidence_band: str
    match_method: str | None
    explanation: str
    candidates: tuple[ResolutionCandidate, ...]
    sanctions_matches: tuple[SanctionsResolutionCandidate, ...]
    related_entities: tuple[RelatedEntity, ...]
    run_key: str

    @property
    def should_attach(self) -> bool:
        return self.matched_entity is not None and self.confidence_band in _ATTACHABLE_BANDS


class EntityResolutionRepository:
    """Small adapter over SQLAlchemy sessions or lightweight test repositories."""

    def __init__(self, source: Any) -> None:
        self.source = source

    def all_of(self, model: type[Any]) -> list[Any]:
        custom_all = getattr(self.source, "all_of", None)
        if callable(custom_all):
            return list(custom_all(model))

        custom_list = getattr(self.source, "list_model", None)
        if callable(custom_list):
            return list(custom_list(model))

        custom_all = getattr(self.source, "all", None)
        if callable(custom_all):
            return list(custom_all(model))

        return list(self.source.execute(select(model)).scalars().all())

    def find_one(self, model: type[Any], **criteria: Any) -> Any | None:
        return find_one(self.source, model, **criteria)

    def add(self, obj: Any) -> Any:
        return add(self.source, obj)


class EntityResolver:
    """Resolve provider/news entities into canonical entity profiles."""

    def __init__(self, repository_or_session: Any) -> None:
        self.repository = EntityResolutionRepository(repository_or_session)

    def resolve(self, request: EntityResolutionRequest) -> EntityResolutionResult:
        target_type = _clean_target_type(request.target_type)
        names = _unique_names(request.names)
        identifiers = _identifier_inputs(request.identifiers)
        country = _clean_country(request.country)
        target_id = str(request.target_id) if request.target_id is not None else ""

        names, identifiers, country, target_id = self._augment_from_target(
            target_type=target_type,
            target_id=target_id,
            names=names,
            identifiers=identifiers,
            country=country,
        )
        if not target_id:
            target_id = stable_hash(
                {
                    "country": country,
                    "identifiers": [
                        {
                            "identifier_type": identifier.identifier_type,
                            "normalized_value": identifier.normalized_value,
                        }
                        for identifier in sorted(
                            identifiers,
                            key=lambda item: (item.identifier_type, item.normalized_value),
                        )
                    ],
                    "names": names,
                    "target_type": target_type,
                }
            )[:32]

        profile_candidates = self._profile_candidates(names, identifiers, country)
        sanctions_matches = self._sanctions_candidates(names, identifiers, country)
        profile_candidates = self._merge_candidates(
            (
                *profile_candidates,
                *self._sanctions_bridge_candidates(sanctions_matches),
            )
        )

        best = profile_candidates[0] if profile_candidates else None
        ambiguous = _has_ambiguous_top_candidate(profile_candidates)
        result_band = NO_MATCH
        matched_entity: EntityProfile | None = None
        confidence_score: float | None = None
        match_method: str | None = None
        explanation = "No deterministic identifier, name, alias, or sanctions bridge matched."

        if best is not None:
            confidence_score = best.confidence_score
            match_method = best.match_method
            result_band = REVIEW_REQUIRED if ambiguous else best.confidence_band
            if ambiguous:
                explanation = (
                    "Multiple canonical entity profiles matched with the same top confidence; "
                    "manual review is required before attaching."
                )
            elif result_band in _ATTACHABLE_BANDS:
                matched_entity = best.entity_profile
                explanation = best.explanation
            else:
                explanation = (
                    f"Best candidate scored {best.confidence_score:.2f}, which requires review "
                    "before attaching."
                )

        related_entities = self._related_entities(matched_entity) if matched_entity else ()
        run_key = _run_key(target_type, target_id, names, identifiers, country)
        result = EntityResolutionResult(
            target_type=target_type,
            target_id=target_id,
            matched_entity=matched_entity,
            confidence_score=confidence_score,
            confidence_band=result_band,
            match_method=match_method,
            explanation=explanation,
            candidates=profile_candidates,
            sanctions_matches=sanctions_matches,
            related_entities=related_entities,
            run_key=run_key,
        )
        if request.persist_run:
            self._persist_run(result, names)
        return result

    def _augment_from_target(
        self,
        *,
        target_type: str,
        target_id: str,
        names: tuple[str, ...],
        identifiers: tuple[IdentifierInput, ...],
        country: str | None,
    ) -> tuple[tuple[str, ...], tuple[IdentifierInput, ...], str | None, str]:
        if target_type in {"sec", "sec_company", "sec_company_submission", "seccompany"} and target_id:
            company = self._find_sec_company(target_id)
            if company is not None:
                target_id = str(company.id)
                names = _append_unique_name(names, company.name)
                identifiers = _append_identifier(identifiers, "cik", company.cik)
                if company.ticker:
                    identifiers = _append_identifier(identifiers, "ticker", company.ticker)

        if target_type in {
            "sanctions",
            "sanctions_entity",
            "sanctionsentity",
            "ofac_sanctions_entity",
            "ofacsanctionsentity",
        } and target_id:
            entity = self._find_sanctions_entity(target_id)
            if entity is not None:
                target_id = str(entity.id)
                names = _append_unique_name(names, entity.primary_name)
                country = country or _clean_country(entity.country)
                for alias in self._sanctions_aliases(entity.id):
                    names = _append_unique_name(names, alias.alias_name)
                for identifier in self._sanctions_identifiers(entity.id):
                    identifiers = _append_identifier(
                        identifiers,
                        identifier.identifier_type,
                        identifier.identifier_value,
                    )

        if target_type in {
            "entity",
            "entity_profile",
            "entityprofile",
            "canonical_entity",
            "canonicalentity",
        } and target_id:
            profile = self._find_entity_profile(target_id)
            if profile is not None:
                target_id = str(profile.id)
                names = _append_unique_name(names, profile.canonical_name)
                country = country or _clean_country(profile.country)
                for identifier_type, attr_name in _PROFILE_IDENTIFIER_ATTRS.items():
                    value = getattr(profile, attr_name, None)
                    if value:
                        identifiers = _append_identifier(identifiers, identifier_type, value)

        return names, identifiers, country, target_id

    def _profile_candidates(
        self,
        names: tuple[str, ...],
        identifiers: tuple[IdentifierInput, ...],
        country: str | None,
    ) -> tuple[ResolutionCandidate, ...]:
        candidates = [
            *self._profile_identifier_candidates(identifiers),
            *self._profile_name_candidates(names, country),
        ]
        return self._merge_candidates(candidates)

    def _profile_identifier_candidates(
        self, identifiers: tuple[IdentifierInput, ...]
    ) -> tuple[ResolutionCandidate, ...]:
        if not identifiers:
            return ()

        candidates: list[ResolutionCandidate] = []
        profiles = self.repository.all_of(EntityProfile)
        for profile in profiles:
            for identifier in identifiers:
                attr_name = _PROFILE_IDENTIFIER_ATTRS.get(identifier.identifier_type)
                if attr_name is None:
                    continue
                profile_value = getattr(profile, attr_name, None)
                if _identifier_value_matches(
                    identifier.identifier_type,
                    identifier.normalized_value,
                    profile_value,
                ):
                    candidates.append(
                        _profile_candidate(
                            profile,
                            confidence_score=1.0,
                            match_method=f"identifier:{identifier.identifier_type}",
                            matched_value=identifier.raw_value,
                            explanation=(
                                f"Exact {identifier.identifier_type.upper()} identifier matched "
                                "the canonical entity profile."
                            ),
                            evidence={
                                "source": f"entity_profiles.{attr_name}",
                                "identifier_type": identifier.identifier_type,
                            },
                        )
                    )

        profile_by_id = {str(profile.id): profile for profile in profiles}
        for row in self.repository.all_of(EntityIdentifier):
            row_type = _canonical_identifier_type(row.identifier_type)
            for identifier in identifiers:
                if row_type != identifier.identifier_type:
                    continue
                if not _identifier_value_matches(
                    row_type,
                    identifier.normalized_value,
                    row.identifier_value,
                ):
                    continue
                profile = profile_by_id.get(str(row.entity_profile_id))
                if profile is None:
                    continue
                candidates.append(
                    _profile_candidate(
                        profile,
                        confidence_score=max(_float_or(row.confidence_score, 1.0), 0.95),
                        match_method=f"identifier:{row_type}",
                        matched_value=identifier.raw_value,
                        explanation=(
                            f"Exact {row_type.upper()} identifier matched a provider-backed "
                            "entity identifier."
                        ),
                        evidence={
                            "source": "entity_identifiers",
                            "identifier_type": row_type,
                            "provider": row.provider,
                        },
                    )
                )

        return tuple(candidates)

    def _profile_name_candidates(
        self, names: tuple[str, ...], country: str | None
    ) -> tuple[ResolutionCandidate, ...]:
        normalized_names = {_normalized_name(name): name for name in names if _normalized_name(name)}
        if not normalized_names:
            return ()

        candidates: list[ResolutionCandidate] = []
        for profile in self.repository.all_of(EntityProfile):
            profile_name = getattr(profile, "normalized_name", None) or _normalized_name(
                profile.canonical_name
            )
            if profile_name not in normalized_names:
                continue
            score = _country_adjusted_score(0.90, country, profile.country)
            candidates.append(
                _profile_candidate(
                    profile,
                    confidence_score=score,
                    match_method="name:normalized",
                    matched_value=normalized_names[profile_name],
                    explanation="Exact normalized entity name matched a canonical profile.",
                    evidence={"source": "entity_profiles.normalized_name"},
                )
            )
        return tuple(candidates)

    def _sanctions_candidates(
        self,
        names: tuple[str, ...],
        identifiers: tuple[IdentifierInput, ...],
        country: str | None,
    ) -> tuple[SanctionsResolutionCandidate, ...]:
        by_entity_id: dict[str, SanctionsResolutionCandidate] = {}
        normalized_names = {_normalized_name(name): name for name in names if _normalized_name(name)}

        for entity in self.repository.all_of(SanctionsEntity):
            entity_name = getattr(entity, "normalized_name", None) or _normalized_name(
                entity.primary_name
            )
            if entity_name in normalized_names:
                self._keep_best_sanctions_candidate(
                    by_entity_id,
                    _sanctions_candidate(
                        entity,
                        confidence_score=_country_adjusted_score(0.90, country, entity.country),
                        match_method="sanctions_name",
                        matched_value=normalized_names[entity_name],
                        explanation="Exact normalized name matched an OFAC sanctions entity.",
                        evidence={"source": "sanctions_entities.normalized_name"},
                    ),
                )

            for identifier in self._sanctions_identifiers(entity.id):
                matched_identifier = _matching_identifier(identifier, identifiers)
                if matched_identifier is None:
                    continue
                self._keep_best_sanctions_candidate(
                    by_entity_id,
                    _sanctions_candidate(
                        entity,
                        confidence_score=0.98,
                        match_method=f"sanctions_identifier:{matched_identifier.identifier_type}",
                        matched_value=matched_identifier.raw_value,
                        explanation="Exact identifier matched an OFAC sanctions identifier.",
                        evidence={
                            "source": "sanctions_identifiers",
                            "identifier_type": matched_identifier.identifier_type,
                        },
                    ),
                )

        for alias in self.repository.all_of(SanctionsAlias):
            alias_name = getattr(alias, "normalized_alias", None) or _normalized_name(
                alias.alias_name
            )
            if alias_name not in normalized_names:
                continue
            entity = self._sanctions_entity_by_id(alias.sanctions_entity_id)
            if entity is None:
                continue
            self._keep_best_sanctions_candidate(
                by_entity_id,
                _sanctions_candidate(
                    entity,
                    confidence_score=_country_adjusted_score(
                        _alias_score(alias.quality), country, entity.country
                    ),
                    match_method="sanctions_alias",
                    matched_value=normalized_names[alias_name],
                    explanation="Exact normalized alias matched an OFAC sanctions alias.",
                    evidence={
                        "source": "sanctions_aliases.normalized_alias",
                        "alias_type": alias.alias_type,
                        "quality": alias.quality,
                    },
                ),
            )

        return tuple(sorted(by_entity_id.values(), key=_sanctions_sort_key))

    def _sanctions_bridge_candidates(
        self, sanctions_matches: tuple[SanctionsResolutionCandidate, ...]
    ) -> tuple[ResolutionCandidate, ...]:
        candidates: list[ResolutionCandidate] = []
        for sanctions_match in sanctions_matches:
            identifiers = tuple(
                _identifier_input(identifier.identifier_type, identifier.identifier_value)
                for identifier in self._sanctions_identifiers(sanctions_match.sanctions_entity.id)
            )
            for candidate in self._profile_identifier_candidates(
                tuple(identifier for identifier in identifiers if identifier is not None)
            ):
                score = min(candidate.confidence_score, sanctions_match.confidence_score)
                candidates.append(
                    _profile_candidate(
                        candidate.entity_profile,
                        confidence_score=score,
                        match_method=f"sanctions_bridge:{candidate.match_method}",
                        matched_value=sanctions_match.matched_value,
                        explanation=(
                            "OFAC sanctions evidence matched the input, then an exact sanctions "
                            "identifier matched the canonical profile."
                        ),
                        evidence={
                            "source": "sanctions_bridge",
                            "sanctions_entity_id": str(sanctions_match.sanctions_entity.id),
                            "sanctions_match_method": sanctions_match.match_method,
                            "profile_match_method": candidate.match_method,
                        },
                    )
                )
        return tuple(candidates)

    def _merge_candidates(
        self, candidates: Sequence[ResolutionCandidate]
    ) -> tuple[ResolutionCandidate, ...]:
        best_by_profile: dict[str, ResolutionCandidate] = {}
        for candidate in candidates:
            key = str(candidate.entity_profile.id)
            existing = best_by_profile.get(key)
            if existing is None or _candidate_sort_key(candidate) < _candidate_sort_key(existing):
                best_by_profile[key] = candidate
        return tuple(sorted(best_by_profile.values(), key=_candidate_sort_key))

    def _related_entities(self, profile: EntityProfile) -> tuple[RelatedEntity, ...]:
        profile_by_id = {
            str(candidate.id): candidate for candidate in self.repository.all_of(EntityProfile)
        }
        related: list[RelatedEntity] = []
        for relationship in self.repository.all_of(EntityRelationship):
            if str(relationship.parent_entity_id) == str(profile.id):
                child = profile_by_id.get(str(relationship.child_entity_id))
                if child is not None:
                    related.append(
                        RelatedEntity(
                            entity_profile=child,
                            relationship=relationship,
                            direction="child",
                        )
                    )
            elif str(relationship.child_entity_id) == str(profile.id):
                parent = profile_by_id.get(str(relationship.parent_entity_id))
                if parent is not None:
                    related.append(
                        RelatedEntity(
                            entity_profile=parent,
                            relationship=relationship,
                            direction="parent",
                        )
                    )
        return tuple(
            sorted(
                related,
                key=lambda item: (
                    item.direction,
                    item.relationship.relationship_type,
                    item.entity_profile.canonical_name,
                ),
            )
        )

    def _persist_run(self, result: EntityResolutionResult, names: tuple[str, ...]) -> None:
        values = {
            "target_type": result.target_type,
            "target_id": result.target_id,
            "input_names": json_safe(names),
            "matched_entity_id": result.matched_entity.id if result.matched_entity else None,
            "confidence_score": result.confidence_score,
            "explanation": result.explanation,
        }
        existing = self.repository.find_one(EntityResolutionRun, run_key=result.run_key)
        if existing is not None:
            for key, value in values.items():
                setattr(existing, key, value)
            return

        self.repository.add(
            EntityResolutionRun(
                id=uuid.uuid4(),
                run_key=result.run_key,
                **values,
            )
        )

    def _find_sec_company(self, target_id: str) -> SECCompany | None:
        normalized_cik = _try_normalize_cik(target_id)
        target_upper = target_id.upper()
        for company in self.repository.all_of(SECCompany):
            if str(company.id) == target_id:
                return company
            if normalized_cik is not None and company.cik == normalized_cik:
                return company
            if company.ticker and company.ticker.upper() == target_upper:
                return company
        return None

    def _find_sanctions_entity(self, target_id: str) -> SanctionsEntity | None:
        for entity in self.repository.all_of(SanctionsEntity):
            if str(entity.id) == target_id or entity.entity_uid == target_id:
                return entity
        return None

    def _find_entity_profile(self, target_id: str) -> EntityProfile | None:
        normalized_cik = _try_normalize_cik(target_id)
        target_upper = target_id.upper()
        for profile in self.repository.all_of(EntityProfile):
            if str(profile.id) == target_id:
                return profile
            if normalized_cik is not None and profile.primary_cik == normalized_cik:
                return profile
            if profile.primary_lei and profile.primary_lei.upper() == target_upper:
                return profile
            if profile.primary_ticker and profile.primary_ticker.upper() == target_upper:
                return profile
        return None

    def _sanctions_aliases(self, sanctions_entity_id: uuid.UUID) -> tuple[SanctionsAlias, ...]:
        return tuple(
            alias
            for alias in self.repository.all_of(SanctionsAlias)
            if str(alias.sanctions_entity_id) == str(sanctions_entity_id)
        )

    def _sanctions_identifiers(
        self, sanctions_entity_id: uuid.UUID
    ) -> tuple[SanctionsIdentifier, ...]:
        return tuple(
            identifier
            for identifier in self.repository.all_of(SanctionsIdentifier)
            if str(identifier.sanctions_entity_id) == str(sanctions_entity_id)
        )

    def _sanctions_entity_by_id(self, sanctions_entity_id: uuid.UUID) -> SanctionsEntity | None:
        for entity in self.repository.all_of(SanctionsEntity):
            if str(entity.id) == str(sanctions_entity_id):
                return entity
        return None

    def _keep_best_sanctions_candidate(
        self,
        by_entity_id: dict[str, SanctionsResolutionCandidate],
        candidate: SanctionsResolutionCandidate,
    ) -> None:
        key = str(candidate.sanctions_entity.id)
        existing = by_entity_id.get(key)
        if existing is None or _sanctions_sort_key(candidate) < _sanctions_sort_key(existing):
            by_entity_id[key] = candidate


def resolve_entity(
    repository_or_session: Any,
    *,
    target_type: str,
    target_id: str | uuid.UUID | None = None,
    names: Sequence[str] = (),
    identifiers: Mapping[str, Any] | None = None,
    country: str | None = None,
    persist_run: bool = True,
) -> EntityResolutionResult:
    """Resolve an entity using deterministic local storage only."""

    return EntityResolver(repository_or_session).resolve(
        EntityResolutionRequest(
            target_type=target_type,
            target_id=target_id,
            names=names,
            identifiers=identifiers or {},
            country=country,
            persist_run=persist_run,
        )
    )


def confidence_band(score: float | None) -> str:
    if score is None or score < 0.65:
        return NO_MATCH
    if score >= 0.95:
        return AUTO_ACCEPTED
    if score >= 0.85:
        return LIKELY_MATCH
    return REVIEW_REQUIRED


def _profile_candidate(
    entity_profile: EntityProfile,
    *,
    confidence_score: float,
    match_method: str,
    matched_value: str,
    explanation: str,
    evidence: Mapping[str, Any],
) -> ResolutionCandidate:
    score = round(confidence_score, 4)
    return ResolutionCandidate(
        entity_profile=entity_profile,
        confidence_score=score,
        confidence_band=confidence_band(score),
        match_method=match_method,
        matched_value=matched_value,
        explanation=explanation,
        evidence=evidence,
    )


def _sanctions_candidate(
    sanctions_entity: SanctionsEntity,
    *,
    confidence_score: float,
    match_method: str,
    matched_value: str,
    explanation: str,
    evidence: Mapping[str, Any],
) -> SanctionsResolutionCandidate:
    score = round(confidence_score, 4)
    return SanctionsResolutionCandidate(
        sanctions_entity=sanctions_entity,
        confidence_score=score,
        confidence_band=confidence_band(score),
        match_method=match_method,
        matched_value=matched_value,
        explanation=explanation,
        evidence=evidence,
    )


def _clean_target_type(value: str) -> str:
    cleaned = "_".join(str(value).strip().casefold().replace("-", "_").split())
    if not cleaned:
        msg = "target_type is required"
        raise ValueError(msg)
    return cleaned


def _clean_country(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip().upper()
    return cleaned or None


def _unique_names(values: Sequence[str]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalized_name(value)
        if not normalized or normalized in seen:
            continue
        names.append(str(value).strip())
        seen.add(normalized)
    return tuple(names)


def _append_unique_name(names: tuple[str, ...], value: str | None) -> tuple[str, ...]:
    if not value:
        return names
    normalized = _normalized_name(value)
    if not normalized or normalized in {_normalized_name(name) for name in names}:
        return names
    return (*names, value.strip())


def _normalized_name(value: str | None) -> str:
    if not value:
        return ""
    return normalize_name(str(value))


def _identifier_inputs(values: Mapping[str, Any]) -> tuple[IdentifierInput, ...]:
    identifiers: tuple[IdentifierInput, ...] = ()
    for raw_type, raw_values in values.items():
        if _is_sequence_value(raw_values):
            for raw_value in raw_values:
                identifiers = _append_identifier(identifiers, str(raw_type), raw_value)
        else:
            identifiers = _append_identifier(identifiers, str(raw_type), raw_values)
    return identifiers


def _append_identifier(
    identifiers: tuple[IdentifierInput, ...], raw_type: str, raw_value: Any
) -> tuple[IdentifierInput, ...]:
    identifier = _identifier_input(raw_type, raw_value)
    if identifier is None:
        return identifiers
    key = (identifier.identifier_type, identifier.normalized_value)
    if key in {
        (existing.identifier_type, existing.normalized_value) for existing in identifiers
    }:
        return identifiers
    return (*identifiers, identifier)


def _identifier_input(raw_type: str, raw_value: Any) -> IdentifierInput | None:
    if raw_value in (None, ""):
        return None
    identifier_type = _canonical_identifier_type(raw_type)
    raw_value_text = str(raw_value).strip()
    normalized_value = _normalize_identifier_value(identifier_type, raw_value_text)
    if not normalized_value:
        return None
    return IdentifierInput(
        identifier_type=identifier_type,
        raw_type=str(raw_type),
        raw_value=raw_value_text,
        normalized_value=normalized_value,
    )


def _canonical_identifier_type(raw_type: str | None) -> str:
    cleaned = "".join(character for character in str(raw_type or "").casefold() if character.isalnum())
    return _IDENTIFIER_ALIASES.get(cleaned, cleaned or "identifier")


def _normalize_identifier_value(identifier_type: str, value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if identifier_type == "cik":
        normalized = _try_normalize_cik(text)
        return normalized or ""
    if identifier_type == "ticker":
        return text.upper()
    if identifier_type == "lei":
        return "".join(text.upper().split())
    return normalize_name(text)


def _identifier_value_matches(
    identifier_type: str, normalized_input_value: str, candidate_value: Any
) -> bool:
    if candidate_value in (None, ""):
        return False
    return _normalize_identifier_value(identifier_type, candidate_value) == normalized_input_value


def _matching_identifier(
    sanctions_identifier: SanctionsIdentifier, identifiers: tuple[IdentifierInput, ...]
) -> IdentifierInput | None:
    sanctions_type = _canonical_identifier_type(sanctions_identifier.identifier_type)
    sanctions_value = _normalize_identifier_value(sanctions_type, sanctions_identifier.identifier_value)
    for identifier in identifiers:
        if identifier.identifier_type != sanctions_type:
            continue
        if identifier.normalized_value == sanctions_value:
            return identifier
    return None


def _country_adjusted_score(
    base_score: float, input_country: str | None, candidate_country: str | None
) -> float:
    normalized_candidate = _clean_country(candidate_country)
    if input_country is None or normalized_candidate is None:
        return base_score
    if input_country == normalized_candidate:
        return min(base_score + 0.02, 0.94)
    return max(base_score - 0.05, 0.65)


def _alias_score(quality: str | None) -> float:
    if not quality:
        return 0.86
    normalized = quality.casefold().strip()
    if normalized in {"strong", "good", "high"}:
        return 0.88
    if normalized in {"weak", "low"}:
        return 0.82
    return 0.86


def _has_ambiguous_top_candidate(candidates: tuple[ResolutionCandidate, ...]) -> bool:
    if len(candidates) < 2:
        return False
    return candidates[0].confidence_score == candidates[1].confidence_score


def _candidate_sort_key(candidate: ResolutionCandidate) -> tuple[float, int, str, str]:
    return (
        -candidate.confidence_score,
        _method_priority(candidate.match_method),
        candidate.entity_profile.canonical_name,
        str(candidate.entity_profile.id),
    )


def _sanctions_sort_key(
    candidate: SanctionsResolutionCandidate,
) -> tuple[float, int, str, str]:
    return (
        -candidate.confidence_score,
        _method_priority(candidate.match_method),
        candidate.sanctions_entity.primary_name,
        str(candidate.sanctions_entity.id),
    )


def _method_priority(method: str) -> int:
    for prefix, priority in _METHOD_PRIORITY.items():
        if method.startswith(prefix):
            return priority
    return 100


def _run_key(
    target_type: str,
    target_id: str,
    names: tuple[str, ...],
    identifiers: tuple[IdentifierInput, ...],
    country: str | None,
) -> str:
    return idempotency_key(
        "entity_resolution",
        "run",
        target_type,
        target_id,
        tuple(_normalized_name(name) for name in names),
        tuple(
            sorted(
                (identifier.identifier_type, identifier.normalized_value)
                for identifier in identifiers
            )
        ),
        country,
    )


def _try_normalize_cik(value: Any) -> str | None:
    try:
        return normalize_cik(str(value))
    except ValueError:
        return None


def _float_or(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_sequence_value(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)
