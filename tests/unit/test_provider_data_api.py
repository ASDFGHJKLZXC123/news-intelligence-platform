"""Read-only provider-data API tests with dependency overrides."""

from __future__ import annotations

import datetime
import decimal
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.provider_data import get_provider_data_repository

NOW = datetime.datetime(2026, 6, 17, 12, 0, tzinfo=datetime.UTC)
TODAY = datetime.date(2026, 6, 17)


class FakeProviderDataRepository:
    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}
        self.company_id = uuid.uuid4()
        self.sanctions_entity_id = uuid.uuid4()
        self.entity_profile_id = uuid.uuid4()
        self.country_indicator_series_id = uuid.uuid4()
        self.series = SimpleNamespace(
            id=uuid.uuid4(),
            provider="fred",
            series_id="GDP",
            title="Gross Domestic Product",
            frequency="Quarterly",
            units="Billions of Dollars",
            seasonal_adjustment="SAAR",
            country="US",
            source="FRED",
            series_metadata={"source": "test"},
        )
        self.company = SimpleNamespace(
            id=self.company_id,
            cik="0000320193",
            name="Apple Inc.",
            ticker="AAPL",
            exchange="NASDAQ",
            sic="3571",
            sic_description="Electronic Computers",
            fiscal_year_end="0928",
            entity_type="operating",
            company_metadata={"source": "test"},
        )
        self.duplicate_company = SimpleNamespace(
            id=uuid.uuid4(),
            cik="0000000002",
            name="Duplicate Ticker Corp.",
            ticker="DUP",
            exchange="NYSE",
            sic="9999",
            sic_description="Duplicate Test",
            fiscal_year_end="1231",
            entity_type="operating",
            company_metadata={"source": "test"},
        )
        self.duplicate_company_alt = SimpleNamespace(
            id=uuid.uuid4(),
            cik="0000000003",
            name="Duplicate Ticker International.",
            ticker="DUP",
            exchange="TSX",
            sic="9999",
            sic_description="Duplicate Test",
            fiscal_year_end="1231",
            entity_type="operating",
            company_metadata={"source": "test"},
        )
        self.sec_facts = [
            SimpleNamespace(
                id=uuid.uuid4(),
                company_id=self.company_id,
                taxonomy="us-gaap",
                concept="Revenues",
                unit="USD",
                period_start=datetime.date(2025, 1, 1),
                period_end=TODAY,
                filed_at=TODAY,
                accession_number="0000320193-26-000001",
                form_type="10-K",
                fiscal_year=2025,
                fiscal_period="FY",
                frame=None,
                value=decimal.Decimal("383300000000"),
                raw_value="383300000000",
                fact_metadata={"source": "test"},
            ),
            SimpleNamespace(
                id=uuid.uuid4(),
                company_id=self.company_id,
                taxonomy="us-gaap",
                concept="NetIncomeLoss",
                unit="USD",
                period_start=datetime.date(2025, 1, 1),
                period_end=TODAY,
                filed_at=TODAY,
                accession_number="0000320193-26-000001",
                form_type="10-K",
                fiscal_year=2025,
                fiscal_period="FY",
                frame=None,
                value=decimal.Decimal("96995000000"),
                raw_value="96995000000",
                fact_metadata={"source": "test"},
            ),
            SimpleNamespace(
                id=uuid.uuid4(),
                company_id=self.company_id,
                taxonomy="us-gaap",
                concept="Assets",
                unit="USD",
                period_start=None,
                period_end=TODAY,
                filed_at=TODAY,
                accession_number="0000320193-26-000001",
                form_type="10-K",
                fiscal_year=2025,
                fiscal_period="FY",
                frame=None,
                value=decimal.Decimal("352583000000"),
                raw_value="352583000000",
                fact_metadata={"source": "test"},
            ),
            SimpleNamespace(
                id=uuid.uuid4(),
                company_id=self.company_id,
                taxonomy="us-gaap",
                concept="Liabilities",
                unit="USD",
                period_start=None,
                period_end=TODAY,
                filed_at=TODAY,
                accession_number="0000320193-26-000001",
                form_type="10-K",
                fiscal_year=2025,
                fiscal_period="FY",
                frame=None,
                value=decimal.Decimal("290437000000"),
                raw_value="290437000000",
                fact_metadata={"source": "test"},
            ),
        ]
        self.sanctions_entity = SimpleNamespace(
            id=self.sanctions_entity_id,
            provider="ofac",
            list_code="sdn",
            entity_uid="12345",
            entity_type="organization",
            primary_name="Example Sanctioned Entity",
            normalized_name="example sanctioned entity",
            country="IR",
            programs=["TEST"],
            remarks="Test sanctions record",
            first_seen_at=NOW,
            last_seen_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        self.entity_profile = SimpleNamespace(
            id=self.entity_profile_id,
            canonical_name="Example Energy Co.",
            normalized_name="example energy co",
            entity_type="company",
            country="US",
            primary_ticker="EXE",
            primary_cik="0000000001",
            primary_lei="549300TEST0000000001",
            website="https://example.test",
            profile_metadata={"source": "gleif"},
            created_at=NOW,
            updated_at=NOW,
        )
        self.country_indicator_series = SimpleNamespace(
            id=self.country_indicator_series_id,
            provider="world_bank",
            indicator_id="NY.GDP.MKTP.CD",
            title="GDP (current US$)",
            description="Gross domestic product in current US dollars",
            unit="USD",
            frequency="annual",
            topic="economy",
            source="World Bank",
            series_metadata={"source": "test"},
        )
        self.country_indicator_observation = SimpleNamespace(
            id=uuid.uuid4(),
            series_id=self.country_indicator_series_id,
            country_code="US",
            country_name="United States",
            observed_on=datetime.date(2025, 12, 31),
            value=decimal.Decimal("29184.90"),
            raw_value="29184.90",
            observation_metadata={"source": "test"},
        )
        self.country_context_snapshot = SimpleNamespace(
            id=uuid.uuid4(),
            country_code="US",
            snapshot_date=TODAY,
            sovereign_context={"debt_risk": "low"},
            governance_context={"stability": "high"},
            debt_context={"external_debt": "manageable"},
            trade_context={"exports": "diverse"},
            development_context={"income_group": "high"},
            snapshot_metadata={"source": "test"},
            created_at=NOW,
        )
        self.humanitarian_report = SimpleNamespace(
            id=uuid.uuid4(),
            provider="reliefweb",
            external_id="rw-1",
            title="Flood response update",
            url="https://reliefweb.int/report/test",
            published_at=NOW,
            country_codes=["US"],
            disaster_types=["flood"],
            organizations=["Example Aid"],
            themes=["disaster management"],
            summary="Operational update",
            body_excerpt="Flooding affected infrastructure.",
            created_at=NOW,
        )
        self.geo_incident = SimpleNamespace(
            id=uuid.uuid4(),
            provider="usgs",
            external_id="usgs-1",
            incident_type="earthquake",
            title="M 5.5 test earthquake",
            country_code="US",
            region="California",
            latitude=decimal.Decimal("37.7749"),
            longitude=decimal.Decimal("-122.4194"),
            magnitude=decimal.Decimal("5.5"),
            severity="moderate",
            observed_at=NOW,
            updated_at=NOW,
            source_url="https://earthquake.usgs.gov/test",
            created_at=NOW,
        )
        self.energy_snapshot = SimpleNamespace(
            id=uuid.uuid4(),
            provider="eia",
            region="US",
            commodity="crude_oil",
            snapshot_date=TODAY,
            price=decimal.Decimal("75.25"),
            inventory=decimal.Decimal("420.0"),
            production=decimal.Decimal("13.1"),
            consumption=decimal.Decimal("20.0"),
            imports=decimal.Decimal("6.5"),
            exports=decimal.Decimal("4.2"),
            snapshot_metadata={"unit": "million barrels"},
            created_at=NOW,
        )

    def list_provider_runs(
        self,
        *,
        provider: str | None,
        status: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["provider_runs"] = {"provider": provider, "status": status, "limit": limit}
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                run_key="fred:macro:test",
                provider=provider or "fred",
                run_type="macro_observations",
                status=status or "succeeded",
                parameters={"series_ids": ["GDP"]},
                stats={"network_called": False},
                error=None,
                item_count=0,
                started_at=NOW,
                completed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        ]

    def list_macro_series(
        self,
        *,
        provider: str | None,
        country: str | None,
        q: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["macro_series"] = {
            "provider": provider,
            "country": country,
            "q": q,
            "limit": limit,
        }
        return [self.series]

    def get_macro_observations(
        self,
        *,
        series_id: str,
        provider: str | None,
        limit: int,
    ) -> tuple[SimpleNamespace | None, list[SimpleNamespace]]:
        self.calls["macro_observations"] = {
            "series_id": series_id,
            "provider": provider,
            "limit": limit,
        }
        if series_id != "GDP":
            return None, []
        return (
            self.series,
            [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    observed_on=TODAY,
                    value=decimal.Decimal("123.45"),
                    raw_value="123.45",
                    realtime_start=TODAY,
                    realtime_end=TODAY,
                    observation_metadata={"source": "test"},
                )
            ],
        )

    def list_sec_companies(
        self,
        *,
        cik: str | None,
        ticker: str | None,
        q: str | None,
        limit: int,
        offset: int = 0,
    ) -> list[SimpleNamespace]:
        self.calls["sec_companies"] = {
            "cik": cik,
            "ticker": ticker,
            "q": q,
            "limit": limit,
            "offset": offset,
        }
        if ticker and ticker.upper() == "DUP":
            return [self.duplicate_company, self.duplicate_company_alt][:limit]
        if ticker and ticker.upper() != "AAPL":
            return []
        if cik and cik.zfill(10) != self.company.cik:
            return []
        return [self.company]

    def count_sec_companies(self, *, cik: str | None, ticker: str | None, q: str | None) -> int:
        # A real filtered total that is deliberately larger than one page of results, so a
        # query-mode test can prove `total` is a COUNT and not `len(items)`.
        self.calls["count_sec_companies"] = {"cik": cik, "ticker": ticker, "q": q}
        return 42

    def list_sec_filings(
        self,
        *,
        cik: str | None,
        ticker: str | None,
        form_type: str | None,
        limit: int,
    ) -> list[tuple[SimpleNamespace, SimpleNamespace]]:
        self.calls["sec_filings"] = {
            "cik": cik,
            "ticker": ticker,
            "form_type": form_type,
            "limit": limit,
        }
        if ticker and ticker.upper() != "AAPL":
            return []
        if cik and cik.zfill(10) != self.company.cik:
            return []
        filing = SimpleNamespace(
            id=uuid.uuid4(),
            company_id=self.company_id,
            accession_number="0000320193-26-000001",
            form_type=form_type or "10-K",
            filing_date=TODAY,
            report_date=TODAY,
            primary_document_url="https://www.sec.gov/test-primary.htm",
            filing_detail_url="https://www.sec.gov/test-index.htm",
            filing_metadata={"source": "test"},
            created_at=NOW,
        )
        return [(filing, self.company)]

    def list_sec_company_facts(
        self,
        *,
        cik: str | None,
        ticker: str | None,
        concepts: list[str] | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["sec_company_facts"] = {
            "cik": cik,
            "ticker": ticker,
            "concepts": concepts,
            "limit": limit,
        }
        if ticker and ticker.upper() != "AAPL":
            return []
        if cik and cik.zfill(10) != self.company.cik:
            return []
        if concepts:
            allowed = set(concepts)
            return [fact for fact in self.sec_facts if fact.concept in allowed]
        return self.sec_facts[:limit]

    def get_sec_company_by_id(self, company_id: uuid.UUID) -> SimpleNamespace | None:
        self.calls["sec_company_by_id"] = {"company_id": company_id}
        if company_id == self.company_id:
            return self.company
        return None

    def raw_items_summary(self, *, provider: str | None) -> list[dict[str, Any]]:
        self.calls["raw_items_summary"] = {"provider": provider}
        return [
            {
                "provider": provider or "gdelt",
                "item_type": "event",
                "count": 3,
                "latest_created_at": NOW,
            }
        ]

    def list_sanctions_entities(
        self,
        *,
        provider: str | None,
        list_code: str | None,
        country: str | None,
        q: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["sanctions_entities"] = {
            "provider": provider,
            "list_code": list_code,
            "country": country,
            "q": q,
            "limit": limit,
        }
        return [self.sanctions_entity]

    def get_sanctions_entity(
        self, *, entity_id: uuid.UUID
    ) -> tuple[SimpleNamespace | None, list[SimpleNamespace], list[SimpleNamespace], list[SimpleNamespace]]:
        self.calls["sanctions_entity"] = {"entity_id": entity_id}
        if entity_id != self.sanctions_entity_id:
            return None, [], [], []
        return (
            self.sanctions_entity,
            [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    alias_name="Example Alias",
                    normalized_alias="example alias",
                    alias_type="aka",
                    quality="strong",
                )
            ],
            [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    identifier_type="passport",
                    identifier_value="A1234567",
                    country="IR",
                    issue_date=TODAY,
                    expiry_date=TODAY,
                )
            ],
            [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    target_type="entity_profile",
                    target_id=str(self.entity_profile_id),
                    sanctions_entity_id=self.sanctions_entity_id,
                    match_method="normalized_name",
                    match_score=decimal.Decimal("0.97"),
                    matched_name="Example Sanctioned Entity",
                    explanation="Exact normalized name match",
                    review_status="accepted",
                    created_at=NOW,
                )
            ],
        )

    def list_recent_sanctions_changes(
        self,
        *,
        provider: str | None,
        since: datetime.datetime | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["recent_sanctions_changes"] = {
            "provider": provider,
            "since": since,
            "limit": limit,
        }
        return [self.sanctions_entity]

    def list_entity_profiles(
        self,
        *,
        country: str | None,
        entity_type: str | None,
        q: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["entity_profiles"] = {
            "country": country,
            "entity_type": entity_type,
            "q": q,
            "limit": limit,
        }
        return [self.entity_profile]

    def list_lei_records(
        self,
        *,
        country: str | None,
        q: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["lei_records"] = {"country": country, "q": q, "limit": limit}
        return [self.entity_profile]

    def list_country_indicator_series(
        self,
        *,
        provider: str | None,
        topic: str | None,
        q: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["country_indicator_series"] = {
            "provider": provider,
            "topic": topic,
            "q": q,
            "limit": limit,
        }
        return [self.country_indicator_series]

    def list_country_indicators(
        self,
        *,
        country_code: str,
        provider: str | None,
        indicator_id: str | None,
        limit: int,
    ) -> tuple[list[tuple[SimpleNamespace, SimpleNamespace]], list[SimpleNamespace]]:
        self.calls["country_indicators"] = {
            "country_code": country_code,
            "provider": provider,
            "indicator_id": indicator_id,
            "limit": limit,
        }
        return [(self.country_indicator_observation, self.country_indicator_series)], [
            self.country_context_snapshot
        ]

    def list_humanitarian_reports(
        self,
        *,
        provider: str | None,
        country_code: str | None,
        q: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["humanitarian_reports"] = {
            "provider": provider,
            "country_code": country_code,
            "q": q,
            "limit": limit,
        }
        return [self.humanitarian_report]

    def list_geo_incidents(
        self,
        *,
        provider: str | None,
        incident_type: str | None,
        country_code: str | None,
        region: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["geo_incidents"] = {
            "provider": provider,
            "incident_type": incident_type,
            "country_code": country_code,
            "region": region,
            "limit": limit,
        }
        return [self.geo_incident]

    def list_energy_market_snapshots(
        self,
        *,
        provider: str | None,
        region: str | None,
        commodity: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["energy_market_snapshots"] = {
            "provider": provider,
            "region": region,
            "commodity": commodity,
            "limit": limit,
        }
        return [self.energy_snapshot]

    def list_energy_series(
        self,
        *,
        provider: str | None,
        region: str | None,
        commodity: str | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        self.calls["energy_series"] = {
            "provider": provider,
            "region": region,
            "commodity": commodity,
            "limit": limit,
        }
        return [self.energy_snapshot]


def _get_with_repo(repo: FakeProviderDataRepository, path: str):
    app.dependency_overrides[get_provider_data_repository] = lambda: repo
    try:
        return TestClient(app).get(path)
    finally:
        app.dependency_overrides.clear()


def test_provider_runs_endpoint_is_read_only_and_filterable() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/provider-runs?provider=fred&status=succeeded&limit=5",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["provider"] == "fred"
    assert body["items"][0]["stats"]["network_called"] is False
    assert repo.calls["provider_runs"] == {"provider": "fred", "status": "succeeded", "limit": 5}


def test_macro_series_and_observations_endpoints() -> None:
    repo = FakeProviderDataRepository()
    series_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/macro/series?provider=fred&country=US&q=gross",
    )
    obs_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/macro/series/GDP/observations?provider=fred&limit=2",
    )

    assert series_resp.status_code == 200
    assert series_resp.json()["items"][0]["series_id"] == "GDP"
    assert obs_resp.status_code == 200
    body = obs_resp.json()
    assert body["series"]["series_id"] == "GDP"
    assert body["observations"][0]["value"] == 123.45
    assert repo.calls["macro_observations"]["limit"] == 2


def test_macro_observations_returns_404_for_missing_series() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(repo, "/api/v1/provider-data/macro/series/MISSING/observations")

    assert resp.status_code == 404


def test_sec_company_and_filing_endpoints() -> None:
    repo = FakeProviderDataRepository()
    companies_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/sec/companies?ticker=AAPL",
    )
    filings_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/sec/filings?cik=320193&form_type=10-K",
    )

    assert companies_resp.status_code == 200
    assert companies_resp.json()["items"][0]["cik"] == "0000320193"
    assert filings_resp.status_code == 200
    filing = filings_resp.json()["items"][0]
    assert filing["ticker"] == "AAPL"
    assert filing["accession_number"] == "0000320193-26-000001"
    assert repo.calls["sec_filings"]["form_type"] == "10-K"


def test_company_research_profiles_endpoint_collects_available_and_marks_missing() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/company-research/profiles?tickers=AAPL,MISSING",
    )

    assert resp.status_code == 200
    body = resp.json()
    # Explicit selectors are a bounded lookup: total is the full requested count, with exact
    # pagination metadata and no legacy `count` key (api-adapter-contract).
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 2
    assert len(body["items"]) == 2

    apple = body["items"][0]
    assert apple["identity"]["ticker"] == "AAPL"
    assert apple["financial"]["metrics"][0]["value"] == "$383.3B"
    assert apple["coverage"]["available"] > 0
    assert apple["coverage"]["missing"] > 0
    financial = {field["key"]: field for field in apple["researchChecklist"]["financial"]}
    assert financial["revenue"]["available"] is True
    assert financial["revenue"]["source"].startswith("SEC company facts")
    valuation = {field["key"]: field for field in apple["researchChecklist"]["valuation"]}
    assert valuation["share_price"]["value"] == "Information not available"
    assert valuation["share_price"]["available"] is False

    missing = body["items"][1]
    assert missing["identity"]["ticker"] == "MISSING"
    assert missing["coverage"]["available"] == 0
    assert missing["financial"]["metrics"][0]["value"] == "Information not available"


def test_company_research_profiles_canonical_route_alias() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        "/api/v1/company-research/profiles?tickers=AAPL",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    profile = body["items"][0]
    assert profile["identity"]["ticker"] == "AAPL"
    assert profile["missingFields"]
    assert "evidenceRefs" in profile
    revenue = {field["key"]: field for field in profile["researchChecklist"]["financial"]}["revenue"]
    assert revenue["category"] == "financial"
    assert revenue["confidence"] > 0
    assert revenue["evidence_refs"]


def test_company_research_profiles_ticker_ambiguity_is_explicit() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        "/api/v1/company-research/profiles?tickers=DUP",
    )

    assert resp.status_code == 200
    profile = resp.json()["items"][0]
    assert profile["identity"]["ticker"] == "DUP"
    assert profile["identity"]["resolution_status"] == "ambiguous_ticker"
    assert len(profile["identity"]["ticker_choices"]) == 2
    assert profile["sources"]["ambiguous_ticker"] is True
    assert profile["coverage"]["available"] == 0


def test_company_research_profiles_support_company_ids_query() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        f"/api/v1/company-research/profiles?company_ids={repo.company_id}",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    profile = body["items"][0]
    assert profile["identity"]["ticker"] == "AAPL"
    assert profile["coverage"]["total"] == 66
    assert repo.calls["sec_company_by_id"]["company_id"] == repo.company_id


def test_company_research_profiles_query_mode_reports_real_filtered_total() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        "/api/v1/company-research/profiles?q=apple&limit=5&offset=10",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    # Query mode reports the real filtered company COUNT, not len(items) of this page.
    assert body["total"] == 42
    assert len(body["items"]) == 1
    assert body["limit"] == 5
    assert body["offset"] == 10
    # The page window is threaded through to the repository read, and the count uses the
    # same filters as the page (no selector predicates in query mode).
    assert repo.calls["sec_companies"] == {
        "cik": None,
        "ticker": None,
        "q": "apple",
        "limit": 5,
        "offset": 10,
    }
    assert repo.calls["count_sec_companies"] == {"cik": None, "ticker": None, "q": "apple"}


def test_company_research_profiles_explicit_selectors_honor_offset_and_limit() -> None:
    repo = FakeProviderDataRepository()
    # Three requested identifiers => total 3; a one-wide window at offset 1 returns the 2nd.
    resp = _get_with_repo(
        repo,
        "/api/v1/company-research/profiles?tickers=AAPL,MISSING,DUP&limit=1&offset=1",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert len(body["items"]) == 1
    # offset=1 skips AAPL and returns the MISSING profile; no identifier category is dropped.
    assert body["items"][0]["identity"]["ticker"] == "MISSING"


def test_company_research_profiles_reject_negative_offset() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        "/api/v1/company-research/profiles?tickers=AAPL&offset=-1",
    )
    assert resp.status_code == 422


def test_company_research_profiles_provider_data_alias_uses_envelope() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/company-research/profiles?tickers=AAPL",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 1


def test_company_research_profile_path_returns_valid_empty_profile_for_unknown_id() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        "/api/v1/company-research/profiles/private-ai-co",
    )

    assert resp.status_code == 200
    profile = resp.json()
    assert profile["identity"]["name"] == "private-ai-co"
    assert profile["coverage"]["available"] == 0
    assert profile["coverage"]["total"] == 66
    assert len(profile["missingFields"]) == 66


def test_sanctions_entity_endpoints() -> None:
    repo = FakeProviderDataRepository()
    list_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/sanctions/entities?provider=ofac&list_code=sdn&country=IR&q=example&limit=10",
    )
    detail_resp = _get_with_repo(
        repo,
        f"/api/v1/provider-data/sanctions/entities/{repo.sanctions_entity_id}",
    )

    assert list_resp.status_code == 200
    assert list_resp.json()["items"][0]["entity_uid"] == "12345"
    assert repo.calls["sanctions_entities"] == {
        "provider": "ofac",
        "list_code": "sdn",
        "country": "IR",
        "q": "example",
        "limit": 10,
    }
    assert detail_resp.status_code == 200
    body = detail_resp.json()
    assert body["entity"]["primary_name"] == "Example Sanctioned Entity"
    assert body["aliases"][0]["alias_name"] == "Example Alias"
    assert body["identifiers"][0]["identifier_value"] == "A1234567"
    assert body["matches"][0]["match_score"] == 0.97


