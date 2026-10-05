"""P3-08/09/10/11: real PostgreSQL, synthetic costs and scripted HTTP only."""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from db.models import (
    LLMRun,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    PersonalWriterMode,
    Source,
)
from db.models.personal_spending import (
    PersonalLegacyUsage,
    PersonalPaidRequest,
    PersonalSpendingState,
)
from services.personal.paid_runtime import DurablePaidHTTPClient
from services.personal.processing import recover_expired_owner, switch_processing_mode
from services.personal.runs import (
    acquire_run,
    create_daily_run,
    finish_run,
    lock_owned_run,
    retry_run,
)
from services.personal.spending import (
    PaidRoute,
    PaidWorkBlocked,
    SpendingLedger,
    acquire_workspace_spending_lock,
    assert_legacy_usage_reconciled,
    import_legacy_usage,
    reconcile_legacy_usage,
    spending_summary,
)
from services.personal.workspace import ensure_workspace
from tests.integration._stage7_db import migrated_disposable_engine
from tests.unit.test_personal_spending import model_route, route_mapping

pytestmark = pytest.mark.integration
NOW = dt.datetime(2026, 9, 20, 12, tzinfo=dt.UTC)


def seed(engine, *, monthly="0.03", run_limit=None, dates=2, now=NOW):
    """Create one valid processor and several monetary clients for that owner.

    Phase 4A permits one processor across dates. The older `dates` argument retains
    its fixture arity; cross-date allowance is demonstrated by explicit handoff below.
    """
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        workspace, _ = ensure_workspace(session)
        source = Source(
            name="Synthetic spending source", feed_url=f"https://synthetic.invalid/{uuid.uuid4()}"
        )
        session.add(source)
        session.flush()
        profile = PersonalProfileRevision(
            workspace_id=workspace.id,
            revision=100,
            execution_profile="assisted",
            schema_revision="personal-profile.v2",
            selected_source_ids=[source.id],
            include_phrases=[],
            exclude_phrases=[],
            settings={
                "ai_enabled": True,
                "monthly_allowance_usd": monthly,
                "run_allowance_usd": run_limit,
                "model_route": model_route(),
            },
        )
        session.add(profile)
        session.flush()
        workspace.active_profile_revision_id = profile.id
        switch_processing_mode(session, "personal", now=now)
        session.flush()
        decision = create_daily_run(session, workspace, now=now)
        run = acquire_run(session, decision.run.id, decision.run.ownership_token, now=now)
        run.scopes_frozen_at = now
        run_id, token, workspace_id = run.id, run.ownership_token, workspace.id
        session.commit()
    ledgers = [
        SpendingLedger(
            session_factory=factory,
            workspace_id=workspace_id,
            run_id=run_id,
            ownership_token=token,
            clock=lambda: now,
        )
        for _ in range(dates)
    ]
    return factory, ledgers


def handoff_to_date(factory, ledger, *, now):
    """Finish this owned fixture before a new calendar date claims the global record."""
    with factory() as session:
        run = session.get(PersonalRun, ledger.run_id)
        if run.state == "running":
            finish_run(
                session, run.id, ledger.ownership_token, state="succeeded", now=ledger.clock()
            )
            session.commit()
        workspace = session.get(PersonalWorkspace, ledger.workspace_id)
        decision = create_daily_run(session, workspace, now=now)
        assert decision.created
        owned = acquire_run(session, decision.run.id, decision.run.ownership_token, now=now)
        owned.scopes_frozen_at = now
        run_id, token = owned.id, owned.ownership_token
        session.commit()
    return SpendingLedger(
        session_factory=factory,
        workspace_id=ledger.workspace_id,
        run_id=run_id,
        ownership_token=token,
        clock=lambda: now,
    )


def reserve(ledger):
    return ledger.reserve(
        PaidRoute.from_mapping(route_mapping(), role="generation"),
        input_token_bound=1000,
        output_token_bound=1000,
    )


