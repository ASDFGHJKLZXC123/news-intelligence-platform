"""Offline captured-feed proof for the actual personal daily coordinator."""

from __future__ import annotations

import datetime
import sys
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    EMBEDDING_DIM,
    Article,
    ArticleEmbedding,
    Claim,
    ClaimEvidence,
    Event,
    EventArticle,
    EvidenceItem,
    LLMRun,
    PersonalCapture,
    PersonalFeedReceipt,
    PersonalRun,
    Report,
    ReportSection,
    Source,
)
from packages.providers.base import RSSItem
from packages.providers.fakes import FakeEmbeddingProvider, FakeRSSProvider
from services.personal.coordinator import PersonalCoordinatorError, run_personal_daily
from services.personal.runs import create_daily_run, retry_run
from services.personal.workspace import configure_profile, ensure_workspace
from tests.integration._personal_processing_clock import install_authored_processing_clock
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_brief_generation import (
    EXECUTIVE_TEXT,
    TOP_EVENT_TEXT,
    _orchestrator,
)


@pytest.fixture(autouse=True)
def authored_processing_clock(monkeypatch):
    from packages.config.settings import get_settings

    monkeypatch.setenv("PERSONAL_PROCESSING_MODE", "personal")
    get_settings.cache_clear()
    install_authored_processing_clock(monkeypatch, sys.modules[__name__])
    yield
    get_settings.cache_clear()


pytestmark = pytest.mark.integration
UTC = datetime.UTC
ROUTE = {
    "mode": "offline_fixture",
    "provider": "callable",
    "model": "offline-fixture-v1",
}


class StepClock:
    def __init__(self, start: datetime.datetime) -> None:
        self.current = start

    def __call__(self) -> datetime.datetime:
        value = self.current
        self.current += datetime.timedelta(seconds=1)
        return value


def _create_run(engine, *, now: datetime.datetime):  # noqa: ANN001, ANN202
    with Session(engine) as session:
        source = Source(
            name="Offline captured coordinator feed",
            feed_url=f"https://offline.example/{uuid.uuid4()}.xml",
        )
        session.add(source)
        session.flush()
        workspace, _ = ensure_workspace(session)
        configure_profile(
            session,
            workspace,
            selected_source_ids=[source.id],
            include_phrases=["agency"],
            settings={"model_route": ROUTE},
        )
        decision = create_daily_run(session, workspace, now=now)
        token = decision.run.ownership_token
        assert token is not None
        ids = (decision.run.id, token, source.id, workspace.id)
        session.commit()
        return ids


def _captured_item(*, published_at: datetime.datetime | None = None) -> RSSItem:
    return RSSItem(
        guid="offline-coordinator-1",
        title="Agency approves coastal resilience project",
        url="https://offline.example/stories/coastal-project?utm_source=fixture",
        published_at=published_at,
        summary="The agency confirmed the coastal resilience project was approved.",
        source="offline-captured-feed",
        provider_name="offline-rss-fixture",
        source_refs=("fixture:feed:one",),
        evidence_refs=("fixture:story:one",),
    )


