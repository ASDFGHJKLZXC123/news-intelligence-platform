"""Disposable-PostgreSQL proof for Phase 2 run ownership, scopes and CORE-01."""

from __future__ import annotations

import datetime
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from db.models import (
    Article,
    Claim,
    ClaimEvidence,
    Event,
    EventArticle,
    EvidenceItem,
    PersonalClaimPreparation,
    PersonalRun,
    PersonalRunEventObservation,
    PersonalWorkspace,
    PersonalWriterMode,
    Source,
)
from services.personal.claims import prepare_article_claims
from services.personal.processing import recover_expired_owner
from services.personal.runs import (
    PersonalOwnershipLost,
    PersonalRunConflict,
    ProcessingBusy,
    WriterModeConflict,
    acquire_run,
    create_daily_run,
    finish_run,
    lock_owned_run,
    retry_run,
)
from services.personal.snapshots import (
    freeze_article_scopes,
    freeze_brief_snapshot,
    record_event_observations,
)
from services.personal.workspace import configure_profile, ensure_workspace
from tests.integration._personal_processing_clock import install_authored_processing_clock
from tests.integration._stage7_db import migrated_disposable_engine


@pytest.fixture(autouse=True)
def authored_processing_clock(monkeypatch):
    from packages.config.settings import get_settings

    monkeypatch.setenv("PERSONAL_PROCESSING_MODE", "personal")
    get_settings.cache_clear()
    install_authored_processing_clock(monkeypatch, sys.modules[__name__])
    yield
    get_settings.cache_clear()


pytestmark = pytest.mark.integration
UTC = datetime.UTC


def _setup(session: Session) -> tuple[PersonalWorkspace, Source]:
    source = Source(name="Captured Feed", feed_url=f"https://example.test/{uuid.uuid4()}.xml")
    session.add(source)
    session.flush()
    workspace, _ = ensure_workspace(session)
    configure_profile(
        session,
        workspace,
        selected_source_ids=[source.id],
        settings={"model_route": {"mode": "offline_fixture"}},
    )
    session.commit()
    return workspace, source


def _article(
    session: Session,
    source: Source,
    index: int,
    *,
    summary: str | None = "The agency confirmed the project was approved.",
) -> Article:
    article = Article(
        source_id=source.id,
        url=f"https://example.test/story/{index}/{uuid.uuid4()}",
        url_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        title=f"Daily coverage item {index}",
        summary=summary,
        published_at=datetime.datetime(2026, 9, 6, 18, index % 60, tzinfo=UTC),
    )
    session.add(article)
    session.flush()
    return article


def _create_and_acquire(
    session: Session, workspace: PersonalWorkspace, now: datetime.datetime
) -> tuple[PersonalRun, uuid.UUID]:
    decision = create_daily_run(session, workspace, now=now)
    token = decision.run.ownership_token
    assert token is not None
    run_id = decision.run.id
    session.commit()
    run = acquire_run(session, run_id, token, now=now)
    session.commit()
    return run, token


def test_same_date_start_is_idempotent_under_concurrency() -> None:
    with migrated_disposable_engine() as engine:
        with Session(engine) as session:
            workspace, _ = _setup(session)
            workspace_id = workspace.id
        now = datetime.datetime(2026, 9, 7, 6, 59, tzinfo=UTC)

        def start() -> tuple[uuid.UUID, bool]:
            with Session(engine) as thread_session:
                thread_workspace = thread_session.get(PersonalWorkspace, workspace_id)
                decision = create_daily_run(thread_session, thread_workspace, now=now)
                result = decision.run.id, decision.created
                thread_session.commit()
                return result

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: start(), range(4)))
        assert len({run_id for run_id, _ in results}) == 1
        assert sum(created for _, created in results) == 1
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(PersonalRun)) == 1
            workspace = session.get(PersonalWorkspace, workspace_id)
            repeat = create_daily_run(session, workspace, now=now)
            assert repeat.run.id == results[0][0]
            assert repeat.created is False and repeat.should_enqueue is False


def test_la_logical_date_retry_preserves_scope_and_stops_after_three_attempts() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace, source = _setup(session)
        article = _article(session, source, 1)
        session.commit()
        # 06:59 UTC is still Sunday in America/Los_Angeles.
        now = datetime.datetime(2026, 9, 7, 6, 59, tzinfo=UTC)
        run, token = _create_and_acquire(session, workspace, now)
        assert run.local_date == datetime.date(2026, 9, 6)
        freeze_article_scopes(
            session,
            run.id,
            token,
            admitted_article_ids=[article.id],
            enrichment_article_ids=[article.id],
            now=now,
        )
        session.commit()
        finish_run(session, run.id, token, state="failed", error={"code": "stage_failed"})
        session.commit()

        second = retry_run(session, workspace, run.id, now=now + datetime.timedelta(days=1))
        assert second.run.attempt == 2
        assert second.run.local_date == datetime.date(2026, 9, 6)
        assert second.run.enrichment_article_ids == [article.id]
        second_token = second.run.ownership_token
        session.commit()
        acquire_run(session, run.id, second_token, now=now + datetime.timedelta(days=1))
        finish_run(session, run.id, second_token, state="failed")
        session.commit()

        third = retry_run(session, workspace, run.id, now=now + datetime.timedelta(days=2))
        assert third.run.attempt == 3
        third_token = third.run.ownership_token
        session.commit()
        acquire_run(session, run.id, third_token, now=now + datetime.timedelta(days=2))
        finish_run(session, run.id, third_token, state="failed")
        session.commit()
        with pytest.raises(PersonalRunConflict, match="three attempts"):
            retry_run(session, workspace, run.id, now=now + datetime.timedelta(days=3))


