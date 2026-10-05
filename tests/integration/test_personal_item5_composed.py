"""Item 5's missing API composition and pre-dispatch fencing assertions; synthetic HTTP only."""

from __future__ import annotations

import uuid
from collections import Counter
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import workers.personal_tasks as worker
from db.models import (
    PersonalBriefSnapshot,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    PersonalWriterMode,
    Report,
    Source,
)
from db.models.personal_spending import PersonalPaidRequest
from services.personal.live_smoke import conservative_payload_token_bound
from services.personal.paid_runtime import DurablePaidHTTPClient
from services.personal.runs import finish_run, retry_run
from services.personal.settings import PersonalSettingsUpdate, update_settings
from services.personal.spending import PaidRoute, PaidWorkBlocked, spending_summary
from services.personal.workspace import ensure_workspace
from services.reports.grounding_prompts import GROUNDING_SCHEMA
from services.reports.prompts import COMPOSITION_SCHEMA
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_brief_api import _client
from tests.integration.test_personal_paid_worker import _runtime
from tests.integration.test_personal_spending import NOW, reserve, seed
from tests.unit.test_personal_spending import model_route, route_mapping
from workers.celery_app import QUEUE_PIPELINE, celery_app

pytestmark = pytest.mark.integration


def _configured_workspace(engine, *, route):
    with Session(engine) as session:
        source = Source(name="Item 5 synthetic feed", feed_url="https://worker.test/rss")
        session.add(source)
        session.flush()
        workspace, _ = ensure_workspace(session)
        profile = update_settings(
            session,
            workspace,
            PersonalSettingsUpdate(
                execution_profile="assisted",
                selected_source_ids=[source.id],
                include_phrases=["agency"],
                ai_enabled=True,
                monthly_allowance_usd="1",
                model_route=route,
            ),
        )
        identities = workspace.id, profile.id, source.id
        session.commit()
    return identities


def _capture_production_delivery(monkeypatch, factory):
    deliveries = []

    def capture(task_name, *, args, queue, task_id):
        # Only the broker transport is replaced. The API still uses its production enqueuer.
        assert task_name == worker.TASK_NAME == "workers.personal_tasks.run_personal_daily"
        assert queue == QUEUE_PIPELINE
        assert len(args) == 2 and all(isinstance(value, str) for value in args)
        uuid.UUID(task_id)
        with factory() as session:
            run = session.get(PersonalRun, uuid.UUID(args[0]))
            assert run.state == "queued" and run.attempt == 1
            assert run.ownership_token == uuid.UUID(args[1])
            assert run.celery_task_id == task_id
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0
        deliveries.append((task_name, tuple(args), queue, task_id))

    monkeypatch.setattr(celery_app, "send_task", capture)
    return deliveries


