"""Unit tests for ADR 0006 item 2B: bounded, precedence-3 Wikidata identity enrichment."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from db.models import (
    EntityAlias,
    EntityIdentifier,
    EntityProfile,
    EntityRelationship,
    RawIngestionItem,
)
from packages.providers.base import (
    EntityIdentityProvider,
    LEIRecord,
    LEIRelationship,
    WikidataAlias,
    WikidataEntity,
    WikidataItemRef,
    WikidataProvider,
    WikidataQidMatch,
)
from packages.providers.fakes import FakeSECCompanyTickerProvider
from services.provider_data import (
    ingest_entity_identity_records,
    ingest_sec_company_tickers,
    ingest_wikidata_identities,
)
from tests.unit.test_provider_data_services import FakeSession

APPLE_QID = "Q312"
ALPHABET_QID = "Q95"
PARENT_QID = "Q1000"
BRAND_QID = "Q2766"
APPLE_CIK = "0000320193"
ALPHABET_CIK = "0001652044"
APPLE_LEI = "HWUPKR0MPOU8FGXBT394"
GLEIF_LEI = "5493001KJTIIGC8Y1R12"


class RecordingWikidataProvider(WikidataProvider):
    """A Wikidata fake that records exactly which bounded inputs the service asked for."""

    def __init__(
        self,
        entities: Sequence[WikidataEntity] = (),
        qid_matches: Sequence[WikidataQidMatch] = (),
    ) -> None:
        self._entities = {entity.qid: entity for entity in entities}
        self._qid_matches = list(qid_matches)
        self.resolved: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.fetched: list[tuple[str, ...]] = []

    def resolve_qids(
        self,
        *,
        ciks: Sequence[str] = (),
        leis: Sequence[str] = (),
        limit: int = 10_000,
    ) -> list[WikidataQidMatch]:
        self.resolved.append((tuple(ciks), tuple(leis)))
        wanted = {("cik", value) for value in ciks} | {("lei", value) for value in leis}
        return [
            match
            for match in self._qid_matches
            if (match.identifier_type, match.identifier_value) in wanted
        ][:limit]

    def fetch_entities(self, qids: Sequence[str], *, limit: int = 10_000) -> list[WikidataEntity]:
        self.fetched.append(tuple(qids))
        return [self._entities[qid] for qid in qids if qid in self._entities][:limit]


class StubGLEIFProvider(EntityIdentityProvider):
    """Answers the GLEIF service with one record per exact query, so item 2A state is real."""

    def __init__(self, records: dict[str, LEIRecord]) -> None:
        self._records = records

    def search_records(
        self, query: str, *, country_code: str | None = None, limit: int = 20
    ) -> list[LEIRecord]:
        record = self._records.get(query)
        return [record] if record is not None else []

    def fetch_relationships(
        self, lei: str, *, relationship_type: str | None = None, limit: int = 100
    ) -> list[LEIRelationship]:
        return []


def _entity(
    qid: str,
    label: str,
    *,
    aliases: Sequence[WikidataAlias] = (),
    tickers: Sequence[str] = (),
    leis: Sequence[str] = (),
    ciks: Sequence[str] = (),
    parents: Sequence[WikidataItemRef] = (),
    subsidiaries: Sequence[WikidataItemRef] = (),
    industries: Sequence[WikidataItemRef] = (),
) -> WikidataEntity:
    return WikidataEntity(
        qid=qid,
        label=label,
        description=f"{label} description",
        aliases=(WikidataAlias(value=label, alias_type="legal_name"), *aliases),
        tickers=tuple(tickers),
        leis=tuple(leis),
        ciks=tuple(ciks),
        parents=tuple(parents),
        subsidiaries=tuple(subsidiaries),
        industries=tuple(industries),
        source_refs=("wikidata:sparql",),
        evidence_refs=(f"https://www.wikidata.org/wiki/{qid}",),
        metadata={"bindings": [{"item": qid}]},
    )


def _cik_match(qid: str, cik: str) -> WikidataQidMatch:
    return WikidataQidMatch(qid=qid, identifier_type="cik", identifier_value=cik)


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


def _aliases(session: FakeSession, profile: Any, source: str = "wikidata") -> dict[str, Any]:
    return {
        item.alias: item
        for item in session.all_of(EntityAlias)
        if item.entity_id == profile.id and item.source == source
    }


# --- Bounded query inputs -------------------------------------------------------------
def test_the_provider_only_ever_sees_seeded_identifiers_and_curated_qids() -> None:
    session = _seeded_session()
    provider = RecordingWikidataProvider(
        entities=[_entity(APPLE_QID, "Apple Inc."), _entity(PARENT_QID, "Bosch GmbH")],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )

    result = ingest_wikidata_identities(session, provider, curated_qids=[PARENT_QID])

    # Resolution is keyed on the CIKs the SEC seed wrote, never on a name or a free-text search.
    assert provider.resolved == [((APPLE_CIK, ALPHABET_CIK), ())]
    # Only the QID the CIK resolved to plus the curated QID are ever fetched.
    assert provider.fetched == [(PARENT_QID, APPLE_QID)]
    assert result.details["qids_requested"] == 2


def test_query_inputs_are_batched_so_every_request_stays_bounded() -> None:
    session = _seeded_session()
    provider = RecordingWikidataProvider(
        entities=[_entity(APPLE_QID, "Apple Inc."), _entity(ALPHABET_QID, "Alphabet Inc.")],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK), _cik_match(ALPHABET_QID, ALPHABET_CIK)],
    )

    ingest_wikidata_identities(session, provider, batch_size=1)

    assert provider.resolved == [((APPLE_CIK,), ()), ((ALPHABET_CIK,), ())]
    assert provider.fetched == [(APPLE_QID,), (ALPHABET_QID,)]


def test_a_curated_qid_that_is_not_a_qid_is_rejected() -> None:
    with pytest.raises(ValueError, match="QID"):
        ingest_wikidata_identities(
            FakeSession(), RecordingWikidataProvider(), curated_qids=["Apple Inc."]
        )


def test_an_unrequested_entity_never_becomes_a_profile() -> None:
    session = _seeded_session()
    # The provider answers with an item nobody resolved or curated.
    provider = RecordingWikidataProvider(entities=[_entity(PARENT_QID, "Rogue Holdings")])
    provider._entities[APPLE_QID] = _entity(APPLE_QID, "Apple Inc.")  # noqa: SLF001
    provider._qid_matches.append(_cik_match(APPLE_QID, APPLE_CIK))  # noqa: SLF001
    provider._entities["Q999"] = _entity("Q999", "Unasked Corp")  # noqa: SLF001

    result = ingest_wikidata_identities(session, provider)

    names = {item.canonical_name for item in session.all_of(EntityProfile)}
    assert names == {"Apple Inc.", "Alphabet Inc."}
    assert result.inserted == 0
    assert result.details["entities_unmatched"] == 0


# --- Precedence -----------------------------------------------------------------------
def test_wikidata_never_overwrites_sec_or_gleif_owned_identity_fields() -> None:
    session = _seeded_session()
    ingest_entity_identity_records(
        session,
        StubGLEIFProvider(
            {
                "Apple Inc.": LEIRecord(
                    lei=APPLE_LEI,
                    legal_name="Apple Inc.",
                    country_code="US",
                    entity_status="ACTIVE",
                )
            }
        ),
    )
    provider = RecordingWikidataProvider(
        entities=[
            _entity(
                APPLE_QID,
                "Apple",  # Wikidata's label is the colloquial form; SEC owns the name.
                tickers=("APC",),
                leis=(GLEIF_LEI,),
                ciks=("0000999999",),
            )
        ],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )

    ingest_wikidata_identities(session, provider)

    profile = _profile(session, "Apple Inc.")
    assert profile.canonical_name == "Apple Inc."
    assert profile.primary_cik == APPLE_CIK
    assert profile.primary_ticker == "AAPL"
    assert profile.primary_lei == APPLE_LEI
    sources = profile.profile_metadata["identity_sources"]
    assert sources["canonical_name"] == "sec-edgar"
    assert sources["primary_cik"] == "sec-edgar"
    assert sources["primary_ticker"] == "sec-edgar"
    assert sources["primary_lei"] == "gleif"
    # The rejected values are still recorded as lower-confidence identifiers, never as fields.
    assert profile.profile_metadata["wikidata"]["precedence"] == 3


def test_wikidata_fills_only_the_fields_no_higher_source_claimed() -> None:
    session = _seeded_session()
    apple = _profile(session, "Apple Inc.")
    # SEC seeds no LEI, so precedence 3 may fill it; the SEC-owned name/ticker stay put.
    provider = RecordingWikidataProvider(
        entities=[
            _entity(
                APPLE_QID,
                "Apple",
                leis=(APPLE_LEI,),
                industries=(WikidataItemRef(qid="Q11661", label="IT"),),
            )
        ],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )

    result = ingest_wikidata_identities(session, provider)

    assert apple.primary_lei == APPLE_LEI
    assert apple.canonical_name == "Apple Inc."
    sources = apple.profile_metadata["identity_sources"]
    assert sources["primary_lei"] == "wikidata"
    assert sources["canonical_name"] == "sec-edgar"
    # Industry has no owned column under ADR 0006, so it is retained as provider metadata.
    assert apple.profile_metadata["wikidata"]["industries"] == [{"label": "IT", "qid": "Q11661"}]
    assert result.details["profiles_enriched"] == 1
    assert result.inserted == 0


def test_an_ambiguous_multi_valued_identifier_claims_no_field() -> None:
    session = _seeded_session()
    apple = _profile(session, "Apple Inc.")
    provider = RecordingWikidataProvider(
        entities=[_entity(APPLE_QID, "Apple", leis=(APPLE_LEI, GLEIF_LEI))],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )

    ingest_wikidata_identities(session, provider)

    # Two LEIs on one item is a Wikidata data error; neither may claim the unique column.
    assert apple.primary_lei is None
    assert "primary_lei" not in apple.profile_metadata["identity_sources"]
    values = {item.identifier_value for item in session.all_of(EntityIdentifier)}
    assert {APPLE_LEI, GLEIF_LEI} <= values


def test_a_unique_identifier_bound_to_another_profile_is_never_stolen() -> None:
    session = _seeded_session()
    apple = _profile(session, "Apple Inc.")
    alphabet = _profile(session, "Alphabet Inc.")
    provider = RecordingWikidataProvider(
        # Wikidata claims Alphabet's item carries Apple's CIK; the unique column already holds it.
        entities=[_entity(ALPHABET_QID, "Alphabet", ciks=(APPLE_CIK,))],
        qid_matches=[_cik_match(ALPHABET_QID, ALPHABET_CIK)],
    )

    ingest_wikidata_identities(session, provider)

    assert apple.primary_cik == APPLE_CIK
    assert alphabet.primary_cik == ALPHABET_CIK


# --- Curated watchlist ----------------------------------------------------------------
def test_a_curated_qid_may_create_the_entity_the_registries_never_list() -> None:
    session = _seeded_session()
    provider = RecordingWikidataProvider(entities=[_entity(PARENT_QID, "Robert Bosch GmbH")])

    result = ingest_wikidata_identities(session, provider, curated_qids=[PARENT_QID])

    profile = _profile(session, "Robert Bosch GmbH")
    assert result.inserted == 1
    assert profile.entity_type == "company"
    assert profile.normalized_name == "robert bosch gmbh"
    assert profile.profile_metadata["identity_sources"]["canonical_name"] == "wikidata"
    assert profile.profile_metadata["wikidata"]["qid"] == PARENT_QID


# --- Identifiers and aliases ----------------------------------------------------------
def test_identifiers_land_under_the_wikidata_provider_at_a_lower_confidence() -> None:
    session = _seeded_session()
    apple = _profile(session, "Apple Inc.")
    provider = RecordingWikidataProvider(
        entities=[
            _entity(APPLE_QID, "Apple", tickers=("AAPL",), leis=(APPLE_LEI,), ciks=(APPLE_CIK,))
        ],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )

    result = ingest_wikidata_identities(session, provider)

    rows = {
        (item.identifier_type, item.identifier_value): item
        for item in session.all_of(EntityIdentifier)
        if item.entity_profile_id == apple.id and item.provider == "wikidata"
    }
    assert set(rows) == {
        ("wikidata_qid", APPLE_QID),
        ("ticker", "AAPL"),
        ("lei", APPLE_LEI),
        ("cik", APPLE_CIK),
    }
    assert all(item.confidence_score == 0.75 for item in rows.values())
    assert rows[("wikidata_qid", APPLE_QID)].source_ref["precedence"] == 3
    assert result.details["identifiers_inserted"] == 4
    # The SEC-sourced identifier rows for the same values are untouched: sources union rather
    # than overwrite (2 CIKs + AAPL/GOOGL/GOOG across the two seeded companies).
    sec_rows = [item for item in session.all_of(EntityIdentifier) if item.provider == "sec-edgar"]
    assert len(sec_rows) == 5


def test_aliases_carry_type_source_and_open_or_dated_intervals() -> None:
    session = _seeded_session()
    apple = _profile(session, "Apple Inc.")
    provider = RecordingWikidataProvider(
        entities=[
            _entity(
                APPLE_QID,
                "Apple",
                aliases=(
                    WikidataAlias(value="Apple Computer", alias_type="colloquial"),
                    WikidataAlias(
                        value="Apple Computer, Inc.",
                        alias_type="former_name",
                        valid_from=datetime.date(1977, 1, 3),
                        valid_to=datetime.date(2007, 1, 9),
                    ),
                    WikidataAlias(value="AAPL", alias_type="ticker"),
                ),
            )
        ],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )

    ingest_wikidata_identities(session, provider)

    aliases = _aliases(session, apple)
    assert aliases["Apple"].alias_type == "legal_name"
    assert (aliases["Apple"].valid_from, aliases["Apple"].valid_to) == (None, None)
    assert aliases["Apple"].normalized_alias == "apple"
    # A dated former name keeps its interval rather than reading as currently valid.
    former = aliases["Apple Computer, Inc."]
    assert former.alias_type == "former_name"
    assert (former.valid_from, former.valid_to) == (
        datetime.date(1977, 1, 3),
        datetime.date(2007, 1, 9),
    )
    assert aliases["AAPL"].alias_type == "ticker"
    # The altLabel "Apple Computer" and the former name "Apple Computer, Inc." share a normalized
    # key, and the schema holds one row per (entity, key, source). The dated former name is the
    # more authoritative of the two, so it is the row that survives — the interval is not lost.
    assert "Apple Computer" not in aliases
    assert all(item.source == "wikidata" for item in aliases.values())
    # The SEC aliases for the same entity are a separate, untouched source.
    assert set(_aliases(session, apple, source="sec-edgar")) == {"Apple Inc.", "AAPL"}


def test_a_brand_is_typed_as_a_brand_and_never_aliased_onto_another_legal_entity() -> None:
    session = _seeded_session()
    apple = _profile(session, "Apple Inc.")
    alphabet = _profile(session, "Alphabet Inc.")
    provider = RecordingWikidataProvider(
        entities=[
            _entity(
                APPLE_QID,
                "Apple",
                aliases=(WikidataAlias(value="iPhone", alias_type="brand_product", qid=BRAND_QID),),
            ),
            # Alphabet's "brand" is an item that is itself a tracked entity (Apple), which is a
            # different legal entity: it must not become an alias of Alphabet.
            _entity(
                ALPHABET_QID,
                "Alphabet",
                aliases=(WikidataAlias(value="Apple", alias_type="brand_product", qid=APPLE_QID),),
            ),
        ],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK), _cik_match(ALPHABET_QID, ALPHABET_CIK)],
    )

    result = ingest_wikidata_identities(session, provider)

    brand = _aliases(session, apple)["iPhone"]
    assert brand.alias_type == "brand_product"
    assert brand.normalized_alias == "iphone"
    # A brand is a name the entity trades under, never its canonical name.
    assert apple.canonical_name == "Apple Inc."
    assert result.details["brands_skipped"] == 1
    assert "Apple" not in _aliases(session, alphabet)
    assert alphabet.canonical_name == "Alphabet Inc."


# --- Parent and subsidiary relations --------------------------------------------------
def test_parent_and_subsidiary_properties_normalize_to_parent_of_with_correct_direction() -> None:
    session = _seeded_session()
    provider = RecordingWikidataProvider(
        entities=[
            # P749 on Apple: the related item is Apple's parent.
            _entity(APPLE_QID, "Apple", parents=(WikidataItemRef(qid=PARENT_QID, label="Holdco"),)),
            # P355 on the holding company: the related item is its subsidiary.
            _entity(
                PARENT_QID,
                "Holdco AG",
                subsidiaries=(WikidataItemRef(qid=ALPHABET_QID, label="Alphabet"),),
            ),
            _entity(ALPHABET_QID, "Alphabet"),
        ],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK), _cik_match(ALPHABET_QID, ALPHABET_CIK)],
    )

    result = ingest_wikidata_identities(session, provider, curated_qids=[PARENT_QID])

    apple = _profile(session, "Apple Inc.")
    alphabet = _profile(session, "Alphabet Inc.")
    holdco = _profile(session, "Holdco AG")
    edges = {
        (item.parent_entity_id, item.child_entity_id): item
        for item in session.all_of(EntityRelationship)
    }
    assert result.details["relationships_inserted"] == 2
    assert set(edges) == {(holdco.id, apple.id), (holdco.id, alphabet.id)}
    assert {item.relationship_type for item in edges.values()} == {"parent_of"}
    assert {item.provider for item in edges.values()} == {"wikidata"}
    assert edges[(holdco.id, apple.id)].relationship_metadata["source_properties"] == ["P749"]
    assert edges[(holdco.id, alphabet.id)].relationship_metadata["source_properties"] == ["P355"]


def test_both_spellings_of_one_pair_merge_into_a_single_edge() -> None:
    session = _seeded_session()
    provider = RecordingWikidataProvider(
        entities=[
            _entity(APPLE_QID, "Apple", parents=(WikidataItemRef(qid=PARENT_QID),)),
            _entity(PARENT_QID, "Holdco AG", subsidiaries=(WikidataItemRef(qid=APPLE_QID),)),
        ],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )

    result = ingest_wikidata_identities(session, provider, curated_qids=[PARENT_QID])

    [edge] = session.all_of(EntityRelationship)
    assert result.details["relationships_inserted"] == 1
    assert edge.parent_entity_id == _profile(session, "Holdco AG").id
    assert edge.child_entity_id == _profile(session, "Apple Inc.").id
    # Neither property is lost to the other's write.
    assert edge.relationship_metadata["source_properties"] == ["P355", "P749"]


def test_a_relation_to_an_unresolved_qid_is_skipped_without_a_placeholder_entity() -> None:
    session = _seeded_session()
    provider = RecordingWikidataProvider(
        entities=[
            _entity(
                APPLE_QID,
                "Apple",
                parents=(WikidataItemRef(qid=PARENT_QID, label="Unknown Holdco"),),
                subsidiaries=(WikidataItemRef(qid="Q7777", label="Unknown Sub"),),
            )
        ],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )

    result = ingest_wikidata_identities(session, provider)

    assert result.details["relationships_inserted"] == 0
    assert result.details["relationships_skipped"] == 2
    assert session.all_of(EntityRelationship) == []
    # The two SEC-seeded profiles and nothing else: a related QID never mints an entity.
    assert len(session.all_of(EntityProfile)) == 2
    names = {item.canonical_name for item in session.all_of(EntityProfile)}
    assert "Unknown Holdco" not in names


def test_a_wikidata_edge_never_overwrites_the_gleif_edge_for_the_same_pair() -> None:
    session = _seeded_session()
    gleif_edge_provider = StubGLEIFProvider(
        {
            "Apple Inc.": LEIRecord(lei=APPLE_LEI, legal_name="Apple Inc.", country_code="US"),
            "Holdco": LEIRecord(lei=GLEIF_LEI, legal_name="Holdco AG", country_code="DE"),
        }
    )
    ingest_entity_identity_records(session, gleif_edge_provider, curated_watchlist=["Holdco"])
    holdco = _profile(session, "Holdco AG")
    apple = _profile(session, "Apple Inc.")
    provider = RecordingWikidataProvider(
        entities=[_entity(APPLE_QID, "Apple", parents=(WikidataItemRef(qid=PARENT_QID),))],
        qid_matches=[
            _cik_match(APPLE_QID, APPLE_CIK),
            WikidataQidMatch(qid=PARENT_QID, identifier_type="lei", identifier_value=GLEIF_LEI),
        ],
    )

    ingest_wikidata_identities(session, provider)

    edges = [
        item
        for item in session.all_of(EntityRelationship)
        if (item.parent_entity_id, item.child_entity_id) == (holdco.id, apple.id)
    ]
    # The unique key includes the provider, so the two sources' claims coexist with their own
    # provenance rather than one silently replacing the other.
    assert {item.provider for item in edges} == {"wikidata"}
    assert all(item.confidence_score == 0.75 for item in edges)


# --- Raw retention and idempotency ----------------------------------------------------
def test_every_fetched_payload_is_retained_raw_against_the_provider_run() -> None:
    session = _seeded_session()
    run_id = uuid.uuid4()
    provider = RecordingWikidataProvider(
        entities=[_entity(APPLE_QID, "Apple", ciks=(APPLE_CIK,))],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )

    result = ingest_wikidata_identities(session, provider, provider_run_id=run_id)

    raw_items = [item for item in session.all_of(RawIngestionItem) if item.provider == "wikidata"]
    assert result.details["raw_inserted"] == 2
    assert {item.item_type for item in raw_items} == {"entity", "qid_match"}
    assert {item.provider_run_id for item in raw_items} == {run_id}
    assert all(item.idempotency_key.startswith("wikidata:") for item in raw_items)
    [entity_raw] = [item for item in raw_items if item.item_type == "entity"]
    assert entity_raw.external_id == APPLE_QID
    # The provider's own payload survives verbatim for replay.
    assert entity_raw.payload["metadata"]["bindings"] == [{"item": APPLE_QID}]
    assert entity_raw.payload["label"] == "Apple"


def test_repeat_run_is_idempotent() -> None:
    session = _seeded_session()
    provider = RecordingWikidataProvider(
        entities=[
            _entity(
                APPLE_QID,
                "Apple",
                aliases=(WikidataAlias(value="iPhone", alias_type="brand_product", qid=BRAND_QID),),
                tickers=("AAPL",),
                leis=(APPLE_LEI,),
                parents=(WikidataItemRef(qid=PARENT_QID),),
            ),
            _entity(PARENT_QID, "Holdco AG"),
        ],
        qid_matches=[_cik_match(APPLE_QID, APPLE_CIK)],
    )
    curated = [PARENT_QID]

    first = ingest_wikidata_identities(session, provider, curated_qids=curated)
    models = (EntityProfile, EntityIdentifier, EntityAlias, EntityRelationship, RawIngestionItem)
    counts = {model: len(session.all_of(model)) for model in models}
    second = ingest_wikidata_identities(session, provider, curated_qids=curated)

    assert first.inserted == 1
    assert first.details["relationships_inserted"] == 1
    assert second.inserted == 0
    assert second.details["identifiers_inserted"] == 0
    assert second.details["aliases_inserted"] == 0
    assert second.details["relationships_inserted"] == 0
    assert second.details["relationships_updated"] == 1
    assert second.details["raw_inserted"] == 0
    assert counts == {model: len(session.all_of(model)) for model in models}
    # The second run resolves the QID it already stored, so it re-enriches rather than re-creates.
    assert second.details["profiles_enriched"] == 2
