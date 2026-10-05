"""Populated-database proof for Phase 2 personal schema, ownership and reads."""

from __future__ import annotations

import datetime
import os
import subprocess
import sys

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from db.models import (
    Article,
    Event,
    EventArticle,
    Job,
    PersonalBriefSnapshot,
    PersonalProfileRevision,
    PersonalRun,
    PersonalRunEventObservation,
    PersonalWorkspace,
    Report,
    Source,
    User,
    WatchlistItem,
)
from services.personal.repository import PersonalRepository
from services.personal.workspace import OwnerBindingConflict, bind_owner, ensure_workspace
from tests.integration._stage7_db import migrated_disposable_engine

pytestmark = pytest.mark.integration
UTC = datetime.UTC


def _remove_blank_migration_bootstrap(session: Session) -> None:
    workspace = session.scalar(select(PersonalWorkspace))
    if workspace is None:
        return
    owner = session.get(User, workspace.owner_id) if workspace.owner_id else None
    workspace.active_profile_revision_id = None
    session.flush()
    for profile in session.scalars(select(PersonalProfileRevision)):
        session.delete(profile)
    session.delete(workspace)
    session.flush()
    if owner is not None and owner.email.endswith("@local.invalid"):
        session.delete(owner)
    session.commit()


def _legacy_data(session: Session) -> tuple[User, User, Event, Article]:
    first = User(email="first@example.test", display_name="First")
    second = User(email="second@example.test", display_name="Second")
    source = Source(name="Publisher", feed_url="https://example.test/feed")
    session.add_all([first, second, source])
    session.flush()
    article = Article(
        source_id=source.id,
        url="https://example.test/story",
        url_hash="a" * 64,
        title="Interest-rate decision announced",
        summary="The central bank announced a 0.25 percentage point change.",
        published_at=datetime.datetime(2026, 9, 6, 20, tzinfo=UTC),
    )
    event = Event(title="Central bank changes interest rate", hotness_score=10)
    legacy = Report(
        report_type="daily_brief",
        brief_date=datetime.date(2026, 9, 6),
        title="Legacy v7",
        status="published",
        version=7,
        content_policy="descriptive_only.v1",
    )
    session.add_all([article, event, legacy])
    session.flush()
    session.add_all(
        [
            EventArticle(event_id=event.id, article_id=article.id),
            WatchlistItem(
                user_id=first.id, item_type="event", item_id=str(event.id), label=event.title
            ),
            WatchlistItem(
                user_id=first.id,
                item_type="company",
                item_id="legacy-company",
                label="Legacy company",
            ),
        ]
    )
    session.commit()
    return first, second, event, article


def _personal_run(
    session: Session, workspace: PersonalWorkspace, event: Event, article: Article
) -> PersonalRun:
    profile = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
    job = Job(
        job_key=f"personal-daily:{workspace.id}:2026-09-06",
        job_type="personal_daily",
        state="succeeded",
        related_ids={},
    )
    session.add(job)
    session.flush()
    run = PersonalRun(
        workspace_id=workspace.id,
        local_date=datetime.date(2026, 9, 6),
        profile_revision_id=profile.id,
        job_id=job.id,
        state="succeeded",
        admitted_article_ids=[article.id],
        enrichment_article_ids=[article.id],
        event_ids=[event.id],
        coverage={"captured": 1, "feed_failures": []},
        stage_results={},
    )
    session.add(run)
    session.flush()
    session.add(
        PersonalRunEventObservation(
            run_id=run.id,
            event_id=event.id,
            revision=1,
            qualifying_article_ids=[article.id],
            source_inputs={
                "event_title": event.title,
                "event_summary": event.summary,
                "articles": [
                    {"id": str(article.id), "title": article.title, "rss_summary": article.summary}
                ],
            },
            ranking_inputs={
                "source_count": 1,
                "hotness": 10,
                "newest_publication_at": article.published_at.isoformat(),
                "sort_key": [-1, 0, -10, 0, -article.published_at.timestamp(), str(event.id)],
            },
        )
    )
    session.commit()
    return run