def test_actual_coordinator_captures_groups_claims_and_publishes_from_empty_tables() -> None:
    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 13, 18, tzinfo=UTC)
        run_id, token, source_id, workspace_id = _create_run(engine, now=now)
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Article)) == 0
            assert session.scalar(select(func.count()).select_from(Event)) == 0
            assert session.scalar(select(func.count()).select_from(Claim)) == 0
            assert session.scalar(select(func.count()).select_from(ClaimEvidence)) == 0
            assert session.scalar(select(func.count()).select_from(Report)) == 0

        result = run_personal_daily(
            run_id,
            token,
            session_factory=lambda: Session(engine, autoflush=False),
            rss_provider_factory=lambda source: FakeRSSProvider([_captured_item()]),
            embedding_provider=FakeEmbeddingProvider(
                dimension=EMBEDDING_DIM, model="offline-fixture-embedding"
            ),
            orchestrator_factory=_orchestrator,
            now=now,
            clock=StepClock(now + datetime.timedelta(seconds=10)),
        )

        assert result.report is not None and result.report.published is True
        assert result.feeds_attempted == result.feeds_succeeded == 1
        assert result.feeds_failed == 0
        assert result.articles_captured == result.articles_admitted == 1
        assert result.events_observed == 1
        assert result.claims_supported >= 1

        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            capture = session.scalar(select(PersonalCapture))
            article = session.scalar(select(Article))
            event = session.scalar(select(Event))
            report = session.get(Report, run.report_id)
            sections = list(
                session.execute(
                    select(ReportSection)
                    .where(ReportSection.report_id == report.id)
                    .order_by(ReportSection.section_order)
                ).scalars()
            )
            assert run.workspace_id == workspace_id
            assert run.state == "succeeded"
            assert run.stage_results["grouping"] == {
                "status": "succeeded",
                "events_created": 1,
                "articles_clustered": 1,
            }
            assert run.capture_started_at < run.capture_ended_at < run.scopes_frozen_at
            assert run.admitted_article_ids == run.enrichment_article_ids == [article.id]
            assert run.coverage == {
                "schema": "personal-capture-coverage.v1",
                "feeds_configured": 1,
                "feeds_attempted": 1,
                "feeds_succeeded": 1,
                "feeds_failed": 0,
                "feeds_paused": 0,
                "feed_failures": [],
                "feed_pauses": [],
                "items_fetched": 1,
                "articles_captured": 1,
                "articles_admitted": 1,
                "pending_total": 0,
                "pending_capacity": 2000,
            }
            assert capture.source_id == source_id
            assert capture.article_id == article.id
            assert capture.admitted_run_id == run.id
            assert capture.admitted_at is not None
            assert article.published_at is None
            assert session.scalar(select(func.count()).select_from(ArticleEmbedding)) == 1
            assert session.scalar(select(func.count()).select_from(EventArticle)) == 1
            assert event.id in run.event_ids
            assert session.scalar(select(func.count()).select_from(Claim)) >= 1
            assert session.scalar(select(func.count()).select_from(ClaimEvidence)) >= 1
            assert session.scalar(select(func.count()).select_from(EvidenceItem)) >= 1
            assert session.scalar(select(func.count()).select_from(LLMRun)) >= 4
            assert report.status == "published"
            assert sections[0].body == EXECUTIVE_TEXT
            assert sections[1].body == TOP_EVENT_TEXT


def test_all_feed_failure_is_durable_and_explicit_retry_uses_same_run() -> None:
    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 14, 18, tzinfo=UTC)
        run_id, token, _source_id, workspace_id = _create_run(engine, now=now)

        class FailedFeed:
            def fetch(self, _feed_url: str):  # noqa: ANN202
                raise RuntimeError("offline feed fixture unavailable")

        with pytest.raises(PersonalCoordinatorError, match="configured personal feeds failed"):
            run_personal_daily(
                run_id,
                token,
                session_factory=lambda: Session(engine, autoflush=False),
                rss_provider_factory=lambda source: FailedFeed(),
                embedding_provider=FakeEmbeddingProvider(
                    dimension=EMBEDDING_DIM, model="offline-fixture-embedding"
                ),
                orchestrator_factory=lambda *_: pytest.fail("failed feed called a model"),
                now=now,
                clock=StepClock(now + datetime.timedelta(seconds=10)),
            )
        with Session(engine) as session:
            failed = session.get(PersonalRun, run_id)
            assert failed.state == "failed"
            assert failed.error["code"] == "all_feeds_failed"
            assert failed.coverage["feeds_failed"] == 1
            assert session.scalar(select(func.count()).select_from(Article)) == 0
            assert session.scalar(select(func.count()).select_from(Report)) == 0
            workspace, _ = ensure_workspace(session)
            assert workspace.id == workspace_id
            decision = retry_run(
                session, workspace, run_id, now=now + datetime.timedelta(minutes=10)
            )
            retry_token = decision.run.ownership_token
            assert retry_token is not None
            session.commit()

        recovered = run_personal_daily(
            run_id,
            retry_token,
            session_factory=lambda: Session(engine, autoflush=False),
            rss_provider_factory=lambda source: FakeRSSProvider([_captured_item(published_at=now)]),
            embedding_provider=FakeEmbeddingProvider(
                dimension=EMBEDDING_DIM, model="offline-fixture-embedding"
            ),
            orchestrator_factory=_orchestrator,
            now=now + datetime.timedelta(minutes=10),
            clock=StepClock(now + datetime.timedelta(minutes=10, seconds=10)),
        )
        assert recovered.report is not None and recovered.report.published is True
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.attempt == 2
            assert run.state == "succeeded"
            assert session.scalar(select(func.count()).select_from(Report)) == 1