def test_sanctions_entity_detail_returns_404_for_missing_entity() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        f"/api/v1/provider-data/sanctions/entities/{uuid.uuid4()}",
    )

    assert resp.status_code == 404


def test_entity_identity_profiles_endpoint() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/entity-identity/profiles?country=US&entity_type=company&q=energy",
    )

    assert resp.status_code == 200
    profile = resp.json()["items"][0]
    assert profile["canonical_name"] == "Example Energy Co."
    assert profile["primary_lei"] == "549300TEST0000000001"
    assert repo.calls["entity_profiles"]["q"] == "energy"


def test_recent_sanctions_changes_and_lei_records_endpoints() -> None:
    repo = FakeProviderDataRepository()
    changes_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/sanctions/recent-changes?provider=ofac&limit=5",
    )
    lei_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/entity-identity/lei-records?country=US&q=energy&limit=5",
    )

    assert changes_resp.status_code == 200
    change = changes_resp.json()["items"][0]
    assert change["change_type"] == "current_record"
    assert change["entity"]["entity_uid"] == "12345"
    assert repo.calls["recent_sanctions_changes"]["provider"] == "ofac"
    assert lei_resp.status_code == 200
    record = lei_resp.json()["items"][0]
    assert record["lei"] == "549300TEST0000000001"
    assert record["legal_name"] == "Example Energy Co."
    assert record["profile"]["primary_ticker"] == "EXE"
    assert repo.calls["lei_records"] == {"country": "US", "q": "energy", "limit": 5}


