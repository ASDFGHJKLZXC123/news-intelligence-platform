"""Independent P4 fault proof through actual PostgreSQL business adapters.

RSS/model responses are synthetic. Takeover is injected *inside* provider calls,
then the production capture/embedding/report paths must refuse the late business
response. Database-loss tests inject connection failures; they do not claim an
actual server outage. Every database comes from the owned migrated fixture.
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from db.models import (
    EMBEDDING_DIM,
    Article,
    ArticleEmbedding,
    Event,
    Job,
    LLMRun,
    PersonalBriefSnapshot,
    PersonalCapture,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    PersonalWriterMode,
    Report,
    ReportSection,
    Source,
)
from db.models.personal_spending import PersonalPaidRequest
from packages.providers.fakes import FakeEmbeddingProvider
from services.personal.briefs import PersonalBriefGenerationError, generate_personal_brief
from services.personal.coordinator import (
    PersonalCoordinatorError,
    _capture_selected_feeds,
    _mark_stage_failure,
    run_personal_daily,
)
from services.personal.deadlines import RuntimeCancelled
from services.personal.paid_runtime import DurablePaidHTTPClient
from services.personal.processing import (
    ProcessingBusy,
    fail_owned_launch,
    record_child_exit,
    record_launcher,
    switch_processing_mode,
)
from services.personal.runs import (
    PersonalOwnershipLost,
    PersonalRunConflict,
    acquire_run,
    create_daily_run,
    finish_run,
    lock_owned_run,
    retry_run,
    retry_status,
)
from services.personal.spending import PaidRoute, PaidWorkBlocked, SpendingLedger
from services.personal.workspace import configure_profile, ensure_workspace
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_brief_generation import (
    _offline_provider_response,
    _orchestrator,
    _prepared_snapshot,
    _provider_orchestrator,
)
from tests.integration.test_personal_coordinator import _captured_item, _create_run
from tests.unit.test_personal_spending import model_route, route_mapping

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def selected_personal_deployment(monkeypatch):
    """Select the supported deployment while retaining the real ownership clock."""
    from packages.config.settings import get_settings

    monkeypatch.setenv("PERSONAL_PROCESSING_MODE", "personal")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _sessions(engine):
    return sessionmaker(engine, autoflush=False, expire_on_commit=False)


def _takeover(engine, run_id, *, terminal=True):
    """Accelerate expiry, then use ordinary explicit retry/acquire/finish APIs.

    A one-second lock timeout makes a forbidden provider-held transaction fail
    visibly instead of deadlocking this independent fault oracle.
    """
    now = dt.datetime.now(dt.UTC)
    factory = _sessions(engine)
    with factory() as session:
        session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        control = session.get(PersonalWriterMode, True, with_for_update=True)
        old = session.get(PersonalRun, run_id, with_for_update=True)
        old.lease_expires_at = control.lease_expires_at = now - dt.timedelta(seconds=1)
        session.flush()
        replacement = retry_run(
            session, session.get(PersonalWorkspace, old.workspace_id), run_id, now=now
        )
        token, generation = replacement.run.ownership_token, replacement.run.fencing_generation
        session.commit()
    with factory() as session:
        acquire_run(session, run_id, token, now=now, generation=generation)
        session.commit()
    if terminal:
        with factory() as session:
            finish_run(
                session,
                run_id,
                token,
                state="succeeded",
                result={"replacement_fault_oracle": True, "attempt": 2},
                now=now,
            )
            session.commit()
    return token, generation


def _assert_newer_terminal(session, run_id):
    run = session.get(PersonalRun, run_id, populate_existing=True)
    assert run.state == "succeeded" and run.attempt == 2
    assert run.result == {"replacement_fault_oracle": True, "attempt": 2}
    assert session.get(Job, run.job_id).state == "succeeded"
    return run


def test_late_rss_response_cannot_insert_capture_or_fail_replacement():
    with migrated_disposable_engine() as engine:
        now = dt.datetime.now(dt.UTC)
        run_id, token, _, _ = _create_run(engine, now=now)
        factory = _sessions(engine)
        with factory() as session:
            acquire_run(session, run_id, token, now=now)
            session.commit()
        invoked = []

        class LateRSS:
            def fetch(self, _url):
                invoked.append("feed")
                _takeover(engine, run_id)
                return [_captured_item(published_at=now)]

        with pytest.raises(PersonalOwnershipLost):
            _capture_selected_feeds(
                run_id,
                token,
                session_factory=factory,
                provider_factory=lambda _source: LateRSS(),
                clock=lambda: now,
            )
        assert invoked == ["feed"]
        _mark_stage_failure(run_id, token, session_factory=factory, code="late_old_failure")
        with factory() as session:
            _assert_newer_terminal(session, run_id)
            for model in (PersonalCapture, Article, Event, Report):
                assert session.scalar(select(func.count()).select_from(model)) == 0


def test_late_embedding_response_cannot_insert_vectors_events_or_fail_replacement():
    with migrated_disposable_engine() as engine:
        now = dt.datetime.now(dt.UTC)
        run_id, token, _, _ = _create_run(engine, now=now)
        factory = _sessions(engine)
        invoked = []
        delegate = FakeEmbeddingProvider(dimension=EMBEDDING_DIM, model="p4-late-fixture")

        class LateEmbedding:
            model_name, model_version = delegate.model_name, delegate.model_version

            def embed(self, texts):
                invoked.append(tuple(texts))
                _takeover(engine, run_id)
                return delegate.embed(texts)

        class RSS:
            def fetch(self, _url):
                return [_captured_item(published_at=now)]

        with pytest.raises((PersonalOwnershipLost, PersonalCoordinatorError)):
            run_personal_daily(
                run_id,
                token,
                session_factory=factory,
                rss_provider_factory=lambda _source: RSS(),
                embedding_provider=LateEmbedding(),
                orchestrator_factory=lambda *_args: pytest.fail("stale embedding began report"),
                now=now,
                clock=lambda: now,
            )
        assert len(invoked) == 1 and len(invoked[0]) == 1
        with factory() as session:
            run = _assert_newer_terminal(session, run_id)
            assert run.scopes_frozen_at is not None
            assert len(run.admitted_article_ids) == len(run.enrichment_article_ids) == 1
            assert session.scalar(select(func.count()).select_from(Article)) == 1
            for model in (ArticleEmbedding, Event, Report):
                assert session.scalar(select(func.count()).select_from(model)) == 0


def test_late_composition_response_cannot_publish_or_fail_newer_attempt():
    with migrated_disposable_engine() as engine:
        now = dt.datetime.now(dt.UTC)
        coverage = {
            "feeds_configured": 1,
            "feeds_attempted": 1,
            "feeds_succeeded": 1,
            "feeds_failed": 0,
            "articles_captured": 1,
        }
        run_id, token, _, snapshot_id = _prepared_snapshot(engine, now=now, coverage=coverage)
        factory = _sessions(engine)
        with factory() as session:
            before = session.get(PersonalBriefSnapshot, snapshot_id).input_hash
        invoked = []

        def late_response(request):
            if not invoked:
                invoked.append(request.requested_schema)
                _takeover(engine, run_id)
            return _offline_provider_response(request)

        with pytest.raises((PersonalOwnershipLost, PersonalBriefGenerationError)):
            generate_personal_brief(
                run_id,
                token,
                session_factory=factory,
                orchestrator_factory=lambda session, route: _provider_orchestrator(
                    session, route, late_response
                ),
            )
        assert invoked
        _mark_stage_failure(run_id, token, session_factory=factory, code="late_old_failure")
        with factory() as session:
            run = _assert_newer_terminal(session, run_id)
            assert run.snapshot_id == snapshot_id
            assert session.get(PersonalBriefSnapshot, snapshot_id).input_hash == before
            assert session.scalar(select(func.count()).select_from(ReportSection)) == 0
            assert session.scalar(select(func.count()).select_from(LLMRun)) == 0
            assert all(row.status != "published" for row in session.scalars(select(Report)))


@pytest.mark.parametrize("expire_on_commit", [True, False])
def test_successful_composition_supports_celery_and_child_session_expiry(expire_on_commit):
    """Default Celery sessions expire; explicit child sessions retain scalar telemetry."""
    with migrated_disposable_engine() as engine:
        now = dt.datetime.now(dt.UTC)
        run_id, token, _, snapshot_id = _prepared_snapshot(
            engine,
            now=now,
            coverage={"feeds_attempted": 1, "feeds_succeeded": 1, "feeds_failed": 0},
        )
        factory = sessionmaker(engine, autoflush=False, expire_on_commit=expire_on_commit)
        with factory() as session:
            before = session.get(PersonalBriefSnapshot, snapshot_id).input_hash
        result = generate_personal_brief(
            run_id, token, session_factory=factory, orchestrator_factory=_orchestrator
        )
        assert result.published is True and result.gate_outcome.value == "pass"
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            report = session.get(Report, run.report_id)
            assert run.state == "succeeded" and report.status == "published"
            assert run.result["published"] is True
            assert run.snapshot_id == snapshot_id
            assert session.get(PersonalBriefSnapshot, snapshot_id).input_hash == before
            assert session.scalar(select(func.count()).select_from(LLMRun)) > 0
            assert session.scalar(select(func.count()).select_from(ReportSection)) > 0


def _paid_owner(engine, *, queued=False):
    factory = _sessions(engine)
    now = dt.datetime.now(dt.UTC)
    with factory() as session:
        source = Source(name="Synthetic paid ledger fixture", feed_url="https://fixture.invalid/p4")
        session.add(source)
        workspace, _ = ensure_workspace(session)
        session.flush()
        revision = (
            int(
                session.scalar(
                    select(func.coalesce(func.max(PersonalProfileRevision.revision), 0)).where(
                        PersonalProfileRevision.workspace_id == workspace.id
                    )
                )
            )
            + 1
        )
        profile = PersonalProfileRevision(
            workspace_id=workspace.id,
            revision=revision,
            execution_profile="assisted",
            schema_revision="personal-profile.v2",
            selected_source_ids=[source.id],
            include_phrases=[],
            exclude_phrases=[],
            settings={
                "ai_enabled": True,
                "monthly_allowance_usd": "1",
                "run_allowance_usd": "1",
                "model_route": model_route(),
            },
        )
        session.add(profile)
        session.flush()
        workspace.active_profile_revision_id = profile.id
        switch_processing_mode(session, "personal", now=now)
        session.commit()
        decision = create_daily_run(session, workspace, now=now)
        run_id, token, workspace_id = decision.run.id, decision.run.ownership_token, workspace.id
        session.commit()
        if not queued:
            run = acquire_run(session, run_id, token, now=now)
            run.scopes_frozen_at = now
            session.commit()
    ledger = SpendingLedger(
        session_factory=factory,
        workspace_id=workspace_id,
        run_id=run_id,
        ownership_token=token,
    )
    return factory, ledger


def _reserve(ledger):
    return ledger.reserve(
        PaidRoute.from_mapping(route_mapping(), role="generation"),
        input_token_bound=1000,
        output_token_bound=1000,
    )


def test_queued_delivery_token_cannot_grant_paid_dispatch_permission():
    with migrated_disposable_engine() as engine:
        factory, ledger = _paid_owner(engine, queued=True)
        with pytest.raises(PaidWorkBlocked, match="stale run attempt"):
            _reserve(ledger)
        with factory() as session:
            assert session.get(PersonalRun, ledger.run_id).state == "queued"
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0


def test_late_paid_usage_reconciles_accounting_only_after_takeover():
    with migrated_disposable_engine() as engine:
        factory, ledger = _paid_owner(engine)
        request_id = _reserve(ledger)
        ledger.dispatch(request_id)
        _takeover(engine, ledger.run_id)
        ledger.reconcile(
            request_id,
            input_tokens=100,
            output_tokens=100,
            provider_request_id="synthetic-late-receipt",
        )
        ledger.reconcile(
            request_id,
            input_tokens=100,
            output_tokens=100,
            provider_request_id="synthetic-late-receipt",
        )
        with pytest.raises(PaidWorkBlocked, match="stale run attempt"):
            _reserve(ledger)
        with pytest.raises(PaidWorkBlocked, match="stale run attempt"):
            ledger.dispatch(request_id)
        with factory() as session:
            _assert_newer_terminal(session, ledger.run_id)
            row = session.get(PersonalPaidRequest, request_id)
            assert (
                row.status == "reconciled" and row.provider_request_id == "synthetic-late-receipt"
            )
            assert row.attempt == 1 and str(row.actual_usd) == "0.002000000000"
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 1
            for model in (Article, Event, Report):
                assert session.scalar(select(func.count()).select_from(model)) == 0


def test_connection_failure_stops_dispatch_and_reconnect_cannot_restore_old_owner():
    with migrated_disposable_engine() as engine:
        factory, ledger = _paid_owner(engine)
        request_id = _reserve(ledger)
        sent = []

        def unavailable():
            raise OperationalError(
                "synthetic database unavailable", {}, RuntimeError("disconnected")
            )

        ledger.session_factory = unavailable
        client = DurablePaidHTTPClient(
            route=PaidRoute.from_mapping(route_mapping(), role="generation"),
            ledger=ledger,
            delegate=SimpleNamespace(post=lambda *_args, **_kwargs: sent.append("network")),
        )
        with pytest.raises(OperationalError):
            client.post(
                "/v1/chat/completions", json={"model": "synthetic-only", "max_tokens": 1000}
            )
        assert sent == []
        ledger.session_factory = factory
        _takeover(engine, ledger.run_id)
        with pytest.raises(PaidWorkBlocked):
            ledger.dispatch(request_id)
        with pytest.raises(PersonalOwnershipLost):
            with factory() as session:
                lock_owned_run(session, ledger.run_id, ledger.ownership_token)
        with factory() as session:
            run = _assert_newer_terminal(session, ledger.run_id)
            assert run.scopes_frozen_at is not None
            row = session.get(PersonalPaidRequest, request_id)
            assert row.status == "reserved" and row.dispatch_attempt_at is None
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 1


def test_stale_launcher_terminal_write_cannot_replace_newer_success():
    with migrated_disposable_engine() as engine:
        now = dt.datetime.now(dt.UTC)
        run_id, token, _, _ = _create_run(engine, now=now)
        factory = _sessions(engine)
        with factory() as session:
            old_generation = session.get(PersonalRun, run_id).fencing_generation
            acquire_run(session, run_id, token, now=now)
            session.commit()
        _takeover(engine, run_id)
        with factory() as session:
            assert (
                fail_owned_launch(
                    session,
                    run_id,
                    token,
                    old_generation,
                    code="stale_supervisor_exit",
                    allow_running=True,
                )
                is False
            )
            session.commit()
            _assert_newer_terminal(session, run_id)


def test_api_retry_reason_requires_confirmed_child_exit_for_failed_run():
    """The displayed action must use the same retained lifetime as retry_run."""
    from apps.api.personal import _run_status

    with migrated_disposable_engine() as engine:
        now = dt.datetime.now(dt.UTC)
        run_id, token, _, _ = _create_run(engine, now=now)
        factory = _sessions(engine)
        launcher = uuid.uuid4()
        with factory() as session:
            generation = session.get(PersonalRun, run_id).fencing_generation
            record_launcher(
                session,
                run_id,
                token,
                generation,
                launcher,
                child_pid=123456789,
                child_identity=str(uuid.uuid4()),
                child_started_at="synthetic-unconfirmed-birth",
            )
            acquire_run(session, run_id, token, now=now)
            finish_run(session, run_id, token, state="failed", now=now)
            session.commit()
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            status = _run_status(session, run)
            assert status["retry_eligible"] is False
            assert status["retry_reason"] == (
                "awaiting confirmed child exit or explicit reconciliation"
            )
            assert record_child_exit(session, run_id, token, generation, launcher)
            session.commit()
        with factory() as session:
            status = _run_status(session, session.get(PersonalRun, run_id))
            assert status["retry_eligible"] is True
            assert status["retry_reason"] == "explicit retry available"
            assert session.scalar(select(func.count()).select_from(PersonalRun)) == 1
            assert session.get(PersonalRun, run_id).attempt == 1


def test_total_three_attempts_survives_dispatch_failures_and_expired_recovery():
    with migrated_disposable_engine() as engine:
        now = dt.datetime.now(dt.UTC)
        run_id, token, _, workspace_id = _create_run(engine, now=now)
        factory = _sessions(engine)
        generations = []
        for attempt in (1, 2, 3):
            with factory() as session:
                run = session.get(PersonalRun, run_id)
                assert run.attempt == attempt and run.state == "queued"
                generations.append(run.fencing_generation)
                assert (
                    fail_owned_launch(
                        session,
                        run_id,
                        run.ownership_token,
                        run.fencing_generation,
                        code="synthetic_launch_failure",
                    )
                    is True
                )
                session.commit()
            if attempt < 3:
                with factory() as session:
                    decision = retry_run(
                        session, session.get(PersonalWorkspace, workspace_id), run_id, now=now
                    )
                    assert decision.should_enqueue and decision.run.profile_revision_id
                    session.commit()
        assert generations == sorted(set(generations))
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            assert retry_status(run, now=now + dt.timedelta(days=1))[0] is False
            with pytest.raises(PersonalRunConflict, match="three attempts"):
                retry_run(
                    session,
                    session.get(PersonalWorkspace, workspace_id),
                    run_id,
                    now=now + dt.timedelta(days=1),
                )
            session.rollback()
            assert session.scalar(select(func.count()).select_from(PersonalRun)) == 1
            assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_terminal_run_with_unconfirmed_child_blocks_mode_switch_until_owned_exit():
    with migrated_disposable_engine() as engine:
        now = dt.datetime.now(dt.UTC)
        run_id, token, _, _ = _create_run(engine, now=now)
        factory = _sessions(engine)
        launcher = uuid.uuid4()
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            generation = run.fencing_generation
            record_launcher(
                session,
                run_id,
                token,
                generation,
                launcher,
                child_pid=987654,
                child_identity=str(uuid.uuid4()),
            )
            acquire_run(session, run_id, token, now=now)
            finish_run(session, run_id, token, state="succeeded", now=now)
            session.commit()
        with factory() as session:
            assert session.get(PersonalWriterMode, True).unconfirmed_child is True
            with pytest.raises(ProcessingBusy):
                switch_processing_mode(session, "legacy", now=now)
            session.rollback()
            assert record_child_exit(session, run_id, token, generation, uuid.uuid4()) is False
            session.commit()
            assert session.get(PersonalWriterMode, True).unconfirmed_child is True
            assert record_child_exit(session, run_id, token, generation, launcher) is True
            switch_processing_mode(session, "legacy", now=now)
            session.commit()
            assert session.get(PersonalWriterMode, True).mode == "legacy"
            assert session.get(PersonalRun, run_id).state == "succeeded"


def test_fatal_cancellation_during_feed_never_starts_the_next_feed():
    with migrated_disposable_engine() as engine:
        factory = _sessions(engine)
        now = dt.datetime.now(dt.UTC)
        with factory() as session:
            sources = [
                Source(
                    name=f"Cancellation fixture {index}",
                    feed_url=f"https://fixture.invalid/cancel-{index}",
                )
                for index in (1, 2)
            ]
            session.add_all(sources)
            session.flush()
            workspace, _ = ensure_workspace(session)
            configure_profile(
                session,
                workspace,
                selected_source_ids=[row.id for row in sources],
                execution_profile="raw",
            )
            session.commit()
            decision = create_daily_run(session, workspace, now=now)
            run_id, token = decision.run.id, decision.run.ownership_token
            session.commit()
        invoked = []

        class CancelledRSS:
            def fetch(self, url):
                invoked.append(url)
                raise RuntimeCancelled("injected graceful feed deadline")

        with pytest.raises(RuntimeCancelled, match="feed deadline"):
            run_personal_daily(
                run_id,
                token,
                session_factory=factory,
                rss_provider_factory=lambda _source: CancelledRSS(),
                embedding_provider=SimpleNamespace(
                    embed=lambda _: pytest.fail("cancelled enrichment")
                ),
                orchestrator_factory=lambda *_: pytest.fail("cancelled report"),
                now=now,
                clock=lambda: now,
            )
        assert len(invoked) == 1
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "failed" and run.attempt == 1
            assert run.stage_results["workflow"]["status"] == "failed"
            assert session.scalar(select(func.count()).select_from(PersonalCapture)) == 0
            assert session.get(PersonalWriterMode, True).active_run_id is None


def test_fatal_cancellation_during_composition_preserves_identity_and_stops_calls():
    with migrated_disposable_engine() as engine:
        now = dt.datetime.now(dt.UTC)
        run_id, token, _, snapshot_id = _prepared_snapshot(
            engine,
            now=now,
            coverage={"feeds_attempted": 1, "feeds_succeeded": 1, "feeds_failed": 0},
        )
        factory = _sessions(engine)
        with factory() as session:
            before = session.get(PersonalBriefSnapshot, snapshot_id).input_hash
        invoked = []

        def cancelled(request):
            invoked.append(request.requested_schema)
            raise RuntimeCancelled("injected graceful model deadline")

        with pytest.raises(RuntimeCancelled, match="model deadline"):
            generate_personal_brief(
                run_id,
                token,
                session_factory=factory,
                orchestrator_factory=lambda session, route: _provider_orchestrator(
                    session, route, cancelled
                ),
            )
        assert len(invoked) == 1
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "failed" and run.snapshot_id == snapshot_id
            assert session.get(PersonalBriefSnapshot, snapshot_id).input_hash == before
            assert session.scalar(select(func.count()).select_from(ReportSection)) == 0
            assert all(row.status == "failed" for row in session.scalars(select(Report)))
