"""Persist descriptive company and industry exposure without predictive outputs.

An accepted, asserted ``EventEntity`` link can be projected onto the canonical company
master without estimating impact, risk, opportunity, or crisis probability.  This module
owns that deliberately narrow projection:

* one ``EventCompany`` row records the direct mention and entity-link confidence;
* one ``EventIndustry`` row records the company's normalized industry label;
* every predictive/scored field on a newly created projection remains ``NULL``.

Denied and speculative mentions remain available in ``event_entities`` for audit, but do
not become company or industry exposures.  Existing scored rows are never cleared or
rewritten; this writer updates only descriptive fields that it owns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from db.models import Company, EventCompany, EventEntity, EventIndustry
from services.entities.event_links import ROLE_ASSERTED
from services.provider_data.common import add, find_all, find_one, flush_pending, json_safe

DIRECT_MENTION_EXPLANATION: Final = (
    "Direct asserted company mention in source coverage for this event."
)


@dataclass(frozen=True, slots=True)
class DescriptiveExposureResult:
    """Counts from one idempotent event projection."""

    event_id: uuid.UUID
    asserted_entity_count: int
    company_count: int
    industry_count: int
    unmatched_entity_count: int
    excluded_nonasserted_count: int
    predictive_fields_written: int = 0

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


def persist_descriptive_exposures(
    session: Any,
    event_id: uuid.UUID,
) -> DescriptiveExposureResult:
    """Project asserted entity links into descriptive company and industry rows.

    The operation is deterministic and idempotent on the existing composite primary keys.
    It intentionally does not assign ``impact_*``, ``risk_score``, or
    ``opportunity_score`` fields.
    """

    links = find_all(session, EventEntity, event_id=event_id)
    asserted = sorted(
        (link for link in links if link.role == ROLE_ASSERTED),
        key=lambda link: str(link.entity_profile_id),
    )
    excluded_count = len(links) - len(asserted)
    company_ids: set[uuid.UUID] = set()
    industry_ids: set[str] = set()
    unmatched_count = 0

    for link in asserted:
        company = find_one(session, Company, entity_profile_id=link.entity_profile_id)
        if company is None:
            unmatched_count += 1
            continue

        company_ids.add(company.id)
        _upsert_company_exposure(session, event_id=event_id, company=company, link=link)

        industry_id = normalize_industry_id(company.industry)
        if industry_id is None:
            continue
        industry_ids.add(industry_id)
        _upsert_industry_exposure(session, event_id=event_id, industry_id=industry_id)

    return DescriptiveExposureResult(
        event_id=event_id,
        asserted_entity_count=len(asserted),
        company_count=len(company_ids),
        industry_count=len(industry_ids),
        unmatched_entity_count=unmatched_count,
        excluded_nonasserted_count=excluded_count,
    )


def normalize_industry_id(value: str | None) -> str | None:
    """Return a stable lowercase identifier for a provider-supplied industry label."""

    if value is None:
        return None
    tokens: list[str] = []
    token: list[str] = []
    for character in value.casefold().strip():
        if character.isalnum():
            token.append(character)
        elif token:
            tokens.append("".join(token))
            token = []
    if token:
        tokens.append("".join(token))
    normalized = "-".join(tokens)
    return normalized or None


def _upsert_company_exposure(
    session: Any,
    *,
    event_id: uuid.UUID,
    company: Company,
    link: EventEntity,
) -> EventCompany:
    existing = find_one(session, EventCompany, event_id=event_id, company_id=company.id)
    incoming_confidence = None if link.confidence_score is None else float(link.confidence_score)
    if existing is not None:
        if incoming_confidence is not None:
            current = (
                None if existing.confidence_score is None else float(existing.confidence_score)
            )
            existing.confidence_score = (
                incoming_confidence if current is None else max(current, incoming_confidence)
            )
        if not existing.exposure_explanation:
            existing.exposure_explanation = DIRECT_MENTION_EXPLANATION
        return existing

    row = add(
        session,
        EventCompany(
            event_id=event_id,
            company_id=company.id,
            confidence_score=incoming_confidence,
            exposure_explanation=DIRECT_MENTION_EXPLANATION,
            impact_direction=None,
            impact_score=None,
            risk_score=None,
        ),
    )
    flush_pending(session)
    return row


def _upsert_industry_exposure(
    session: Any,
    *,
    event_id: uuid.UUID,
    industry_id: str,
) -> EventIndustry:
    existing = find_one(
        session,
        EventIndustry,
        event_id=event_id,
        industry_id=industry_id,
    )
    if existing is not None:
        return existing

    row = add(
        session,
        EventIndustry(
            event_id=event_id,
            industry_id=industry_id,
            impact_direction=None,
            impact_score=None,
            risk_score=None,
            opportunity_score=None,
        ),
    )
    flush_pending(session)
    return row


__all__ = [
    "DIRECT_MENTION_EXPLANATION",
    "DescriptiveExposureResult",
    "normalize_industry_id",
    "persist_descriptive_exposures",
]