def test_pending_capture_is_bounded_and_round_robin_admission_creates_only_selected_articles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 15, 18, tzinfo=UTC)
        with Session(engine) as session:
            sources = [
                Source(
                    name=f"Offline feed {index}",
                    feed_url=f"https://offline.example/feed-{uuid.uuid4()}.xml",
                )
                for index in range(2)
            ]
            session.add_all(sources)
            session.flush()
            workspace, _ = ensure_workspace(session)
            configure_profile(
                session,
                workspace,
                selected_source_ids=[item.id for item in sources],
                execution_profile="raw",
                settings={"run_article_limit": 3},
            )
            decision = create_daily_run(session, workspace, now=now)
            run_id = decision.run.id
            token = decision.run.ownership_token
            assert token is not None
            source_ids = tuple(sorted((item.id for item in sources), key=str))
            session.commit()

        items_by_source = {
            source_id: [
                RSSItem(
                    guid=f"{source_id}-new",
                    title=f"Agency newest item from {source_id}",
                    url=f"https://offline.example/{source_id}/new",
                    published_at=now,
                    summary="The agency confirmed a bounded intake item was received.",
                    provider_name="offline-rss-fixture",
                ),
                RSSItem(
                    guid=f"{source_id}-old",
                    title=f"Agency older item from {source_id}",
                    url=f"https://offline.example/{source_id}/old",
                    published_at=now - datetime.timedelta(days=1),
                    summary="The agency confirmed an older bounded intake item was received.",
                    provider_name="offline-rss-fixture",
                ),
            ]
            for source_id in source_ids
        }
        result = run_personal_daily(
            run_id,
            token,
            session_factory=lambda: Session(engine, autoflush=False),
            rss_provider_factory=lambda source: FakeRSSProvider(items_by_source[source.id]),
            embedding_provider=FakeEmbeddingProvider(
                dimension=EMBEDDING_DIM, model="offline-fixture-embedding"
            ),
            orchestrator_factory=lambda *_: pytest.fail("raw profile called a model"),
            now=now,
            clock=StepClock(now + datetime.timedelta(seconds=10)),
        )
        assert result.report is None
        assert result.articles_captured == 4
        assert result.articles_admitted == 3

        with Session(engine) as session:
            captures = list(session.execute(select(PersonalCapture)).scalars())
            admitted = {item.canonical_url for item in captures if item.admitted_at is not None}
            pending = {item.canonical_url for item in captures if item.admitted_at is None}
            assert admitted == {
                f"https://offline.example/{source_ids[0]}/new",
                f"https://offline.example/{source_ids[0]}/old",
                f"https://offline.example/{source_ids[1]}/new",
            }
            assert pending == {f"https://offline.example/{source_ids[1]}/old"}
            assert session.scalar(select(func.count()).select_from(Article)) == 3
            run = session.get(PersonalRun, run_id)
            assert run.enrichment_article_ids == []
            assert run.coverage["pending_total"] == 1
            assert run.stage_results["embedding"] == {
                "status": "disabled",
                "reason": "disabled_by_profile",
            }