def revise(factory, ledger, *, monthly, enabled=True):
    with factory() as session, session.begin():
        acquire_workspace_spending_lock(session, ledger.workspace_id)
        workspace = session.get(PersonalWorkspace, ledger.workspace_id)
        old = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
        profile = PersonalProfileRevision(
            workspace_id=workspace.id,
            revision=old.revision + 1,
            execution_profile="assisted",
            settings={**old.settings, "monthly_allowance_usd": monthly, "ai_enabled": enabled},
        )
        session.add(profile)
        session.flush()
        workspace.active_profile_revision_id = profile.id


def test_competing_reservations_and_explicit_next_date_share_one_month_allowance():
    with migrated_disposable_engine() as engine:
        factory, ledgers = seed(engine)
        barrier = threading.Barrier(2)

        def competing(ledger):
            barrier.wait(timeout=10)
            try:
                identity = reserve(ledger)
                ledger.dispatch(identity)
                return "dispatched"
            except PaidWorkBlocked as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(competing, ledgers))
        assert sorted(results) == ["allowance_reached", "dispatched"]
        with factory() as session:
            rows = session.scalars(select(PersonalPaidRequest)).all()
            assert len(rows) == 1 and rows[0].reserved_usd == Decimal("0.02")
        following = handoff_to_date(factory, ledgers[0], now=NOW + dt.timedelta(days=1))
        assert following.run_id != ledgers[0].run_id
        with pytest.raises(PaidWorkBlocked, match="allowance reached"):
            reserve(following)


def test_crash_reconstruction_retains_cost_retry_requires_new_credit_and_receipt_is_idempotent():
    with migrated_disposable_engine() as engine:
        factory, (ledger, _) = seed(engine)
        identity = reserve(ledger)
        ledger.dispatch(identity)  # Simulated crash at handoff: no completion write.
        restarted = SpendingLedger(
            session_factory=factory,
            workspace_id=ledger.workspace_id,
            run_id=ledger.run_id,
            ownership_token=ledger.ownership_token,
            clock=lambda: NOW,
        )
        with pytest.raises(PaidWorkBlocked, match="allowance reached"):
            reserve(restarted)
        restarted.reconcile(
            identity, input_tokens=100, output_tokens=100, provider_request_id="synthetic-receipt"
        )
        restarted.reconcile(
            identity, input_tokens=100, output_tokens=100, provider_request_id="synthetic-receipt"
        )
        retry_id = reserve(restarted)
        assert retry_id != identity
        with factory() as session:
            summary = spending_summary(session, ledger.workspace_id, now=NOW)
            assert Decimal(summary["finalized_usd"]) == Decimal("0.002")
            assert Decimal(summary["reserved_usd"]) == Decimal("0.02")
            assert Decimal(summary["remaining_usd"]) == Decimal("0.008")
        with pytest.raises(ValueError, match="cannot be rewritten"):
            restarted.reconcile(identity, input_tokens=1, output_tokens=1)


def test_month_handoff_moves_reservation_and_late_receipt_stays_in_dispatch_month():
    with migrated_disposable_engine() as engine:
        clock = [dt.datetime(2026, 9, 30, 23, 59, 59, tzinfo=dt.UTC)]
        factory, (ledger, _) = seed(engine, monthly="1", now=clock[0])
        ledger.clock = lambda: clock[0]
        identity = reserve(ledger)
        clock[0] = dt.datetime(2026, 10, 1, 0, 0, 1, tzinfo=dt.UTC)
        ledger.dispatch(identity)
        clock[0] = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
        ledger.reconcile(identity, input_tokens=100, output_tokens=100)
        with factory() as session:
            row = session.get(PersonalPaidRequest, identity)
            assert row.accounting_month == dt.date(2026, 10, 1)
            assert row.dispatch_attempt_at.month == 10 and row.reconciled_at.month == 11
            summary = spending_summary(
                session, ledger.workspace_id, now=dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
            )
            assert summary["next_reset_at"] == "2026-10-31T17:00:00-07:00"
            assert Decimal(summary["finalized_usd"]) == Decimal("0.002")