def test_preloaded_stale_run_and_writer_mode_cannot_reclaim_ownership() -> None:
    with migrated_disposable_engine() as engine:
        with Session(engine) as setup:
            workspace, _ = _setup(setup)
            run, first_token = _create_and_acquire(
                setup, workspace, datetime.datetime(2026, 9, 6, 18, tzinfo=UTC)
            )
            finish_run(setup, run.id, first_token, state="failed")
            setup.commit()
            run_id, workspace_id = run.id, workspace.id

        with Session(engine) as stale, Session(engine) as current:
            stale_run = stale.get(PersonalRun, run_id)
            stale_mode = stale.scalar(select(PersonalWriterMode))
            assert stale_run.state == "failed" and stale_mode.mode == "personal"

            current_workspace = current.get(PersonalWorkspace, workspace_id)
            decision = retry_run(
                current,
                current_workspace,
                run_id,
                now=datetime.datetime(2026, 9, 7, 18, tzinfo=UTC),
            )
            current_token = decision.run.ownership_token
            current.commit()
            acquire_run(
                current,
                run_id,
                current_token,
                now=datetime.datetime(2026, 9, 7, 18, tzinfo=UTC),
            )
            finish_run(current, run_id, current_token, state="succeeded")
            current.commit()

            stale_workspace = stale.get(PersonalWorkspace, workspace_id)
            with pytest.raises(PersonalRunConflict, match="succeeded"):
                retry_run(
                    stale,
                    stale_workspace,
                    run_id,
                    now=datetime.datetime(2026, 9, 8, 18, tzinfo=UTC),
                )
            stale.rollback()
            with pytest.raises(PersonalOwnershipLost):
                lock_owned_run(stale, run_id, first_token)
            stale.rollback()

            current.execute(
                update(PersonalWriterMode)
                .where(PersonalWriterMode.singleton.is_(True))
                .values(mode="legacy")
            )
            current.commit()
            with pytest.raises(WriterModeConflict, match="legacy writer mode"):
                lock_owned_run(stale, run_id, current_token)


def test_seven_observations_survive_terminal_failure_before_snapshot() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace, source = _setup(session)
        articles = [_article(session, source, index) for index in range(7)]
        events = []
        for index, article in enumerate(articles):
            event = Event(title=f"Observed event {index}", hotness_score=80 - index)
            session.add(event)
            session.flush()
            session.add(EventArticle(event_id=event.id, article_id=article.id))
            events.append(event)
        session.commit()
        now = datetime.datetime(2026, 9, 6, 18, tzinfo=UTC)
        run, token = _create_and_acquire(session, workspace, now)
        article_ids = [article.id for article in articles]
        freeze_article_scopes(
            session,
            run.id,
            token,
            admitted_article_ids=article_ids,
            enrichment_article_ids=article_ids,
            now=now,
        )
        observations = record_event_observations(session, run.id, token)
        assert len(observations) == 7
        session.commit()
        finish_run(session, run.id, token, state="failed", error={"code": "before_snapshot"})
        session.commit()
        session.refresh(run)
        assert run.snapshot_id is None
        assert len(run.terminal_manifest["observations"]) == 7
        assert {item["event_id"] for item in run.terminal_manifest["observations"]} == {
            str(event.id) for event in events
        }


