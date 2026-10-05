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

from apps.api.intelligence import (
    DashboardMapPoint,
    DashboardMetricCounts,
    EventCompanyView,
    EventListRow,
    get_crisis_prediction_reads_enabled,
    get_intelligence_repository,
)
from apps.api.main import app
from db.models.core import (
    REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY,
    REPORT_CONTENT_POLICY_PREDICTION_BACKED,
)

NOW = datetime.datetime(2026, 6, 20, 12, 0, tzinfo=datetime.UTC)
TODAY = datetime.date(2026, 6, 20)
USER_ID = uuid.uuid4()
EVENT_ID = uuid.uuid4()
COMPANY_ID = uuid.uuid4()
REPORT_ID = uuid.uuid4()
CLAIM_ID = uuid.uuid4()
SIGNAL_ID = uuid.uuid4()

#: The fakes return this filtered total while every page holds a single item, so a test can
#: prove `total` is the real relation size and not `len(items)` (api-adapter-contract).
PARTIAL_TOTAL = 137


class FakeIntelligenceRepository:
    def __init__(self) -> None:
        self.total = PARTIAL_TOTAL
        # Records the (limit, offset) each paginated read received, so a test can prove the
        # endpoint threads them through to the repository.
        self.received: dict[str, dict[str, int]] = {}
        self.source = SimpleNamespace(
            id=uuid.uuid4(),
            name="GDELT",
            source_type="news_api",
            feed_url="https://gdelt.example.test",
            homepage_url="https://gdeltproject.org",
            active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.source_health = SimpleNamespace(
            id=uuid.uuid4(),
            source_id=self.source.id,
            provider="gdelt",
            checked_at=NOW,
            status="healthy",
            latency_ms=120,
            error_rate=decimal.Decimal("0"),
            items_fetched=100,
        )
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
            hotness_score=decimal.Decimal("71"),
            article_count=4,
            source_count=3,
            first_seen_at=NOW,
            last_seen_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        # The event->company link joined to company identity: the embedded name/ticker the
        # expanded event contract requires (no bare-UUID N+1).
        self.event_company_view = EventCompanyView(
            company_id=COMPANY_ID,
            display_name="Example Semiconductors",
            primary_ticker="EXSM",
            exchange="NASDAQ",
            industry="Semiconductors",
            impact_direction="negative",
            impact_score=decimal.Decimal("72"),
            risk_score=decimal.Decimal("58"),
            confidence_score=decimal.Decimal("0.81"),
            exposure_explanation="Direct supplier of delayed components.",
        )
        self.event_industry = SimpleNamespace(
            event_id=EVENT_ID,
            industry_id="semiconductors",
            impact_direction="negative",
            impact_score=decimal.Decimal("69"),
            risk_score=decimal.Decimal("55"),
            opportunity_score=decimal.Decimal("38"),
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
        # The real CrisisPrediction that supplies a RiskDetail's model_rating. Carries every
        # CrisisRating field; `top_drivers` deliberately mixes labeled and unlabeled entries so a
        # test can prove the unlabeled one is skipped rather than assigned an invented name.
        self.crisis_prediction = SimpleNamespace(
            id=uuid.uuid4(),
            target_type="country",
            target_id="US",
            risk_type="sovereign",
            as_of_date=TODAY,
            probability_0_6m=decimal.Decimal("0.10"),
            probability_6_12m=decimal.Decimal("0.15"),
            probability_12_18m=decimal.Decimal("0.05"),
            probability_within_18m=decimal.Decimal("0.30"),
            risk_score=decimal.Decimal("41.5"),
            risk_level="medium",
            confidence_score=decimal.Decimal("0.82"),
            model_versions={"ensemble": "ensemble.v1"},
            top_drivers=[
                {"name": "Deposit outflows", "contribution": 12.0},
                {"signal": "credit_spread_zscore", "contribution": 8.0},
                {"score": 1.0},  # unlabeled: must be skipped, never given a synthetic label
            ],
            historical_analogies=[
                {
                    "component": "historical_analogy",
                    "title": "1998 sovereign stress episode",
                    "similarity_score": 0.71,
                }
            ],
            evidence_refs=[{"source_type": "article", "source_id": "a1"}],
            what_could_escalate=["Sovereign rating downgrade"],
            what_could_reduce_risk=["IMF backstop confirmed", "FX reserves rebuild"],
            created_at=NOW,
        )
        # The distinct (target_type, target_id) rows the risk_type is really observed against;
        # `country` has no RiskDetail related-id field and must be dropped, not fabricated onto one.
        self.related_targets = [
            ("company", str(COMPANY_ID)),
            ("country", "US"),
            ("event", str(EVENT_ID)),
            ("industry", "semiconductors"),
        ]

    def latest_daily_summary(self) -> Any:
        return self.summary

    def count_open_alerts(self) -> int:
        return 2

    def dashboard_metric_counts(
        self,
        *,
        now: datetime.datetime,
        prediction_backed_outputs_enabled: bool = False,
    ) -> Any:
        self.received["dashboard_metric_counts"] = {
            "now": now,
            "prediction_backed_outputs_enabled": prediction_backed_outputs_enabled,
        }
        return DashboardMetricCounts(
            events_today=5,
            high_risk_events=2,
            affected_industries=3,
            affected_companies=4,
        )

    def upcoming_triggers(self, *, now: datetime.datetime, limit: int) -> list[Any]:
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                event_id=EVENT_ID,
                timestamp=NOW + datetime.timedelta(days=2),
                title="Labor-market data release",
                description="Single most important input to the recession estimate.",
                importance="high",
            )
        ]

    def event_map(
        self,
        *,
        limit: int,
        prediction_backed_outputs_enabled: bool = False,
    ) -> list[Any]:
        self.received["event_map_policy"] = {
            "prediction_backed_outputs_enabled": prediction_backed_outputs_enabled
        }
        return [
            DashboardMapPoint(
                location_name="Washington, D.C.",
                latitude=decimal.Decimal("38.9"),
                longitude=decimal.Decimal("-77.0"),
                event_count=1,
                max_risk_score=decimal.Decimal("76"),
                dominant_event_type="policy",
                related_event_ids=(str(EVENT_ID),),
            )
        ]

    def company_ranking(self, *, limit: int) -> list[tuple[Any, Any]]:
        return [(self.company, self.company_rollup)]

    def dashboard_industry_summary(self, *, limit: int) -> list[Any]:
        return [self.industry_rollup]

    def list_risk_scores(
        self,
        *,
        risk_type: str | None,
        target_type: str | None,
        limit: int,
    ) -> list[Any]:
        return [self.risk_score]

    def list_risk_radar_scores(
        self,
        *,
        target_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Any], int]:
        self.received["list_risk_radar_scores"] = {"limit": limit, "offset": offset}
        return [self.risk_score], self.total

    def latest_risk_observation(self, *, risk_type: str) -> Any:
        self.received["latest_risk_observation"] = {"risk_type": risk_type}
        return self.risk_score

    def latest_crisis_prediction(
        self, *, risk_type: str, target_type: str | None, target_id: str | None
    ) -> Any:
        self.received["latest_crisis_prediction"] = {
            "risk_type": risk_type,
            "target_type": target_type,
            "target_id": target_id,
        }
        return self.crisis_prediction

    def related_risk_targets(self, *, risk_type: str, limit: int) -> list[tuple[str, str]]:
        self.received["related_risk_targets"] = {"risk_type": risk_type, "limit": limit}
        return list(self.related_targets)

    def risk_observation_history(
        self, *, risk_type: str, cutoff: datetime.datetime, limit: int, offset: int
    ) -> tuple[list[Any], int]:
        self.received["risk_observation_history"] = {
            "risk_type": risk_type,
            "cutoff": cutoff,
            "limit": limit,
            "offset": offset,
        }
        return [self.risk_score], self.total

    def list_events(
        self,
        *,
        q: str | None = None,
        country: str | None = None,
        event_type: str | None = None,
        date_from: datetime.date | None = None,
        date_to: datetime.date | None = None,
        risk_level: str | None = None,
        industry: str | None = None,
        company: str | None = None,
        status: str | None = None,
        limit: int,
        offset: int,
        prediction_backed_outputs_enabled: bool = False,
    ) -> tuple[list[Any], int]:
        self.received["list_events"] = {"limit": limit, "offset": offset}
        self.received["list_events_policy"] = {
            "prediction_backed_outputs_enabled": prediction_backed_outputs_enabled,
        }
        return [
            EventListRow(
                event=self.event,
                risk_score=decimal.Decimal("58"),
                confidence_score=decimal.Decimal("0.81"),
                status="developing",
                companies=(self.event_company_view,),
                industries=(self.event_industry,),
                locations=(self.location,),
            )
        ], self.total

    def get_event_detail(
        self,
        event_id: uuid.UUID,
        *,
        prediction_backed_outputs_enabled: bool = False,
    ) -> tuple[Any, list[Any], list[Any], list[Any], list[Any]]:
        self.received["get_event_detail"] = {
            "prediction_backed_outputs_enabled": prediction_backed_outputs_enabled
        }
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
            [self.event_company_view],
            [self.event_industry],
            [self.location],
        )

    def list_geo_events(
        self, *, country_code: str | None, limit: int, offset: int
    ) -> tuple[list[tuple[Any, Any]], int]:
        self.received["list_geo_events"] = {"limit": limit, "offset": offset}
        return [(self.location, self.event)], self.total

    def list_companies(
        self,
        *,
        q: str | None,
        sector: str | None,
        country: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Any], int]:
        self.received["list_companies"] = {"limit": limit, "offset": offset}
        return [self.company], self.total

    def get_company(
        self,
        identifier: str,
        *,
        prediction_backed_outputs_enabled: bool = False,
    ) -> tuple[Any, Any]:
        self.received["get_company"] = {
            "prediction_backed_outputs_enabled": prediction_backed_outputs_enabled
        }
        return (
            self.company,
            self.company_rollup if prediction_backed_outputs_enabled else None,
        )

    def list_industries(self, *, limit: int, offset: int) -> tuple[list[Any], int]:
        self.received["list_industries"] = {"limit": limit, "offset": offset}
        return [self.industry_rollup], self.total

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

    def list_alerts(
        self, *, user_id: uuid.UUID | None, status: str | None, limit: int, offset: int
    ) -> tuple[list[Any], int]:
        self.received["list_alerts"] = {"limit": limit, "offset": offset}
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
        ], self.total

    def list_watchlist(
        self, *, user_id: uuid.UUID | None, limit: int, offset: int
    ) -> tuple[list[Any], int]:
        self.received["list_watchlist"] = {"limit": limit, "offset": offset}
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
        ], self.total

    def list_reports(
        self,
        *,
        user_id: uuid.UUID | None,
        limit: int,
        prediction_backed_outputs_enabled: bool = False,
    ) -> list[tuple[Any, list[Any]]]:
        self.received["list_reports_policy"] = {
            "prediction_backed_outputs_enabled": prediction_backed_outputs_enabled
        }
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
                    content_policy=REPORT_CONTENT_POLICY_PREDICTION_BACKED,
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

    def claim_is_referenced_by_descriptive_report(
        self, claim_id: uuid.UUID, *, descriptive_only: bool = True
    ) -> bool:
        self.received["claim_policy"] = {
            "claim_id": claim_id,
            "descriptive_only": descriptive_only,
        }
        return False

    def list_jobs(self, *, state: str | None, limit: int, offset: int) -> tuple[list[Any], int]:
        self.received["list_jobs"] = {"limit": limit, "offset": offset}
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
        ], self.total

    def list_sources(
        self, *, active: bool | None, limit: int, offset: int
    ) -> tuple[list[tuple[Any, Any]], int]:
        self.received["list_sources"] = {"limit": limit, "offset": offset}
        return [(self.source, self.source_health)], self.total

    def list_model_runs(self, *, limit: int, offset: int) -> tuple[list[Any], int]:
        self.received["list_model_runs"] = {"limit": limit, "offset": offset}
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
        ], self.total


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_intelligence_repository] = FakeIntelligenceRepository
    app.dependency_overrides[get_crisis_prediction_reads_enabled] = lambda: True
    try:
        yield TestClient(app, client=("127.0.0.1", 5000))
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def prediction_client() -> Iterator[TestClient]:
    app.dependency_overrides[get_intelligence_repository] = FakeIntelligenceRepository
    app.dependency_overrides[get_crisis_prediction_reads_enabled] = lambda: True
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
    assert (
        responses[f"/api/v1/companies/{COMPANY_ID}"].json()["company"]["risk_rollup"]["top_driver"]
        == "supply chain disruption"
    )
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


