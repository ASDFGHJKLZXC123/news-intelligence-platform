"""Derived entity-intelligence API endpoints."""

from __future__ import annotations

import decimal
import uuid
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from apps.api.provider_data import _iso
from db.base import get_session
from db.models import EntityProfile, EntityRelationship, SanctionsEntity
from services.entities import EntityResolutionResult, resolve_entity

router = APIRouter(prefix="/api/v1/entities", tags=["entities"])


class EntityResolveRequest(BaseModel):
    """Request body for deterministic entity resolution."""

    target_type: str = Field(min_length=1)
    target_id: str | None = None
    names: list[str] = Field(default_factory=list)
    identifiers: dict[str, Any] = Field(default_factory=dict)
    country: str | None = None
    persist_run: bool = True


SessionDep = Annotated[Session, Depends(get_session)]


@router.post("/resolve")
def resolve_entity_endpoint(request: EntityResolveRequest, session: SessionDep) -> dict[str, Any]:
    result = resolve_entity(
        session,
        target_type=request.target_type,
        target_id=request.target_id,
        names=request.names,
        identifiers=request.identifiers,
        country=request.country,
        persist_run=request.persist_run,
    )
    return _serialize_resolution_result(result)


def _serialize_resolution_result(result: EntityResolutionResult) -> dict[str, Any]:
    return {
        "target_type": result.target_type,
        "target_id": result.target_id,
        "run_key": result.run_key,
        "matched_entity": _serialize_entity_profile(result.matched_entity)
        if result.matched_entity
        else None,
        "confidence_score": _json_scalar(result.confidence_score),
        "confidence_band": result.confidence_band,
        "match_method": result.match_method,
        "explanation": result.explanation,
        "should_attach": result.should_attach,
        "candidates": [
            {
                "entity": _serialize_entity_profile(candidate.entity_profile),
                "confidence_score": _json_scalar(candidate.confidence_score),
                "confidence_band": candidate.confidence_band,
                "match_method": candidate.match_method,
                "matched_value": candidate.matched_value,
                "explanation": candidate.explanation,
                "evidence": _json_mapping(candidate.evidence),
            }
            for candidate in result.candidates
        ],
        "sanctions_matches": [
            {
                "sanctions_entity": _serialize_sanctions_entity(match.sanctions_entity),
                "confidence_score": _json_scalar(match.confidence_score),
                "confidence_band": match.confidence_band,
                "match_method": match.match_method,
                "matched_value": match.matched_value,
                "explanation": match.explanation,
                "evidence": _json_mapping(match.evidence),
            }
            for match in result.sanctions_matches
        ],
        "related_entities": [
            {
                "entity": _serialize_entity_profile(item.entity_profile),
                "relationship": _serialize_entity_relationship(item.relationship),
                "direction": item.direction,
            }
            for item in result.related_entities
        ],
    }


def _serialize_entity_profile(profile: EntityProfile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "id": str(profile.id),
        "canonical_name": profile.canonical_name,
        "normalized_name": profile.normalized_name,
        "entity_type": profile.entity_type,
        "country": profile.country,
        "primary_ticker": profile.primary_ticker,
        "primary_cik": profile.primary_cik,
        "primary_lei": profile.primary_lei,
        "website": profile.website,
        "metadata": profile.profile_metadata,
        "created_at": _iso(profile.created_at),
        "updated_at": _iso(profile.updated_at),
    }


def _serialize_sanctions_entity(entity: SanctionsEntity) -> dict[str, Any]:
    return {
        "id": str(entity.id),
        "provider": entity.provider,
        "list_code": entity.list_code,
        "entity_uid": entity.entity_uid,
        "entity_type": entity.entity_type,
        "primary_name": entity.primary_name,
        "normalized_name": entity.normalized_name,
        "country": entity.country,
        "programs": entity.programs,
        "remarks": entity.remarks,
        "first_seen_at": _iso(entity.first_seen_at),
        "last_seen_at": _iso(entity.last_seen_at),
    }


def _serialize_entity_relationship(relationship: EntityRelationship) -> dict[str, Any]:
    return {
        "id": str(relationship.id),
        "parent_entity_id": str(relationship.parent_entity_id),
        "child_entity_id": str(relationship.child_entity_id),
        "relationship_type": relationship.relationship_type,
        "provider": relationship.provider,
        "confidence_score": _json_scalar(relationship.confidence_score),
        "valid_from": _iso(relationship.valid_from),
        "valid_to": _iso(relationship.valid_to),
        "metadata": relationship.relationship_metadata,
    }


def _json_scalar(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_scalar(item) for key, item in value.items()}


def _json_sequence(value: Sequence[Any]) -> list[Any]:
    return [_json_scalar(item) for item in value]
