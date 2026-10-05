"""Ordinary registered v2 worker, real ledger/providers, synthetic transport only."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import services.personal.paid_runtime as paid_runtime
import workers.personal_tasks as worker
from apps.api import personal as personal_api
from apps.api.personal_reading import get_spending
from db.models import (
    Article,
    ArticleEmbedding,
    LLMRun,
    PersonalBriefSnapshot,
    PersonalCapture,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    Report,
    Source,
)
from db.models.personal_spending import PersonalPaidRequest
from packages.config.settings import Settings
from packages.providers.base import RSSItem
from packages.providers.fakes import FakeRSSProvider
from services.personal.runs import (
    acquire_run,
    create_daily_run,
    finish_run,
    mark_delivery_failed,
    retry_run,
)
from services.personal.settings import PersonalSettingsUpdate, update_settings
from services.personal.spending import PaidRoute, SpendingLedger
from services.personal.workspace import ensure_workspace
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_worker import _ScriptedLiveHTTPClient
from tests.unit.test_personal_spending import model_route

pytestmark = pytest.mark.integration
_DEADLINE_CLIENT = paid_runtime.DeadlineHTTPClient


def _queued(engine, *, enabled=True, monthly="1", now=None):
    now = now or dt.datetime.now(dt.UTC)
    with Session(engine) as session:
        source = Source(name="Synthetic ordinary worker feed", feed_url="https://worker.test/rss")
        session.add(source)
        session.flush()
        workspace, _ = ensure_workspace(session)
        update_settings(
            session,
            workspace,
            PersonalSettingsUpdate(
                execution_profile="assisted",
                selected_source_ids=[source.id],
                include_phrases=["agency"],
                ai_enabled=enabled,
                monthly_allowance_usd=monthly,
                model_route=model_route(),
            ),
        )
        decision = create_daily_run(session, workspace, now=now)
        identities = decision.run.id, decision.run.ownership_token, workspace.id, source.id
        session.commit()
    return identities


def _runtime(monkeypatch, engine, *, gate=True, credentials=True, failure=None, on_send=None):
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            app_env="test",
            personal_paid_runtime_enabled=gate,
            openai_api_key="synthetic-placeholder" if credentials else "",
            anthropic_api_key="",
            gemini_api_key="",
            deepseek_api_key="",
            personal_offline_fixture_path="",
        ),
    )
    rss = FakeRSSProvider(
        [
            RSSItem(
                guid="ordinary-1",
                title="Agency approves coastal resilience project",
                url="https://worker.test/article/1",
                published_at=dt.datetime.now(dt.UTC),
                summary="The agency confirmed that the coastal resilience project was approved.",
                source="https://worker.test/rss",
            )
        ]
    )
    monkeypatch.setattr(worker, "HttpRSSProvider", lambda: rss)
    script = _ScriptedLiveHTTPClient()
    calls = []
    delegates = []
    with factory() as session:
        prior_requests = session.scalar(select(func.count()).select_from(PersonalPaidRequest))

    def respond(request):
        assert request.url.host == "api.openai.com"
        payload = json.loads(request.content)
        path = request.url.path
        role = "embedding" if path == "/v1/embeddings" else "generation"
        calls.append((role, payload))
        # Observe the independent durable handoff before the physical send seam.
        with factory() as session:
            requests = session.scalars(select(PersonalPaidRequest)).all()
            assert len(requests) == prior_requests + len(calls)
            sent = [row for row in requests if row.status == "dispatching"]
            assert len(sent) == 1
            run = session.get(PersonalRun, sent[0].run_id)
            assert run.scopes_frozen_at is not None and run.enrichment_article_ids
            assert sent[0].route == model_route()[role]
            assert sent[0].dispatch_attempt_at is not None
        if on_send:
            on_send(role, factory)
        if failure == "embedding_retry" and len(calls) == 1:
            return httpx.Response(503, request=request, json={"error": "synthetic retry"})
        if (
            failure == "schema_retry"
            and role == "generation"
            and sum(item[0] == "generation" for item in calls) == 1
        ):
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 80},
                },
            )
        if failure == "generation_timeout" and role == "generation":
            raise httpx.ReadTimeout("synthetic transport timeout", request=request)
        return script.post(path, json=payload)

    class SyntheticDeadlineClient(_DEADLINE_CLIENT):
        def __init__(self, *, base_url):
            super().__init__(base_url=base_url, transport=httpx.MockTransport(respond))
            self.closed = False
            delegates.append(self)

        def close(self):
            self.closed = True
            super().close()

    monkeypatch.setattr(paid_runtime, "DeadlineHTTPClient", SyntheticDeadlineClient)
    return factory, calls, delegates


@pytest.mark.parametrize("failure", [None, "embedding_retry", "schema_retry"])
def test_registered_ordinary_worker_publishes_through_durable_guard(monkeypatch, failure):
    with migrated_disposable_engine() as engine:
        run_id, token, _, _ = _queued(engine)
        factory, calls, delegates = _runtime(monkeypatch, engine, failure=failure)
        result = worker.run_personal_daily_task.run(str(run_id), str(token))
        assert result["status"] == "succeeded" and result["published"]
        assert result["fixture_label"] is None
        assert result["articles_captured"] == result["articles_admitted"] == 1
        assert result["events_observed"] == 1 and result["claims_supported"] >= 1
        assert len(calls) == (5 if failure is None else 6)
        assert delegates and all(client.closed for client in delegates)
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            report = session.get(Report, run.report_id)
            snapshot = session.get(PersonalBriefSnapshot, run.snapshot_id)
            assert report.status == "published" and snapshot.model_route == model_route()
            assert run.stage_results["grouping"]["events_created"] == 1
            assert session.scalar(select(func.count()).select_from(ArticleEmbedding)) == 1
            rows = session.scalars(select(PersonalPaidRequest)).all()
            assert len({row.id for row in rows}) == len(calls)
            assert sum(row.status == "uncertain" for row in rows) == int(
                failure == "embedding_retry"
            )
            assert all(row.status in {"reconciled", "uncertain"} for row in rows)
            assert all(row.attempt == 1 and row.run_id == run_id for row in rows)
            assert sum(row.actual_usd or row.reserved_usd for row in rows) < Decimal("1")
            audits = session.scalars(select(LLMRun)).all()
            assert audits and all(
                row.model_params["personal_paid_ledger_accounted"] for row in audits
            )


@pytest.mark.parametrize(
    "enabled,gate,credentials,monthly,reason",
    [
        (False, True, True, "1", "ai_disabled"),
        (True, False, True, "1", "paid_runtime_disabled"),
        (True, True, False, "1", "configuration_missing"),
        (True, True, True, "0", "allowance_reached"),
        (True, True, True, "0.0000001", "allowance_reached"),
    ],
)
def test_ordinary_worker_falls_back_to_readable_raw_without_dispatch(
    monkeypatch, enabled, gate, credentials, monthly, reason
):
    with migrated_disposable_engine() as engine:
        run_id, token, _, _ = _queued(engine, enabled=enabled, monthly=monthly)
        factory, calls, delegates = _runtime(
            monkeypatch, engine, gate=gate, credentials=credentials
        )
        result = worker.run_personal_daily_task.run(str(run_id), str(token))
        assert result["status"] == "succeeded" and not result["published"]
        assert result["articles_captured"] == result["articles_admitted"] == 1
        assert calls == [] and all(client.closed for client in delegates)
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            assert run.result["reason"] == reason
            assert run.stage_results["embedding"].get("reason") == reason
            assert session.scalar(select(func.count()).select_from(Article)) == 1
            assert session.scalar(select(func.count()).select_from(Report)) == 0
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0
            capture = session.scalar(select(PersonalCapture))
            assert capture.article_id is not None and capture.enrichment_state != "grouped"


def test_ordinary_generation_allowance_block_keeps_report_unpublished(monkeypatch):
    with migrated_disposable_engine() as engine:
        run_id, token, _, _ = _queued(engine, monthly="0.01")
        factory, calls, delegates = _runtime(monkeypatch, engine)
        result = worker.run_personal_daily_task.run(str(run_id), str(token))
        assert result["status"] == "succeeded" and not result["published"]
        assert [role for role, _ in calls] == ["embedding"]
        assert all(client.closed for client in delegates)
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            assert run.result["paid_work_reason"] == "allowance_reached"
            assert run.stage_results["report"] == {
                "status": "blocked",
                "reason": "allowance_reached",
            }
            assert session.get(Report, run.report_id).status == "failed"
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 1


def test_gate_off_raw_articles_transfer_to_a_later_assisted_run(monkeypatch):
    with migrated_disposable_engine() as engine:
        run_id, token, workspace_id, _ = _queued(engine)
        factory, calls, _ = _runtime(monkeypatch, engine, gate=False)
        first = worker.run_personal_daily_task.run(str(run_id), str(token))
        assert first["status"] == "succeeded" and not first["published"] and not calls
        with factory() as session:
            workspace = session.get(PersonalWorkspace, workspace_id)
            next_run = create_daily_run(
                session, workspace, now=dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
            )
            next_id, next_token = next_run.run.id, next_run.run.ownership_token
            session.commit()
        factory, calls, _ = _runtime(monkeypatch, engine)
        second = worker.run_personal_daily_task.run(str(next_id), str(next_token))
        assert second["status"] == "succeeded" and second["published"]
        assert second["articles_admitted"] == 0 and second["events_observed"] == 1
        assert [role for role, _ in calls] == ["embedding"] + ["generation"] * 4
        with factory() as session:
            original = session.get(PersonalRun, run_id)
            assert original.result["reason"] == "paid_runtime_disabled"
            assert original.stage_results["embedding"]["status"] == "disabled"
            capture = session.scalar(select(PersonalCapture))
            assert capture.admitted_run_id == run_id and capture.processing_run_id == next_id
            assert capture.enrichment_state == "grouped"
            assert session.scalar(select(func.count()).select_from(Article)) == 1


def test_api_queued_other_run_obligation_competes_with_ordinary_worker(monkeypatch):
    with migrated_disposable_engine() as engine:
        primary_now = dt.datetime.now(dt.UTC)
        run_id, token, workspace_id, _ = _queued(engine, monthly="0.03", now=primary_now)
        with Session(engine) as session:
            primary_date = session.get(PersonalRun, run_id).local_date
            assert mark_delivery_failed(session, run_id, token, now=primary_now)
            session.commit()

        competitor_now = primary_now + dt.timedelta(days=1)
        monkeypatch.setattr(personal_api, "_utc_now", lambda: competitor_now)
        deliveries = []
        with Session(engine) as session:
            response = personal_api.start_personal_run(
                session, lambda *args: deliveries.append(args)
            )
            assert response.status_code == 202 and len(deliveries) == 1
            competitor_id, delivery_token, _ = deliveries[0]
            competitor = acquire_run(
                session, competitor_id, delivery_token, now=competitor_now, rotate_token=True
            )
            assert competitor.local_date == primary_date + dt.timedelta(days=1)
            competitor_token = competitor.ownership_token
            competitor.scopes_frozen_at = competitor_now
            session.commit()
        factory = sessionmaker(engine, expire_on_commit=False)
        competitor_ledger = SpendingLedger(
            session_factory=factory,
            workspace_id=workspace_id,
            run_id=competitor_id,
            ownership_token=competitor_token,
            clock=lambda: competitor_now,
        )
        obligation = competitor_ledger.reserve(
            PaidRoute.from_mapping(model_route()["generation"], role="generation"),
            input_token_bound=1000,
            output_token_bound=1000,
        )
        competitor_ledger.dispatch(obligation)
        competitor_ledger.mark_uncertain(obligation, "synthetic other-run crash")
        with factory() as session:
            finish_run(session, competitor_id, competitor_token, state="failed", now=competitor_now)
            session.commit()

        # Retry the original calendar identity only after the other date has released
        # the sole processor. Its uncertain obligation remains in the shared month.
        retry_now = dt.datetime.now(dt.UTC)
        monkeypatch.setattr(personal_api, "_utc_now", lambda: retry_now)
        with factory() as session:
            response = personal_api.retry_personal_run(
                run_id, session, lambda *args: deliveries.append(args)
            )
            assert len(deliveries) == 2 and response["run"]["attempt"] == 2
            retried_id, retry_token, _ = deliveries[1]
            assert retried_id == run_id and retry_token != token
            assert session.get(PersonalRun, run_id).local_date == primary_date
        factory, calls, _ = _runtime(monkeypatch, engine)
        result = worker.run_personal_daily_task.run(str(run_id), str(retry_token))
        assert result["status"] == "succeeded" and not result["published"]
        assert [role for role, _ in calls] == ["embedding"]
        with factory() as session:
            summary = get_spending(session)
            assert Decimal(summary["unresolved_usd"]) == Decimal("0.02")
            assert Decimal(summary["finalized_usd"]) == Decimal("0.00024")
            assert Decimal(summary["remaining_usd"]) == Decimal("0.00976")
            assert (
                session.get(PersonalRun, run_id).result["paid_work_reason"] == "allowance_reached"
            )
            assert session.get(PersonalPaidRequest, obligation).status == "uncertain"
            assert session.get(PersonalRun, run_id).attempt == 2
            assert session.get(PersonalRun, competitor_id).attempt == 1


def test_ordinary_failed_report_retry_reuses_snapshot_and_retains_uncertainty(monkeypatch):
    with migrated_disposable_engine() as engine:
        run_id, token, workspace_id, source_id = _queued(engine)
        factory, calls, delegates = _runtime(monkeypatch, engine, failure="generation_timeout")
        result = worker.run_personal_daily_task.run(str(run_id), str(token))
        assert result["status"] in {"failed", "partially_failed"} and not result["published"]
        assert all(client.closed for client in delegates)
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            snapshot = session.get(PersonalBriefSnapshot, run.snapshot_id)
            original = (
                snapshot.id,
                snapshot.input_hash,
                run.profile_revision_id,
                tuple(run.enrichment_article_ids),
            )
            requests = session.scalars(select(PersonalPaidRequest)).all()
            uncertain = {row.id: row.reserved_usd for row in requests if row.status == "uncertain"}
            assert uncertain and len(requests) == len(calls)
            assert session.get(Report, run.report_id).status == "failed"
            workspace = session.get(PersonalWorkspace, workspace_id)
            changed_route = model_route()
            changed_route["generation"]["price_revision"] = "next-run-only"
            update_settings(
                session,
                workspace,
                PersonalSettingsUpdate(
                    execution_profile="assisted",
                    selected_source_ids=[source_id],
                    include_phrases=["unmatched next run setting"],
                    ai_enabled=True,
                    monthly_allowance_usd="1",
                    model_route=changed_route,
                ),
            )
            decision = retry_run(session, workspace, run_id, now=dt.datetime.now(dt.UTC))
            retry_token = decision.run.ownership_token
            session.commit()
        factory, retry_calls, retry_delegates = _runtime(monkeypatch, engine)

        def no_recapture():
            raise AssertionError("frozen retry must not refetch feeds")

        monkeypatch.setattr(worker, "HttpRSSProvider", no_recapture)
        result = worker.run_personal_daily_task.run(str(run_id), str(retry_token))
        assert result["status"] == "succeeded" and result["published"]
        assert [role for role, _ in retry_calls] == ["generation"] * 4
        assert all(client.closed for client in retry_delegates)
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            snapshot = session.get(PersonalBriefSnapshot, run.snapshot_id)
            assert (
                snapshot.id,
                snapshot.input_hash,
                run.profile_revision_id,
                tuple(run.enrichment_article_ids),
            ) == original
            assert snapshot.model_route == model_route()
            assert (
                session.get(PersonalProfileRevision, run.profile_revision_id).settings[
                    "model_route"
                ]
                == model_route()
            )
            assert session.scalar(select(func.count()).select_from(PersonalCapture)) == 1
            assert session.scalar(select(func.count()).select_from(Report)) == 2
            for request_id, reserved in uncertain.items():
                row = session.get(PersonalPaidRequest, request_id)
                assert row.status == "uncertain" and row.reserved_usd == reserved
            rows = session.scalars(
                select(PersonalPaidRequest).where(PersonalPaidRequest.attempt == 2)
            ).all()
            assert len(rows) == 4 and all(row.status == "reconciled" for row in rows)


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
