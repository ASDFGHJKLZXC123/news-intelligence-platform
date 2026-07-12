"""Frontend-facing intelligence API tests with dependency overrides."""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.intelligence import get_intelligence_repository
from apps.api.main import app

NOW = datetime.datetime(2026, 6, 20, 12, 0, tzinfo=datetime.UTC)
TODAY = datetime.date(2026, 6, 20)
USER_ID = uuid.uuid4()
EVENT_ID = uuid.uuid4()
COMPANY_ID = uuid.uuid4()
REPORT_ID = uuid.uuid4()
CLAIM_ID = uuid.uuid4()
SIGNAL_ID = uuid.uuid4()


class FakeIntelligenceRepository:
    def __init__(self) -> None:
        self.risk_score = SimpleNamespace(
            id=uuid.uuid4(),
            target_type="country",
            target_id="US",
            risk_type="sovereign",
            score=decimal.Decimal("41.5"),
            level="medium",
            confidence_score=decimal.Decimal("0.82"),
            as_of=NOW,
            model_version="risk-v1",
            evidence_refs=[{"id": "evidence-1"}],
            driver_refs=[{"name": "macro"}],
        )
        self.summary = SimpleNamespace(
            id=uuid.uuid4(),
            summary_date=TODAY,
            overall_risk_level="medium",
            confidence_score=decimal.Decimal("0.8"),
            summary="Global risk remains moderate.",
            key_points=["Rates stable", "Energy risk elevated"],
            model_rating_prediction_id=uuid.uuid4(),
            generated_by_run_id=uuid.uuid4(),
            created_at=NOW,
        )
        self.event = SimpleNamespace(
            id=EVENT_ID,
            title="Port disruption affects semiconductor shipments",
            summary="A logistics shock delays components.",
            event_type="supply_chain",
            country="US",
            region="California",
            severity_score=decimal.Decimal("67"),
            article_count=4,
            source_count=3,
            first_seen_at=NOW,
            last_seen_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        self.location = SimpleNamespace(
            id=uuid.uuid4(),
            event_id=EVENT_ID,
            location_name="Port of Los Angeles",
            country_code="US",
            region="California",
            latitude=decimal.Decimal("33.7361"),
            longitude=decimal.Decimal("-118.2922"),
            location_type="port",
            confidence_score=decimal.Decimal("0.91"),
            created_at=NOW,
        )
        self.company = SimpleNamespace(
            id=COMPANY_ID,
            entity_profile_id=uuid.uuid4(),
            display_name="Example Semiconductors",
            legal_name="Example Semiconductors Inc.",
            primary_ticker="EXSM",
            exchange="NASDAQ",
            country="US",
            sector="Technology",
            industry="Semiconductors",
            website="https://example.test",
            logo_url="https://example.test/logo.png",
            logo_source="company",
            active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.company_rollup = SimpleNamespace(
            id=uuid.uuid4(),
            company_id=COMPANY_ID,
            as_of=NOW,
            impact_score=decimal.Decimal("72"),
            risk_score=decimal.Decimal("58"),
            opportunity_score=decimal.Decimal("35"),
            related_event_count=2,
            top_driver="supply chain disruption",
            confidence_score=decimal.Decimal("0.84"),
        )
        self.industry_rollup = SimpleNamespace(
            id=uuid.uuid4(),
            industry_id="semiconductors",
            as_of=NOW,
            impact_score=decimal.Decimal("69"),
            risk_score=decimal.Decimal("55"),
            opportunity_score=decimal.Decimal("38"),
            news_velocity_score=decimal.Decimal("80"),
            related_event_count=5,
            summary="Supply chain risk is elevated.",
        )

    def latest_daily_summary(self) -> Any:
        return self.summary

    def count_open_alerts(self) -> int:
        return 2

    def list_risk_scores(
        self,
        *,
        risk_type: str | None,
        target_type: str | None,
        limit: int,
    ) -> list[Any]:
        return [self.risk_score]

    def list_events(
        self,
        *,
        q: str | None,
        country: str | None,
        event_type: str | None,
        limit: int,
    ) -> list[Any]:
        return [self.event]

    def get_event_detail(self, event_id: uuid.UUID) -> tuple[Any, list[Any], list[Any], list[Any], list[Any]]:
        return (
            self.event,
            [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    timestamp=NOW,
                    title="Initial disruption reported",
                    description="Local authorities confirmed delays.",
                    importance="high",
                    evidence_refs=[{"id": "article-1"}],
                )
            ],
            [
                SimpleNamespace(
                    company_id=COMPANY_ID,
                    impact_direction="negative",
                    impact_score=decimal.Decimal("72"),
                    risk_score=decimal.Decimal("58"),
                    confidence_score=decimal.Decimal("0.81"),
                )
            ],
            [
                SimpleNamespace(
                    industry_id="semiconductors",
                    impact_direction="negative",
                    impact_score=decimal.Decimal("69"),
                    risk_score=decimal.Decimal("55"),
                    opportunity_score=decimal.Decimal("38"),
                )
            ],
            [self.location],
        )

    def list_geo_events(self, *, country_code: str | None, limit: int) -> list[tuple[Any, Any]]:
        return [(self.location, self.event)]

    def list_companies(
        self,
        *,
        q: str | None,
        sector: str | None,
        country: str | None,
        limit: int,
    ) -> list[Any]:
        return [self.company]

    def get_company(self, identifier: str) -> tuple[Any, Any]:
        return self.company, self.company_rollup

    def list_industries(self, *, limit: int) -> list[Any]:
        return [self.industry_rollup]

    def get_industry(self, industry_id: str) -> Any:
        return self.industry_rollup

    def list_historical(
        self,
        *,
        target_type: str | None,
        risk_type: str | None,
        limit: int,
    ) -> list[Any]:
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                target_type="country",
                target_id="US",
                risk_type="sovereign",
                as_of_date=TODAY,
                risk_score=decimal.Decimal("41.5"),
                risk_level="medium",
                confidence_score=decimal.Decimal("0.82"),
                top_drivers=[{"name": "macro"}],
                evidence_refs=[{"id": "evidence-1"}],
            )
        ]

    def list_alerts(self, *, user_id: uuid.UUID | None, status: str | None, limit: int) -> list[Any]:
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                user_id=USER_ID,
                alert_rule_id=uuid.uuid4(),
                title="Company risk increased",
                message="Risk moved above threshold.",
                severity="high",
                risk_score=decimal.Decimal("72"),
                alert_type="risk_threshold",
                related_event_id=EVENT_ID,
                related_company_id=COMPANY_ID,
                related_industry_id="semiconductors",
                evidence_refs=[{"id": "evidence-1"}],
                evidence_signal_ids=[SIGNAL_ID],
                state="escalated",
                dedupe_key="company:banking:threshold",
                score_version="v1",
                what_could_reduce_risk=[
                    {"signal_ref": "deposit_outflow", "comparator": "<", "threshold": 0.2}
                ],
                news_driven=decimal.Decimal("0.4"),
                experimental=True,
                superseded_by=None,
                created_at=NOW,
                updated_at=NOW,
                resolved_at=None,
            )
        ]

    def list_watchlist(self, *, user_id: uuid.UUID | None, limit: int) -> list[Any]:
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                user_id=USER_ID,
                item_type="company",
                item_id=str(COMPANY_ID),
                label="Example Semiconductors",
                item_metadata={"ticker": "EXSM"},
                alert_enabled=True,
                created_at=NOW,
            )
        ]

    def list_reports(self, *, user_id: uuid.UUID | None, limit: int) -> list[tuple[Any, list[Any]]]:
        return [
            (
                SimpleNamespace(
                    id=REPORT_ID,
                    # The daily brief is global: it has no owner.
                    user_id=None,
                    report_type="daily_brief",
                    brief_date=NOW.date(),
                    event_id=None,
                    title="Daily Intelligence Brief",
                    status="published",
                    version=2,
                    change_reason="upstream event reprocessed",
                    stale=False,
                    confidence_score=decimal.Decimal("0.86"),
                    generated_by_run_id=uuid.uuid4(),
                    created_at=NOW,
                    updated_at=NOW,
                ),
                [
                    SimpleNamespace(
                        id=uuid.uuid4(),
                        section_order=1,
                        title="Overview",
                        body="Risk remains moderate.",
                        blocks=[{"text": "Risk remains moderate.", "claim_ids": [str(CLAIM_ID)]}],
                        evidence_refs=[CLAIM_ID],
                        grounding_status="passed",
                    )
                ],
            )
        ]

    def list_jobs(self, *, state: str | None, limit: int) -> list[Any]:
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                job_key="ingest:gdelt",
                job_type="provider_ingestion",
                state="completed",
                attempt=1,
                max_attempts=3,
                safe_to_rerun=True,
                created_at=NOW,
                updated_at=NOW,
            )
        ]

    def list_sources(self, *, active: bool | None, limit: int) -> tuple[list[Any], list[Any]]:
        return (
            [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    name="GDELT",
                    source_type="news_api",
                    feed_url="https://gdelt.example.test",
                    homepage_url="https://gdeltproject.org",
                    active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
            ],
            [
                SimpleNamespace(
                    id=uuid.uuid4(),
                    source_id=uuid.uuid4(),
                    provider="gdelt",
                    checked_at=NOW,
                    status="healthy",
                    latency_ms=120,
                    error_rate=decimal.Decimal("0"),
                    items_fetched=100,
                )
            ],
        )

    def list_model_runs(self, *, limit: int) -> list[Any]:
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                prompt_name="risk_summary",
                prompt_version="v1",
                provider="openai",
                model="gpt-test",
                output_schema_version="v1",
                cost_usd=decimal.Decimal("0.12"),
                latency_ms=850,
                created_at=NOW,
            )
        ]


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_intelligence_repository] = FakeIntelligenceRepository
    try:
        yield TestClient(app, client=("127.0.0.1", 5000))
    finally:
        app.dependency_overrides.clear()


