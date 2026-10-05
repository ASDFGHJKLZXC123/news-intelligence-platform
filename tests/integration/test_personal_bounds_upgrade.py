"""A real populated 0020-to-head upgrade retains personal and legacy evidence."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, Table, create_engine, func, select
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.orm import Session

from db.models import (
    Article,
    Event,
    Job,
    LLMRun,
    PersonalCapture,
    PersonalLegacyUsage,
    PersonalPaidRequest,
    PersonalProfileRevision,
    PersonalRun,
    PersonalSpendingState,
    PersonalWorkspace,
    Report,
    Source,
    User,
    WatchlistItem,
)
from services.ingestion.normalize import url_hash
from services.personal import spending
from services.personal.spending import (
    PaidWorkBlocked,
    assert_legacy_usage_reconciled,
    import_legacy_usage,
    spending_summary,
)
from services.personal.workspace import ensure_workspace
from tests.integration._personal_preupgrade import (
    configure_preupgrade_profile,
    create_preupgrade_run,
)
from tests.integration._stage7_db import _disposable_database, require_disposable_postgres, url_for
from tests.integration.test_personal_brief_api import _client

pytestmark = pytest.mark.integration


def _migrate(url: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_populated_upgrade_preserves_sources_raw_saved_reports_and_imports_known_unknown_usage_once(
    monkeypatch: pytest.MonkeyPatch,
):
    require_disposable_postgres()
    with _disposable_database() as database:
        url = url_for(database)
        _migrate(url, "0020")
        engine = create_engine(url)
        now = dt.datetime.now(dt.UTC)
        try:
            with Session(engine) as session:
                source = Source(
                    name="Retained existing source", feed_url="https://offline.example/retained.xml"
                )
                event = Event(title="Retained saved event")
                session.add_all([source, event])
                session.flush()
                workspace, _ = ensure_workspace(session)
                profile = configure_preupgrade_profile(session, workspace, [source.id])
                expected_configuration = {
                    "profile_revision_id": str(profile.id),
                    "profile_revision": profile.revision,
                    "route_mode": None,
                    "routes": [],
                }
                article = Article(
                    source_id=source.id,
                    url="https://offline.example/retained",
                    url_hash=url_hash("https://offline.example/retained"),
                    title="Retained raw title",
                    summary="Retained RSS source text",
                    published_at=None,
                )
                report = Report(
                    user_id=workspace.owner_id,
                    report_type="daily_brief",
                    title="Immutable legacy report",
                    status="published",
                    version=1,
                    brief_date=now.date(),
                    content_policy="descriptive_only.v1",
                )
                saved = WatchlistItem(
                    user_id=workspace.owner_id,
                    item_type="event",
                    item_id=str(event.id),
                    label="Retained saved label",
                )
                known = LLMRun(
                    prompt_name="legacy",
                    prompt_version="v1",
                    provider="scripted",
                    model="existing",
                    cost_usd=Decimal("0.005"),
                    started_at=now,
                )
                unknown = LLMRun(
                    prompt_name="legacy",
                    prompt_version="v1",
                    provider="scripted",
                    model="existing",
                    cost_usd=None,
                    started_at=now,
                )
                timeout_zero = LLMRun(
                    prompt_name="legacy-timeout",
                    prompt_version="v1",
                    provider="openai",
                    model="existing",
                    status="failed",
                    error_message="provider invocation failure",
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0,
                    started_at=now,
                )
                session.add_all([article, report, saved, known, unknown, timeout_zero])
                session.flush()
                run = create_preupgrade_run(
                    session,
                    workspace,
                    profile,
                    now=now,
                    admitted_article_ids=[article.id],
                    scopes_frozen_at=now,
                )
                # Reflect only the pre-upgrade table so no new ORM default is inserted.
                old_capture = Table(
                    "personal_captures", MetaData(), autoload_with=session.connection()
                )
                capture_id = uuid.uuid4()
                session.execute(
                    old_capture.insert().values(
                        id=capture_id,
                        workspace_id=workspace.id,
                        run_id=run.id,
                        source_id=source.id,
                        canonical_url=article.url,
                        url_hash=article.url_hash,
                        title=article.title,
                        rss_summary=article.summary,
                        published_at=None,
                        captured_at=now,
                        article_id=article.id,
                        admitted_run_id=run.id,
                        admitted_at=now,
                        truncated=False,
                        receipt={"admission_ordinal": 0},
                    )
                )
                identities = {
                    "workspace": workspace.id,
                    "run": run.id,
                    "source": source.id,
                    "article": article.id,
                    "report": report.id,
                    "saved": saved.id,
                    "known": known.id,
                    "unknown": unknown.id,
                    "timeout": timeout_zero.id,
                }
                session.commit()
            engine.dispose()
            _migrate(url, "head")
            with Session(engine) as session:
                article = session.get(Article, identities["article"])
                assert (article.title, article.summary, article.published_at) == (
                    "Retained raw title",
                    "Retained RSS source text",
                    None,
                )
                assert session.get(Report, identities["report"]).title == "Immutable legacy report"
                assert session.get(Report, identities["report"]).status == "published"
                assert (
                    session.get(WatchlistItem, identities["saved"]).label == "Retained saved label"
                )
                capture = session.get(PersonalCapture, capture_id)
                assert (
                    capture.admitted_at == now and capture.enrichment_state == "disabled_by_profile"
                )
                assert (
                    capture.article_id == identities["article"]
                    and capture.processing_run_id == identities["run"]
                )
                run = session.get(PersonalRun, identities["run"])
                assert run.enrichment_article_ids == [] and run.admitted_article_ids == [
                    identities["article"]
                ]
                assert session.get(PersonalLegacyUsage, identities["known"]).actual_usd == Decimal(
                    "0.005"
                )
                assert session.get(PersonalLegacyUsage, identities["unknown"]).actual_usd is None
                assert session.get(PersonalLegacyUsage, identities["timeout"]).actual_usd is None
                import_legacy_usage(session, identities["workspace"], now=now)
                import_legacy_usage(session, identities["workspace"], now=now)
                assert session.scalar(select(func.count()).select_from(PersonalLegacyUsage)) == 3
                assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0
                summary = spending_summary(session, identities["workspace"], now=now)
                assert Decimal(summary["finalized_usd"]) == Decimal("0.005")
                assert summary["unreconciled_legacy_count"] == 2
                with pytest.raises(PaidWorkBlocked) as blocked:
                    assert_legacy_usage_reconciled(session, identities["workspace"], now=now)
                assert blocked.value.code == "legacy_usage_unreconciled"

            # Exercise the actual read API after the populated migration. Freeze its
            # accounting clock so a UTC month rollover cannot change this comparison.
            monkeypatch.setattr(
                spending,
                "spending_summary",
                lambda session, workspace_id: spending_summary(session, workspace_id, now=now),
            )
            retained_models = (
                Source,
                Event,
                Article,
                Report,
                WatchlistItem,
                User,
                Job,
                LLMRun,
                PersonalWorkspace,
                PersonalProfileRevision,
                PersonalRun,
                PersonalCapture,
                PersonalLegacyUsage,
                PersonalSpendingState,
                PersonalPaidRequest,
            )

            def retained_rows():
                with engine.connect() as connection:
                    return {
                        model.__tablename__: [
                            dict(row)
                            for row in connection.execute(
                                select(model.__table__).order_by(
                                    *model.__table__.primary_key.columns
                                )
                            ).mappings()
                        ]
                        for model in retained_models
                    }

            before_api = retained_rows()
            writes = []

            def record_write(_connection, _cursor, statement, _parameters, _context, _executemany):
                operation = statement.lstrip().split(None, 1)[0].upper()
                if operation in {
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "MERGE",
                    "CREATE",
                    "ALTER",
                    "DROP",
                    "TRUNCATE",
                }:
                    writes.append(operation)

            sqlalchemy_event.listen(engine, "before_cursor_execute", record_write)
            try:
                with _client(engine) as client:
                    first = client.get("/api/v1/personal/spending")
                    repeated = client.get("/api/v1/personal/spending")
                    assert first.status_code == repeated.status_code == 200
                    assert first.json() == repeated.json() == summary
            finally:
                sqlalchemy_event.remove(engine, "before_cursor_execute", record_write)

            api_summary = first.json()
            assert api_summary["current_configuration"] == expected_configuration
            assert api_summary["accounted_routes"] == [
                {
                    "source": "legacy_usage",
                    "accounting_period": now.strftime("%Y-%m"),
                    "role": None,
                    "provider": provider,
                    "model": "existing",
                    "model_version": None,
                    "price_revision": None,
                }
                for provider in ("openai", "scripted")
            ]
            assert api_summary["status"] == "legacy_usage_unreconciled"
            assert api_summary["reserved_usd"] == api_summary["unresolved_usd"] == "0"
            assert writes == []
            assert retained_rows() == before_api
            assert before_api[PersonalPaidRequest.__tablename__] == []
        finally:
            engine.dispose()


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
