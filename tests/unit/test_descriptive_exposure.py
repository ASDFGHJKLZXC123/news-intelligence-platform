"""Descriptive company/industry projections stay on the safe side of Gate G."""

from __future__ import annotations

import uuid

from db.models import Company, EventCompany, EventEntity, EventIndustry
from services.entities.descriptive_exposure import (
    DIRECT_MENTION_EXPLANATION,
    normalize_industry_id,
    persist_descriptive_exposures,
)
from services.entities.event_links import ROLE_ASSERTED, ROLE_DENIED, ROLE_SPECULATIVE
from tests.unit.entity_linking_fakes import FakeSession, profile


def _company(entity_id: uuid.UUID, *, industry: str | None = "Semiconductors & Equipment"):
    return Company(
        id=uuid.uuid4(),
        entity_profile_id=entity_id,
        display_name="Acme Corp",
        industry=industry,
    )


def _link(
    event_id: uuid.UUID,
    entity_id: uuid.UUID,
    *,
    role: str = ROLE_ASSERTED,
    confidence: float = 0.8,
) -> EventEntity:
    return EventEntity(
        event_id=event_id,
        entity_profile_id=entity_id,
        role=role,
        confidence_score=confidence,
    )


def test_asserted_company_and_industry_are_persisted_without_scores() -> None:
    event_id = uuid.uuid4()
    entity = profile("Acme Corp")
    company = _company(entity.id)
    session = FakeSession(entity, company, _link(event_id, entity.id))

    result = persist_descriptive_exposures(session, event_id)

    assert result.as_dict() == {
        "event_id": str(event_id),
        "asserted_entity_count": 1,
        "company_count": 1,
        "industry_count": 1,
        "unmatched_entity_count": 0,
        "excluded_nonasserted_count": 0,
        "predictive_fields_written": 0,
    }
    exposure = session.all_of(EventCompany)[0]
    assert exposure.company_id == company.id
    assert float(exposure.confidence_score) == 0.8
    assert exposure.exposure_explanation == DIRECT_MENTION_EXPLANATION
    assert exposure.impact_direction is None
    assert exposure.impact_score is None
    assert exposure.risk_score is None

    industry = session.all_of(EventIndustry)[0]
    assert industry.industry_id == "semiconductors-equipment"
    assert industry.impact_direction is None
    assert industry.impact_score is None
    assert industry.risk_score is None
    assert industry.opportunity_score is None


def test_denied_and_speculative_mentions_remain_audit_only() -> None:
    event_id = uuid.uuid4()
    denied = profile("Denied Corp")
    speculative = profile("Maybe Corp")
    session = FakeSession(
        denied,
        speculative,
        _company(denied.id),
        _company(speculative.id),
        _link(event_id, denied.id, role=ROLE_DENIED),
        _link(event_id, speculative.id, role=ROLE_SPECULATIVE),
    )

    result = persist_descriptive_exposures(session, event_id)

    assert result.excluded_nonasserted_count == 2
    assert session.all_of(EventCompany) == []
    assert session.all_of(EventIndustry) == []


def test_projection_is_idempotent_merges_confidence_and_never_rewrites_scores() -> None:
    event_id = uuid.uuid4()
    entity = profile("Acme Corp")
    company = _company(entity.id)
    link = _link(event_id, entity.id, confidence=0.6)
    existing = EventCompany(
        event_id=event_id,
        company_id=company.id,
        confidence_score=0.7,
        risk_score=88,
    )
    session = FakeSession(entity, company, link, existing)

    persist_descriptive_exposures(session, event_id)
    link.confidence_score = 0.9
    persist_descriptive_exposures(session, event_id)

    assert len(session.all_of(EventCompany)) == 1
    assert len(session.all_of(EventIndustry)) == 1
    assert float(existing.confidence_score) == 0.9
    assert float(existing.risk_score) == 88
    assert existing.exposure_explanation == DIRECT_MENTION_EXPLANATION


def test_unmatched_entity_and_blank_industry_are_reported_without_invention() -> None:
    event_id = uuid.uuid4()
    unmatched = profile("No Company Master")
    matched = profile("No Industry Corp")
    session = FakeSession(
        unmatched,
        matched,
        _company(matched.id, industry=" -- "),
        _link(event_id, unmatched.id),
        _link(event_id, matched.id),
    )

    result = persist_descriptive_exposures(session, event_id)

    assert result.asserted_entity_count == 2
    assert result.unmatched_entity_count == 1
    assert result.company_count == 1
    assert result.industry_count == 0
    assert len(session.all_of(EventCompany)) == 1
    assert session.all_of(EventIndustry) == []


def test_industry_normalization_is_stable() -> None:
    assert normalize_industry_id(" Software—Infrastructure / Cloud ") == (
        "software-infrastructure-cloud"
    )
    assert normalize_industry_id(None) is None
    assert normalize_industry_id("...") is None