def test_api_enqueuer_registered_worker_preserves_frozen_identity_payload_and_exact_cost(
    monkeypatch,
):
    with migrated_disposable_engine() as engine:
        workspace_id, profile_id, _ = _configured_workspace(engine, route=model_route())
        observed_requests = []
        delivery = {}

        def observe_send(role, factory):
            payload = calls[-1][1]
            with factory() as session:
                row = session.scalar(
                    select(PersonalPaidRequest).where(PersonalPaidRequest.status == "dispatching")
                )
                run = session.get(PersonalRun, row.run_id)
                route = PaidRoute.from_mapping(model_route()[role], role=role)
                assert row.workspace_id == run.workspace_id == workspace_id
                assert str(row.run_id) == delivery["args"][0]
                control = session.get(PersonalWriterMode, True)
                assert str(run.ownership_token) != delivery["args"][1]
                assert control.active_run_id == run.id
                assert control.active_attempt == run.attempt
                assert control.ownership_token == run.ownership_token
                assert control.delivery_token == uuid.UUID(delivery["args"][1])
                assert control.generation == run.fencing_generation
                assert row.attempt == run.attempt == 1
                assert run.profile_revision_id == profile_id and run.scopes_frozen_at is not None
                assert row.role == role and row.route == route.to_mapping()
                assert row.input_token_bound == conservative_payload_token_bound(payload)
                assert row.output_token_bound == (0 if role == "embedding" else 1000)
                assert row.reserved_usd == route.cost(row.input_token_bound, row.output_token_bound)
                assert row.actual_usd is None and row.dispatch_attempt_at is not None
                if role == "generation":
                    snapshot = session.get(PersonalBriefSnapshot, run.snapshot_id)
                    assert snapshot.run_id == run.id and snapshot.profile_revision_id == profile_id
                    assert snapshot.model_route == model_route()
                observed_requests.append(row.id)

        factory, calls, delegates = _runtime(monkeypatch, engine, on_send=observe_send)
        deliveries = _capture_production_delivery(monkeypatch, factory)
        with _client(engine) as client:
            response = client.post("/api/v1/personal/runs")
            assert response.status_code == 202, response.text
            assert len(deliveries) == 1
            delivery["args"] = deliveries[0][1]
            assert response.json()["run"]["id"] == delivery["args"][0]
            assert response.json()["run"]["profile_revision_id"] == str(profile_id)
            assert response.json()["run"]["state"] == "queued"

        task = celery_app.tasks[deliveries[0][0]]
        assert task.name == worker.TASK_NAME and task.run == worker.run_personal_daily_task.run
        result = task.run(*delivery["args"])
        assert result["status"] == "succeeded" and result["published"]
        assert [role for role, _ in calls] == ["embedding"] + ["generation"] * 4
        assert len(set(observed_requests)) == len(calls) == 5
        assert delegates and all(delegate.closed for delegate in delegates)
        embedding_payload = calls[0][1]
        assert embedding_payload["model"] == "synthetic-only"
        assert (
            embedding_payload["dimensions"] == 1536
            and embedding_payload["encoding_format"] == "float"
        )
        assert len(embedding_payload["input"]) == 1
        schemas = Counter()
        for _, payload in calls[1:]:
            assert payload["model"] == "synthetic-only" and payload["max_completion_tokens"] == 1000
            assert payload["messages"][0]["content"]
            schemas[payload["response_format"]["json_schema"]["name"]] += 1
        assert schemas == {COMPOSITION_SCHEMA: 2, GROUNDING_SCHEMA: 2}
        with factory() as session:
            run = session.get(PersonalRun, uuid.UUID(delivery["args"][0]))
            assert session.get(Report, run.report_id).status == "published"
            assert (
                session.get(PersonalProfileRevision, profile_id).settings["model_route"]
                == model_route()
            )
            rows = {row.id: row for row in session.scalars(select(PersonalPaidRequest))}
            assert set(rows) == set(observed_requests)
            for row in rows.values():
                expected_usage = (24, 0) if row.role == "embedding" else (100, 80)
                expected_cost = Decimal("0.00024") if row.role == "embedding" else Decimal("0.0018")
                assert row.status == "reconciled" and row.reconciled_at is not None
                assert (row.input_tokens, row.output_tokens) == expected_usage
                assert row.actual_usd == expected_cost
        with _client(engine) as client:
            response = client.get("/api/v1/personal/spending")
            assert response.status_code == 200
            summary = response.json()
            assert Decimal(summary["finalized_usd"]) == Decimal("0.00744")
            assert Decimal(summary["reserved_usd"]) == Decimal(summary["unresolved_usd"]) == 0
            assert Decimal(summary["remaining_usd"]) == Decimal("0.99256")
            assert summary["current_configuration"]["profile_revision_id"] == str(profile_id)
            assert {route["price_revision"] for route in summary["accounted_routes"]} == {
                "synthetic-test-prices-not-live"
            }