# --- Expanded event-list contract (Stage 7 item 3) -----------------------------------


def test_events_list_item_carries_scores_status_and_embedded_companies(client: TestClient) -> None:
    item = client.get("/api/v1/events").json()["items"][0]
    # The event core survives, plus the new derived scores/status.
    assert item["id"] == str(EVENT_ID)
    assert item["hotness_score"] == 71.0
    assert item["risk_score"] == 58.0
    # 58 falls in the canonical 56-75 "high" band (risk_level_for_score).
    assert item["risk_level"] == "high"
    assert item["confidence_score"] == 0.81
    assert item["status"] == "developing"
    assert item["first_seen_at"].endswith("Z")
    # Company identity is embedded with name and ticker -- not a bare UUID.
    company = item["companies"][0]
    assert company["company_id"] == str(COMPANY_ID)
    assert company["display_name"] == "Example Semiconductors"
    assert company["primary_ticker"] == "EXSM"
    assert company["impact_direction"] == "negative"
    assert company["risk_score"] == 58.0
    # Linked industry/location identity is preserved too.
    assert item["industries"][0]["industry_id"] == "semiconductors"
    assert item["locations"][0]["location_name"] == "Port of Los Angeles"


def test_events_list_null_risk_stays_null_never_fabricated() -> None:
    repo = FakeIntelligenceRepository()
    repo.list_events = lambda **_: (  # type: ignore[method-assign]
        [
            EventListRow(
                event=repo.event,
                risk_score=None,
                confidence_score=None,
                status="new",
                companies=(),
                industries=(),
                locations=(),
            )
        ],
        1,
    )
    try:
        item = _client_with(repo).get("/api/v1/events").json()["items"][0]
    finally:
        app.dependency_overrides.clear()
    # A missing score is not invented, and no level is guessed from it.
    assert item["risk_score"] is None
    assert item["risk_level"] is None
    assert item["confidence_score"] is None
    assert item["status"] == "new"


