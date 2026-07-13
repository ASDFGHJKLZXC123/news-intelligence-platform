"""Unit tests for DB-free provider-data service ingestion."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from db.models import (
    MacroObservation,
    MacroSeries,
    RawIngestionItem,
    SECCompany,
    SECCompanyFact,
    SECFiling,
)
from packages.providers.base import FREDObservation, SECSubmission
from packages.providers.base import SECCompanyFact as SECCompanyFactDTO
from packages.providers.fakes import FakeFREDProvider, FakeGDELTProvider, FakeSECEdgarProvider
from services.provider_data import (
    ingest_fred_series,
    ingest_gdelt_raw_items,
    ingest_sec_companies,
)


class FakeSession:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def add(self, obj: Any) -> None:
        self.items.append(obj)

    def find_one(self, model: type[Any], **criteria: Any) -> Any | None:
        for item in self.find_all(model, **criteria):
            return item
        return None

    def find_all(self, model: type[Any], **criteria: Any) -> list[Any]:
        return [
            item
            for item in self.all_of(model)
            if all(getattr(item, key) == value for key, value in criteria.items())
        ]

    def all_of(self, model: type[Any]) -> list[Any]:
        return [item for item in self.items if isinstance(item, model)]


def test_gdelt_ingestion_retains_raw_articles_events_and_reruns_idempotently() -> None:
    session = FakeSession()
    provider = FakeGDELTProvider()
    run_id = uuid.uuid4()

    first = ingest_gdelt_raw_items(
        session,
        provider,
        query="bank stress",
        provider_run_id=run_id,
    )
    second = ingest_gdelt_raw_items(
        session,
        provider,
        query="bank stress",
        provider_run_id=run_id,
    )

    assert first.fetched == 3
    assert first.inserted == 3
    assert first.skipped == 0
    assert second["fetched"] == 3
    assert second["inserted"] == 0
    assert second["skipped"] == 3
    raw_items = session.all_of(RawIngestionItem)
    assert len(raw_items) == 3
    assert {item.item_type for item in raw_items} == {"article", "event"}
    assert {item.provider for item in raw_items} == {"gdelt"}
    assert all(item.provider_run_id == run_id for item in raw_items)
    assert all(len(item.payload_hash) == 64 for item in raw_items)
    assert raw_items[0].payload["provider_name"] == "fake-provider"


def test_fred_ingestion_upserts_series_observations_raw_payloads_and_reruns() -> None:
    session = FakeSession()
    observed_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    observation = FREDObservation(
        series_id="GDP",
        observed_at=observed_at,
        value=123.45,
        realtime_start=datetime.date(2026, 1, 2),
        realtime_end=datetime.date(2026, 1, 3),
        metadata={
            "country": "US",
            "frequency": "Quarterly",
            "title": "Gross Domestic Product",
            "units": "Billions of Dollars",
            "value": "123.45",
        },
    )
    provider = FakeFREDProvider({"GDP": [observation]})

    first = ingest_fred_series(session, provider, series_ids=["GDP"])
    second = ingest_fred_series(session, provider, series_ids=["GDP"])

    assert first.as_dict()["fetched"] == 1
    assert first.inserted == 1
    assert first.skipped == 0
    assert first.details["series_inserted"] == 1
    assert second.fetched == 1
    assert second.inserted == 0
    assert second.skipped == 1
    assert len(session.all_of(MacroSeries)) == 1
    series = session.all_of(MacroSeries)[0]
    assert series.provider == "fred"
    assert series.series_id == "GDP"
    assert series.title == "Gross Domestic Product"
    assert series.country == "US"
    assert len(session.all_of(MacroObservation)) == 1
    persisted = session.all_of(MacroObservation)[0]
    assert persisted.series_uuid == series.id
    assert persisted.observed_on == datetime.date(2026, 1, 1)
    assert persisted.raw_value == "123.45"
    assert persisted.observation_metadata["schema_version"] == "fred-observation.v1"
    assert len(session.all_of(RawIngestionItem)) == 1


def test_sec_ingestion_upserts_company_filings_facts_raw_payloads_and_reruns() -> None:
    session = FakeSession()
    filed_at = datetime.datetime(2026, 2, 3, tzinfo=datetime.UTC)
    period_end_at = datetime.datetime(2025, 12, 31, tzinfo=datetime.UTC)
    submission = SECSubmission(
        cik="0000320193",
        accession_number="0000320193-26-000001",
        form="10-K",
        filed_at=filed_at,
        report_at=period_end_at,
        company_name="Example Public Company",
        primary_document="example-10k.htm",
        metadata={"ticker": "EXM", "exchange": "NYSE", "sic": "3571"},
    )
    fact = SECCompanyFactDTO(
        cik="0000320193",
        taxonomy="us-gaap",
        concept="Assets",
        unit="USD",
        value="1000",
        accession_number="0000320193-26-000001",
        filed_at=filed_at,
        period_end_at=period_end_at,
        form="10-K",
        fiscal_year=2025,
        fiscal_period="FY",
        metadata={"start": "2025-01-01"},
    )
    provider = FakeSECEdgarProvider(
        submissions_by_cik={"0000320193": [submission]},
        facts_by_cik={"0000320193": [fact]},
    )

    first = ingest_sec_companies(session, provider, ciks=["320193"])
    second = ingest_sec_companies(session, provider, ciks=["0000320193"])

    assert first.fetched == 2
    assert first.inserted == 2
    assert first.skipped == 0
    assert first.details["companies_inserted"] == 1
    assert first.details["raw_inserted"] == 2
    assert second.fetched == 2
    assert second.inserted == 0
    assert second.skipped == 2
    assert len(session.all_of(SECCompany)) == 1
    company = session.all_of(SECCompany)[0]
    assert company.cik == "0000320193"
    assert company.name == "Example Public Company"
    assert company.ticker == "EXM"
    assert len(session.all_of(SECFiling)) == 1
    filing = session.all_of(SECFiling)[0]
    assert filing.company_id == company.id
    assert filing.accession_number == "0000320193-26-000001"
    assert filing.form_type == "10-K"
    assert len(session.all_of(SECCompanyFact)) == 1
    persisted_fact = session.all_of(SECCompanyFact)[0]
    assert persisted_fact.company_id == company.id
    assert persisted_fact.period_start == datetime.date(2025, 1, 1)
    assert persisted_fact.period_end == datetime.date(2025, 12, 31)
    assert persisted_fact.raw_value == "1000"
    assert persisted_fact.value == 1000.0
    assert len(session.all_of(RawIngestionItem)) == 2
