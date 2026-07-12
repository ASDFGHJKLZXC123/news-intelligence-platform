"""Unit tests for deterministic entity resolution."""

from __future__ import annotations

import uuid
from typing import Any

from db.models import (
    EntityProfile,
    EntityRelationship,
    EntityResolutionRun,
    SanctionsAlias,
    SanctionsEntity,
    SanctionsIdentifier,
    SECCompany,
)
from services.entities import AUTO_ACCEPTED, LIKELY_MATCH, REVIEW_REQUIRED, resolve_entity
from services.provider_data.common import normalize_name


class FakeSession:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def add(self, obj: Any) -> None:
        self.items.append(obj)

    def find_one(self, model: type[Any], **criteria: Any) -> Any | None:
        for item in self.items:
            if not isinstance(item, model):
                continue
            if all(getattr(item, key) == value for key, value in criteria.items()):
                return item
        return None

    def all_of(self, model: type[Any]) -> list[Any]:
        return [item for item in self.items if isinstance(item, model)]


def test_sec_company_resolves_by_cik_and_returns_gleif_relationship_context() -> None:
    session = FakeSession()
    company = SECCompany(
        id=uuid.uuid4(),
        cik="0000320193",
        name="Example Public Company",
        ticker="EXM",
    )
    profile = _profile(
        "Example Public Company",
        primary_cik="0000320193",
        primary_ticker="EXM",
        primary_lei="5493001KJTIIGC8Y1R12",
    )
    parent = _profile("Example Parent Holdings", primary_lei="54930084UKLVMY22DS16")
    relationship = EntityRelationship(
        id=uuid.uuid4(),
        parent_entity_id=parent.id,
        child_entity_id=profile.id,
        relationship_type="IS_DIRECTLY_CONSOLIDATED_BY",
        provider="gleif",
        confidence_score=1.0,
    )
    for item in (company, profile, parent, relationship):
        session.add(item)

    result = resolve_entity(session, target_type="sec_company", target_id="320193")
    repeat = resolve_entity(session, target_type="sec_company", target_id="0000320193")

    assert result.matched_entity == profile
    assert result.confidence_score == 1.0
    assert result.confidence_band == AUTO_ACCEPTED
    assert result.match_method == "identifier:cik"
    assert result.should_attach
    assert result.related_entities[0].entity_profile == parent
    assert result.related_entities[0].direction == "parent"
    assert repeat.run_key == result.run_key
    assert len(session.all_of(EntityResolutionRun)) == 1


def test_sanctions_entity_resolves_to_profile_by_lei_identifier() -> None:
    session = FakeSession()
    lei = "5493001KJTIIGC8Y1R12"
    profile = _profile("Example Financial Corp", primary_lei=lei)
    sanctions_entity = _sanctions_entity("1001", "Blocked Example Holdings")
    sanctions_identifier = SanctionsIdentifier(
        id=uuid.uuid4(),
        sanctions_entity_id=sanctions_entity.id,
        identifier_type="Legal Entity Identifier",
        identifier_value=lei,
    )
    for item in (profile, sanctions_entity, sanctions_identifier):
        session.add(item)

    result = resolve_entity(
        session,
        target_type="sanctions_entity",
        target_id=sanctions_entity.id,
    )

    assert result.matched_entity == profile
    assert result.confidence_band == AUTO_ACCEPTED
    assert result.match_method == "identifier:lei"
    assert result.sanctions_matches[0].match_method == "sanctions_identifier:lei"
    assert result.sanctions_matches[0].sanctions_entity == sanctions_entity


def test_news_name_resolves_through_sanctions_alias_and_identifier_bridge() -> None:
    session = FakeSession()
    lei = "5493001KJTIIGC8Y1R12"
    profile = _profile("Example Financial Corporation", primary_lei=lei)
    sanctions_entity = _sanctions_entity("1001", "Blocked Example Holdings")
    sanctions_alias = SanctionsAlias(
        id=uuid.uuid4(),
        sanctions_entity_id=sanctions_entity.id,
        alias_name="Example Finance",
        normalized_alias=normalize_name("Example Finance"),
        alias_type="aka",
        quality="strong",
    )
    sanctions_identifier = SanctionsIdentifier(
        id=uuid.uuid4(),
        sanctions_entity_id=sanctions_entity.id,
        identifier_type="LEI",
        identifier_value=lei,
    )
    for item in (profile, sanctions_entity, sanctions_alias, sanctions_identifier):
        session.add(item)

    result = resolve_entity(
        session,
        target_type="news_entity",
        target_id="article-1:org-1",
        names=["Example Finance"],
    )

    assert result.matched_entity == profile
    assert result.confidence_band == LIKELY_MATCH
    assert result.confidence_score == 0.88
    assert result.match_method == "sanctions_bridge:identifier:lei"
    assert result.sanctions_matches[0].match_method == "sanctions_alias"
    assert result.should_attach


def test_ambiguous_exact_name_match_requires_review_and_does_not_attach() -> None:
    session = FakeSession()
    first = _profile("Example Group")
    second = _profile("Example Group")
    session.add(first)
    session.add(second)

    result = resolve_entity(
        session,
        target_type="news_entity",
        target_id="article-2:org-1",
        names=["Example Group"],
    )

    assert result.matched_entity is None
    assert result.confidence_band == REVIEW_REQUIRED
    assert result.confidence_score == 0.9
    assert not result.should_attach
    assert {candidate.entity_profile for candidate in result.candidates} == {first, second}
    run = session.all_of(EntityResolutionRun)[0]
    assert run.matched_entity_id is None


def _profile(
    name: str,
    *,
    primary_cik: str | None = None,
    primary_ticker: str | None = None,
    primary_lei: str | None = None,
) -> EntityProfile:
    return EntityProfile(
        id=uuid.uuid4(),
        canonical_name=name,
        normalized_name=normalize_name(name),
        primary_cik=primary_cik,
        primary_ticker=primary_ticker,
        primary_lei=primary_lei,
    )


def _sanctions_entity(entity_uid: str, name: str) -> SanctionsEntity:
    return SanctionsEntity(
        id=uuid.uuid4(),
        provider="ofac",
        list_code="SDN",
        entity_uid=entity_uid,
        primary_name=name,
        normalized_name=normalize_name(name),
    )