def test_frontend_query_endpoints_return_database_shaped_payloads(client: TestClient) -> None:
    paths = [
        "/api/v1/dashboard",
        "/api/v1/events",
        f"/api/v1/events/{EVENT_ID}",
        "/api/v1/geo/events",
        "/api/v1/risk-radar",
        "/api/v1/risk-radar/sovereign",
        "/api/v1/industries",
        "/api/v1/industries/semiconductors",
        "/api/v1/companies",
        f"/api/v1/companies/{COMPANY_ID}",
        "/api/v1/historical",
        "/api/v1/alerts",
        "/api/v1/watchlist",
        "/api/v1/reports",
        "/api/v1/admin/jobs",
        "/api/v1/admin/sources",
        "/api/v1/admin/models",
    ]

    responses = {path: client.get(path) for path in paths}

    assert {path: response.status_code for path, response in responses.items()} == {
        path: 200 for path in paths
    }
    assert responses["/api/v1/dashboard"].json()["summary"]["overall_risk_level"] == "medium"
    assert responses["/api/v1/events"].json()["items"][0]["id"] == str(EVENT_ID)
    assert responses["/api/v1/geo/events"].json()["items"][0]["event"]["id"] == str(EVENT_ID)
    assert responses[f"/api/v1/companies/{COMPANY_ID}"].json()["company"]["risk_rollup"][
        "top_driver"
    ] == "supply chain disruption"
    assert responses["/api/v1/reports"].json()["items"][0]["sections"][0]["title"] == "Overview"


