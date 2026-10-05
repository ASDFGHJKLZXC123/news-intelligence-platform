"""Raw RSS reading stays available independently of optional AI runtime."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    EMBEDDING_DIM,
    Event,
    EventArticle,
    PersonalCapture,
    PersonalRun,
    PersonalWorkspace,
    Source,
)
from packages.providers.base import RSSItem
from packages.providers.fakes import FakeEmbeddingProvider, FakeRSSProvider
from services.personal.coordinator import PersonalCoordinatorError, run_personal_daily
from services.personal.runs import create_daily_run
from services.personal.settings import PersonalSettingsUpdate, update_settings
from services.personal.spending import PaidWorkBlocked
from services.personal.workspace import ensure_workspace
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_brief_api import _client

pytestmark = pytest.mark.integration
NOW = dt.datetime(2026, 9, 20, 18, tzinfo=dt.UTC)


def _run(engine, *, profile="raw", enabled=False, route=None, embedding=None, failed_feed=False):
    with Session(engine) as session:
        source = Source(
            name="Synthetic RSS only", feed_url=f"https://offline.example/{uuid.uuid4()}"
        )
        session.add(source)
        session.flush()
        workspace, _ = ensure_workspace(session)
        update_settings(
            session,
            workspace,
            PersonalSettingsUpdate(
                selected_source_ids=[source.id],
                execution_profile=profile,
                ai_enabled=enabled,
                model_route=route or {},
                run_article_limit=2,
                include_phrases=["AI"],
            ),
        )
        decision = create_daily_run(session, workspace, now=NOW)
        run_id, token = decision.run.id, decision.run.ownership_token
        workspace_id, source_id = workspace.id, source.id
        session.commit()
    items = [
        RSSItem(
            guid=str(n),
            title=title,
            summary="Retained RSS description.",
            published_at=None,
            url=f"https://offline.example/{source_id}/{n}",
        )
        for n, title in enumerate(
            ["AI research published", "Agency said research published", "AI backlog item"]
        )
    ]

    def no_model(*args, **kwargs):
        pytest.fail("raw/blocked workflow attempted report generation")

    def feed_factory(source):
        if failed_feed:
            raise RuntimeError("scripted feed failure")
        return FakeRSSProvider(items)

    run_personal_daily(
        run_id,
        token,
        session_factory=lambda: Session(engine, autoflush=False),
        rss_provider_factory=feed_factory,
        embedding_provider=embedding or FakeEmbeddingProvider(dimension=EMBEDDING_DIM),
        orchestrator_factory=no_model,
        now=NOW,
        clock=lambda: NOW,
    )
    return run_id, workspace_id, source_id


@pytest.mark.parametrize(
    ("profile", "enabled", "reason"),
    [
        ("raw", False, "disabled_by_profile"),
        ("assisted", False, "ai_disabled"),
        ("assisted", True, "configuration_missing"),
    ],
)
def test_raw_api_without_optional_ai_preserves_unknown_dates_pagination_and_backlog(
    profile, enabled, reason
):
    with migrated_disposable_engine() as engine:
        run_id, _, _ = _run(engine, profile=profile, enabled=enabled)
        with _client(engine) as client:
            rows = client.get("/api/v1/personal/articles?limit=1").json()
            assert rows["total"] == 2 and len(rows["items"]) == 1
            first = rows["items"][0]
            assert first["published_at"] is None and first["admitted_at"]
            assert first["enrichment_state"] == reason
            assert (
                client.get("/api/v1/personal/articles?offset=1&limit=1").json()["items"][0]["id"]
                != first["id"]
            )
            assert client.get("/api/v1/personal/articles?interest_only=true").json()["total"] == 1
            assert client.get("/api/v1/personal/articles?limit=101").status_code == 422
            assert client.get(f"/api/v1/personal/articles?run_id={uuid.uuid4()}").status_code == 404
            assert client.get("/api/v1/personal/articles?q=research").json()["total"] == 2
            assert client.get("/api/v1/personal/articles?q=Retained%20RSS").json()["total"] == 2
            pending = client.get("/api/v1/personal/backlog").json()
            assert pending["total"] == pending["eligible_total"] == 1
            collection = client.get("/api/v1/personal/collection").json()
            assert collection["observed_candidates"] == 3
            assert collection["pending_candidates"] == 1
            assert collection["admitted_articles"] == 2
            assert collection["grouped_articles"] == 0
            assert collection["receipts"][0]["status"] == "complete"
            assert client.get("/api/v1/personal/spending").json()["remaining_usd"] == "0"
            assert (
                client.get(f"/api/v1/personal/runs/{run_id}").json()["run"]["state"] == "succeeded"
            )


def test_grouped_article_replaces_raw_card_and_original_detail_remains_readable():
    with migrated_disposable_engine() as engine:
        _, workspace_id, source_id = _run(engine)
        with Session(engine) as session:
            capture = session.scalar(
                select(PersonalCapture).where(PersonalCapture.admitted_at.is_not(None))
            )
            article_id = capture.article_id
            event = Event(title="Existing grouped source")
            session.add(event)
            session.flush()
            event_id = event.id
            session.add(EventArticle(event_id=event_id, article_id=article_id))
            session.get(Source, source_id).active = False
            session.commit()
        with _client(engine) as client:
            assert client.get("/api/v1/personal/articles").json()["total"] == 1
            detail = client.get(f"/api/v1/personal/articles/{article_id}").json()["article"]
            assert detail["event_id"] == str(event_id) and detail["enrichment_state"] == "grouped"
            assert client.get("/api/v1/personal/articles?ungrouped=false").json()["total"] == 2
            counts = client.get("/api/v1/personal/collection").json()
            assert counts["grouped_articles"] == 1
            assert counts["completed_enrichment"] == 0
            pending = client.get("/api/v1/personal/backlog").json()
            assert pending["disabled_source_total"] == 1 and pending["eligible_total"] == 0
        with Session(engine) as session:
            assert session.get(PersonalWorkspace, workspace_id) is not None


def test_partial_feed_failure_does_not_claim_a_disabled_provider_failed():
    with migrated_disposable_engine() as engine:
        run_id, _, _ = _run(engine)
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            run.state = "partially_failed"
            run.error = {
                "code": "partial_capture",
                "message": "One feed failed; retained RSS is available.",
            }
            session.commit()
        with _client(engine) as client:
            rows = client.get("/api/v1/personal/articles").json()["items"]
            assert all(row["enrichment_state"] == "disabled_by_profile" for row in rows)
            assert all("One feed failed" in row["error"] for row in rows)


def test_budget_block_retains_readable_transferable_articles_without_repeated_admission():
    class NoAllowance:
        model_name = "scripted"
        model_version = "v1"

        def embed(self, _texts):
            raise PaidWorkBlocked("allowance_reached")

    with migrated_disposable_engine() as engine:
        run_id, _, _ = _run(
            engine,
            profile="assisted",
            route={"mode": "offline_fixture", "fixture_id": "bounds"},
            embedding=NoAllowance(),
        )
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "succeeded" and run.result["reason"] == "allowance_reached"
            assert run.stage_results["embedding"]["status"] == "blocked"
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PersonalCapture)
                    .where(PersonalCapture.enrichment_state == "deferred_budget")
                )
                == 2
            )
        with _client(engine) as client:
            assert client.get("/api/v1/personal/articles").json()["total"] == 2


def test_required_provider_failure_stays_failed_with_retained_raw_content():
    class BrokenProvider:
        model_name = "scripted"
        model_version = "v1"

        def embed(self, _texts):
            raise RuntimeError("scripted provider failure")

    with migrated_disposable_engine() as engine:
        with pytest.raises(PersonalCoordinatorError):
            _run(
                engine,
                profile="assisted",
                route={"mode": "offline_fixture", "fixture_id": "bounds"},
                embedding=BrokenProvider(),
            )
        with _client(engine) as client:
            rows = client.get("/api/v1/personal/articles").json()
            assert rows["total"] == 2
            assert all(
                row["enrichment_state"] == "provider_failed" and row["error"]
                for row in rows["items"]
            )
        with Session(engine) as session:
            run = session.scalar(select(PersonalRun))
            assert run.state == "failed"
            assert run.stage_results["entity_linking"]["reason"] == "disabled_by_profile"


@pytest.fixture(autouse=True)
def _phase4a_personal_test_context(monkeypatch):
    """Retain authored fixture time under explicit personal deployment selection."""
    import sys

    from packages.config.settings import get_settings
    from tests.integration._personal_processing_clock import install_authored_processing_clock

    monkeypatch.setenv("PERSONAL_PROCESSING_MODE", "personal")
    get_settings.cache_clear()
    install_authored_processing_clock(monkeypatch, sys.modules[__name__])
    yield
    get_settings.cache_clear()