def test_country_indicator_series_and_country_endpoint() -> None:
    repo = FakeProviderDataRepository()
    series_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/country-indicators/series?provider=world_bank&topic=economy&q=gdp",
    )
    country_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/country-indicators/us?provider=world_bank&indicator_id=NY.GDP.MKTP.CD&limit=3",
    )

    assert series_resp.status_code == 200
    assert series_resp.json()["items"][0]["indicator_id"] == "NY.GDP.MKTP.CD"
    assert country_resp.status_code == 200
    body = country_resp.json()
    assert body["country_code"] == "US"
    assert body["observations"][0]["value"] == 29184.9
    assert body["observations"][0]["series"]["provider"] == "world_bank"
    assert body["context_snapshots"][0]["sovereign_context"]["debt_risk"] == "low"
    assert repo.calls["country_indicators"]["limit"] == 3


def test_humanitarian_geo_and_energy_endpoints() -> None:
    repo = FakeProviderDataRepository()
    humanitarian_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/humanitarian/reports?provider=reliefweb&country_code=US&q=flood",
    )
    geo_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/geo/incidents?provider=usgs&incident_type=earthquake&country_code=US&region=California",
    )
    energy_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/energy/market-snapshots?provider=eia&region=US&commodity=crude_oil",
    )
    energy_series_resp = _get_with_repo(
        repo,
        "/api/v1/provider-data/energy/series?provider=eia&region=US&commodity=crude_oil",
    )

    assert humanitarian_resp.status_code == 200
    assert humanitarian_resp.json()["items"][0]["external_id"] == "rw-1"
    assert geo_resp.status_code == 200
    assert geo_resp.json()["items"][0]["magnitude"] == 5.5
    assert energy_resp.status_code == 200
    snapshot = energy_resp.json()["items"][0]
    assert snapshot["price"] == 75.25
    assert snapshot["metadata"]["unit"] == "million barrels"
    assert repo.calls["energy_market_snapshots"]["commodity"] == "crude_oil"
    assert energy_series_resp.status_code == 200
    series = energy_series_resp.json()["items"][0]
    assert series["series_key"] == "eia:US:crude_oil"
    assert series["latest_snapshot"]["inventory"] == 420.0
    assert repo.calls["energy_series"]["commodity"] == "crude_oil"


def test_raw_items_summary_endpoint() -> None:
    repo = FakeProviderDataRepository()
    resp = _get_with_repo(repo, "/api/v1/provider-data/raw-items/summary?provider=gdelt")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["total"] == 3
    assert body["items"][0]["provider"] == "gdelt"
