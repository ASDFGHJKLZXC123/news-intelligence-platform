"""Derived entity API tests with deterministic fake sessions."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient

from apps.api.main import app
from db.base import get_session
from db.models import EntityProfile, EntityRelationship, EntityResolutionRun, SECCompany
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


def _post_with_session(session: FakeSession, payload: dict[str, Any]):
    app.dependency_overrides[get_session] = lambda: session
    try:
        return TestClient(app, client=("127.0.0.1", 5000)).post(
            "/api/v1/entities/resolve",
            json=payload,
        )
    finally:
        app.dependency_overrides.clear()


def test_entity_resolve_endpoint_matches_sec_company_by_cik() -> None:
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

    resp = _post_with_session(
        session,
        {"target_type": "sec_company", "target_id": "320193"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["should_attach"] is True
    assert body["confidence_band"] == "auto_accepted"
    assert body["match_method"] == "identifier:cik"
    assert body["matched_entity"]["primary_cik"] == "0000320193"
    assert body["related_entities"][0]["direction"] == "parent"
    assert body["related_entities"][0]["entity"]["canonical_name"] == "Example Parent Holdings"
    assert len(session.all_of(EntityResolutionRun)) == 1


def test_entity_resolve_endpoint_returns_review_for_ambiguous_names() -> None:
    session = FakeSession()
    session.add(_profile("Example Group"))
    session.add(_profile("Example Group"))

    resp = _post_with_session(
        session,
        {
            "target_type": "news_entity",
            "target_id": "article-1:org-1",
            "names": ["Example Group"],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["should_attach"] is False
    assert body["matched_entity"] is None
    assert body["confidence_band"] == "review_required"
    assert len(body["candidates"]) == 2


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