def test_explicit_month_handoff_fences_prior_owner_and_cancel_only_before_send():
    with migrated_disposable_engine() as engine:
        october = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
        factory, (ledger, other) = seed(engine, now=october - dt.timedelta(seconds=1))
        old = reserve(ledger)
        next_date = october + dt.timedelta(hours=8)
        other = handoff_to_date(factory, ledger, now=next_date)
        ledger.clock = lambda: next_date
        dispatched = reserve(other)
        other.dispatch(dispatched)
        with pytest.raises(PaidWorkBlocked, match="stale run attempt"):
            ledger.dispatch(old)
        ledger.cancel_before_dispatch(old)
        with pytest.raises(ValueError):
            other.cancel_before_dispatch(dispatched)
        with factory() as session:
            assert session.get(PersonalPaidRequest, old).status == "cancelled"
            assert session.get(PersonalPaidRequest, dispatched).status == "dispatching"


def test_month_handoff_revalidates_retained_next_bucket_cost_before_send():
    with migrated_disposable_engine() as engine:
        october = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
        factory, (ledger, _) = seed(engine, now=october - dt.timedelta(seconds=1))
        identity = reserve(ledger)
        # A retained accounting obligation consumes the destination bucket without
        # constructing a second simultaneous processor or expanding the .03 cap.
        with factory() as session, session.begin():
            session.add(
                LLMRun(
                    prompt_name="retained-month-boundary-cost",
                    prompt_version="v1",
                    provider="synthetic",
                    model="synthetic",
                    cost_usd=Decimal("0.02"),
                    created_at=october,
                    started_at=october,
                )
            )
        ledger.clock = lambda: october
        with pytest.raises(PaidWorkBlocked, match="allowance reached"):
            ledger.dispatch(identity)
        with factory() as session:
            row = session.get(PersonalPaidRequest, identity)
            assert row.status == "reserved" and row.dispatch_attempt_at is None
            assert row.accounting_month == dt.date(2026, 9, 1)
            summary = spending_summary(session, ledger.workspace_id, now=october)
            assert Decimal(summary["finalized_usd"]) == Decimal("0.02")
            assert Decimal(summary["monthly_allowance_usd"]) == Decimal("0.03")
        ledger.cancel_before_dispatch(identity)


def test_live_lowering_applies_before_dispatch_but_raise_cannot_expand_frozen_run():
    with migrated_disposable_engine() as engine:
        factory, (ledger, _) = seed(engine)
        identity = reserve(ledger)
        revise(factory, ledger, monthly="0.01")
        with pytest.raises(PaidWorkBlocked):
            ledger.dispatch(identity)
        ledger.cancel_before_dispatch(identity)
        revise(factory, ledger, monthly="1")
        first = reserve(ledger)
        ledger.dispatch(first)
        with pytest.raises(PaidWorkBlocked):
            reserve(ledger)
        revise(factory, ledger, monthly="1", enabled=False)
        with pytest.raises(PaidWorkBlocked, match="ai disabled"):
            reserve(ledger)


def test_independent_commit_survives_business_rollback_without_row_lock_deadlock():
    with migrated_disposable_engine() as engine:
        factory, (ledger, _) = seed(engine)
        with factory() as business:
            run = lock_owned_run(business, ledger.run_id, ledger.ownership_token, now=NOW)
            run.coverage = {"would_rollback": True}
            business.flush()
            identity = reserve(ledger)
            ledger.dispatch(identity)
            business.rollback()
        with factory() as session:
            assert session.get(PersonalPaidRequest, identity).status == "dispatching"
            assert session.get(PersonalRun, ledger.run_id).coverage == {}


