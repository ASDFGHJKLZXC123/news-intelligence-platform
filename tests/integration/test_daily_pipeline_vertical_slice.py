"""Manual daily-pipeline vertical slice against a disposable PostgreSQL database.

The worker boundary is invoked directly: no broker, provider, model, API key, or network call is
allowed. Deterministic injected stages write one representative row at every descriptive layer,
while the production SQLAlchemy lifecycle store owns the real Job/sidecar transitions.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    EMBEDDING_DIM,
    Alert,
    Article,
    ArticleEmbedding,
    Company,
    CompanyRiskRollup,
    CrisisPrediction,
    EntityProfile,
    Event,
    EventAnalogy,
    EventArticle,
    EventCompany,
    EventEmbedding,
    EventEntity,
    EventIndustry,
    EventRiskFeature,
    ForecastScenario,
    HistoricalEpisode,
    HistoricalEpisodeEmbedding,
    IndustryRiskRollup,
    Job,
    Report,
    RiskWarning,
    Source,
)
from db.models.pipeline import PipelineRunDetail
from packages.config.settings import get_settings
from packages.jobs import JobState
from services.pipeline import (
    EXCLUDED_GATE_G_STAGES,
    DailyPipelineCoordinator,
    PipelineStage,
    PipelineState,
    StageContext,
    StageResult,
    daily_pipeline_identity,
)
from services.pipeline.sqlalchemy_store import SQLAlchemyPipelineLifecycleStore
from workers import pipeline_tasks

pytestmark = pytest.mark.integration

PROCESS_DATE = datetime.date(2026, 7, 28)
OBSERVED_AT = datetime.datetime(2026, 7, 28, 12, tzinfo=datetime.UTC)
MODEL = "text-embedding-3-small"
MODEL_VERSION = "nip-es1-20260728-0123456789abcdefabcd"

SOURCE_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")
ARTICLE_ID = uuid.UUID("20000000-0000-4000-8000-000000000002")
EVENT_ID = uuid.UUID("30000000-0000-4000-8000-000000000003")
ENTITY_ID = uuid.UUID("40000000-0000-4000-8000-000000000004")
COMPANY_ID = uuid.UUID("50000000-0000-4000-8000-000000000005")
EPISODE_ID = uuid.UUID("60000000-0000-4000-8000-000000000006")
ANALOGY_ID = uuid.UUID("70000000-0000-4000-8000-000000000007")
REPORT_ID = uuid.UUID("80000000-0000-4000-8000-000000000008")

QUEUED_AT = datetime.datetime(2026, 7, 29, 9, tzinfo=datetime.UTC)
STARTED_AT = datetime.datetime(2026, 7, 29, 9, 1, tzinfo=datetime.UTC)
COMPLETED_AT = datetime.datetime(2026, 7, 29, 9, 2, tzinfo=datetime.UTC)

_DISPOSABLE_PREFIX = "nip_pipeline_slice_"

SessionFactory = Callable[[], Session]


def _vector() -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[0] = 1.0
    return vector


@pytest.fixture
def migrated_pipeline_db(require_postgres: None, monkeypatch: pytest.MonkeyPatch):
    """Apply migration head to a throwaway database and return its own session factory."""

    configured = make_url(get_settings().database_url)
    database_name = f"{_DISPOSABLE_PREFIX}{uuid.uuid4().hex[:12]}"
    assert database_name != configured.database

    admin = create_engine(
        configured.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        future=True,
    )
    engine = None
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        database_url = configured.set(database=database_name)
        monkeypatch.setenv(
            "DATABASE_URL",
            database_url.render_as_string(hide_password=False),
        )
        get_settings.cache_clear()
        command.upgrade(Config("alembic.ini"), "head")

        engine = create_engine(database_url, future=True)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0019"
        yield sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
    finally:
        if engine is not None:
            engine.dispose()
        get_settings.cache_clear()
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
            leaked = connection.scalar(
                text("SELECT count(*) FROM pg_database WHERE datname = :database_name"),
                {"database_name": database_name},
            )
        admin.dispose()
        assert leaked == 0, f"disposable database {database_name} leaked"


class _DeterministicStages:
    """No-network stage runners that make the schema effects of one run observable."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self.calls = {stage: 0 for stage in PipelineStage}

    def runners(self) -> Mapping[PipelineStage, Callable[[StageContext], StageResult]]:
        return {
            PipelineStage.INGESTION: self.ingestion,
            PipelineStage.ARTICLE_EMBEDDINGS: self.article_embeddings,
            PipelineStage.CLUSTERING: self.clustering,
            PipelineStage.ENTITY_LINKING: self.entity_linking,
            PipelineStage.EVENT_EMBEDDINGS: self.event_embeddings,
            PipelineStage.ANALOGIES: self.analogies,
            PipelineStage.DAILY_BRIEF: self.daily_brief,
        }

    def _called(self, context: StageContext, expected: PipelineStage) -> None:
        assert context.stage is expected
        assert context.identity == daily_pipeline_identity(PROCESS_DATE)
        self.calls[expected] += 1

    def ingestion(self, context: StageContext) -> StageResult:
        self._called(context, PipelineStage.INGESTION)
        with self._session_factory.begin() as session:
            session.add(
                Source(
                    id=SOURCE_ID,
                    name="Deterministic Pipeline Wire",
                    source_type="rss",
                    feed_url="https://pipeline-fixture.invalid/feed.xml",
                )
            )
            session.flush()
            session.add(
                Article(
                    id=ARTICLE_ID,
                    source_id=SOURCE_ID,
                    url="https://pipeline-fixture.invalid/articles/one",
                    url_hash="1" * 64,
                    title="Port closure disrupts a manufacturer",
                    body="The closure delayed inbound components for Acme Manufacturing.",
                    language="en",
                    published_at=OBSERVED_AT,
                    fetched_at=OBSERVED_AT,
                )
            )
        return StageResult.succeeded(
            item_count=1,
            output={"article_ids": [str(ARTICLE_ID)]},
        )

    def article_embeddings(self, context: StageContext) -> StageResult:
        self._called(context, PipelineStage.ARTICLE_EMBEDDINGS)
        with self._session_factory.begin() as session:
            session.add(
                ArticleEmbedding(
                    article_id=ARTICLE_ID,
                    model=MODEL,
                    model_version=MODEL_VERSION,
                    dimension=EMBEDDING_DIM,
                    embedding=_vector(),
                )
            )
        return StageResult.succeeded(
            item_count=1,
            output={"article_ids": [str(ARTICLE_ID)]},
        )

    def clustering(self, context: StageContext) -> StageResult:
        self._called(context, PipelineStage.CLUSTERING)
        with self._session_factory.begin() as session:
            session.add(
                Event(
                    id=EVENT_ID,
                    title="Port closure disrupts Acme inbound components",
                    summary="Reporting confirms a component delay linked to the closure.",
                    event_type="supply_chain_disruption",
                    country="US",
                    region="West Coast",
                    severity_score=42.0,
                    hotness_score=58.0,
                    article_count=1,
                    source_count=1,
                    first_seen_at=OBSERVED_AT,
                    last_seen_at=OBSERVED_AT,
                )
            )
            session.flush()
            session.add_all(
                [
                    EventArticle(
                        event_id=EVENT_ID,
                        article_id=ARTICLE_ID,
                        similarity=1.0,
                    ),
                    EventRiskFeature(
                        event_id=EVENT_ID,
                        country="US",
                        region="West Coast",
                        risk_type="supply_chain",
                        event_type="supply_chain_disruption",
                        mechanism="port_closure",
                        severity_score=42.0,
                        source_diversity_score=1.0,
                        official_confirmation=True,
                        affected_industries=["industrial_manufacturing"],
                        affected_companies=["Acme Manufacturing"],
                        observed_at=OBSERVED_AT,
                        evidence_article_ids=[ARTICLE_ID],
                        confidence_score=0.9,
                    ),
                ]
            )
        return StageResult.succeeded(
            item_count=1,
            output={"event_ids": [str(EVENT_ID)]},
        )

    def entity_linking(self, context: StageContext) -> StageResult:
        self._called(context, PipelineStage.ENTITY_LINKING)
        with self._session_factory.begin() as session:
            session.add(
                EntityProfile(
                    id=ENTITY_ID,
                    canonical_name="Acme Manufacturing",
                    normalized_name="acme manufacturing",
                    entity_type="company",
                    country="US",
                    primary_ticker="ACME",
                )
            )
            session.flush()
            session.add(
                Company(
                    id=COMPANY_ID,
                    entity_profile_id=ENTITY_ID,
                    display_name="Acme Manufacturing",
                    legal_name="Acme Manufacturing, Inc.",
                    primary_ticker="ACME",
                    exchange="NYSE",
                    country="US",
                    sector="Industrials",
                    industry="industrial_manufacturing",
                )
            )
            session.flush()
            session.add_all(
                [
                    EventEntity(
                        event_id=EVENT_ID,
                        entity_profile_id=ENTITY_ID,
                        role="affected_company",
                        impact_direction=None,
                        impact_score=None,
                        confidence_score=0.9,
                    ),
                    EventCompany(
                        event_id=EVENT_ID,
                        company_id=COMPANY_ID,
                        impact_direction=None,
                        impact_score=None,
                        risk_score=None,
                        exposure_explanation=(
                            "The article names Acme as affected by an inbound component delay."
                        ),
                        confidence_score=0.9,
                    ),
                    EventIndustry(
                        event_id=EVENT_ID,
                        industry_id="industrial_manufacturing",
                        impact_direction=None,
                        impact_score=None,
                        risk_score=None,
                        opportunity_score=None,
                    ),
                ]
            )
        return StageResult.succeeded(
            item_count=1,
            output={
                "event_ids": [str(EVENT_ID)],
                "company_ids": [str(COMPANY_ID)],
            },
        )

    def event_embeddings(self, context: StageContext) -> StageResult:
        self._called(context, PipelineStage.EVENT_EMBEDDINGS)
        with self._session_factory.begin() as session:
            session.add(
                EventEmbedding(
                    event_id=EVENT_ID,
                    model=MODEL,
                    model_version=MODEL_VERSION,
                    dimension=EMBEDDING_DIM,
                    embedding=_vector(),
                )
            )
        return StageResult.succeeded(
            item_count=1,
            output={"event_ids": [str(EVENT_ID)]},
        )

    def analogies(self, context: StageContext) -> StageResult:
        self._called(context, PipelineStage.ANALOGIES)
        with self._session_factory.begin() as session:
            session.add(
                HistoricalEpisode(
                    id=EPISODE_ID,
                    name="Deterministic logistics interruption",
                    episode_type="supply_shock",
                    onset_date=datetime.date(2021, 3, 23),
                    onset_summary="A transport chokepoint delayed industrial inputs.",
                    onset_embedding=_vector(),
                    model=MODEL,
                    model_version=MODEL_VERSION,
                    outcome_summary="Backlogs cleared after the route reopened.",
                    outcomes=["recovery"],
                    affected_industries=["industrial_manufacturing"],
                    regime_tags=["fixture"],
                    version=1,
                )
            )
            session.flush()
            session.add_all(
                [
                    HistoricalEpisodeEmbedding(
                        historical_episode_id=EPISODE_ID,
                        model=MODEL,
                        model_version=MODEL_VERSION,
                        episode_version=1,
                        dimension=EMBEDDING_DIM,
                        onset_embedding=_vector(),
                        input_sha256="a" * 64,
                        input_contract_version="pipeline-vertical-slice.v1",
                        snapshot_manifest_sha256="b" * 64,
                    ),
                    EventAnalogy(
                        id=ANALOGY_ID,
                        event_id=EVENT_ID,
                        historical_episode_id=EPISODE_ID,
                        similarity_score=88.0,
                        rationale="Both events interrupted time-sensitive industrial inputs.",
                        limitations=["Different transport modes and market conditions."],
                        regime_caveats=["Fixture comparison; no forecast is implied."],
                        evidence_refs={"article_ids": [str(ARTICLE_ID)]},
                    ),
                ]
            )
        return StageResult.succeeded(
            item_count=1,
            output={"analogy_ids": [str(ANALOGY_ID)]},
        )

    def daily_brief(self, context: StageContext) -> StageResult:
        self._called(context, PipelineStage.DAILY_BRIEF)
        with self._session_factory.begin() as session:
            session.add(
                Report(
                    id=REPORT_ID,
                    report_type="daily_brief",
                    brief_date=PROCESS_DATE,
                    title="Daily Intelligence Brief — July 28, 2026",
                    status="published",
                    version=1,
                    confidence_score=0.9,
                )
            )
        return StageResult.succeeded(
            item_count=1,
            output={"report_id": str(REPORT_ID), "status": "published"},
        )


