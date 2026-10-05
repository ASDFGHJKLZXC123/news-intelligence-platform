"""Spending GET metadata uses retained routes, never current prices for old charges."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from db.models import LLMRun, PersonalProfileRevision, PersonalRun, PersonalWorkspace
from db.models.personal_spending import PersonalLegacyUsage, PersonalPaidRequest
from services.personal import spending
from services.personal.spending import PaidRoute, import_legacy_usage, spending_summary
from services.personal.workspace import ensure_workspace
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_brief_api import _client
from tests.integration.test_personal_spending import NOW, handoff_to_date, reserve, revise, seed

pytestmark = pytest.mark.integration
SECRET = "synthetic-credential-must-not-appear"


def _identity(
    role,
    provider="openai",
    model="synthetic-only",
    version="fixture-v1",
    price="synthetic-test-prices-not-live",
):
    return {
        "role": role,
        "provider": provider,
        "model": model,
        "model_version": version,
        "price_revision": price,
    }


def test_spending_get_distinguishes_frozen_routes_from_disabled_current_configuration(monkeypatch):
    with migrated_disposable_engine() as engine:
        prior = NOW.replace(month=8)
        factory, (ledger, _) = seed(engine, monthly="1", now=prior)
        old_finalized = reserve(ledger)
        ledger.dispatch(old_finalized)
        ledger.reconcile(old_finalized, input_tokens=100, output_tokens=100)
        old_uncertain = reserve(ledger)
        ledger.dispatch(old_uncertain)
        ledger.mark_uncertain(old_uncertain, reason="synthetic timeout")
        ledger = handoff_to_date(factory, ledger, now=NOW)
        finalized = reserve(ledger)
        ledger.dispatch(finalized)
        ledger.reconcile(finalized, input_tokens=100, output_tokens=100)
        reserved = reserve(ledger)
        cancelled = reserve(ledger)
        ledger.cancel_before_dispatch(cancelled)
        non_billable = reserve(ledger)
        ledger.dispatch(non_billable)
        ledger.confirm_non_billable(non_billable, receipt="synthetic-nonbillable-receipt")
        revise(factory, ledger, monthly="1", enabled=False)
        with factory() as session, session.begin():
            workspace = session.get(PersonalWorkspace, ledger.workspace_id)
            profile = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
            routes = profile.settings["model_route"]
            profile.settings = {
                **profile.settings,
                "api_key": SECRET,
                "model_route": {
                    **routes,
                    "authorization": SECRET,
                    "generation": {
                        **routes["generation"],
                        "provider": "anthropic",
                        "model": "next-synthetic-model",
                        "model_version": "fixture-v2",
                        "price_revision": "next-synthetic-prices",
                        "api_key": SECRET,
                    },
                },
            }
            current_id, current_revision = str(profile.id), profile.revision
            for request_id in (old_uncertain, finalized, reserved):
                row = session.get(PersonalPaidRequest, request_id)
                row.route = {**row.route, "api_key": SECRET, "credentials": {"token": SECRET}}
                row.evidence = {"authorization": SECRET}
                row.provider_request_id = SECRET
            for request_id, excluded_model in (
                (old_finalized, "previous-finalized-excluded"),
                (cancelled, "cancelled-excluded"),
                (non_billable, "nonbillable-excluded"),
            ):
                row = session.get(PersonalPaidRequest, request_id)
                row.route = {**row.route, "model": excluded_model}
        summary_function = spending_summary
        monkeypatch.setattr(
            spending,
            "spending_summary",
            lambda session, workspace_id: summary_function(session, workspace_id, now=NOW),
        )
        with _client(engine) as client:
            response = client.get("/api/v1/personal/spending")
            assert response.status_code == 200
            summary = response.json()
            assert client.get("/api/v1/personal/spending").json() == summary
        assert summary["current_configuration"] == {
            "profile_revision_id": current_id,
            "profile_revision": current_revision,
            "route_mode": "live",
            "routes": [
                _identity(
                    "generation",
                    "anthropic",
                    "next-synthetic-model",
                    "fixture-v2",
                    "next-synthetic-prices",
                ),
                _identity("embedding"),
            ],
        }
        assert summary["accounted_routes"] == [
            {"source": "paid_request", "accounting_period": "2026-08", **_identity("generation")},
            {"source": "paid_request", "accounting_period": "2026-09", **_identity("generation")},
        ]
        assert summary["status"] == "ai_disabled"
        assert summary["accounting_period"] == "2026-09"
        assert summary["accounting_timezone"] == "UTC"
        assert summary["next_reset_at"] == "2026-09-30T17:00:00-07:00"
        assert Decimal(summary["finalized_usd"]) == Decimal("0.002")
        assert Decimal(summary["reserved_usd"]) == Decimal("0.02")
        assert Decimal(summary["unresolved_usd"]) == Decimal("0.04")
        assert summary["remaining_usd"] == "0"
        assert SECRET not in json.dumps(summary)
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 6
            assert session.get(PersonalPaidRequest, old_uncertain).status == "uncertain"
            assert session.get(PersonalPaidRequest, reserved).status == "reserved"


def test_legacy_metadata_keeps_unknown_prices_and_is_identical_before_and_after_import():
    with migrated_disposable_engine() as engine:
        factory, (ledger, _) = seed(engine, monthly="1")
        with factory() as session, session.begin():
            for cost, params in (
                (Decimal("0.005"), {"price_revision": "unverified-extra", "api_key": SECRET}),
                (None, None),
                (Decimal("0.9"), {"personal_paid_ledger_accounted": True}),
            ):
                session.add(
                    LLMRun(
                        prompt_name="synthetic",
                        prompt_version="v1",
                        provider="legacy-provider",
                        model="legacy-model" if cost != Decimal("0.9") else "guarded-copy-excluded",
                        model_params=params,
                        cost_usd=cost,
                        created_at=NOW - dt.timedelta(hours=1),
                    )
                )
        with factory() as session:
            before = session.scalar(select(func.count()).select_from(PersonalLegacyUsage))
            summary = spending_summary(session, ledger.workspace_id, now=NOW)
            assert summary["accounted_routes"] == [
                {
                    "source": "legacy_usage",
                    "accounting_period": "2026-09",
                    **_identity(None, "legacy-provider", "legacy-model", None, None),
                }
            ]
            assert summary["unreconciled_legacy_count"] == 1
            assert Decimal(summary["finalized_usd"]) == Decimal("0.005")
            assert SECRET not in json.dumps(summary)
            assert not session.new and not session.dirty
            assert session.scalar(select(func.count()).select_from(PersonalLegacyUsage)) == before
        with factory() as session, session.begin():
            import_legacy_usage(session, ledger.workspace_id, now=NOW)
        with factory() as session:
            assert spending_summary(session, ledger.workspace_id, now=NOW) == summary


def test_spending_metadata_without_active_profile_or_paid_records_is_explicit():
    with migrated_disposable_engine() as engine:
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            workspace, _ = ensure_workspace(session)
            workspace.active_profile_revision_id = None
            session.commit()
            summary = spending_summary(session, workspace.id, now=NOW)
            assert summary["current_configuration"] == {
                "profile_revision_id": None,
                "profile_revision": None,
                "route_mode": None,
                "routes": [],
            }
            assert summary["accounted_routes"] == []
            assert summary["monthly_allowance_usd"] == summary["remaining_usd"] == "0"
            assert not session.new and not session.dirty


def test_same_period_charges_retain_both_price_revisions_for_the_same_provider_and_model():
    with migrated_disposable_engine() as engine:
        factory, (first, second) = seed(engine, monthly="1")
        original = reserve(first)
        first.dispatch(original)
        first.reconcile(original, input_tokens=100, output_tokens=100)
        with factory() as session, session.begin():
            workspace = session.get(PersonalWorkspace, first.workspace_id)
            old = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
            changed_route = {
                **old.settings["model_route"]["generation"],
                "price_revision": "synthetic-v2-prices",
                "input_usd_per_million_tokens": "20",
                "output_usd_per_million_tokens": "20",
            }
            current = PersonalProfileRevision(
                workspace_id=workspace.id,
                revision=old.revision + 1,
                execution_profile="assisted",
                schema_revision="personal-profile.v2",
                selected_source_ids=old.selected_source_ids,
                include_phrases=old.include_phrases,
                exclude_phrases=old.exclude_phrases,
                settings={
                    **old.settings,
                    "model_route": {**old.settings["model_route"], "generation": changed_route},
                },
            )
            session.add(current)
            session.flush()
            workspace.active_profile_revision_id = current.id
            first_profile_id = session.get(PersonalRun, first.run_id).profile_revision_id
            current_profile_id = current.id
        second = handoff_to_date(factory, first, now=NOW + dt.timedelta(days=1))
        with factory() as session:
            assert session.get(PersonalRun, first.run_id).profile_revision_id == first_profile_id
            assert session.get(PersonalRun, second.run_id).profile_revision_id == current_profile_id
        revised = second.reserve(
            PaidRoute.from_mapping(changed_route, role="generation"),
            input_token_bound=1000,
            output_token_bound=1000,
        )
        second.dispatch(revised)
        second.reconcile(revised, input_tokens=100, output_tokens=100)
        with factory() as session:
            summary = spending_summary(session, first.workspace_id, now=NOW)
            assert summary["accounted_routes"] == [
                {
                    "source": "paid_request",
                    "accounting_period": "2026-09",
                    **_identity("generation"),
                },
                {
                    "source": "paid_request",
                    "accounting_period": "2026-09",
                    **_identity("generation", price="synthetic-v2-prices"),
                },
            ]
            assert Decimal(summary["finalized_usd"]) == Decimal("0.006")
            assert Decimal(summary["remaining_usd"]) == Decimal("0.994")
            assert (
                summary["current_configuration"]["routes"][0]["price_revision"]
                == "synthetic-v2-prices"
            )