def test_alert_wire_status_is_the_adr0010_lifecycle_state(client: TestClient) -> None:
    alert = client.get("/api/v1/alerts").json()["items"][0]
    # The adapter contract pins the wire field `status` to the lifecycle vocabulary.
    assert alert["status"] == "escalated"
    assert alert["state"] == "escalated"
    assert alert["dedupe_key"] == "company:banking:threshold"
    assert alert["news_driven"] == 0.4
    assert alert["experimental"] is True
    assert alert["evidence_signal_ids"] == [str(SIGNAL_ID)]
    assert alert["resolved_at"] is None


def test_report_wire_shape_carries_versioning_and_claim_level_citations(
    client: TestClient,
) -> None:
    report = client.get("/api/v1/reports").json()["items"][0]
    # A daily brief is global and versioned; regeneration bumps the version.
    assert report["user_id"] is None
    assert report["brief_date"] == "2026-06-20"
    assert report["version"] == 2
    assert report["stale"] is False

    section = report["sections"][0]
    # evidence_refs is a flat list of claim IDs the Evidence Drawer can resolve.
    assert section["evidence_refs"] == [str(CLAIM_ID)]
    assert section["grounding_status"] == "passed"
    assert section["blocks"][0]["claim_ids"] == [str(CLAIM_ID)]


def test_timestamps_are_utc_iso8601_with_trailing_z(client: TestClient) -> None:
    # The adapter cannot infer a timezone from a naive timestamp (api-adapter-contract).
    created_at = client.get("/api/v1/alerts").json()["items"][0]["created_at"]
    assert created_at == "2026-06-20T12:00:00Z"
    assert client.get("/api/v1/events").json()["items"][0]["created_at"].endswith("Z")