def _count(session: Session, model: type[Any]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_manual_worker_persists_descriptive_slice_and_replays_idempotently(
    migrated_pipeline_db: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = daily_pipeline_identity(PROCESS_DATE)
    timestamps = iter((QUEUED_AT, STARTED_AT, COMPLETED_AT))
    store = SQLAlchemyPipelineLifecycleStore(
        session_factory=migrated_pipeline_db,
        now=lambda: next(timestamps, COMPLETED_AT),
    )
    stages = _DeterministicStages(migrated_pipeline_db)

    queued = store.queue(identity)
    assert queued.should_enqueue is True
    assert queued.snapshot.state is JobState.QUEUED
    assert queued.snapshot.queued_at == QUEUED_AT

    monkeypatch.setattr(
        pipeline_tasks,
        "build_lifecycle_store",
        lambda _delivery_token=None: store,
    )
    monkeypatch.setattr(
        pipeline_tasks,
        "build_stage_runners",
        lambda _store: stages.runners(),
    )
    worker_result = pipeline_tasks.run_daily_pipeline_task.run(PROCESS_DATE.isoformat())

    assert worker_result["status"] == PipelineState.SUCCEEDED.value
    assert worker_result["state"] == JobState.SUCCEEDED.value
    assert worker_result["idempotent"] is False
    assert worker_result["result"]["excluded_gate_g_stages"] == [
        stage.value for stage in EXCLUDED_GATE_G_STAGES
    ]

    snapshot = store.get_snapshot(identity)
    assert snapshot.job_id == identity.process_id
    assert snapshot.job_key == identity.process_key
    assert snapshot.state is JobState.SUCCEEDED
    assert snapshot.attempt == 1
    assert snapshot.error is None
    assert snapshot.celery_task_id is None
    assert snapshot.queued_at == QUEUED_AT
    assert snapshot.started_at == STARTED_AT
    assert snapshot.completed_at == COMPLETED_AT
    assert snapshot.result == worker_result["result"]

    replay = DailyPipelineCoordinator(
        stage_runners=stages.runners(),
        lifecycle_store=store,
    ).run(PROCESS_DATE)
    assert replay.state is PipelineState.SUCCEEDED
    assert replay.idempotent_skip is True
    assert stages.calls == dict.fromkeys(PipelineStage, 1)

    duplicate_queue = store.queue(identity)
    assert duplicate_queue.should_enqueue is False
    assert duplicate_queue.idempotent is True
    assert duplicate_queue.snapshot.attempt == 1

    with migrated_pipeline_db() as session:
        assert _count(session, Job) == 1
        assert _count(session, PipelineRunDetail) == 1
        assert _count(session, Source) == 1
        assert _count(session, Article) == 1
        assert _count(session, ArticleEmbedding) == 1
        assert _count(session, Event) == 1
        assert _count(session, EventArticle) == 1
        assert _count(session, EventRiskFeature) == 1
        assert _count(session, EventEntity) == 1
        assert _count(session, Company) == 1
        assert _count(session, EventCompany) == 1
        assert _count(session, EventIndustry) == 1
        assert _count(session, EventEmbedding) == 1
        assert _count(session, EventAnalogy) == 1
        assert _count(session, Report) == 1

        article_embedding = session.get(
            ArticleEmbedding,
            {
                "article_id": ARTICLE_ID,
                "model": MODEL,
                "model_version": MODEL_VERSION,
            },
        )
        event_embedding = session.get(
            EventEmbedding,
            {
                "event_id": EVENT_ID,
                "model": MODEL,
                "model_version": MODEL_VERSION,
            },
        )
        assert article_embedding is not None
        assert event_embedding is not None

        event_entity = session.get(
            EventEntity,
            {"event_id": EVENT_ID, "entity_profile_id": ENTITY_ID},
        )
        event_company = session.get(
            EventCompany,
            {"event_id": EVENT_ID, "company_id": COMPANY_ID},
        )
        event_industry = session.get(
            EventIndustry,
            {
                "event_id": EVENT_ID,
                "industry_id": "industrial_manufacturing",
            },
        )
        assert event_entity is not None
        assert event_company is not None
        assert event_industry is not None
        assert event_entity.impact_score is None
        assert event_company.impact_score is None
        assert event_company.risk_score is None
        assert event_industry.impact_score is None
        assert event_industry.risk_score is None
        assert event_industry.opportunity_score is None

        # Gate G is closed: the descriptive run never emits prediction/rollup/alert rows.
        for predictive_model in (
            CrisisPrediction,
            ForecastScenario,
            RiskWarning,
            CompanyRiskRollup,
            IndustryRiskRollup,
            Alert,
        ):
            assert _count(session, predictive_model) == 0