def test_missing_route_api_run_keeps_raw_reader_available_without_paid_dispatch(monkeypatch):
    with migrated_disposable_engine() as engine:
        _configured_workspace(engine, route={})
        factory, calls, delegates = _runtime(monkeypatch, engine)
        deliveries = _capture_production_delivery(monkeypatch, factory)
        with _client(engine) as client:
            response = client.post("/api/v1/personal/runs")
            assert response.status_code == 202, response.text
        assert len(deliveries) == 1
        result = celery_app.tasks[deliveries[0][0]].run(*deliveries[0][1])
        assert result["status"] == "succeeded" and not result["published"]
        assert calls == [] and delegates == []
        with factory() as session:
            run = session.get(PersonalRun, uuid.UUID(deliveries[0][1][0]))
            assert run.result["reason"] == "configuration_missing"
            assert run.stage_results["embedding"]["reason"] == "configuration_missing"
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0
            assert session.scalar(select(func.count()).select_from(Report)) == 0
        with _client(engine) as client:
            response = client.get("/api/v1/personal/articles")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == len(body["items"]) == 1
            article = body["items"][0]
            assert article["title"] == "Agency approves coastal resilience project"
            assert article["url"] == "https://worker.test/article/1"
            assert article["snippet"] and article["event_id"] is None


@pytest.mark.parametrize("reason", ["scope_not_frozen", "route_changed"])
def test_guard_rejects_unfrozen_scope_or_mismatched_route_before_rows_or_send(reason):
    with migrated_disposable_engine() as engine:
        factory, (ledger,) = seed(engine, monthly="1", dates=1)
        mapping = route_mapping()
        if reason == "scope_not_frozen":
            with factory() as session, session.begin():
                session.get(PersonalRun, ledger.run_id).scopes_frozen_at = None
        else:
            mapping["price_revision"] = "synthetic-mismatched-revision"
        delegate = Mock()
        guard = DurablePaidHTTPClient(
            route=PaidRoute.from_mapping(mapping, role="generation"),
            ledger=ledger,
            delegate=delegate,
        )
        with pytest.raises(PaidWorkBlocked) as rejected:
            guard.post(
                "/v1/chat/completions",
                json={"model": "synthetic-only", "max_completion_tokens": 1000},
            )
        assert rejected.value.code == ledger.blocked_code == reason
        delegate.post.assert_not_called()
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0


def test_reserved_dispatch_rejects_rotated_owner_and_retains_original_obligation():
    with migrated_disposable_engine() as engine:
        factory, (ledger,) = seed(engine, monthly="1", dates=1)
        identity = reserve(ledger)
        with factory() as session, session.begin():
            session.get(PersonalWriterMode, True).mode = "personal"
        with factory() as session, session.begin():
            finish_run(session, ledger.run_id, ledger.ownership_token, state="failed")
        with factory() as session, session.begin():
            decision = retry_run(
                session, session.get(PersonalWorkspace, ledger.workspace_id), ledger.run_id, now=NOW
            )
            new_token = decision.run.ownership_token
            assert decision.run.attempt == 2 and new_token != ledger.ownership_token
        with pytest.raises(PaidWorkBlocked) as rejected:
            ledger.dispatch(identity)
        assert rejected.value.code == "stale_run_attempt"
        delegate = Mock()
        guard = DurablePaidHTTPClient(
            route=PaidRoute.from_mapping(route_mapping(), role="generation"),
            ledger=ledger,
            delegate=delegate,
        )
        with pytest.raises(PaidWorkBlocked, match="stale run attempt"):
            guard.post(
                "/v1/chat/completions",
                json={"model": "synthetic-only", "max_completion_tokens": 1000},
            )
        delegate.post.assert_not_called()
        with factory() as session:
            row = session.get(PersonalPaidRequest, identity)
            assert (
                row.status == "reserved"
                and row.dispatch_attempt_at is None
                and row.actual_usd is None
            )
            assert row.attempt == 1 and row.reserved_usd == Decimal("0.02")
            assert row.route == route_mapping()
            run = session.get(PersonalRun, ledger.run_id)
            assert run.state == "queued" and run.attempt == 2 and run.ownership_token == new_token
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 1
            summary = spending_summary(session, ledger.workspace_id, now=NOW)
            assert Decimal(summary["reserved_usd"]) == Decimal("0.02")
            assert Decimal(summary["remaining_usd"]) == Decimal("0.98")


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
