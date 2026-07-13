"""Unit tests for ADR 0006 item 2A: bounded, precedence-safe GLEIF enrichment."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from db.models import (
    EntityAlias,
    EntityIdentifier,
    EntityProfile,
    EntityRelationship,
    RawIngestionItem,
)
from packages.providers.base import EntityIdentityProvider, LEIRecord, LEIRelationship
from packages.providers.fakes import FakeSECCompanyTickerProvider
from services.provider_data import (
    ingest_entity_identity_records,
    ingest_sec_company_tickers,
)
from services.provider_data.common import normalize_alias
from tests.unit.test_provider_data_services import FakeSession

APPLE_LEI = "HWUPKR0MPOU8FGXBT394"
DIRECT_PARENT_LEI = "213800D1EI4B9WTWWD28"
ULTIMATE_PARENT_LEI = "5493001KJTIIGC8Y1R12"
START_AT = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class RecordingGLEIFProvider(EntityIdentityProvider):
    """A GLEIF fake that records its search inputs and answers per exact query string."""

    def __init__(
        self,
        results: dict[str, list[LEIRecord]] | None = None,
        relationships: list[LEIRelationship] | None = None,
    ) -> None:
        self._results = results or {}
        self._relationships = relationships or []
        self.queries: list[str] = []

    def search_records(
        self, query: str, *, country_code: str | None = None, limit: int = 20
    ) -> list[LEIRecord]:
        self.queries.append(query)
        records = self._results.get(query, [])
        if country_code is not None:
            records = [record for record in records if record.country_code == country_code]
        return records[:limit]

    def fetch_relationships(
        self, lei: str, *, relationship_type: str | None = None, limit: int = 100
    ) -> list[LEIRelationship]:
        return [item for item in self._relationships if item.lei == lei][:limit]


def _record(lei: str, legal_name: str, *, country_code: str = "US") -> LEIRecord:
    return LEIRecord(
        lei=lei,
        legal_name=legal_name,
        entity_status="ACTIVE",
        registration_status="ISSUED",
        country_code=country_code,
        jurisdiction="US-DE",
        legal_form="Corporation",
        last_updated_at=datetime.datetime(2026, 1, 5, 9, 0, tzinfo=datetime.UTC),
        source_refs=("gleif:lei-records",),
        evidence_refs=(f"gleif:lei:{lei}",),
    )


def _relationship(lei: str, related_lei: str, relationship_type: str) -> LEIRelationship:
    return LEIRelationship(
        relationship_id=f"{lei}:{related_lei}:{relationship_type}",
        lei=lei,
        related_lei=related_lei,
        relationship_type=relationship_type,
        status="ACTIVE",
        start_at=START_AT,
    )


def _seeded_session() -> FakeSession:
    """A session carrying the ADR 0006 item-1 SEC seed (Apple Inc. + Alphabet Inc.)."""

    session = FakeSession()
    ingest_sec_company_tickers(session, FakeSECCompanyTickerProvider())
    return session


def _profile(session: FakeSession, canonical_name: str) -> Any:
    matches = [
        item for item in session.all_of(EntityProfile) if item.canonical_name == canonical_name
    ]
    assert len(matches) == 1
    return matches[0]


# --- Bounded search inputs ------------------------------------------------------------
def test_search_inputs_are_bounded_to_watchlist_and_seeded_profiles() -> None:
    session = _seeded_session()
    provider = RecordingGLEIFProvider(
        results={
            "Apple Inc.": [
                _record(APPLE_LEI, "Apple Inc."),
                # GLEIF search is fuzzy: an unrelated hit must never become an entity.
                _record("529900W18LQJJN6SJ336", "Apple Bank For Savings"),
            ],
            "Example Financial Holdings Inc.": [
                _record(ULTIMATE_PARENT_LEI, "Example Financial Holdings Inc.")
            ],
        }
    )

    result = ingest_entity_identity_records(
        session,
        provider,
        curated_watchlist=["Example Financial Holdings Inc.", "   ", ""],
    )

    assert sorted(provider.queries) == [
        "Alphabet Inc.",
        "Apple Inc.",
        "Example Financial Holdings Inc.",
    ]
    assert result.details["records_unmatched"] == 1
    canonical_names = {item.canonical_name for item in session.all_of(EntityProfile)}
    assert "Apple Bank For Savings" not in canonical_names
    # The curated name is the only way a new entity enters the store.
    assert canonical_names == {"Apple Inc.", "Alphabet Inc.", "Example Financial Holdings Inc."}
    assert result.inserted == 1


def test_callers_cannot_pass_arbitrary_queries() -> None:
    with pytest.raises(TypeError):
        ingest_entity_identity_records(
            FakeSession(),
            RecordingGLEIFProvider(),
            queries=["anything"],  # type: ignore[call-arg]
        )


# --- Precedence ----------------------------------------------------------------------
def test_gleif_never_overwrites_sec_owned_name_cik_or_ticker() -> None:
    session = _seeded_session()
    provider = RecordingGLEIFProvider(results={"Apple Inc.": [_record(APPLE_LEI, "APPLE INC.")]})

    ingest_entity_identity_records(session, provider)

    profile = _profile(session, "Apple Inc.")
    assert profile.canonical_name == "Apple Inc."
    assert profile.primary_cik == "0000320193"
    assert profile.primary_ticker == "AAPL"
    sources = profile.profile_metadata["identity_sources"]
    assert sources["canonical_name"] == "sec-edgar"
    assert sources["primary_cik"] == "sec-edgar"
    assert sources["primary_ticker"] == "sec-edgar"


def test_gleif_fills_missing_fields_and_claims_only_those() -> None:
    session = _seeded_session()
    provider = RecordingGLEIFProvider(
        results={"Apple Inc.": [_record(APPLE_LEI, "Apple Inc.", country_code="US")]}
    )

    result = ingest_entity_identity_records(session, provider)

    profile = _profile(session, "Apple Inc.")
    assert profile.primary_lei == APPLE_LEI
    assert profile.country == "US"
    sources = profile.profile_metadata["identity_sources"]
    assert sources["primary_lei"] == "gleif"
    assert sources["country"] == "gleif"
    gleif_metadata = profile.profile_metadata["gleif"]
    assert gleif_metadata["legal_form"] == "Corporation"
    assert gleif_metadata["precedence"] == 2
    # The item-1 SEC provenance block survives the merge.
    assert profile.profile_metadata["source"]["dataset"] == "company_tickers.json"
    assert result.details["profiles_enriched"] == 1
    assert result.details["identifiers_inserted"] == 1

    identifier = session.all_of(EntityIdentifier)[-1]
    assert (identifier.identifier_type, identifier.provider) == ("lei", "gleif")
    assert identifier.identifier_value == APPLE_LEI


def test_legal_name_alias_is_merged_under_the_gleif_source() -> None:
    session = _seeded_session()
    # A different surface form of the same normalized alias key as the SEC title.
    provider = RecordingGLEIFProvider(results={"Apple Inc.": [_record(APPLE_LEI, "APPLE INC")]})

    ingest_entity_identity_records(session, provider)

    profile = _profile(session, "Apple Inc.")
    aliases = [item for item in session.all_of(EntityAlias) if item.entity_id == profile.id]
    gleif_aliases = [item for item in aliases if item.source == "gleif"]
    assert len(gleif_aliases) == 1
    alias = gleif_aliases[0]
    assert alias.alias == "APPLE INC"
    assert alias.normalized_alias == normalize_alias("Apple Inc.") == "apple"
    assert alias.alias_type == "legal_name"
    # GLEIF dates no name interval, so the current legal name is stored open-ended.
    assert (alias.valid_from, alias.valid_to) == (None, None)
    # The SEC-sourced aliases keep the same normalized key: alias sources union, never overwrite.
    assert {item.source for item in aliases} == {"sec-edgar", "gleif"}


def test_a_second_lei_cannot_steal_a_profile_already_bound_to_one() -> None:
    """Apple Inc. and Apple Ltd share the alias key "apple"; the bound LEI must stand."""

    session = _seeded_session()
    provider = RecordingGLEIFProvider(
        results={
            "Apple Inc.": [
                _record(APPLE_LEI, "Apple Inc."),
                _record("529900W18LQJJN6SJ336", "Apple Ltd", country_code="GB"),
            ]
        }
    )

    result = ingest_entity_identity_records(session, provider)

    profile = _profile(session, "Apple Inc.")
    assert profile.primary_lei == APPLE_LEI
    assert profile.country == "US"
    assert result.details["records_ambiguous"] == 1
    # The loser mints no profile, no identifier, and no alias of its own.
    assert len(session.all_of(EntityProfile)) == 2
    assert [item.identifier_value for item in session.all_of(EntityIdentifier)].count(
        "529900W18LQJJN6SJ336"
    ) == 0


# --- Level-2 relationships ------------------------------------------------------------
def test_direct_and_ultimate_parents_normalize_to_parent_of_with_correct_direction() -> None:
    session = _seeded_session()
    provider = RecordingGLEIFProvider(
        results={
            "Apple Inc.": [_record(APPLE_LEI, "Apple Inc.")],
            "Example Holdings": [_record(DIRECT_PARENT_LEI, "Example Holdings Ltd.")],
            "Example Ultimate Group": [
                _record(ULTIMATE_PARENT_LEI, "Example Ultimate Group AG", country_code="DE")
            ],
        },
        relationships=[
            _relationship(APPLE_LEI, DIRECT_PARENT_LEI, "DIRECT_PARENT"),
            # The equivalent Level-2 spelling for the ultimate parent.
            _relationship(APPLE_LEI, ULTIMATE_PARENT_LEI, "IS_ULTIMATELY_CONSOLIDATED_BY"),
        ],
    )

    result = ingest_entity_identity_records(
        session,
        provider,
        curated_watchlist=["Example Holdings", "Example Ultimate Group"],
    )

    child = _profile(session, "Apple Inc.")
    direct_parent = _profile(session, "Example Holdings Ltd.")
    ultimate_parent = _profile(session, "Example Ultimate Group AG")
    relationships = session.all_of(EntityRelationship)

    assert result.details["relationships_inserted"] == 2
    assert {item.relationship_type for item in relationships} == {"parent_of"}
    by_parent = {item.parent_entity_id: item for item in relationships}
    assert by_parent[direct_parent.id].relationship_metadata["parent_levels"] == ["direct"]
    assert by_parent[direct_parent.id].child_entity_id == child.id
    assert by_parent[ultimate_parent.id].relationship_metadata["parent_levels"] == ["ultimate"]
    assert by_parent[ultimate_parent.id].child_entity_id == child.id
    assert by_parent[direct_parent.id].valid_from == START_AT.date()
    assert by_parent[ultimate_parent.id].valid_to is None


def test_one_parent_that_is_both_direct_and_ultimate_merges_into_a_single_edge() -> None:
    """GLEIF reports both levels separately; the unique key admits one row, so they merge."""

    session = _seeded_session()
    provider = RecordingGLEIFProvider(
        results={
            "Apple Inc.": [_record(APPLE_LEI, "Apple Inc.")],
            "Example Holdings": [_record(DIRECT_PARENT_LEI, "Example Holdings Ltd.")],
        },
        relationships=[
            _relationship(APPLE_LEI, DIRECT_PARENT_LEI, "IS_DIRECTLY_CONSOLIDATED_BY"),
            _relationship(APPLE_LEI, DIRECT_PARENT_LEI, "IS_ULTIMATELY_CONSOLIDATED_BY"),
        ],
    )

    result = ingest_entity_identity_records(
        session, provider, curated_watchlist=["Example Holdings"]
    )

    relationships = session.all_of(EntityRelationship)
    assert result.details["relationships_inserted"] == 1
    assert len(relationships) == 1
    edge = relationships[0]
    assert edge.parent_entity_id == _profile(session, "Example Holdings Ltd.").id
    assert edge.child_entity_id == _profile(session, "Apple Inc.").id
    # Neither level is lost to the other's write.
    assert edge.relationship_metadata["parent_levels"] == ["direct", "ultimate"]
    assert edge.relationship_metadata["source_relationship_types"] == [
        "IS_DIRECTLY_CONSOLIDATED_BY",
        "IS_ULTIMATELY_CONSOLIDATED_BY",
    ]
    assert edge.valid_from == START_AT.date()


def test_relationship_to_unresolved_lei_is_skipped_without_placeholder_profiles() -> None:
    session = _seeded_session()
    provider = RecordingGLEIFProvider(
        results={"Apple Inc.": [_record(APPLE_LEI, "Apple Inc.")]},
        relationships=[_relationship(APPLE_LEI, DIRECT_PARENT_LEI, "DIRECT_PARENT")],
    )

    result = ingest_entity_identity_records(session, provider)

    assert result.details["relationships_inserted"] == 0
    assert result.details["relationships_skipped"] == 1
    assert session.all_of(EntityRelationship) == []
    # Two SEC-seeded profiles and no anonymous LEI placeholder for the unknown parent.
    assert len(session.all_of(EntityProfile)) == 2
    assert session.find_one(EntityProfile, primary_lei=DIRECT_PARENT_LEI) is None


# --- Raw retention -------------------------------------------------------------------
def test_every_fetched_record_is_retained_raw_against_the_provider_run() -> None:
    session = _seeded_session()
    run_id = uuid.uuid4()
    provider = RecordingGLEIFProvider(
        results={
            "Apple Inc.": [
                _record(APPLE_LEI, "Apple Inc."),
                # Retained even though it is out of bounds and never becomes an entity.
                _record("529900W18LQJJN6SJ336", "Apple Bank For Savings"),
            ]
        },
        relationships=[_relationship(APPLE_LEI, DIRECT_PARENT_LEI, "DIRECT_PARENT")],
    )

    result = ingest_entity_identity_records(session, provider, provider_run_id=run_id)

    raw_items = [item for item in session.all_of(RawIngestionItem) if item.provider == "gleif"]
    assert result.details["raw_inserted"] == 3
    assert len(raw_items) == 3
    assert {item.provider_run_id for item in raw_items} == {run_id}
    by_type: dict[str, list[Any]] = {}
    for item in raw_items:
        by_type.setdefault(item.item_type, []).append(item)
    assert sorted(item.external_id for item in by_type["lei_record"]) == [
        "529900W18LQJJN6SJ336",
        APPLE_LEI,
    ]
    # The relationship is retained raw even though it resolves to no edge.
    assert result.details["relationships_skipped"] == 1
    [raw_relationship] = by_type["lei_relationship"]
    assert raw_relationship.payload["related_lei"] == DIRECT_PARENT_LEI
    assert raw_relationship.payload["relationship_type"] == "DIRECT_PARENT"
    assert all(item.idempotency_key.startswith("gleif:") for item in raw_items)


# --- Idempotency ---------------------------------------------------------------------
def test_repeat_run_is_idempotent() -> None:
    session = _seeded_session()
    provider = RecordingGLEIFProvider(
        results={
            "Apple Inc.": [_record(APPLE_LEI, "Apple Inc.")],
            "Example Holdings": [_record(DIRECT_PARENT_LEI, "Example Holdings Ltd.")],
        },
        relationships=[_relationship(APPLE_LEI, DIRECT_PARENT_LEI, "DIRECT_PARENT")],
    )
    watchlist = ["Example Holdings"]

    first = ingest_entity_identity_records(session, provider, curated_watchlist=watchlist)
    counts = {
        model: len(session.all_of(model))
        for model in (EntityProfile, EntityIdentifier, EntityAlias, EntityRelationship)
    }
    second = ingest_entity_identity_records(session, provider, curated_watchlist=watchlist)

    assert first.inserted == 1
    assert first.details["relationships_inserted"] == 1
    assert second.inserted == 0
    assert second.details["identifiers_inserted"] == 0
    assert second.details["aliases_inserted"] == 0
    assert second.details["relationships_inserted"] == 0
    assert second.details["raw_inserted"] == 0
    assert counts == {
        model: len(session.all_of(model))
        for model in (EntityProfile, EntityIdentifier, EntityAlias, EntityRelationship)
    }