def test_grouping_and_all_seven_observations_commit_atomically_then_retry_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.providers.base import EmbeddingResult

    class OrthogonalEmbeddingProvider:
        model_name = "offline-orthogonal"
        model_version = "v1"

        def embed(self, texts: list[str]) -> list[EmbeddingResult]:
            results = []
            for index, _text in enumerate(texts):
                vector = [0.0] * EMBEDDING_DIM
                vector[index] = 1.0
                results.append(
                    EmbeddingResult(
                        vector=tuple(vector),
                        provider_name="offline-fixture",
                        model_name=self.model_name,
                        model_version=self.model_version,
                        dimension=EMBEDDING_DIM,
                        model_run_id=f"offline-{index}",
                    )
                )
            return results

    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 16, 18, tzinfo=UTC)
        run_id, token, _source_id, workspace_id = _create_run(engine, now=now)
        items = [
            RSSItem(
                guid=f"offline-seven-{index}",
                title=f"Agency confirmed project number {100 + index}",
                url=f"https://offline.example/seven/{index}",
                published_at=now - datetime.timedelta(minutes=index),
                summary=f"The agency confirmed project number {100 + index} was approved.",
                provider_name="offline-rss-fixture",
            )
            for index in range(7)
        ]

        def fail_after_grouping(*_args, **_kwargs):  # noqa: ANN202
            raise RuntimeError("offline fault after grouping")

        with monkeypatch.context() as scoped:
            scoped.setattr(
                "services.personal.coordinator.prepare_article_claims", fail_after_grouping
            )
            with pytest.raises(PersonalCoordinatorError, match="workflow failed"):
                run_personal_daily(
                    run_id,
                    token,
                    session_factory=lambda: Session(engine, autoflush=False),
                    rss_provider_factory=lambda source: FakeRSSProvider(items),
                    embedding_provider=OrthogonalEmbeddingProvider(),
                    orchestrator_factory=lambda *_: pytest.fail("faulted run called a model"),
                    now=now,
                    clock=StepClock(now + datetime.timedelta(seconds=10)),
                )

        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "failed"
            assert run.stage_results["grouping"]["events_created"] == 7
            assert run.stage_results["observations"]["events_observed"] == 7
            assert len(run.event_ids) == 7
            assert len(run.terminal_manifest["observations"]) == 7
            workspace, _ = ensure_workspace(session)
            assert workspace.id == workspace_id
            retry = retry_run(session, workspace, run_id, now=now + datetime.timedelta(minutes=5))
            retry_token = retry.run.ownership_token
            assert retry_token is not None
            session.commit()

        resumed = run_personal_daily(
            run_id,
            retry_token,
            session_factory=lambda: Session(engine, autoflush=False),
            rss_provider_factory=lambda source: pytest.fail("frozen retry refetched RSS"),
            embedding_provider=OrthogonalEmbeddingProvider(),
            orchestrator_factory=_orchestrator,
            now=now + datetime.timedelta(minutes=5),
            clock=StepClock(now + datetime.timedelta(minutes=5, seconds=10)),
        )
        assert resumed.report is not None and resumed.report.published is True
        assert len(resumed.report.selected_event_ids) == 5
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "succeeded"
            assert run.attempt == 2
            assert len(run.terminal_manifest["observations"]) == 7
            assert session.scalar(select(func.count()).select_from(Report)) == 1


def test_duplicate_feed_item_does_not_consume_the_last_pending_capacity_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.personal.coordinator as coordinator
    from services.ingestion.normalize import url_hash
    from services.personal.runs import acquire_run

    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 17, 18, tzinfo=UTC)
        run_id, token, source_id, workspace_id = _create_run(engine, now=now)
        duplicate_url = "https://offline.example/existing"
        with Session(engine) as session:
            session.add(
                PersonalCapture(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    source_id=source_id,
                    canonical_url=duplicate_url,
                    url_hash=url_hash(duplicate_url),
                    title="Existing pending item",
                    captured_at=now,
                    truncated=False,
                    receipt={"schema": "personal-capture-receipt.v1"},
                )
            )
            acquire_run(session, run_id, token, now=now)
            session.commit()

        items = [
            RSSItem(
                guid="duplicate",
                title="Duplicate pending item",
                url=duplicate_url,
                published_at=now,
            ),
            RSSItem(
                guid="new",
                title="New pending item",
                url="https://offline.example/new",
                published_at=now,
            ),
        ]
        monkeypatch.setattr(coordinator, "MAX_PENDING_CAPTURES", 2)
        with Session(engine) as session:
            receipt = PersonalFeedReceipt(
                workspace_id=workspace_id,
                run_id=run_id,
                source_id=source_id,
                attempt=1,
                started_at=now,
                status="capturing",
                details={},
            )
            session.add(receipt)
            session.flush()
            receipt_id = receipt.id
            session.commit()
        with Session(engine, autoflush=False) as session:
            outcome = coordinator._capture_one_source(
                session,
                run_id,
                token,
                source_id,
                provider_factory=lambda source: FakeRSSProvider(items),
                captured_at=now + datetime.timedelta(seconds=1),
                receipt_id=receipt_id,
            )
            session.commit()
        assert outcome.fetched == 2
        assert outcome.retained == 1
        assert outcome.capacity_reached is False
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(PersonalCapture)) == 2
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PersonalCapture)
                    .where(PersonalCapture.canonical_url == "https://offline.example/new")
                )
                == 1
            )