@pytest.mark.parametrize("mutation", ["retry", "finish"])
def test_dispatch_handoff_fences_concurrent_ownership_changes(mutation):
    with migrated_disposable_engine() as engine:
        factory, (ledger, _) = seed(engine)
        with factory() as session, session.begin():
            session.get(PersonalWriterMode, True).mode = "personal"
        identity = reserve(ledger)
        observed_owner = threading.Event()
        allow_dispatch = threading.Event()
        retry_started = threading.Event()
        retry_rotated = threading.Event()
        original_context = ledger._context

        def paused_context(session):
            result = original_context(session)
            observed_owner.set()
            assert allow_dispatch.wait(timeout=10)
            return result

        ledger._context = paused_context

        def rotate_token():
            with factory() as session, session.begin():
                retry_started.set()
                if mutation == "retry":
                    retry_run(
                        session,
                        session.get(PersonalWorkspace, ledger.workspace_id),
                        ledger.run_id,
                        now=NOW + dt.timedelta(minutes=35, seconds=1),
                    )
                else:
                    finish_run(
                        session, ledger.run_id, ledger.ownership_token, state="succeeded", now=NOW
                    )
                retry_rotated.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            dispatch_future = executor.submit(ledger.dispatch, identity)
            assert observed_owner.wait(timeout=10)
            retry_future = executor.submit(rotate_token)
            assert retry_started.wait(timeout=10)
            try:
                assert not retry_rotated.wait(timeout=0.2)
            finally:
                allow_dispatch.set()
            dispatch_future.result(timeout=10)
            retry_future.result(timeout=10)
        with factory() as session:
            row = session.get(PersonalPaidRequest, identity)
            assert row.status == "dispatching" and row.attempt == 1
            run = session.get(PersonalRun, ledger.run_id)
            assert (run.attempt, run.state) == (
                (2, "queued") if mutation == "retry" else (1, "succeeded")
            )
        ledger._context = original_context
        with pytest.raises(PaidWorkBlocked, match="stale run attempt"):
            reserve(ledger)


def test_legacy_costs_import_once_and_unknown_blocks_paid_enablement():
    with migrated_disposable_engine() as engine:
        factory, (ledger, _) = seed(engine)
        with factory() as session, session.begin():
            state = session.get(PersonalSpendingState, ledger.workspace_id)
            if state:
                session.delete(state)  # Test populated cutover, before any paid request.
            for cost in (Decimal("0.01"), None):
                session.add(
                    LLMRun(
                        prompt_name="synthetic-legacy",
                        prompt_version="v1",
                        provider="fixture",
                        model="fixture",
                        cost_usd=cost,
                        created_at=NOW - dt.timedelta(days=1),
                        started_at=NOW - dt.timedelta(days=1),
                    )
                )
        with factory() as session, session.begin():
            import_legacy_usage(session, ledger.workspace_id, now=NOW)
            import_legacy_usage(session, ledger.workspace_id, now=NOW)
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(PersonalLegacyUsage)) == 2
            summary = spending_summary(session, ledger.workspace_id, now=NOW)
            assert summary["unreconciled_legacy_count"] == 1
            assert Decimal(summary["finalized_usd"]) == Decimal("0.01")
            with pytest.raises(PaidWorkBlocked, match="Prior current-month"):
                assert_legacy_usage_reconciled(session, ledger.workspace_id, now=NOW)
        with pytest.raises(PaidWorkBlocked, match="legacy usage unreconciled"):
            reserve(ledger)
        with factory() as session, session.begin():
            unknown = session.scalar(
                select(PersonalLegacyUsage).where(PersonalLegacyUsage.actual_usd.is_(None))
            )
            reconcile_legacy_usage(
                session,
                ledger.workspace_id,
                unknown.llm_run_id,
                actual_usd="0.001",
                receipt="synthetic-receipt",
            )
        with factory() as session:
            assert_legacy_usage_reconciled(session, ledger.workspace_id, now=NOW)


def test_read_summary_discloses_post_upgrade_legacy_without_mutation_or_guarded_double_count():
    with migrated_disposable_engine() as engine:
        factory, (ledger, _) = seed(engine)
        with factory() as session, session.begin():
            for cost, parameters in (
                (Decimal("0.005"), None),
                (None, None),
                (Decimal("0.9"), {"personal_paid_ledger_accounted": True}),
            ):
                session.add(
                    LLMRun(
                        prompt_name="synthetic",
                        prompt_version="v1",
                        provider="fixture",
                        model="fixture",
                        cost_usd=cost,
                        model_params=parameters,
                        created_at=NOW - dt.timedelta(hours=1),
                    )
                )
        with factory() as session:
            before = session.scalar(select(func.count()).select_from(PersonalLegacyUsage))
            summary = spending_summary(session, ledger.workspace_id, now=NOW)
            assert summary["unreconciled_legacy_count"] == 1
            assert Decimal(summary["finalized_usd"]) == Decimal("0.005")
            assert not session.new and not session.dirty
            assert session.scalar(select(func.count()).select_from(PersonalLegacyUsage)) == before
        with factory() as session, session.begin():
            import_legacy_usage(session, ledger.workspace_id, now=NOW)
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(PersonalLegacyUsage)) == 2
            assert spending_summary(session, ledger.workspace_id, now=NOW) == summary


