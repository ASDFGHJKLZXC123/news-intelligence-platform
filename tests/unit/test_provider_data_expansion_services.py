"""Unit tests for DB-free provider-data expansion ingestion services."""

from __future__ import annotations

from typing import Any

from db.models import (
    CountryIndicatorObservation,
    CountryIndicatorSeries,
    EnergyMarketSnapshot,
    EntityIdentifier,
    EntityProfile,
    EntityRelationship,
    GeoIncident,
    HumanitarianReport,
    RawIngestionItem,
    SanctionsAlias,
    SanctionsEntity,
    SanctionsIdentifier,
    SanctionsList,
)
from packages.providers.fakes import (
    FakeCountryIndicatorProvider,
    FakeEnergyProvider,
    FakeEntityIdentityProvider,
    FakeGeoIncidentProvider,
    FakeHumanitarianProvider,
    FakeSanctionsProvider,
)
from services.provider_data import (
    ingest_country_indicators,
    ingest_energy_series,
    ingest_entity_identity_records,
    ingest_geo_incidents,
    ingest_humanitarian_reports,
    ingest_sanctions_entities,
)


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


def test_sanctions_ingestion_upserts_entities_aliases_identifiers_and_raw_items() -> None:
    session = FakeSession()
    provider = FakeSanctionsProvider()

    first = ingest_sanctions_entities(session, provider, program="CYBER2")
    second = ingest_sanctions_entities(session, provider, program="CYBER2")

    assert first.fetched == 1
    assert first.inserted == 1
    assert first.details["aliases_inserted"] == 1
    assert first.details["identifiers_inserted"] == 1
    assert second.inserted == 0
    assert second.skipped == 1
    assert len(session.all_of(SanctionsList)) == 1
    assert len(session.all_of(SanctionsEntity)) == 1
    assert len(session.all_of(SanctionsAlias)) == 1
    assert len(session.all_of(SanctionsIdentifier)) == 1
    assert len(session.all_of(RawIngestionItem)) == 1
    entity = session.all_of(SanctionsEntity)[0]
    assert entity.normalized_name == "example sanctioned entity"
    assert entity.programs == ["CYBER2"]


def test_entity_identity_ingestion_upserts_profiles_identifiers_relationships_and_raw() -> None:
    session = FakeSession()
    provider = FakeEntityIdentityProvider()

    first = ingest_entity_identity_records(session, provider, queries=["Example Financial"])
    second = ingest_entity_identity_records(session, provider, queries=["Example Financial"])

    assert first.fetched == 1
    assert first.inserted == 1
    assert first.details["identifiers_inserted"] == 1
    assert first.details["relationships_inserted"] == 1
    assert second.inserted == 0
    assert len(session.all_of(EntityProfile)) == 2  # record profile + related LEI placeholder
    assert len(session.all_of(EntityIdentifier)) == 1
    assert len(session.all_of(EntityRelationship)) == 1
    assert len(session.all_of(RawIngestionItem)) == 2


def test_country_indicator_ingestion_upserts_series_observations_and_raw() -> None:
    session = FakeSession()
    provider = FakeCountryIndicatorProvider()

    first = ingest_country_indicators(
        session,
        provider,
        country_codes=["USA"],
        indicator_ids=["NY.GDP.MKTP.CD"],
        start_year=2025,
        limit=1,
    )
    second = ingest_country_indicators(
        session,
        provider,
        country_codes=["USA"],
        indicator_ids=["NY.GDP.MKTP.CD"],
        start_year=2025,
        limit=1,
    )

    assert first.fetched == 1
    assert first.inserted == 1
    assert first.details["series_inserted"] == 1
    assert second.inserted == 0
    assert second.skipped == 1
    assert len(session.all_of(CountryIndicatorSeries)) == 1
    assert len(session.all_of(CountryIndicatorObservation)) == 1
    assert len(session.all_of(RawIngestionItem)) == 1


def test_humanitarian_geo_and_energy_ingestion_are_idempotent() -> None:
    session = FakeSession()

    humanitarian = ingest_humanitarian_reports(
        session,
        FakeHumanitarianProvider(),
        query="flood",
        country_code="USA",
    )
    geo = ingest_geo_incidents(session, FakeGeoIncidentProvider(), region="US")
    energy = ingest_energy_series(session, FakeEnergyProvider(), series_ids=["PET.WCRSTUS1.W"])

    repeat_humanitarian = ingest_humanitarian_reports(
        session,
        FakeHumanitarianProvider(),
        query="flood",
        country_code="USA",
    )
    repeat_geo = ingest_geo_incidents(session, FakeGeoIncidentProvider(), region="US")
    repeat_energy = ingest_energy_series(session, FakeEnergyProvider(), series_ids=["PET.WCRSTUS1.W"])

    assert humanitarian.inserted == 1
    assert repeat_humanitarian.inserted == 0
    assert geo.inserted == 1
    assert repeat_geo.inserted == 0
    assert energy.inserted == 3
    assert repeat_energy.inserted == 0
    assert len(session.all_of(HumanitarianReport)) == 1
    assert len(session.all_of(GeoIncident)) == 1
    assert len(session.all_of(EnergyMarketSnapshot)) == 3
    assert len(session.all_of(RawIngestionItem)) == 5
