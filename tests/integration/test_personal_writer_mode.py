"""PostgreSQL proof that legacy writers honor the personal singleton fence."""

from __future__ import annotations

import datetime
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Article, Job, PersonalWriterMode, Source
from packages.providers.fakes import FakeRSSProvider
from services.pipeline import daily_pipeline_identity
from services.pipeline.sqlalchemy_store import SQLAlchemyPipelineLifecycleStore
from services.reports import DailyBriefGenerationResult, GateOutcome, window_for_date
from services.reports.lifecycle import ReportSnapshot
from services.writer_mode import (
    LegacyWriterModeConflict,
    queue_legacy_daily_brief,
)
from tests.integration._personal_processing_clock import install_authored_processing_clock
from tests.integration._stage7_db import migrated_disposable_engine
from workers import ingestion_tasks, report_tasks


@pytest.fixture(autouse=True)
def authored_processing_clock(monkeypatch):
    from packages.config.settings import get_settings

    monkeypatch.setenv("PERSONAL_PROCESSING_MODE", "legacy")
    get_settings.cache_clear()
    install_authored_processing_clock(monkeypatch, sys.modules[__name__])
    yield
    get_settings.cache_clear()


pytestmark = pytest.mark.integration


def _mode(engine, value: str) -> None:  # noqa: ANN001
    with Session(engine) as session:
        row = session.scalar(
            select(PersonalWriterMode)
            .where(PersonalWriterMode.singleton.is_(True))
            .with_for_update()
        )
        assert row is not None
        row.mode = value
        session.commit()


def test_personal_mode_rejects_pipeline_queue_and_direct_ingestion_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with migrated_disposable_engine() as engine:
        with Session(engine) as session:
            source = Source(name="Blocked legacy feed", feed_url="https://legacy.test/rss")
            session.add(source)
            session.commit()
            source_id = source.id
        _mode(engine, "personal")
        store = SQLAlchemyPipelineLifecycleStore(
            session_factory=lambda: Session(engine, autoflush=False)
        )
        with pytest.raises(LegacyWriterModeConflict, match="personal writer mode"):
            store.queue(daily_pipeline_identity(datetime.date(2026, 9, 9)))

        provider_built = False

        def provider_factory():  # noqa: ANN202
            nonlocal provider_built
            provider_built = True
            return FakeRSSProvider([])

        monkeypatch.setattr(
            ingestion_tasks, "SessionLocal", lambda: Session(engine, autoflush=False)
        )
        monkeypatch.setattr(ingestion_tasks, "HttpRSSProvider", provider_factory)
        with pytest.raises(LegacyWriterModeConflict, match="personal writer mode"):
            ingestion_tasks.ingest_source_feed.run(str(source_id))
        assert provider_built is False
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Article)) == 0


def test_legacy_direct_writer_holds_mode_lock_through_provider_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with migrated_disposable_engine() as engine:
        with Session(engine) as session:
            source = Source(name="Legacy feed", feed_url="https://legacy.test/rss")
            session.add(source)
            session.commit()
            source_id = source.id

        entered_provider = threading.Event()
        release_provider = threading.Event()
        switch_started = threading.Event()
        switch_finished = threading.Event()
        errors: list[BaseException] = []

        class BlockingProvider(FakeRSSProvider):
            def fetch(self, feed_url: str):  # noqa: ANN202
                entered_provider.set()
                if not release_provider.wait(5):
                    raise TimeoutError("test did not release legacy provider")
                return super().fetch(feed_url)

        monkeypatch.setattr(
            ingestion_tasks, "SessionLocal", lambda: Session(engine, autoflush=False)
        )
        monkeypatch.setattr(ingestion_tasks, "HttpRSSProvider", BlockingProvider)

        def run_writer() -> None:
            try:
                ingestion_tasks.ingest_source_feed.run(str(source_id))
            except BaseException as exc:  # pragma: no cover - asserted after join
                errors.append(exc)

        def switch_mode() -> None:
            try:
                switch_started.set()
                _mode(engine, "personal")
                switch_finished.set()
            except BaseException as exc:  # pragma: no cover - asserted after join
                errors.append(exc)

        writer = threading.Thread(target=run_writer)
        writer.start()
        assert entered_provider.wait(5)
        switcher = threading.Thread(target=switch_mode)
        switcher.start()
        assert switch_started.wait(5)
        time.sleep(0.15)
        assert not switch_finished.is_set()
        release_provider.set()
        writer.join(5)
        switcher.join(5)
        assert not writer.is_alive() and not switcher.is_alive()
        assert errors == []
        assert switch_finished.is_set()
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Article)) == 2
            mode = session.scalar(select(PersonalWriterMode.mode))
            assert mode == "personal"