@pytest.mark.parametrize(
    "query",
    ["status=bogus", "risk_level=extreme", "date_from=2026-06-20&date_to=2026-06-01"],
)
def test_events_list_rejects_invalid_filters_with_422(query: str) -> None:
    repo = FakeIntelligenceRepository()
    try:
        resp = _client_with(repo).get(f"/api/v1/events?{query}")
    finally:
        app.dependency_overrides.clear()
    # Unknown enum values and an inverted range are 422 -- never a silently empty page.
    assert resp.status_code == 422
    assert set(resp.json()) == {"error"}


@pytest.mark.parametrize(
    "query",
    ["status=resolved", "risk_level=high", "date_from=2026-06-01&date_to=2026-06-30"],
)
def test_events_list_accepts_valid_filters(query: str) -> None:
    repo = FakeIntelligenceRepository()
    try:
        resp = _client_with(repo).get(f"/api/v1/events?{query}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200


def test_event_detail_companies_embed_name_and_ticker(client: TestClient) -> None:
    body = client.get(f"/api/v1/events/{EVENT_ID}").json()
    company = body["companies"][0]
    # Detail companies carry identity too, so the adapter performs no per-company UUID read.
    assert company["company_id"] == str(COMPANY_ID)
    assert company["display_name"] == "Example Semiconductors"
    assert company["primary_ticker"] == "EXSM"
    assert body["industries"][0]["industry_id"] == "semiconductors"


# --- Expanded dashboard contract (Stage 7 item 4) ------------------------------------


def test_dashboard_has_five_new_blocks_and_preserves_existing(client: TestClient) -> None:
    body = client.get("/api/v1/dashboard").json()
    # The existing blocks survive untouched.
    assert body["summary"]["overall_risk_level"] == "medium"
    assert isinstance(body["risk_scores"], list)
    assert body["alerts"]["open_count"] == 2
    # All five new top-level blocks exist and are lists.
    for block in (
        "metrics",
        "upcoming_triggers",
        "event_map",
        "company_ranking",
        "industry_summary",
    ):
        assert isinstance(body[block], list), block


def test_dashboard_metrics_are_real_counts_plus_overall_risk(client: TestClient) -> None:
    metrics = {m["label"]: m for m in client.get("/api/v1/dashboard").json()["metrics"]}
    assert metrics["Events Today"]["value"] == 5
    assert metrics["High-Risk Events"]["value"] == 2
    assert metrics["Affected Industries"]["value"] == 3
    assert metrics["Affected Companies"]["value"] == 4
    # Open alerts reuses the same live count the alerts block reports.
    assert metrics["Open Alerts"]["value"] == 2
    # The only severity carried is the persisted daily-summary band.
    assert metrics["Overall Risk"]["value"] == "medium"
    assert metrics["Overall Risk"]["severity"] == "medium"
    # A bare count invents neither a severity nor a comparison window.
    assert "severity" not in metrics["Events Today"]
    assert "previous_value" not in metrics["Events Today"]
    assert "change" not in metrics["Events Today"]


def test_dashboard_metrics_omit_overall_risk_when_no_summary() -> None:
    repo = FakeIntelligenceRepository()
    repo.latest_daily_summary = lambda: None  # type: ignore[method-assign]
    try:
        body = _client_with(repo).get("/api/v1/dashboard").json()
    finally:
        app.dependency_overrides.clear()
    assert body["summary"] is None
    assert "Overall Risk" not in [m["label"] for m in body["metrics"]]


def test_dashboard_event_map_projects_coordinates_and_keeps_unique_ids(client: TestClient) -> None:
    point = client.get("/api/v1/dashboard").json()["event_map"][0]
    assert point["location_name"] == "Washington, D.C."
    assert point["latitude"] == 38.9
    assert point["longitude"] == -77.0
    # Deterministic equirectangular projection to 0..1 (north at the top).
    assert point["x"] == (-77.0 + 180.0) / 360.0
    assert point["y"] == (90.0 - 38.9) / 180.0
    assert point["max_risk_score"] == 76.0
    assert point["dominant_event_type"] == "policy"
    assert point["related_event_ids"] == [str(EVENT_ID)]
    assert len(set(point["related_event_ids"])) == len(point["related_event_ids"])


def test_dashboard_company_ranking_carries_identity_and_null_direction(client: TestClient) -> None:
    row = client.get("/api/v1/dashboard").json()["company_ranking"][0]
    assert row["company_id"] == str(COMPANY_ID)
    assert row["name"] == "Example Semiconductors"
    assert row["ticker"] == "EXSM"
    assert row["industry"] == "Semiconductors"
    assert row["country"] == "US"
    assert row["risk_score"] == 58.0
    assert row["top_driver"] == "supply chain disruption"
    assert row["last_updated_at"].endswith("Z")
    # The rollup persists no direction: null, never fabricated from the score sign.
    assert row["impact_direction"] is None


def test_dashboard_industry_summary_uses_id_as_name_and_null_direction(client: TestClient) -> None:
    row = client.get("/api/v1/dashboard").json()["industry_summary"][0]
    assert row["industry_id"] == "semiconductors"
    # No catalog: the persisted industry_id is the display identity.
    assert row["industry_name"] == "semiconductors"
    assert row["risk_score"] == 55.0
    assert row["news_velocity_score"] == 80.0
    assert row["direction"] is None


def test_dashboard_upcoming_trigger_uses_description_as_reason(client: TestClient) -> None:
    trigger = client.get("/api/v1/dashboard").json()["upcoming_triggers"][0]
    assert trigger["title"] == "Labor-market data release"
    assert trigger["reason"] == "Single most important input to the recession estimate."
    assert trigger["related_event_id"] == str(EVENT_ID)
    # A timeline item carries no company link.
    assert trigger["related_company_id"] is None
    assert trigger["importance"] == "high"
    assert trigger["expected_at"].endswith("Z")


# --- Consumed-list pagination envelope (Stage 7 item 2) -------------------------------

#: Every consumed list endpoint standardized onto `?limit=&offset=` + {items,total,limit,offset},
#: paired with the repository method (recorded in `received`) it must thread the page through.
_CONSUMED_LIST_ENDPOINTS = [
    ("/api/v1/events", "list_events"),
    ("/api/v1/geo/events", "list_geo_events"),
    ("/api/v1/risk-radar", "list_risk_radar_scores"),
    ("/api/v1/industries", "list_industries"),
    ("/api/v1/companies", "list_companies"),
    ("/api/v1/alerts", "list_alerts"),
    ("/api/v1/watchlist", "list_watchlist"),
    ("/api/v1/admin/jobs", "list_jobs"),
    ("/api/v1/admin/sources", "list_sources"),
    ("/api/v1/admin/models", "list_model_runs"),
]


def _client_with(
    repo: FakeIntelligenceRepository, *, prediction_reads_enabled: bool = True
) -> TestClient:
    app.dependency_overrides[get_intelligence_repository] = lambda: repo
    app.dependency_overrides[get_crisis_prediction_reads_enabled] = lambda: prediction_reads_enabled
    return TestClient(app, client=("127.0.0.1", 5000))


def test_gate_g_closed_withholds_prediction_backed_api_outputs_without_reading_them() -> None:
    repo = FakeIntelligenceRepository()

    def forbidden_read(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("closed Gate G attempted a prediction-backed repository read")

    repo.latest_daily_summary = forbidden_read  # type: ignore[method-assign]
    repo.count_open_alerts = forbidden_read  # type: ignore[method-assign]
    repo.company_ranking = forbidden_read  # type: ignore[method-assign]
    repo.dashboard_industry_summary = forbidden_read  # type: ignore[method-assign]
    repo.list_industries = forbidden_read  # type: ignore[method-assign]
    repo.get_industry = forbidden_read  # type: ignore[method-assign]
    repo.list_alerts = forbidden_read  # type: ignore[method-assign]
    try:
        closed = _client_with(repo, prediction_reads_enabled=False)

        dashboard = closed.get("/api/v1/dashboard").json()
        assert dashboard["summary"] is None
        assert dashboard["alerts"] == {"open_count": 0}
        assert dashboard["company_ranking"] == []
        assert dashboard["industry_summary"] == []
        assert dashboard["risk_scores"][0]["score"] == 41.5
        assert dashboard["upcoming_triggers"]
        assert (
            repo.received["dashboard_metric_counts"]["prediction_backed_outputs_enabled"] is False
        )
        assert repo.received["event_map_policy"]["prediction_backed_outputs_enabled"] is False

        event = closed.get("/api/v1/events").json()["items"][0]
        company = event["companies"][0]
        industry = event["industries"][0]
        assert company["display_name"] == "Example Semiconductors"
        assert company["exposure_explanation"] is None
        assert company["impact_direction"] is None
        assert company["impact_score"] is None
        assert company["risk_score"] is None
        assert industry["industry_id"] == "semiconductors"
        assert industry["impact_direction"] is None
        assert industry["impact_score"] is None
        assert industry["risk_score"] is None
        assert industry["opportunity_score"] is None
        assert repo.received["list_events_policy"]["prediction_backed_outputs_enabled"] is False

        company_detail = closed.get(f"/api/v1/companies/{COMPANY_ID}").json()["company"]
        assert company_detail["display_name"] == "Example Semiconductors"
        assert "risk_rollup" not in company_detail
        assert repo.received["get_company"]["prediction_backed_outputs_enabled"] is False

        assert closed.get("/api/v1/industries").json()["items"] == []
        assert closed.get("/api/v1/industries/semiconductors").status_code == 404
        assert closed.get("/api/v1/alerts").json()["items"] == []

        # The fake's only report is explicitly prediction-backed, so even an alternate
        # repository implementation that returned it is filtered again at the endpoint.
        reports = closed.get("/api/v1/reports").json()
        assert reports == {"items": [], "count": 0}
        assert repo.received["list_reports_policy"]["prediction_backed_outputs_enabled"] is False
    finally:
        app.dependency_overrides.clear()


def test_gate_g_closed_report_listing_accepts_only_explicit_descriptive_policy() -> None:
    repo = FakeIntelligenceRepository()
    rows = repo.list_reports(
        user_id=None,
        limit=50,
        prediction_backed_outputs_enabled=False,
    )
    rows[0][0].content_policy = REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
    repo.list_reports = lambda **_kwargs: rows  # type: ignore[method-assign]
    try:
        payload = _client_with(repo, prediction_reads_enabled=False).get("/api/v1/reports").json()
    finally:
        app.dependency_overrides.clear()

    assert payload["count"] == 1
    assert payload["items"][0]["sections"][0]["title"] == "Overview"


@pytest.mark.parametrize(("path", "method"), _CONSUMED_LIST_ENDPOINTS)
def test_consumed_list_endpoint_uses_exact_pagination_envelope(path: str, method: str) -> None:
    repo = FakeIntelligenceRepository()
    try:
        resp = _client_with(repo).get(f"{path}?limit=7&offset=3")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    # Exactly the four envelope keys -- and crucially no `count` (api-adapter-contract).
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 7
    assert body["offset"] == 3
    # `total` is the real filtered relation size, not `len(items)`, on a partial page.
    assert body["total"] == PARTIAL_TOTAL
    assert len(body["items"]) == 1
    assert body["total"] > len(body["items"])
    # The page arguments actually reached the repository read.
    assert repo.received[method] == {"limit": 7, "offset": 3}


@pytest.mark.parametrize("path", [path for path, _ in _CONSUMED_LIST_ENDPOINTS])
def test_consumed_list_endpoint_rejects_negative_offset(path: str) -> None:
    repo = FakeIntelligenceRepository()
    try:
        resp = _client_with(repo).get(f"{path}?offset=-1")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_admin_sources_item_merges_latest_health_and_drops_top_level_lists() -> None:
    repo = FakeIntelligenceRepository()
    try:
        body = _client_with(repo).get("/api/v1/admin/sources").json()
    finally:
        app.dependency_overrides.clear()

    # The old nonstandard top-level sources/health contract is gone.
    assert set(body) == {"items", "total", "limit", "offset"}
    item = body["items"][0]
    assert item["id"] == str(repo.source.id)
    assert item["name"] == "GDELT"
    # Latest health is merged into the item so the adapter maps straight to SourceStatus.
    assert item["latest_health"]["status"] == "healthy"
    assert item["latest_health"]["source_id"] == str(repo.source.id)
    assert item["latest_health"]["error_rate"] == 0.0


def test_admin_sources_item_health_is_null_when_absent() -> None:
    repo = FakeIntelligenceRepository()
    repo.list_sources = lambda *, active, limit, offset: ([(repo.source, None)], 1)  # type: ignore[method-assign]
    try:
        body = _client_with(repo).get("/api/v1/admin/sources").json()
    finally:
        app.dependency_overrides.clear()
    assert body["items"][0]["latest_health"] is None


# --- Risk-radar detail RiskDetail contract (Stage 7 item 5) ---------------------------

#: Every key types.ts RiskDetail requires, in snake_case. The detail must carry exactly these
#: (plus the real observation metadata) -- and never the raw-observation `items`/`count` shape.
_RISK_DETAIL_KEYS = {
    "risk_type",
    "score",
    "severity",
    "model_rating",
    "probability_by_horizon",
    "main_drivers",
    "signals",
    "historical_comparisons",
    "leading_indicators",
    "invalidation_signals",
    "related_event_ids",
    "related_industry_ids",
    "related_company_ids",
}


def test_risk_detail_returns_riskdetail_envelope_not_raw_observations(client: TestClient) -> None:
    body = client.get("/api/v1/risk-radar/sovereign").json()
    # A single detail object under `risk`, not the consumed-list/raw-observation shape.
    assert set(body) == {"risk"}
    assert "items" not in body and "count" not in body
    risk = body["risk"]
    assert _RISK_DETAIL_KEYS <= set(risk)
    # Spine comes from the latest real observation, not a fixture.
    assert risk["risk_type"] == "sovereign"
    assert risk["score"] == 41.5
    assert risk["severity"] == "medium"
    assert risk["confidence_score"] == 0.82
    assert risk["as_of"] == "2026-06-20T12:00:00Z"
    assert risk["model_version"] == "risk-v1"
    assert risk["target_type"] == "country"
    assert risk["target_id"] == "US"


def test_risk_detail_gate_closed_never_reads_or_serializes_crisis_prediction() -> None:
    repo = FakeIntelligenceRepository()

    def fail_prediction_read(**_: Any) -> Any:
        raise AssertionError("closed Gate G must not query CrisisPrediction")

    repo.latest_crisis_prediction = fail_prediction_read  # type: ignore[method-assign]
    try:
        risk = (
            _client_with(repo, prediction_reads_enabled=False)
            .get("/api/v1/risk-radar/sovereign")
            .json()["risk"]
        )
    finally:
        app.dependency_overrides.clear()

    assert risk["score"] == 41.5
    assert risk["severity"] == "medium"
    assert risk["main_drivers"] == ["macro"]
    assert risk["model_rating"] is None
    assert risk["probability_by_horizon"] == []
    assert risk["historical_comparisons"] == []
    assert risk["invalidation_signals"] == []
    assert "latest_crisis_prediction" not in repo.received


def test_historical_gate_closed_never_reads_or_serializes_crisis_prediction() -> None:
    repo = FakeIntelligenceRepository()

    def fail_historical_read(**_: Any) -> Any:
        raise AssertionError("closed Gate G must not query CrisisPrediction history")

    repo.list_historical = fail_historical_read  # type: ignore[method-assign]
    try:
        response = _client_with(repo, prediction_reads_enabled=False).get("/api/v1/historical")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


def test_historical_gate_open_returns_crisis_prediction_rows() -> None:
    repo = FakeIntelligenceRepository()
    try:
        response = _client_with(repo, prediction_reads_enabled=True).get("/api/v1/historical")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["risk_type"] == "sovereign"


def test_risk_detail_model_rating_serializes_every_crisisrating_field(
    prediction_client: TestClient,
) -> None:
    rating = prediction_client.get("/api/v1/risk-radar/sovereign").json()["risk"]["model_rating"]
    assert set(rating) == {
        "target_type",
        "target_id",
        "risk_type",
        "as_of_date",
        "probability_0_6m",
        "probability_6_12m",
        "probability_12_18m",
        "probability_within_18m",
        "risk_score",
        "risk_level",
        "confidence_score",
        "model_versions",
        "top_drivers",
        "evidence_refs",
        "what_could_escalate",
        "what_could_reduce_risk",
    }
    assert rating["as_of_date"] == "2026-06-20"
    assert rating["probability_within_18m"] == 0.30
    assert rating["risk_level"] == "medium"
    assert rating["model_versions"] == {"ensemble": "ensemble.v1"}
    assert rating["what_could_reduce_risk"] == ["IMF backstop confirmed", "FX reserves rebuild"]


def test_risk_detail_horizons_use_canonical_tokens_from_the_prediction(
    prediction_client: TestClient,
) -> None:
    horizons = prediction_client.get("/api/v1/risk-radar/sovereign").json()["risk"][
        "probability_by_horizon"
    ]
    assert horizons == [
        {"horizon": "0_6m", "probability": 0.10},
        {"horizon": "6_12m", "probability": 0.15},
        {"horizon": "12_18m", "probability": 0.05},
        {"horizon": "within_18m", "probability": 0.30},
    ]


def test_risk_detail_drivers_analogies_and_invalidation_are_real_and_skip_unlabeled(
    prediction_client: TestClient,
) -> None:
    risk = prediction_client.get("/api/v1/risk-radar/sovereign").json()["risk"]
    # Labeled drivers only (name, then signal); the score-only entry is skipped, not renamed.
    assert risk["main_drivers"] == ["Deposit outflows", "credit_spread_zscore"]
    # Historical comparisons are the real analogy titles.
    assert risk["historical_comparisons"] == ["1998 sovereign stress episode"]
    # Invalidation signals are exactly what_could_reduce_risk.
    assert risk["invalidation_signals"] == ["IMF backstop confirmed", "FX reserves rebuild"]
    # No persisted source for these two, so they are empty -- never fabricated.
    assert risk["signals"] == []
    assert risk["leading_indicators"] == []


def test_risk_detail_related_ids_split_by_target_and_drop_unsupported(client: TestClient) -> None:
    risk = client.get("/api/v1/risk-radar/sovereign").json()["risk"]
    assert risk["related_event_ids"] == [str(EVENT_ID)]
    assert risk["related_industry_ids"] == ["semiconductors"]
    assert risk["related_company_ids"] == [str(COMPANY_ID)]
    # `country` targets have no RiskDetail field: they are dropped, not forced onto a list.
    for ids in (
        risk["related_event_ids"],
        risk["related_industry_ids"],
        risk["related_company_ids"],
    ):
        assert "US" not in ids


def test_risk_detail_matches_prediction_to_the_chosen_observation_target() -> None:
    repo = FakeIntelligenceRepository()
    try:
        _client_with(repo, prediction_reads_enabled=True).get("/api/v1/risk-radar/sovereign")
    finally:
        app.dependency_overrides.clear()
    # The prediction lookup is handed the observation's own target so it can prefer a matching row.
    assert repo.received["latest_crisis_prediction"] == {
        "risk_type": "sovereign",
        "target_type": "country",
        "target_id": "US",
    }


def test_risk_detail_null_model_rating_when_no_prediction() -> None:
    repo = FakeIntelligenceRepository()
    repo.latest_crisis_prediction = lambda **_: None  # type: ignore[method-assign]
    try:
        risk = (
            _client_with(repo, prediction_reads_enabled=True)
            .get("/api/v1/risk-radar/sovereign")
            .json()["risk"]
        )
    finally:
        app.dependency_overrides.clear()
    # No prediction: model_rating/horizons/analogies/invalidation collapse to null/[] -- never stubbed.
    assert risk["model_rating"] is None
    assert risk["probability_by_horizon"] == []
    assert risk["historical_comparisons"] == []
    assert risk["invalidation_signals"] == []
    # main_drivers falls back to the observation's own real driver_refs ([{"name": "macro"}]).
    assert risk["main_drivers"] == ["macro"]
    # The spine still comes from the real observation.
    assert risk["score"] == 41.5
    assert risk["severity"] == "medium"


def test_risk_detail_404_when_no_observation() -> None:
    repo = FakeIntelligenceRepository()
    repo.latest_risk_observation = lambda **_: None  # type: ignore[method-assign]
    try:
        resp = _client_with(repo).get("/api/v1/risk-radar/sovereign")
    finally:
        app.dependency_overrides.clear()
    # No real observation -> 404 error envelope, never a zeroed/fixture detail.
    assert resp.status_code == 404
    assert set(resp.json()) == {"error"}
    assert resp.json()["error"]["code"] == "not_found"


# --- Risk-radar history time series (Stage 7 item 5 / shape gap 3) --------------------


def test_risk_history_uses_pagination_envelope_and_real_total() -> None:
    repo = FakeIntelligenceRepository()
    try:
        resp = _client_with(repo).get(
            "/api/v1/risk-radar/sovereign/history?days=30&limit=7&offset=3"
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    # Exactly the consumed-list envelope -- no `count`.
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 7
    assert body["offset"] == 3
    assert body["total"] == PARTIAL_TOTAL
    assert body["total"] > len(body["items"])
    # Page args + risk_type reached the repository read.
    threaded = repo.received["risk_observation_history"]
    assert threaded["risk_type"] == "sovereign"
    assert threaded["limit"] == 7
    assert threaded["offset"] == 3
    # Each item is a real observation row (as_of UTC-Z, score, level, target/model metadata).
    item = body["items"][0]
    assert item["as_of"] == "2026-06-20T12:00:00Z"
    assert item["score"] == 41.5
    assert item["level"] == "medium"
    assert item["risk_type"] == "sovereign"


def test_risk_history_cutoff_uses_fixed_utc_now_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    import apps.api.intelligence as intel

    monkeypatch.setattr(intel, "_utc_now", lambda: NOW)
    repo = FakeIntelligenceRepository()
    try:
        _client_with(repo).get("/api/v1/risk-radar/sovereign/history?days=10")
    finally:
        app.dependency_overrides.clear()
    # cutoff = fixed now - days, deterministic under the seam.
    assert repo.received["risk_observation_history"]["cutoff"] == NOW - datetime.timedelta(days=10)


@pytest.mark.parametrize("query", ["days=0", "days=366", "days=-5", "days=abc"])
def test_risk_history_rejects_out_of_range_days_with_422(query: str) -> None:
    repo = FakeIntelligenceRepository()
    try:
        resp = _client_with(repo).get(f"/api/v1/risk-radar/sovereign/history?{query}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422
    assert set(resp.json()) == {"error"}


def test_risk_history_rejects_negative_offset_with_422() -> None:
    repo = FakeIntelligenceRepository()
    try:
        resp = _client_with(repo).get("/api/v1/risk-radar/sovereign/history?offset=-1")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_risk_history_does_not_shadow_the_detail_route(client: TestClient) -> None:
    # `/{risk_type}` is the RiskDetail; `/{risk_type}/history` is the paginated series -- distinct.
    detail = client.get("/api/v1/risk-radar/sovereign").json()
    history = client.get("/api/v1/risk-radar/sovereign/history").json()
    assert set(detail) == {"risk"}
    assert set(history) == {"items", "total", "limit", "offset"}
