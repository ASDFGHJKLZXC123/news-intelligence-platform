"""Phase 4A PostgreSQL proof for database-wide ownership and generation fencing."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Event, PersonalRun, PersonalWriterMode, Report, Source
from services.personal.processing import (
    ProcessingBusy,
    assert_mode_agreement,
    switch_processing_mode,
)
from services.personal.runs import (
    PersonalOwnershipLost,
    WriterModeConflict,
    acquire_run,
    create_daily_run,
    finish_run,
    lock_owned_run,
    mark_delivery_failed,
    retry_run,
)
from services.personal.workspace import configure_profile, ensure_workspace
from tests.integration._stage7_db import migrated_disposable_engine

pytestmark = pytest.mark.integration


def _setup(session: Session):
    source = Source(name="Phase 4A fixture", feed_url=f"https://fixture.invalid/{uuid.uuid4()}")
    session.add(source)
    session.flush()
    workspace, _ = ensure_workspace(session)
    configure_profile(session, workspace, selected_source_ids=[source.id], execution_profile="raw")
    session.commit()
    return workspace


def test_upgrade_retains_legacy_mode_and_configuration_agreement() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        control = session.get(PersonalWriterMode, True)
        assert control.mode == "legacy"
        assert control.generation == 0
        assert control.active_run_id is None
        assert_mode_agreement(session, "legacy")
        with pytest.raises(WriterModeConflict, match="does not agree"):
            assert_mode_agreement(session, "personal")


def test_global_claim_blocks_a_different_date_and_same_date_is_idempotent() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace = _setup(session)
        now = dt.datetime.now(dt.UTC)
        first = create_daily_run(session, workspace, now=now)
        session.commit()
        repeated = create_daily_run(session, workspace, now=now)
        assert repeated.run.id == first.run.id and not repeated.should_enqueue
        session.commit()
        with pytest.raises(ProcessingBusy, match=str(first.run.id)):
            create_daily_run(session, workspace, now=now + dt.timedelta(days=1))
        session.rollback()
        assert len(session.scalars(select(PersonalRun)).all()) == 1


def test_expired_handoff_recovery_fences_delayed_old_delivery() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace = _setup(session)
        now = dt.datetime.now(dt.UTC)
        queued = create_daily_run(session, workspace, now=now)
        run_id, old_token, old_generation = (
            queued.run.id,
            queued.run.ownership_token,
            queued.run.fencing_generation,
        )
        session.commit()
        later = now + dt.timedelta(minutes=2, seconds=1)
        with pytest.raises(PersonalOwnershipLost, match="expired"):
            acquire_run(session, run_id, old_token, now=later, generation=old_generation)
        session.rollback()
        replacement = retry_run(session, workspace, run_id, now=later)
        token, generation = replacement.run.ownership_token, replacement.run.fencing_generation
        assert generation > old_generation
        session.commit()
        with pytest.raises(PersonalOwnershipLost):
            acquire_run(session, run_id, old_token, now=later, generation=old_generation)
        session.rollback()
        running = acquire_run(session, run_id, token, now=later, generation=generation)
        assert running.attempt == 2
        session.commit()


def test_fixed_running_deadlines_and_no_lease_renewal() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace = _setup(session)
        now = dt.datetime.now(dt.UTC)
        queued = create_daily_run(session, workspace, now=now)
        token = queued.run.ownership_token
        session.commit()
        run = acquire_run(session, queued.run.id, token, now=now)
        assert run.graceful_deadline_at == now + dt.timedelta(minutes=25)
        assert run.hard_deadline_at == now + dt.timedelta(minutes=30)
        assert run.lease_expires_at == now + dt.timedelta(minutes=35)
        session.commit()
        owned = lock_owned_run(session, run.id, token, now=now + dt.timedelta(minutes=20))
        assert owned.lease_expires_at == now + dt.timedelta(minutes=35)
        session.commit()
        with pytest.raises(PersonalOwnershipLost, match="expired"):
            lock_owned_run(session, run.id, token, now=now + dt.timedelta(minutes=35))


def test_stale_attempt_cannot_write_event_report_or_newer_terminal_state() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace = _setup(session)
        now = dt.datetime.now(dt.UTC)
        queued = create_daily_run(session, workspace, now=now)
        run_id, old_token = queued.run.id, queued.run.ownership_token
        session.commit()
        acquire_run(session, run_id, old_token, now=now)
        session.commit()
        later = now + dt.timedelta(minutes=35, seconds=1)
        retry = retry_run(session, workspace, run_id, now=later)
        token = retry.run.ownership_token
        session.commit()
        acquire_run(session, run_id, token, now=later)
        finish_run(session, run_id, token, state="succeeded", now=later)
        session.commit()
        for model in (Event, Report):
            with pytest.raises(PersonalOwnershipLost):
                lock_owned_run(session, run_id, old_token, now=later)
                session.add(model(title="stale attempt must never persist"))
                session.commit()
            session.rollback()
        with pytest.raises(PersonalOwnershipLost):
            finish_run(session, run_id, old_token, state="failed", now=later)
        session.rollback()
        assert session.get(PersonalRun, run_id).state == "succeeded"
        assert session.get(PersonalWriterMode, True).active_run_id is None


def test_mode_switch_is_idle_only_durable_and_invalidates_generations() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace = _setup(session)
        now = dt.datetime.now(dt.UTC)
        queued = create_daily_run(session, workspace, now=now)
        run_id, token = queued.run.id, queued.run.ownership_token
        session.commit()
        with pytest.raises(ProcessingBusy):
            switch_processing_mode(session, "legacy", now=now)
        session.rollback()
        acquire_run(session, run_id, token, now=now)
        finish_run(session, run_id, token, state="failed", now=now)
        session.commit()
        old_generation = session.get(PersonalWriterMode, True).generation
        switch_processing_mode(session, "legacy", now=now)
        session.commit()
        control = session.get(PersonalWriterMode, True, populate_existing=True)
        assert control.mode == "legacy" and control.generation > old_generation
        assert session.get(PersonalRun, run_id).state == "failed"


def test_explicit_expiry_recovery_never_launches_or_resets_attempts() -> None:
    from services.personal.processing import recover_expired_owner

    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace = _setup(session)
        now = dt.datetime.now(dt.UTC)
        queued = create_daily_run(session, workspace, now=now)
        run_id = queued.run.id
        generation = queued.run.fencing_generation
        session.commit()
        with pytest.raises(ProcessingBusy):
            recover_expired_owner(session, run_id, now=now + dt.timedelta(minutes=1))
        session.rollback()
        recovered = recover_expired_owner(
            session, run_id, now=now + dt.timedelta(minutes=2, seconds=1), generation=generation
        )
        session.commit()
        assert recovered.state == "failed" and recovered.attempt == 1
        assert recovered.error["code"] == "ownership_lease_expired"
        assert session.get(PersonalWriterMode, True).active_run_id is None
        assert len(session.scalars(select(PersonalRun)).all()) == 1


def test_concurrent_different_dates_have_one_global_owner() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from db.models import PersonalWorkspace

    with migrated_disposable_engine() as engine:
        with Session(engine) as session:
            workspace = _setup(session)
            workspace_id = workspace.id
        now = dt.datetime.now(dt.UTC)

        def start(offset):
            with Session(engine) as session:
                workspace = session.get(PersonalWorkspace, workspace_id)
                try:
                    decision = create_daily_run(
                        session, workspace, now=now + dt.timedelta(days=offset)
                    )
                    session.commit()
                    return decision.run.id, decision.should_enqueue
                except ProcessingBusy as exc:
                    session.rollback()
                    return exc.active_run_id, False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, (0, 1)))
        assert len({run_id for run_id, _ in results}) == 1
        assert sum(launch for _, launch in results) == 1
        with Session(engine) as session:
            assert len(session.scalars(select(PersonalRun)).all()) == 1


def test_takeover_retains_old_child_until_exact_old_exit_is_confirmed() -> None:
    from services.personal.processing import record_child_exit, record_launcher

    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace = _setup(session)
        now = dt.datetime.now(dt.UTC)
        queued = create_daily_run(session, workspace, now=now)
        run_id, old_token, old_generation = (
            queued.run.id,
            queued.run.ownership_token,
            queued.run.fencing_generation,
        )
        old_launcher, old_child = uuid.uuid4(), uuid.uuid4()
        record_launcher(
            session,
            run_id,
            old_token,
            old_generation,
            old_launcher,
            child_pid=123456789,
            child_identity=str(old_child),
            child_started_at=1.0,
        )
        session.commit()
        acquire_run(session, run_id, old_token, now=now)
        session.commit()
        later = now + dt.timedelta(minutes=35, seconds=1)
        replacement = retry_run(session, workspace, run_id, now=later)
        token, generation = replacement.run.ownership_token, replacement.run.fencing_generation
        new_launcher, new_child = uuid.uuid4(), uuid.uuid4()
        record_launcher(
            session,
            run_id,
            token,
            generation,
            new_launcher,
            child_pid=123456790,
            child_identity=str(new_child),
            child_started_at=2.0,
        )
        session.commit()
        control = session.get(PersonalWriterMode, True, populate_existing=True)
        assert len(control.child_history) == 2 and control.unconfirmed_child
        acquire_run(session, run_id, token, now=later)
        finish_run(session, run_id, token, state="succeeded", now=later)
        assert record_child_exit(session, run_id, token, generation, new_launcher)
        session.commit()
        with pytest.raises(ProcessingBusy):
            switch_processing_mode(session, "legacy", now=later)
        session.rollback()
        assert record_child_exit(session, run_id, old_token, old_generation, old_launcher)
        session.commit()
        assert session.get(PersonalRun, run_id, populate_existing=True).state == "succeeded"
        switch_processing_mode(session, "legacy", now=later)
        session.commit()
        assert all(
            entry["exited"]
            for entry in session.get(PersonalWriterMode, True).child_history.values()
        )


def test_supported_maintenance_rejects_queued_legacy_writer() -> None:
    from db.models import Job
    from services.writer_mode import LegacyWriterModeConflict, require_legacy_maintenance_mode

    with migrated_disposable_engine() as engine, Session(engine) as session:
        session.add(
            Job(
                job_key=f"legacy-maintenance-block:{uuid.uuid4()}",
                job_type="daily_pipeline",
                state="queued",
                attempt=1,
                max_attempts=3,
                related_ids={},
                safe_to_rerun=True,
            )
        )
        session.commit()
        with pytest.raises(LegacyWriterModeConflict, match="queued or running"):
            require_legacy_maintenance_mode(session)


def test_explicit_child_reconciliation_preserves_terminal_success_and_attempt(monkeypatch) -> None:
    from services.personal.processing import reconcile_child_lifetimes, record_launcher

    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace = _setup(session)
        now = dt.datetime.now(dt.UTC)
        queued = create_daily_run(session, workspace, now=now)
        run_id, token, generation = (
            queued.run.id,
            queued.run.ownership_token,
            queued.run.fencing_generation,
        )
        record_launcher(
            session,
            run_id,
            token,
            generation,
            uuid.uuid4(),
            child_pid=123456789,
            child_identity=str(uuid.uuid4()),
            child_started_at="recorded fixture birth",
        )
        acquire_run(session, run_id, token, now=now)
        finish_run(session, run_id, token, state="succeeded", now=now)
        session.commit()
        monkeypatch.setattr(
            "services.personal.processing.process_birth_identity", lambda _pid: None
        )
        assert reconcile_child_lifetimes(session) == 1
        session.commit()
        run = session.get(PersonalRun, run_id, populate_existing=True)
        assert run.state == "succeeded" and run.attempt == 1
        assert not session.get(PersonalWriterMode, True).unconfirmed_child


@pytest.mark.parametrize("operation", ["create", "retry"])
@pytest.mark.parametrize("transport", ["celery", "subprocess"])
def test_queued_claim_records_selected_transport_before_any_delivery(
    monkeypatch, operation, transport
):
    from packages.config.settings import get_settings

    monkeypatch.setenv("PERSONAL_PROCESSING_MODE", "personal")
    monkeypatch.setenv("PERSONAL_PROCESSING_TRANSPORT", "celery")
    get_settings.cache_clear()
    try:
        with migrated_disposable_engine() as engine:
            with Session(engine) as session:
                workspace = _setup(session)
                now = dt.datetime.now(dt.UTC)
                if operation == "retry":
                    prior = create_daily_run(session, workspace, now=now)
                    run_id, token = prior.run.id, prior.run.ownership_token
                    session.commit()
                    assert mark_delivery_failed(session, run_id, token, now=now)
                    session.commit()
                monkeypatch.setenv("PERSONAL_PROCESSING_TRANSPORT", transport)
                get_settings.cache_clear()
                decision = (
                    create_daily_run(session, workspace, now=now)
                    if operation == "create"
                    else retry_run(session, workspace, run_id, now=now)
                )
                run_id = decision.run.id
                session.commit()
            # Observe the durable queued record from a separate request before any
            # record_delivery, supervisor callback or child has been invoked.
            with Session(engine) as observer:
                run = observer.get(PersonalRun, run_id)
                assert run.state == "queued" and run.started_at is None
                assert run.delivery_transport == transport
                assert run.celery_task_id is None
                assert run.attempt == (1 if operation == "create" else 2)
    finally:
        get_settings.cache_clear()