def test_manual_report_job_is_durable_and_a_transient_worker_failure_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief_date = datetime.date(2026, 9, 9)
    task_id = uuid.uuid4()
    report_id = uuid.uuid4()
    with migrated_disposable_engine() as engine:
        with Session(engine, autoflush=False) as session:
            queued = queue_legacy_daily_brief(session, brief_date=brief_date, task_id=task_id)
            assert queued.id == task_id

        calls = 0

        def generate(_brief_date, **_kwargs):  # noqa: ANN001, ANN202
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary provider fault")
            return DailyBriefGenerationResult(
                report=ReportSnapshot(
                    id=report_id,
                    user_id=None,
                    report_type="daily_brief",
                    brief_date=brief_date,
                    event_id=None,
                    title="Daily Brief",
                    status="published",
                    version=2,
                    change_reason="automatic retry",
                    stale=False,
                    generated_by_run_id=None,
                    created_at=None,
                    updated_at=None,
                ),
                brief_date=brief_date,
                window=window_for_date(brief_date),
                selected_event_ids=(),
                selected_event_count=0,
                quiet_day=True,
                gate_outcome=GateOutcome.PASS,
                published=True,
                section_count=2,
                version=2,
                change_reason="automatic retry",
                generated_by_run_id=None,
            )

        monkeypatch.setattr(report_tasks, "SessionLocal", lambda: Session(engine, autoflush=False))
        monkeypatch.setattr(
            report_tasks, "build_redis_client", lambda: SimpleNamespace(close=lambda: None)
        )
        monkeypatch.setattr(
            report_tasks, "build_generation_orchestrator_factory", lambda **_kwargs: object()
        )
        monkeypatch.setattr(report_tasks, "generate_daily_brief", generate)
        monkeypatch.setattr(
            report_tasks,
            "get_settings",
            lambda: SimpleNamespace(crisis_prediction_reads_enabled=False),
        )

        with pytest.raises(RuntimeError, match="queued date"):
            report_tasks.run_daily_brief_generation.run("2026-09-08", str(task_id))
        with Session(engine) as session:
            untouched = session.get(Job, task_id)
            assert untouched is not None
            assert untouched.state == "queued"
            assert untouched.attempt == 1
            assert untouched.error is None
        assert calls == 0

        with pytest.raises(RuntimeError, match="temporary provider fault"):
            report_tasks.run_daily_brief_generation.run(brief_date.isoformat(), str(task_id))
        with Session(engine) as session:
            failed = session.get(Job, task_id)
            assert failed is not None
            assert failed.state == "failed"
            assert failed.attempt == 1
            assert failed.error["retryable"] is True

        payload = report_tasks.run_daily_brief_generation.run(brief_date.isoformat(), str(task_id))
        assert payload["status"] == "published"
        with Session(engine) as session:
            succeeded = session.get(Job, task_id)
            assert succeeded is not None
            assert succeeded.state == "succeeded"
            assert succeeded.attempt == 2
            assert succeeded.related_ids["report_id"] == str(report_id)

        duplicate = report_tasks.run_daily_brief_generation.run(
            brief_date.isoformat(), str(task_id)
        )
        assert duplicate["status"] == "duplicate_terminal_delivery"
        assert calls == 2
