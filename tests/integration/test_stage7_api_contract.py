"""Stage 7 frontend read contracts against a live, migrated, disposable Postgres.

The DB-free wiring -- routing, status codes, the pagination/RiskDetail wire shapes, the query
*statement* bounds -- is proven against fakes and a recording session in tests/unit/
test_intelligence_api.py, test_pagination_queries.py and test_http_wire.py. What only real SQL can
prove lives here, driven through the *real* ``IntelligenceRepository`` over a throwaway database:

* ``GET /api/v1/events`` -- the count/page/derived-column statements actually compile and execute on
  Postgres (correlated risk/status subqueries, the ``events.id::text`` cast join for company/risk,
  the inclusive UTC-day activity window), and the ``total``, filters (risk_level / status / industry
  / company-by-ticker/name/UUID / date), derived scores and embedded identities agree with seeded
  facts.
* ``GET /api/v1/dashboard`` -- the five added blocks read real persisted facts and the
  latest/deduplicated rollups (DISTINCT ON per company/industry), never a fixture backfill.
* ``GET /api/v1/risk-radar`` / ``/{risk_type}`` / ``/{risk_type}/history`` -- the paginated list,
  the descriptive RiskDetail while Gate G is closed, the explicitly enabled matched-prediction
  enrichment, grouped related targets, and the chronological series with a real filtered total.
* ``GET /api/v1/evidence/{claim_id}`` -- the ``claim_evidence -> evidence_items`` join with the
  article cast/left-join still yields a safe summary-first snippet and never article body/raw payload.
* the accepted latest-per-industry dedup (shape gap 6) still selects one row per industry.
* every consumed timestamp serializes as trailing-``Z`` UTC.

Database hygiene (folder workflow rules): never the default ``news`` database. This module owns a
throwaway ``nip_stage7_<hex>`` database on ``localhost:55432``, migrated with ``alembic upgrade
head`` (so the SQL runs against the accepted schema), seeded once, read-only thereafter, and dropped
with a leak check on exit -- see ``tests/integration/_stage7_db``.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

import apps.api.intelligence as intelligence
from apps.api.main import app
from db.base import get_session
from db.models import (
    Alert,
    Article,
    Claim,
    ClaimEvidence,
    Company,
    CompanyRiskRollup,
    CrisisPrediction,
    DailyIntelligenceSummary,
    EntityProfile,
    Event,
    EventCompany,
    EventIndustry,
    EventLocation,
    EventTimelineItem,
    EvidenceItem,
    IndustryRiskRollup,
    RiskScoreObservation,
    Source,
    User,
)
from tests.integration import _stage7_db as stage7_db
from tests.integration._stage7_db import (
    FORBIDDEN_DB,
    STAGE7_DB_PREFIX,
    migrated_disposable_engine,
)

pytestmark = pytest.mark.integration

UTC = datetime.UTC

#: The fixed UTC clock the dashboard/history reads use through the ``_utc_now`` seam, so the
#: "today" metric window and the ``as_of >= now - days`` history cutoff are deterministic.
NOW = datetime.datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _dt(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, month, day, hour, minute, tzinfo=UTC)


@dataclass(frozen=True)
class Seeded:
    alpha_id: uuid.UUID
    bravo_id: uuid.UUID
    charlie_id: uuid.UUID
    exsm_company_id: uuid.UUID
    bbnk_company_id: uuid.UUID
    claim_id: uuid.UUID
    article_id: uuid.UUID
    total_events: int
    total_observations: int
    sovereign_observation_count: int
    future_trigger_id: uuid.UUID


# --------------------------------------------------------------------------------------
# Disposable, migrated database + one committed seed, shared read-only across the module.
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    with migrated_disposable_engine() as eng:
        yield eng


def _seed(session: Session) -> Seeded:
    # Two canonical companies (each needs its own entity profile: companies.entity_profile_id).
    exsm_profile = EntityProfile(
        canonical_name="Example Semiconductors", normalized_name="example semiconductors"
    )
    bbnk_profile = EntityProfile(canonical_name="Britannia Bank", normalized_name="britannia bank")
    session.add_all([exsm_profile, bbnk_profile])
    session.flush()
    exsm = Company(
        entity_profile_id=exsm_profile.id,
        display_name="Example Semiconductors",
        legal_name="Example Semiconductors Inc.",
        primary_ticker="EXSM",
        exchange="NASDAQ",
        country="US",
        sector="Technology",
        industry="Semiconductors",
        active=True,
    )
    bbnk = Company(
        entity_profile_id=bbnk_profile.id,
        display_name="Britannia Bank",
        legal_name="Britannia Bank plc",
        primary_ticker="BBNK",
        exchange="LSE",
        country="GB",
        sector="Financials",
        industry="Banking",
        active=True,
    )
    session.add_all([exsm, bbnk])
    session.flush()

    # ALPHA: US supply-chain event, coverage widening on 2026-07-14 -> derived status "developing".
    alpha = Event(
        title="Alpha port disruption affects semiconductor shipments",
        summary="A logistics shock delays components.",
        event_type="supply_chain",
        country="US",
        region="California",
        severity_score=67,
        hotness_score=71,
        article_count=4,
        source_count=3,
        first_seen_at=_dt(7, 14, 6),
        last_seen_at=_dt(7, 14, 8),
        created_at=_dt(7, 14, 6),
        updated_at=_dt(7, 14, 8),
    )
    # BRAVO: GB banking event, static coverage/record -> derived status "new"; no direct risk
    # observation, so event risk falls back to max linked company/industry risk (25 -> low).
    bravo = Event(
        title="Bravo regional bank stress",
        summary="Deposit outflows at a regional lender.",
        event_type="banking",
        country="GB",
        region="London",
        severity_score=40,
        hotness_score=40,
        first_seen_at=_dt(7, 12, 9),
        last_seen_at=_dt(7, 12, 9),
        created_at=_dt(7, 12, 9),
        updated_at=_dt(7, 12, 9),
    )
    # CHARLIE: US energy event with a resolved (and no live) alert -> derived status "resolved";
    # a direct critical observation (90) is its event risk.
    charlie = Event(
        title="Charlie refinery outage",
        summary="An outage curbs regional supply.",
        event_type="energy",
        country="US",
        region="Texas",
        severity_score=55,
        hotness_score=55,
        first_seen_at=_dt(7, 10, 10),
        last_seen_at=_dt(7, 10, 10),
        created_at=_dt(7, 10, 10),
        updated_at=_dt(7, 10, 10),
    )
    session.add_all([alpha, bravo, charlie])
    session.flush()

    session.add_all(
        [
            EventCompany(
                event_id=alpha.id,
                company_id=exsm.id,
                impact_direction="negative",
                impact_score=72,
                risk_score=58,
                confidence_score=0.81,
                exposure_explanation="Direct supplier of delayed components.",
            ),
            EventCompany(
                event_id=bravo.id,
                company_id=bbnk.id,
                impact_direction="negative",
                impact_score=40,
                risk_score=20,
                confidence_score=0.6,
            ),
            EventIndustry(
                event_id=alpha.id,
                industry_id="semiconductors",
                impact_direction="negative",
                impact_score=69,
                risk_score=55,
                opportunity_score=38,
            ),
            EventIndustry(
                event_id=bravo.id,
                industry_id="banking",
                impact_direction="negative",
                impact_score=50,
                risk_score=25,
                opportunity_score=10,
            ),
            EventIndustry(
                event_id=charlie.id,
                industry_id="energy",
                impact_direction="negative",
                impact_score=60,
                risk_score=45,
                opportunity_score=20,
            ),
            # ALPHA and CHARLIE carry mappable coordinates; BRAVO's are NULL (unplaceable -> off map).
            EventLocation(
                event_id=alpha.id,
                location_name="Port of Los Angeles",
                country_code="US",
                region="California",
                latitude=33.7361,
                longitude=-118.2922,
                location_type="port",
                confidence_score=0.91,
                created_at=_dt(7, 14, 6),
            ),
            EventLocation(
                event_id=charlie.id,
                location_name="Houston",
                country_code="US",
                region="Texas",
                latitude=29.7604,
                longitude=-95.3698,
                location_type="city",
                confidence_score=0.8,
                created_at=_dt(7, 10, 10),
            ),
            EventLocation(
                event_id=bravo.id,
                location_name="London",
                country_code="GB",
                region="London",
                latitude=None,
                longitude=None,
                location_type="city",
                confidence_score=0.7,
                created_at=_dt(7, 12, 9),
            ),
        ]
    )

    # Timeline: one genuinely future entry (after NOW) and one past entry, both on ALPHA.
    future_trigger = EventTimelineItem(
        event_id=alpha.id,
        timestamp=_dt(7, 16, 9),
        title="Labor-market data release",
        description="Single most important input to the recession estimate.",
        importance="high",
    )
    session.add_all(
        [
            future_trigger,
            EventTimelineItem(
                event_id=alpha.id,
                timestamp=_dt(7, 13, 9),
                title="Initial disruption reported",
                description="Local authorities confirmed delays.",
                importance="medium",
            ),
        ]
    )
    session.flush()

    # Risk observations. ALPHA/CHARLIE carry the event-target risk the list/dashboard read; the
    # five sovereign rows (across four target types) drive the risk-radar detail/history. ALPHA's
    # sovereign row (07-11) is older than its supply-chain row (07-14T08), so ALPHA's event risk
    # stays 58, while the latest *sovereign* row is the 07-14 country/US spine.
    session.add_all(
        [
            RiskScoreObservation(
                target_type="event",
                target_id=str(alpha.id),
                risk_type="supply_chain",
                score=58,
                level="high",
                confidence_score=0.81,
                as_of=_dt(7, 14, 8),
                model_version="risk-v1",
                driver_refs=[{"name": "port throughput"}],
            ),
            RiskScoreObservation(
                target_type="event",
                target_id=str(charlie.id),
                risk_type="energy",
                score=90,
                level="critical",
                confidence_score=0.7,
                as_of=_dt(7, 10, 10),
                model_version="risk-v1",
            ),
            RiskScoreObservation(
                target_type="country",
                target_id="US",
                risk_type="sovereign",
                score=40,
                level="medium",
                confidence_score=0.80,
                as_of=_dt(7, 10),
                model_version="risk-v1",
                evidence_refs=[{"id": "e1"}],
                driver_refs=[{"name": "macro"}],
            ),
            RiskScoreObservation(
                target_type="event",
                target_id=str(alpha.id),
                risk_type="sovereign",
                score=35,
                level="medium",
                confidence_score=0.5,
                as_of=_dt(7, 11),
                model_version="risk-v1",
            ),
            RiskScoreObservation(
                target_type="industry",
                target_id="semiconductors",
                risk_type="sovereign",
                score=45,
                level="medium",
                confidence_score=0.6,
                as_of=_dt(7, 12),
                model_version="risk-v1",
            ),
            RiskScoreObservation(
                target_type="company",
                target_id=str(exsm.id),
                risk_type="sovereign",
                score=50,
                level="medium",
                confidence_score=0.7,
                as_of=_dt(7, 13),
                model_version="risk-v1",
            ),
            RiskScoreObservation(
                target_type="country",
                target_id="US",
                risk_type="sovereign",
                score=41.5,
                level="medium",
                confidence_score=0.82,
                as_of=_dt(7, 14),
                model_version="risk-v1",
                driver_refs=[{"name": "Observed macro stress"}],
            ),
        ]
    )

    # The matching CrisisPrediction (country/US) supplies the RiskDetail model_rating. A same-
    # risk_type but different-target (country/GB) prediction is deliberately *more recent* to prove
    # target match beats recency; a different-risk_type (banking) prediction must never be returned.
    session.add_all(
        [
            CrisisPrediction(
                target_type="country",
                target_id="US",
                risk_type="sovereign",
                as_of_date=datetime.date(2026, 7, 14),
                probability_0_6m=0.10,
                probability_6_12m=0.15,
                probability_12_18m=0.05,
                probability_within_18m=0.30,
                risk_score=41.5,
                risk_level="medium",
                confidence_score=0.82,
                model_versions={"ensemble": "ensemble.v1"},
                top_drivers=[
                    {"name": "Deposit outflows", "contribution": 12.0},
                    {"signal": "credit_spread_zscore", "contribution": 8.0},
                    {"score": 1.0},
                ],
                historical_analogies=[
                    {"component": "historical_analogy", "title": "1998 sovereign stress episode"}
                ],
                evidence_refs=[{"source_type": "article", "source_id": "a1"}],
                what_could_escalate=["Sovereign rating downgrade"],
                what_could_reduce_risk=["IMF backstop confirmed", "FX reserves rebuild"],
            ),
            CrisisPrediction(
                target_type="country",
                target_id="GB",
                risk_type="sovereign",
                as_of_date=datetime.date(2026, 7, 15),
                probability_0_6m=0.2,
                probability_6_12m=0.2,
                probability_12_18m=0.2,
                probability_within_18m=0.4,
                risk_score=55,
                risk_level="medium",
                confidence_score=0.7,
            ),
            CrisisPrediction(
                target_type="country",
                target_id="US",
                risk_type="banking",
                as_of_date=datetime.date(2026, 7, 20),
                probability_0_6m=0.3,
                probability_6_12m=0.3,
                probability_12_18m=0.3,
                probability_within_18m=0.6,
                risk_score=70,
                risk_level="high",
                confidence_score=0.9,
            ),
        ]
    )

    # Company risk rollups: EXSM's latest (07-14, 58) must win over its stale one (07-10, 10).
    session.add_all(
        [
            CompanyRiskRollup(
                company_id=exsm.id,
                as_of=_dt(7, 10),
                impact_score=20,
                risk_score=10,
                related_event_count=1,
                top_driver="stale",
                confidence_score=0.5,
            ),
            CompanyRiskRollup(
                company_id=exsm.id,
                as_of=_dt(7, 14),
                impact_score=72,
                risk_score=58,
                related_event_count=2,
                top_driver="supply chain disruption",
                confidence_score=0.84,
            ),
            CompanyRiskRollup(
                company_id=bbnk.id,
                as_of=_dt(7, 12),
                impact_score=40,
                risk_score=30,
                related_event_count=1,
                top_driver="deposit outflows",
                confidence_score=0.7,
            ),
        ]
    )

    # Industry rollups: "semiconductors" has two snapshots (latest 07-14, risk 55); "banking" one.
    # The dedup must yield one row per industry (two total), latest per industry.
    session.add_all(
        [
            IndustryRiskRollup(
                industry_id="semiconductors",
                as_of=_dt(7, 10),
                impact_score=50,
                risk_score=40,
                news_velocity_score=60,
                related_event_count=3,
                summary="stale",
            ),
            IndustryRiskRollup(
                industry_id="semiconductors",
                as_of=_dt(7, 14),
                impact_score=69,
                risk_score=55,
                news_velocity_score=80,
                related_event_count=5,
                summary="Supply chain risk elevated.",
            ),
            IndustryRiskRollup(
                industry_id="banking",
                as_of=_dt(7, 12),
                impact_score=50,
                risk_score=30,
                news_velocity_score=40,
                related_event_count=2,
                summary="Banking risk moderate.",
            ),
        ]
    )

    # Latest daily summary (07-14, high) wins over the earlier one (07-13, medium).
    session.add_all(
        [
            DailyIntelligenceSummary(
                summary_date=datetime.date(2026, 7, 13),
                overall_risk_level="medium",
                confidence_score=0.7,
                summary="Yesterday.",
                key_points=["a"],
            ),
            DailyIntelligenceSummary(
                summary_date=datetime.date(2026, 7, 14),
                overall_risk_level="high",
                confidence_score=0.8,
                summary="Global risk elevated.",
                key_points=["Rates stable", "Energy risk elevated"],
            ),
        ]
    )

    # Alerts: a resolved alert on CHARLIE (its "resolved" status source) and one live "open" alert
    # (the dashboard open count is 1). Alerts need an owning user.
    user = User(email="analyst@test.example", display_name="Analyst")
    session.add(user)
    session.flush()
    session.add_all(
        [
            Alert(
                user_id=user.id,
                title="Charlie resolved",
                message="Cleared.",
                severity="low",
                alert_type="risk_threshold",
                state="resolved",
                related_event_id=charlie.id,
                resolved_at=_dt(7, 11),
            ),
            Alert(
                user_id=user.id,
                title="Open alert",
                message="Live.",
                severity="high",
                alert_type="risk_threshold",
                state="open",
            ),
        ]
    )

    # Evidence drawer: an article-backed supporting link and a filing-backed contradicting link on
    # one claim. The article carries a summary (the safe snippet) and a body/raw payload that must
    # never leak.
    source = Source(name="Reuters", feed_url=f"https://feed/{uuid.uuid4()}", authority_score=0.8)
    session.add(source)
    session.flush()
    article = Article(
        source_id=source.id,
        url=f"https://x/{uuid.uuid4()}",
        url_hash=uuid.uuid4().hex,
        title="Bank under pressure",
        summary="A concise summary of the article.",
        body="SECRET BODY that must never leak whole. " * 20,
        published_at=_dt(7, 14, 4),
    )
    session.add(article)
    session.flush()
    article_ev = EvidenceItem(
        source_type="article",
        source_id=str(article.id),
        title="Bank under pressure",
        publisher="Reuters",
        url="https://news.example/bank",
        published_at=_dt(7, 14, 4),
        credibility_score=0.8,
        raw_ref={"do_not": "expose"},
        evidence_metadata={"internal": "do_not_expose"},
    )
    filing_ev = EvidenceItem(source_type="filing", source_id="filing-1", title="Quarterly filing")
    session.add_all([article_ev, filing_ev])
    session.flush()
    claim = Claim(
        claim_text="The bank faces a liquidity squeeze.",
        claim_type="assertion",
        confidence_score=0.9,
    )
    session.add(claim)
    session.flush()
    session.add_all(
        [
            ClaimEvidence(
                claim_id=claim.id,
                evidence_item_id=article_ev.id,
                support_type="supports",
                confidence_score=0.88,
            ),
            ClaimEvidence(
                claim_id=claim.id, evidence_item_id=filing_ev.id, support_type="contradicts"
            ),
        ]
    )

    session.commit()
    return Seeded(
        alpha_id=alpha.id,
        bravo_id=bravo.id,
        charlie_id=charlie.id,
        exsm_company_id=exsm.id,
        bbnk_company_id=bbnk.id,
        claim_id=claim.id,
        article_id=article.id,
        total_events=3,
        total_observations=7,
        sovereign_observation_count=5,
        future_trigger_id=future_trigger.id,
    )


@pytest.fixture(scope="module")
def seeded(engine: Engine) -> Seeded:
    with Session(engine) as session:
        return _seed(session)


@pytest.fixture
def client(engine: Engine, seeded: Seeded, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose session seam is bound to the disposable DB (never ``news``).

    Overriding ``get_session`` alone points the intelligence repository at the disposable engine
    (FastAPI resolves it per request). ``_utc_now`` is pinned to :data:`NOW` so the dashboard
    "today"/future reads and the history cutoff are deterministic. Loopback client so the API-key
    middleware admits the requests in a keyless local env.
    """

    def _session_override() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(intelligence, "_utc_now", lambda: NOW)
    app.dependency_overrides[get_session] = _session_override
    # Stage 7's legacy contract scenarios intentionally exercise the diagnostic,
    # prediction-backed surface. Gate-G-closed scenarios override this fixture explicitly.
    app.dependency_overrides[intelligence.get_crisis_prediction_reads_enabled] = lambda: True
    try:
        yield TestClient(app, client=("127.0.0.1", 5000))
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------------------
# Database-hygiene guard (folder workflow rule): the owned database is a Stage 7 disposable.
# --------------------------------------------------------------------------------------