def test_owner_selection_today_sources_and_saved_persistence_on_migrated_database() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        _remove_blank_migration_bootstrap(session)
        first, second, event, article = _legacy_data(session)
        workspace, choices = ensure_workspace(session)
        session.commit()
        assert workspace.owner_id is None
        assert {choice.id for choice in choices} == {first.id, second.id}
        assert (
            session.scalar(select(Report.title).where(Report.report_type == "daily_brief"))
            == "Legacy v7"
        )
        assert (
            session.scalar(
                select(WatchlistItem.item_id).where(WatchlistItem.item_type == "company")
            )
            == "legacy-company"
        )

        bind_owner(session, workspace, second.id)
        session.commit()
        with pytest.raises(OwnerBindingConflict):
            bind_owner(session, workspace, first.id)
        session.rollback()

        run = _personal_run(session, workspace, event, article)
        pinned_observation = session.scalar(
            select(PersonalRunEventObservation).where(PersonalRunEventObservation.run_id == run.id)
        )
        snapshot = PersonalBriefSnapshot(
            workspace_id=workspace.id,
            run_id=run.id,
            profile_revision_id=run.profile_revision_id,
            candidate_event_ids=[event.id],
            selected_event_ids=[event.id],
            input_payload={
                "candidates": [
                    {
                        "observation_id": str(pinned_observation.id),
                        "event_id": str(event.id),
                        "revision": 1,
                    }
                ]
            },
            input_hash="a" * 64,
            model_route={"mode": "offline_fixture"},
            prepared_at=datetime.datetime(2026, 9, 6, 22, tzinfo=UTC),
        )
        session.add(snapshot)
        session.flush()
        run.snapshot_id = snapshot.id
        session.add(
            PersonalRunEventObservation(
                run_id=run.id,
                event_id=event.id,
                revision=2,
                qualifying_article_ids=[],
                source_inputs={
                    "event_title": "Later observation title",
                    "event_summary": "Later observation summary",
                    "articles": [],
                },
                ranking_inputs={
                    "source_count": 0,
                    "hotness": 1,
                    "newest_publication_at": None,
                    "sort_key": [0, 0, -1, 1, 0, str(event.id)],
                },
            )
        )
        pinned_title = event.title
        event.title = "Later mutable event title"
        event.summary = "Later mutable event summary"
        event.hotness_score = 1
        session.commit()
        repo = PersonalRepository(session, workspace)
        items, total, displayed, newer = repo.list_events(
            run_id=None, query="interest rate", saved_only=False, limit=1, offset=0
        )
        assert total == 1 and items[0]["id"] == str(event.id)
        assert pinned_title != event.title
        assert items[0]["title"] == "Later mutable event title"
        assert items[0]["summary"] == "Later mutable event summary"
        assert items[0]["hotness_score"] == 10
        assert items[0]["observation_revision"] == 1
        current_match, current_total, *_ = repo.list_events(
            run_id=None, query="later mutable event", saved_only=False, limit=1, offset=0
        )
        assert current_total == 1 and current_match[0]["id"] == str(event.id)
        assert displayed.id == run.id and newer.id == run.id

        sources, source_total = repo.event_sources(event.id, run_id=run.id, limit=1, offset=0)
        assert source_total == 1
        assert sources[0]["url"] == article.url
        assert sources[0]["in_run_scope"] is True
        assert sources[0]["qualifies_for_brief"] is True

        first_save = repo.save(event.id)
        second_save = repo.save(event.id)
        session.commit()
        assert first_save.id == second_save.id
        session.expire_all()
        saved, saved_total = PersonalRepository(
            session, session.get(PersonalWorkspace, workspace.id)
        ).saved(limit=50, offset=0)
        assert saved_total == 1 and saved[0]["event_id"] == str(event.id)
        repo.unsave(event.id)
        repo.unsave(event.id)
        session.commit()
        assert (
            session.scalar(
                select(text("count(*)"))
                .select_from(WatchlistItem)
                .where(WatchlistItem.user_id == second.id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(text("count(*)"))
                .select_from(WatchlistItem)
                .where(WatchlistItem.user_id == first.id)
            )
            == 2
        )


def test_report_link_trigger_rejects_unlinked_personal_report() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        _remove_blank_migration_bootstrap(session)
        first = User(email="owner@example.test")
        session.add(first)
        session.commit()
        workspace, _ = ensure_workspace(session)
        session.commit()
        report = Report(
            user_id=first.id,
            report_type="personal_daily_brief",
            brief_date=datetime.date(2026, 9, 6),
            title="Unlinked",
            status="published",
            version=1,
            content_policy="descriptive_only.v1",
        )
        session.add(report)
        with pytest.raises(DBAPIError, match="personal report"):
            session.commit()
        session.rollback()


def test_nonempty_personal_downgrade_refuses_data_loss() -> None:
    with migrated_disposable_engine() as engine:
        with Session(engine) as session:
            original_workspace, _ = ensure_workspace(session)
            original_workspace_id = original_workspace.id
            session.commit()
        env = dict(os.environ)
        env["DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0019"],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        assert "Processing ownership and history must be retained" in result.stderr
        with Session(engine) as session:
            workspace, _ = ensure_workspace(session)
            assert workspace.id == original_workspace_id
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0023_personal_processing_control"
            )


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
