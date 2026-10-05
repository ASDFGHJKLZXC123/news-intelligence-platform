"""A populated 0022 upgrade preserves earlier personal and legacy evidence."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.orm import Session

from db.models import (
    Article,
    Event,
    EventArticle,
    LLMRun,
    PersonalBriefSnapshot,
    PersonalCapture,
    PersonalLegacyUsage,
    PersonalPaidRequest,
    PersonalReportLink,
    PersonalRun,
    PersonalSpendingState,
    PersonalWriterMode,
    Report,
    ReportSection,
    Source,
    WatchlistItem,
)
from services.ingestion.normalize import url_hash
from services.personal.processing import recover_expired_owner, switch_processing_mode
from services.personal.workspace import ensure_workspace
from tests.integration._personal_preupgrade import (
    configure_preupgrade_profile,
    create_preupgrade_run,
)
from tests.integration._stage7_db import _disposable_database, require_disposable_postgres, url_for

pytestmark = pytest.mark.integration
NOW = dt.datetime(2026, 9, 30, 18, tzinfo=dt.UTC)


def _migrate(url: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _snapshot(connection, tables):
    return {
        name: [
            dict(row)
            for row in connection.execute(
                select(table).order_by(*table.primary_key.columns)
            ).mappings()
        ]
        for name, table in tables.items()
    }


@pytest.mark.parametrize(
    "mode,active_state", [("legacy", None), ("personal", "queued"), ("personal", "running")]
)
def test_populated_0022_upgrade_retains_mode_scopes_versions_saves_and_obligations(
    mode, active_state
):
    require_disposable_postgres()
    with _disposable_database() as database:
        url = url_for(database)
        _migrate(url, "0022_personal_spending")
        engine = create_engine(url)
        try:
            with Session(engine) as session:
                source = Source(name="Prior source", feed_url="https://offline.example/upgrade.xml")
                event = Event(title="Retained event", summary="Retained source-supported summary")
                session.add_all([source, event])
                session.flush()
                workspace, _ = ensure_workspace(session)
                profile = configure_preupgrade_profile(
                    session,
                    workspace,
                    [source.id],
                    execution_profile="assisted",
                    settings={
                        "monthly_limit_usd": "0.5",
                        "run_limit_usd": "0.1",
                        "model_routes": {
                            "brief": {
                                "provider": "scripted",
                                "model": "retained-model",
                                "price_revision": "prior-price",
                            }
                        },
                    },
                    schema_revision="personal-profile.v2",
                )
                article = Article(
                    source_id=source.id,
                    url="https://offline.example/prior-raw",
                    url_hash=url_hash("https://offline.example/prior-raw"),
                    title="Retained raw article",
                    summary="Source text stays exact",
                    published_at=None,
                )
                session.add(article)
                session.flush()
                session.add(EventArticle(event_id=event.id, article_id=article.id))
                prior = create_preupgrade_run(
                    session,
                    workspace,
                    profile,
                    now=NOW - dt.timedelta(days=1),
                    admitted_article_ids=[article.id],
                    enrichment_article_ids=[article.id],
                    event_ids=[event.id],
                    scopes_frozen_at=NOW - dt.timedelta(days=1),
                    coverage={"source_ids": [str(source.id)]},
                    stage_results={"capture": {"count": 1}},
                    terminal_manifest={"retained": True},
                    result={"published_versions": 2},
                )
                snapshot = PersonalBriefSnapshot(
                    workspace_id=workspace.id,
                    run_id=prior.id,
                    profile_revision_id=profile.id,
                    candidate_event_ids=[event.id],
                    selected_event_ids=[event.id],
                    input_payload={"events": [{"id": str(event.id), "summary": event.summary}]},
                    input_hash="a" * 64,
                    model_route={"provider": "scripted", "model": "retained-model"},
                )
                session.add(snapshot)
                session.flush()
                old_runs = Table("personal_runs", MetaData(), autoload_with=session.connection())
                session.execute(
                    old_runs.update()
                    .where(old_runs.c.id == prior.id)
                    .values(snapshot_id=snapshot.id)
                )
                for version in (1, 2):
                    report = Report(
                        user_id=workspace.owner_id,
                        report_type="personal_daily_brief",
                        title=f"Retained published version {version}",
                        status="published",
                        version=version,
                        brief_date=prior.local_date,
                        content_policy="descriptive_only.v1",
                    )
                    session.add(report)
                    session.flush()
                    session.add_all(
                        [
                            PersonalReportLink(
                                report_id=report.id,
                                workspace_id=workspace.id,
                                run_id=prior.id,
                                snapshot_id=snapshot.id,
                                brief_date=prior.local_date,
                                version=version,
                            ),
                            ReportSection(
                                report_id=report.id,
                                title="Retained summary",
                                body=f"Immutable source-supported text, version {version}",
                                section_order=0,
                            ),
                        ]
                    )
                    if version == 2:
                        session.execute(
                            old_runs.update()
                            .where(old_runs.c.id == prior.id)
                            .values(report_id=report.id)
                        )
                session.add_all(
                    [
                        WatchlistItem(
                            user_id=workspace.owner_id,
                            item_type="event",
                            item_id=str(event.id),
                            label="Retained saved label",
                        ),
                        PersonalCapture(
                            workspace_id=workspace.id,
                            run_id=prior.id,
                            source_id=source.id,
                            canonical_url=article.url,
                            url_hash=article.url_hash,
                            title=article.title,
                            rss_summary=article.summary,
                            article_id=article.id,
                            admitted_run_id=prior.id,
                            admitted_at=NOW,
                            processing_run_id=prior.id,
                            enrichment_state="complete",
                            receipt={"admission_ordinal": 0},
                        ),
                        PersonalPaidRequest(
                            workspace_id=workspace.id,
                            run_id=prior.id,
                            attempt=1,
                            accounting_month=dt.date(2026, 9, 1),
                            status="uncertain",
                            role="brief",
                            route={
                                "provider": "scripted",
                                "model": "retained-model",
                                "price_revision": "prior-price",
                            },
                            input_token_bound=100,
                            output_token_bound=30,
                            reserved_usd=Decimal("0.02"),
                            actual_usd=None,
                            evidence={"dispatch_may_have_started": True},
                            reserved_at=NOW,
                            dispatch_attempt_at=NOW,
                        ),
                        PersonalSpendingState(workspace_id=workspace.id, legacy_cutover_at=NOW),
                    ]
                )
                legacy = LLMRun(
                    prompt_name="retained-legacy",
                    prompt_version="v1",
                    provider="scripted",
                    model="retained-model",
                    cost_usd=Decimal("0.005"),
                    started_at=NOW,
                )
                session.add(legacy)
                session.flush()
                session.add(
                    PersonalLegacyUsage(
                        llm_run_id=legacy.id,
                        accounting_month=dt.date(2026, 9, 1),
                        actual_usd=Decimal("0.005"),
                        evidence={"retained_cost": True},
                    )
                )
                old_mode = Table(
                    "personal_writer_mode", MetaData(), autoload_with=session.connection()
                )
                session.execute(old_mode.update().values(mode=mode))
                active = None
                token = uuid.uuid4()
                lease = NOW + dt.timedelta(minutes=2 if active_state == "queued" else 35)
                if active_state:
                    active = create_preupgrade_run(
                        session,
                        workspace,
                        profile,
                        now=NOW,
                        state=active_state,
                        token=token,
                        lease_expires_at=lease,
                        celery_task_id="prior-accepted-delivery",
                    )
                workspace_id, prior_id = workspace.id, prior.id
                session.commit()

            # Keep reflection from 0022: every existing column is compared, including
            # timestamps, identities, report versions, original scopes and all cost data.
            names = (
                "sources",
                "articles",
                "events",
                "event_articles",
                "users",
                "watchlist_items",
                "reports",
                "report_sections",
                "llm_runs",
                "jobs",
                "personal_workspaces",
                "personal_profile_revisions",
                "personal_runs",
                "personal_captures",
                "personal_brief_snapshots",
                "personal_report_links",
                "personal_paid_requests",
                "personal_spending_state",
                "personal_legacy_usage",
                "personal_writer_mode",
            )
            with engine.connect() as connection:
                old_tables = {
                    name: Table(name, MetaData(), autoload_with=connection) for name in names
                }
                before = _snapshot(connection, old_tables)
            _migrate(url, "head")
            with engine.connect() as connection:
                assert _snapshot(connection, old_tables) == before
                assert "child_history" in {
                    column["name"]
                    for column in inspect(connection).get_columns("personal_writer_mode")
                }
            with Session(engine) as session:
                control = session.get(PersonalWriterMode, True)
                assert control.mode == mode
                assert control.child_history == {} and not control.unconfirmed_child
                assert session.get(PersonalRun, prior_id).fencing_generation == 0
                if active is None:
                    assert control.generation == 0 and control.active_run_id is None
                else:
                    migrated = session.get(PersonalRun, active.id)
                    assert (
                        control.active_run_id,
                        control.workspace_id,
                        control.active_attempt,
                    ) == (active.id, workspace_id, 1)
                    assert control.generation == migrated.fencing_generation == 1
                    assert migrated.ownership_token == token
                    assert control.lease_expires_at == migrated.lease_expires_at == lease
                    assert (
                        migrated.delivery_transport == "celery"
                        and migrated.celery_task_id == "prior-accepted-delivery"
                    )
                    if active_state == "queued":
                        assert control.delivery_token == token and migrated.queued_at == NOW
                        assert migrated.started_at is None
                    else:
                        assert control.ownership_token == token and migrated.started_at == NOW
                        assert migrated.graceful_deadline_at == NOW + dt.timedelta(minutes=25)
                        assert migrated.hard_deadline_at == NOW + dt.timedelta(minutes=30)
                    recover_expired_owner(session, active.id, now=lease + dt.timedelta(seconds=1))
                switch_processing_mode(session, "legacy")
                session.commit()
            # Supported runtime rollback changes control only (and explicitly recovers
            # the one expired owner); it retains schema, saved items and monetary evidence.
            with engine.connect() as connection:
                retained = {
                    name: table
                    for name, table in old_tables.items()
                    if name not in {"personal_writer_mode", "personal_runs", "jobs"}
                }
                assert _snapshot(connection, retained) == {name: before[name] for name in retained}
                assert len(connection.execute(select(old_tables["reports"])).all()) == 2
        finally:
            engine.dispose()