def test_provider_embedding_retry_has_separate_durable_reservations():
    from packages.providers.openai_embeddings import (
        OPENAI_EMBEDDING_DIMENSIONS,
        OpenAIEmbeddingProvider,
    )

    with migrated_disposable_engine() as engine:
        factory, (ledger, _) = seed(engine, monthly="1")
        physical = []

        def scripted(request):
            physical.append(request)
            if len(physical) == 1:
                return httpx.Response(500, json={"error": "scripted transient failure"})
            return httpx.Response(
                200,
                json={
                    "model": "synthetic-only",
                    "usage": {"prompt_tokens": 20},
                    "data": [{"index": 0, "embedding": [0.0] * OPENAI_EMBEDDING_DIMENSIONS}],
                },
            )

        transport = httpx.Client(
            base_url="https://synthetic.invalid", transport=httpx.MockTransport(scripted)
        )
        route = PaidRoute.from_mapping(route_mapping("embedding"), role="embedding")
        guard = DurablePaidHTTPClient(route=route, ledger=ledger, delegate=transport)
        provider = OpenAIEmbeddingProvider(
            api_key="synthetic-no-network",
            model_name=route.model,
            model_version=route.model_version,
            client=guard,
            max_attempts=2,
            sleep=lambda _: None,
        )
        try:
            assert len(provider.embed(["synthetic text"])) == 1
        finally:
            provider.close()
        with factory() as session:
            rows = session.scalars(
                select(PersonalPaidRequest).order_by(PersonalPaidRequest.reserved_at)
            ).all()
            assert len(rows) == len(physical) == 2
            assert {row.status for row in rows} == {"uncertain", "reconciled"}
            assert len({row.id for row in rows}) == 2
            assert all(
                row.dispatch_attempt_at is not None and row.role == "embedding" for row in rows
            )


def test_scripted_http_timeout_retains_each_physical_request_and_no_expiry_credit():
    with migrated_disposable_engine() as engine:
        factory, (ledger, _) = seed(engine, monthly="1", run_limit="0.03")

        def timeout(*_args, **_kwargs):
            raise httpx.ReadTimeout("scripted")

        client = DurablePaidHTTPClient(
            route=PaidRoute.from_mapping(route_mapping(), role="generation"),
            ledger=ledger,
            delegate=SimpleNamespace(post=timeout),
        )
        with pytest.raises(httpx.ReadTimeout):
            client.post(
                "/v1/chat/completions", json={"model": "synthetic-only", "max_tokens": 1000}
            )
        ledger.clock = lambda: dt.datetime(2027, 1, 1, tzinfo=dt.UTC)
        with factory() as session:
            summary = spending_summary(session, ledger.workspace_id, now=ledger.clock())
            assert Decimal(summary["unresolved_usd"]) > 0
            row = session.scalar(select(PersonalPaidRequest))
            assert row.status == "uncertain" and row.actual_usd is None
        # Explicit recovery/retry obtains fresh authority while retaining the same
        # logical run's frozen ceiling and every uncertain earlier-month request.
        with factory() as session:
            recover_expired_owner(session, ledger.run_id, now=ledger.clock())
            session.commit()
            decision = retry_run(
                session,
                session.get(PersonalWorkspace, ledger.workspace_id),
                ledger.run_id,
                now=ledger.clock(),
            )
            owned = acquire_run(
                session, ledger.run_id, decision.run.ownership_token, now=ledger.clock()
            )
            ledger.ownership_token = owned.ownership_token
            assert owned.attempt == 2
            session.commit()
        with pytest.raises(PaidWorkBlocked, match="allowance reached"):
            reserve(ledger)