def test_article_scope_cannot_be_owned_by_two_active_or_retryable_runs() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace, source = _setup(session)
        article = _article(session, source, 1)
        session.commit()
        first_now = datetime.datetime(2026, 9, 6, 18, tzinfo=UTC)
        first, first_token = _create_and_acquire(session, workspace, first_now)
        freeze_article_scopes(
            session,
            first.id,
            first_token,
            admitted_article_ids=[article.id],
            enrichment_article_ids=[article.id],
            now=first_now,
        )
        session.commit()
        second_now = first_now + datetime.timedelta(days=1)
        # Phase 4A adds one global owner. An expired prior run still requires
        # explicit recovery before another date can probe its retained scope.
        with pytest.raises(ProcessingBusy):
            create_daily_run(session, workspace, now=second_now)
        session.rollback()
        recover_expired_owner(session, first.id, now=second_now)
        session.commit()
        second, second_token = _create_and_acquire(session, workspace, second_now)
        with pytest.raises(PersonalOwnershipLost, match="belongs to another personal run"):
            freeze_article_scopes(
                session,
                second.id,
                second_token,
                admitted_article_ids=[article.id],
                enrichment_article_ids=[article.id],
                now=second_now,
            )
        session.rollback()

        finish_run(session, second.id, second_token, state="failed")
        session.commit()
        for attempt in (2, 3):
            retried = retry_run(
                session,
                workspace,
                first.id,
                now=first_now + datetime.timedelta(days=attempt),
            )
            retry_token = retried.run.ownership_token
            session.commit()
            acquire_run(
                session,
                first.id,
                retry_token,
                now=first_now + datetime.timedelta(days=attempt),
            )
            finish_run(session, first.id, retry_token, state="failed")
            session.commit()
        resumed = retry_run(
            session, workspace, second.id, now=first_now + datetime.timedelta(days=4)
        )
        second_token = resumed.run.ownership_token
        session.commit()
        acquire_run(session, second.id, second_token, now=first_now + datetime.timedelta(days=4))
        session.commit()
        with pytest.raises(PersonalOwnershipLost, match="transfer is explicit"):
            freeze_article_scopes(
                session,
                second.id,
                second_token,
                admitted_article_ids=[article.id],
                enrichment_article_ids=[article.id],
                now=second_now,
            )


def test_fresh_articles_produce_exact_claim_evidence_paths_and_snapshot_rejects_bad_span() -> None:
    with migrated_disposable_engine() as engine, Session(engine) as session:
        workspace, source = _setup(session)
        summaries = [
            "The central bank announced a rate cut to 0.25.",
            "Dr. Lee confirmed the central bank cut rates.",
            'Did officials confirm that the agency approved the project?"',
            "Top 10 reasons the company reported a loss.",
        ]
        articles = [
            _article(session, source, index, summary=summary)
            for index, summary in enumerate(summaries)
        ]
        event = Event(title="Central bank announcements", hotness_score=85)
        session.add(event)
        session.flush()
        session.add_all(
            [EventArticle(event_id=event.id, article_id=article.id) for article in articles]
        )
        session.commit()
        assert session.scalar(select(func.count()).select_from(Claim)) == 0

        now = datetime.datetime(2026, 9, 6, 18, tzinfo=UTC)
        run, token = _create_and_acquire(session, workspace, now)
        article_ids = [article.id for article in articles]
        freeze_article_scopes(
            session,
            run.id,
            token,
            admitted_article_ids=article_ids,
            enrichment_article_ids=article_ids,
            now=now,
        )
        record_event_observations(session, run.id, token)
        preparations = prepare_article_claims(session, run.id, token)
        session.commit()
        assert sum(item.status == "supported" for item in preparations) == 2
        assert sum(item.status == "abstained" for item in preparations) == 2
        assert session.scalar(select(func.count()).select_from(Claim)) == 2
        assert session.scalar(select(func.count()).select_from(EvidenceItem)) == 2
        assert session.scalar(select(func.count()).select_from(ClaimEvidence)) == 2
        assert session.scalar(select(func.count()).select_from(PersonalClaimPreparation)) == 4
        prepare_article_claims(session, run.id, token)
        session.commit()
        assert session.scalar(select(func.count()).select_from(PersonalClaimPreparation)) == 4
        assert session.scalar(select(func.count()).select_from(Claim)) == 2

        observation = session.scalar(
            select(PersonalRunEventObservation).where(PersonalRunEventObservation.run_id == run.id)
        )
        original_source_inputs = observation.source_inputs
        corrupted_source_inputs = dict(original_source_inputs)
        corrupted_articles = [dict(item) for item in original_source_inputs["articles"]]
        corrupted_articles[0]["rss_summary"] = "Changed after the revision froze."
        corrupted_source_inputs["articles"] = corrupted_articles
        observation.source_inputs = corrupted_source_inputs
        session.commit()
        with pytest.raises(RuntimeError, match="differs from its pinned revision"):
            freeze_brief_snapshot(
                session,
                run.id,
                token,
                model_route={"mode": "offline-test"},
                prepared_at=now,
            )
        session.rollback()
        observation = session.get(PersonalRunEventObservation, observation.id)
        observation.source_inputs = original_source_inputs
        session.commit()

        supported = session.scalar(
            select(PersonalClaimPreparation)
            .where(PersonalClaimPreparation.run_id == run.id)
            .where(PersonalClaimPreparation.status == "supported")
            .limit(1)
        )
        supported.span_end -= 1
        session.commit()
        with pytest.raises(RuntimeError, match="span does not match"):
            freeze_brief_snapshot(
                session,
                run.id,
                token,
                model_route={"mode": "offline-test"},
                prepared_at=now,
            )
        session.rollback()
        assert (
            session.scalar(
                select(func.count())
                .select_from(PersonalRunEventObservation)
                .where(PersonalRunEventObservation.run_id == run.id)
            )
            == 1
        )