def test_owned_database_is_a_stage7_disposable_never_news(engine: Engine) -> None:
    name = engine.url.database
    assert name is not None
    assert name.startswith(STAGE7_DB_PREFIX)
    assert name != FORBIDDEN_DB and FORBIDDEN_DB not in name


# --------------------------------------------------------------------------------------
# Helper-level safety proofs (no Postgres, deterministic): a mandatory CREATE failure fails
# loudly and a pre-existing Stage 7 disposable blocks creation -- both pytest.fail, never skip.
# --------------------------------------------------------------------------------------


class _EngineFailingCreate:
    """A stand-in admin engine whose CREATE raises, recording that it is disposed on the way out."""

    def __init__(self) -> None:
        self.disposed = False

    def connect(self) -> _EngineFailingCreate:
        return self

    def __enter__(self) -> _EngineFailingCreate:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated CREATE DATABASE failure")

    def dispose(self) -> None:
        self.disposed = True


def test_disposable_create_failure_fails_loudly_and_never_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No pre-existing leak, but CREATE fails: the helper must pytest.fail (mandatory coverage never
    # silently skips) and dispose the admin engine on the way out. No real Postgres is touched.
    fake = _EngineFailingCreate()
    monkeypatch.setattr(stage7_db, "list_leaked_stage7_databases", lambda: [])
    monkeypatch.setattr(stage7_db, "admin_engine", lambda: fake)
    try:
        with stage7_db._disposable_database():
            raise AssertionError("a failed CREATE must stop before yielding")
    except pytest.skip.Exception as skipped:  # the exact defect being guarded against
        raise AssertionError(f"CREATE failure skipped, not failed: {skipped}") from skipped
    except pytest.fail.Exception as failed:
        assert "could not CREATE" in str(failed)
        assert fake.disposed is True
    else:
        raise AssertionError("expected a pytest.fail from the CREATE-failure branch")


def test_pre_existing_stage7_database_blocks_creation_and_never_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A leftover nip_stage7_* database halts the helper before any maintenance connection or CREATE:
    # it neither creates another disposable nor drops the pre-existing one, and it fails (not skips).
    def _admin_must_not_be_used() -> object:
        raise AssertionError("admin_engine must not be reached once a Stage 7 leak is detected")

    monkeypatch.setattr(stage7_db, "list_leaked_stage7_databases", lambda: ["nip_stage7_leftover"])
    monkeypatch.setattr(stage7_db, "admin_engine", _admin_must_not_be_used)
    try:
        with stage7_db._disposable_database():
            raise AssertionError("a pre-existing Stage 7 leak must stop before yielding")
    except pytest.skip.Exception as skipped:  # proving the leak-check cannot silently skip either
        raise AssertionError(f"pre-existing leak skipped, not failed: {skipped}") from skipped
    except pytest.fail.Exception as failed:
        assert "nip_stage7_leftover" in str(failed)
    else:
        raise AssertionError("expected a pytest.fail when a Stage 7 disposable already exists")


# --------------------------------------------------------------------------------------
# GET /api/v1/events (contract 1): pagination, filters, derived scores/status, embedded identity.
# --------------------------------------------------------------------------------------


def test_events_page_envelope_and_real_total_over_a_partial_page(
    client: TestClient, seeded: Seeded
) -> None:
    body = client.get("/api/v1/events?limit=2&offset=0").json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert (body["limit"], body["offset"]) == (2, 0)
    # Real filtered relation size, before LIMIT -- greater than the returned page length.
    assert body["total"] == seeded.total_events
    assert len(body["items"]) == 2
    assert body["total"] > len(body["items"])
    # Default order: newest coverage first (ALPHA 07-14, BRAVO 07-12, CHARLIE 07-10).
    assert [item["id"] for item in body["items"]] == [str(seeded.alpha_id), str(seeded.bravo_id)]
    # The offset page returns the remaining event and reports the same total.
    tail = client.get("/api/v1/events?limit=2&offset=2").json()
    assert [item["id"] for item in tail["items"]] == [str(seeded.charlie_id)]
    assert tail["total"] == seeded.total_events


def test_events_item_carries_derived_scores_status_and_embedded_identity(
    client: TestClient, seeded: Seeded
) -> None:
    alpha = client.get("/api/v1/events?limit=1").json()["items"][0]
    assert alpha["id"] == str(seeded.alpha_id)
    # Derived from the latest event-target observation (58 -> canonical "high"), confidence carried.
    assert alpha["hotness_score"] == 71.0
    assert alpha["risk_score"] == 58.0
    assert alpha["risk_level"] == "high"
    assert alpha["confidence_score"] == 0.81
    assert alpha["status"] == "developing"
    # Trailing-Z UTC on the event timestamps.
    assert alpha["first_seen_at"] == "2026-07-14T06:00:00Z"
    assert alpha["created_at"].endswith("Z")
    # Company identity embedded via the cast id-join -- name + ticker, not a bare UUID.
    company = alpha["companies"][0]
    assert company["company_id"] == str(seeded.exsm_company_id)
    assert company["display_name"] == "Example Semiconductors"
    assert company["primary_ticker"] == "EXSM"
    assert company["risk_score"] == 58.0
    assert alpha["industries"][0]["industry_id"] == "semiconductors"
    assert alpha["locations"][0]["location_name"] == "Port of Los Angeles"


def test_events_fallback_risk_is_max_linked_score_with_null_confidence(
    client: TestClient, seeded: Seeded
) -> None:
    # BRAVO has no direct observation: risk = greatest(company 20, industry 25) = 25 -> "low",
    # and confidence stays null (no direct observation to read it from) -- never fabricated.
    items = {item["id"]: item for item in client.get("/api/v1/events?limit=50").json()["items"]}
    bravo = items[str(seeded.bravo_id)]
    assert bravo["risk_score"] == 25.0
    assert bravo["risk_level"] == "low"
    assert bravo["confidence_score"] is None
    assert bravo["status"] == "new"


@pytest.mark.parametrize(
    ("query", "expected_ids"),
    [
        ("country=us", ("alpha_id", "charlie_id")),
        ("event_type=banking", ("bravo_id",)),
        ("risk_level=high", ("alpha_id",)),
        ("risk_level=critical", ("charlie_id",)),
        ("risk_level=low", ("bravo_id",)),
        ("status=developing", ("alpha_id",)),
        ("status=new", ("bravo_id",)),
        ("status=resolved", ("charlie_id",)),
        ("industry=semiconductors", ("alpha_id",)),
        ("industry=bank", ("bravo_id",)),
        ("company=EXSM", ("alpha_id",)),
        ("company=Example%20Semi", ("alpha_id",)),
    ],
)
def test_events_filters_narrow_the_real_total_to_seeded_facts(
    client: TestClient, seeded: Seeded, query: str, expected_ids: tuple[str, ...]
) -> None:
    body = client.get(f"/api/v1/events?{query}").json()
    expected = {str(getattr(seeded, name)) for name in expected_ids}
    assert {item["id"] for item in body["items"]} == expected
    # The count shares the page's predicates, so total equals the matched-fact count exactly.
    assert body["total"] == len(expected)


def test_events_company_filter_by_uuid_matches_the_linked_event(
    client: TestClient, seeded: Seeded
) -> None:
    body = client.get(f"/api/v1/events?company={seeded.exsm_company_id}").json()
    assert {item["id"] for item in body["items"]} == {str(seeded.alpha_id)}
    assert body["total"] == 1


def test_events_date_window_is_inclusive_utc_day(client: TestClient, seeded: Seeded) -> None:
    # 2026-07-12 inclusive on both ends: only BRAVO (coverage 07-12T09:00) qualifies; ALPHA
    # (07-14) and CHARLIE (07-10) fall outside the single-day window.
    body = client.get("/api/v1/events?date_from=2026-07-12&date_to=2026-07-12").json()
    assert {item["id"] for item in body["items"]} == {str(seeded.bravo_id)}
    assert body["total"] == 1
    # From 07-13 onward excludes BRAVO/CHARLIE and keeps only ALPHA.
    later = client.get("/api/v1/events?date_from=2026-07-13").json()
    assert {item["id"] for item in later["items"]} == {str(seeded.alpha_id)}


# --------------------------------------------------------------------------------------
# GET /api/v1/dashboard (contract 2): the five added blocks over real persisted facts.
# --------------------------------------------------------------------------------------


def test_dashboard_preserves_existing_and_adds_five_blocks(client: TestClient) -> None:
    body = client.get("/api/v1/dashboard").json()
    assert body["summary"]["overall_risk_level"] == "high"  # latest (07-14) daily summary
    assert isinstance(body["risk_scores"], list) and body["risk_scores"]
    assert body["alerts"]["open_count"] == 1  # one live (open) alert; the resolved one is not live
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
    assert metrics["Events Today"]["value"] == 1  # only ALPHA has coverage on NOW's day (07-14)
    assert metrics["High-Risk Events"]["value"] == 2  # ALPHA (58) and CHARLIE (90) clear 55
    assert metrics["Affected Industries"]["value"] == 3  # semiconductors, banking, energy
    assert metrics["Affected Companies"]["value"] == 2  # EXSM, BBNK
    assert metrics["Open Alerts"]["value"] == 1
    assert metrics["Overall Risk"]["value"] == "high"
    assert metrics["Overall Risk"]["severity"] == "high"
    assert "severity" not in metrics["Events Today"]


def test_dashboard_event_map_projects_coordinates_and_drops_uncoordinated(
    client: TestClient, seeded: Seeded
) -> None:
    points = {p["location_name"]: p for p in client.get("/api/v1/dashboard").json()["event_map"]}
    # BRAVO's London has null coordinates and cannot be placed.
    assert "London" not in points
    la = points["Port of Los Angeles"]
    assert la["latitude"] == 33.7361 and la["longitude"] == -118.2922
    assert la["x"] == (-118.2922 + 180.0) / 360.0
    assert la["y"] == (90.0 - 33.7361) / 180.0
    assert la["max_risk_score"] == 58.0
    assert la["dominant_event_type"] == "supply_chain"
    assert la["related_event_ids"] == [str(seeded.alpha_id)]


def test_dashboard_company_ranking_uses_latest_rollup_per_company(
    client: TestClient, seeded: Seeded
) -> None:
    ranking = client.get("/api/v1/dashboard").json()["company_ranking"]
    # Ranked by latest-rollup risk desc: EXSM (58) then BBNK (30). EXSM's stale rollup (10) loses.
    assert [row["company_id"] for row in ranking] == [
        str(seeded.exsm_company_id),
        str(seeded.bbnk_company_id),
    ]
    top = ranking[0]
    assert top["name"] == "Example Semiconductors"
    assert top["ticker"] == "EXSM"
    assert top["risk_score"] == 58.0  # the latest rollup, not the stale 10
    assert top["top_driver"] == "supply chain disruption"
    assert top["last_updated_at"] == "2026-07-14T00:00:00Z"
    assert top["impact_direction"] is None  # no persisted direction -> null, never guessed


def test_dashboard_industry_summary_dedups_latest_per_industry(client: TestClient) -> None:
    rows = {r["industry_id"]: r for r in client.get("/api/v1/dashboard").json()["industry_summary"]}
    assert set(rows) == {"semiconductors", "banking"}
    # semiconductors latest (07-14) wins over its stale snapshot (40).
    assert rows["semiconductors"]["risk_score"] == 55.0
    assert rows["semiconductors"]["news_velocity_score"] == 80.0
    assert rows["semiconductors"]["direction"] is None


def test_dashboard_upcoming_trigger_is_the_future_entry_only(
    client: TestClient, seeded: Seeded
) -> None:
    triggers = client.get("/api/v1/dashboard").json()["upcoming_triggers"]
    # Only the entry after NOW (07-16); the 07-13 past entry is excluded.
    assert [t["id"] for t in triggers] == [str(seeded.future_trigger_id)]
    trigger = triggers[0]
    assert trigger["title"] == "Labor-market data release"
    assert trigger["reason"] == "Single most important input to the recession estimate."
    assert trigger["related_event_id"] == str(seeded.alpha_id)
    assert trigger["related_company_id"] is None
    assert trigger["expected_at"] == "2026-07-16T09:00:00Z"


# --------------------------------------------------------------------------------------
# GET /api/v1/risk-radar + /{risk_type} + /{risk_type}/history (contract 3).
# --------------------------------------------------------------------------------------


def test_risk_radar_list_paginates_over_real_total(client: TestClient, seeded: Seeded) -> None:
    body = client.get("/api/v1/risk-radar?limit=2&offset=0").json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == seeded.total_observations
    assert len(body["items"]) == 2
    assert body["total"] > len(body["items"])
    # Each row is a serialized observation carrying trailing-Z as_of.
    assert all(item["as_of"].endswith("Z") for item in body["items"])


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


def test_risk_detail_gate_closed_keeps_descriptive_observation(
    client: TestClient, seeded: Seeded
) -> None:
    app.dependency_overrides[intelligence.get_crisis_prediction_reads_enabled] = lambda: False
    try:
        body = client.get("/api/v1/risk-radar/sovereign").json()
    finally:
        app.dependency_overrides[intelligence.get_crisis_prediction_reads_enabled] = lambda: True
    assert set(body) == {"risk"}
    risk = body["risk"]
    assert _RISK_DETAIL_KEYS <= set(risk)
    # Spine = the latest real sovereign observation (07-14 country/US, 41.5).
    assert risk["risk_type"] == "sovereign"
    assert risk["score"] == 41.5
    assert risk["severity"] == "medium"
    assert risk["confidence_score"] == 0.82
    assert risk["as_of"] == "2026-07-14T00:00:00Z"
    assert risk["model_version"] == "risk-v1"
    assert (risk["target_type"], risk["target_id"]) == ("country", "US")
    assert risk["main_drivers"] == ["Observed macro stress"]
    assert risk["model_rating"] is None
    assert risk["probability_by_horizon"] == []
    assert risk["historical_comparisons"] == []
    assert risk["invalidation_signals"] == []


def test_risk_detail_gate_open_assembles_matched_prediction(
    client: TestClient, seeded: Seeded
) -> None:
    app.dependency_overrides[intelligence.get_crisis_prediction_reads_enabled] = lambda: True
    try:
        body = client.get("/api/v1/risk-radar/sovereign").json()
    finally:
        app.dependency_overrides[intelligence.get_crisis_prediction_reads_enabled] = lambda: True

    assert set(body) == {"risk"}
    risk = body["risk"]
    assert _RISK_DETAIL_KEYS <= set(risk)
    # Spine = the latest real sovereign observation (07-14 country/US, 41.5).
    assert risk["risk_type"] == "sovereign"
    assert risk["score"] == 41.5
    assert risk["severity"] == "medium"
    assert risk["confidence_score"] == 0.82
    assert risk["as_of"] == "2026-07-14T00:00:00Z"
    assert risk["model_version"] == "risk-v1"
    assert (risk["target_type"], risk["target_id"]) == ("country", "US")
    # model_rating = the matching country/US prediction, chosen over the more recent GB one.
    rating = risk["model_rating"]
    assert rating is not None
    assert (rating["target_type"], rating["target_id"]) == ("country", "US")
    assert rating["as_of_date"] == "2026-07-14"
    assert rating["probability_within_18m"] == 0.30
    assert rating["model_versions"] == {"ensemble": "ensemble.v1"}
    assert rating["what_could_reduce_risk"] == ["IMF backstop confirmed", "FX reserves rebuild"]
    # Horizons use the canonical tokens from the prediction.
    assert risk["probability_by_horizon"] == [
        {"horizon": "0_6m", "probability": 0.10},
        {"horizon": "6_12m", "probability": 0.15},
        {"horizon": "12_18m", "probability": 0.05},
        {"horizon": "within_18m", "probability": 0.30},
    ]
    # Labeled drivers only; the score-only entry is skipped, never renamed.
    assert risk["main_drivers"] == ["Deposit outflows", "credit_spread_zscore"]
    assert risk["historical_comparisons"] == ["1998 sovereign stress episode"]
    assert risk["invalidation_signals"] == ["IMF backstop confirmed", "FX reserves rebuild"]
    assert risk["signals"] == [] and risk["leading_indicators"] == []


def test_risk_detail_related_ids_group_by_target_and_drop_unsupported(
    client: TestClient, seeded: Seeded
) -> None:
    risk = client.get("/api/v1/risk-radar/sovereign").json()["risk"]
    # The distinct real sovereign observation targets, split by type; country/US is unsupported
    # by any RiskDetail related-id list and is dropped rather than forced onto one.
    assert risk["related_event_ids"] == [str(seeded.alpha_id)]
    assert risk["related_industry_ids"] == ["semiconductors"]
    assert risk["related_company_ids"] == [str(seeded.exsm_company_id)]
    for ids in (
        risk["related_event_ids"],
        risk["related_industry_ids"],
        risk["related_company_ids"],
    ):
        assert "US" not in ids


def test_risk_detail_missing_risk_type_is_404_envelope(client: TestClient) -> None:
    resp = client.get("/api/v1/risk-radar/no_such_risk_type")
    assert resp.status_code == 404
    assert set(resp.json()) == {"error"}
    assert resp.json()["error"]["code"] == "not_found"


def test_risk_history_is_chronological_real_series_with_real_total(
    client: TestClient, seeded: Seeded
) -> None:
    body = client.get("/api/v1/risk-radar/sovereign/history?days=30&limit=2&offset=0").json()
    assert set(body) == {"items", "total", "limit", "offset"}
    # All five sovereign observations fall in the 30-day window before NOW.
    assert body["total"] == seeded.sovereign_observation_count
    assert len(body["items"]) == 2
    assert body["total"] > len(body["items"])
    # Chronological ascending as_of; the first page starts at the earliest instant.
    stamps = [item["as_of"] for item in body["items"]]
    assert stamps == sorted(stamps)
    assert stamps[0] == "2026-07-10T00:00:00Z"
    # The full series spans the earliest to the latest seeded instant.
    full = client.get("/api/v1/risk-radar/sovereign/history?days=30&limit=50").json()["items"]
    assert len(full) == seeded.sovereign_observation_count
    all_stamps = [item["as_of"] for item in full]
    assert all_stamps == sorted(all_stamps)
    assert all_stamps[0] == "2026-07-10T00:00:00Z"
    assert all_stamps[-1] == "2026-07-14T00:00:00Z"


# --------------------------------------------------------------------------------------
# GET /api/v1/evidence/{claim_id} (contract 4): the accepted drawer join, still safe.
# --------------------------------------------------------------------------------------

_EVIDENCE_KEYS = {
    "evidence_item_id",
    "support_type",
    "confidence",
    "source_type",
    "source_id",
    "title",
    "publisher",
    "url",
    "published_at",
    "credibility",
    "snippet",
    "snippet_origin",
    "snippet_truncated",
}


def test_evidence_drawer_returns_real_links_and_a_safe_article_snippet(
    client: TestClient, seeded: Seeded
) -> None:
    resp = client.get(f"/api/v1/evidence/{seeded.claim_id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["claim"] == {
        "id": str(seeded.claim_id),
        "text": "The bank faces a liquidity squeeze.",
        "type": "assertion",
        "confidence": 0.9,
    }
    evidence = payload["evidence"]
    # Order by published_at desc nullslast: the dated article precedes the undated filing.
    assert [e["support_type"] for e in evidence] == ["supports", "contradicts"]
    art = evidence[0]
    assert art["source_type"] == "article"
    assert art["source_id"] == str(seeded.article_id)
    assert art["publisher"] == "Reuters"
    assert art["url"] == "https://news.example/bank"
    assert art["credibility"] == 0.8
    assert art["confidence"] == 0.88
    # Summary-first, bounded, derived -- never the raw body.
    assert art["snippet"] == "A concise summary of the article."
    assert art["snippet_origin"] == "summary" and len(art["snippet"]) <= 200
    assert art["published_at"] == "2026-07-14T04:00:00Z"
    assert evidence[1]["snippet"] is None  # non-article evidence has no snippet
    for item in evidence:
        assert set(item) == _EVIDENCE_KEYS
    # No article body or raw payload ever crosses the boundary.
    assert "SECRET BODY" not in resp.text
    assert "do_not_expose" not in resp.text and "do_not" not in resp.text


def test_evidence_drawer_unknown_claim_is_404(client: TestClient) -> None:
    resp = client.get(f"/api/v1/evidence/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert set(resp.json()) == {"error"}


# --------------------------------------------------------------------------------------
# GET /api/v1/industries (contract 4): the accepted latest-per-industry dedup (shape gap 6).
# --------------------------------------------------------------------------------------


def test_industries_list_dedups_latest_per_industry_and_counts_distinct(
    client: TestClient,
) -> None:
    body = client.get("/api/v1/industries").json()
    assert set(body) == {"items", "total", "limit", "offset"}
    # Three rollup snapshots collapse to two distinct industries; total counts the deduped rows.
    assert body["total"] == 2
    ids = [row["industry_id"] for row in body["items"]]
    assert ids == ["semiconductors", "banking"]  # ordered by latest as_of desc
    semis = body["items"][0]
    assert semis["risk_score"] == 55.0  # the latest snapshot, not the stale 40
    assert semis["as_of"] == "2026-07-14T00:00:00Z"
