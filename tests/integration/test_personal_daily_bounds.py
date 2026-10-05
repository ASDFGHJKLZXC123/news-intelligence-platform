"""Phase 3 admission/scope acceptance against migrated disposable PostgreSQL.

All feeds are synthetic. These checks run the production settings, lifecycle, capture,
admission and freeze helpers; they never initialize a paid provider or touch the shared DB.
"""

from __future__ import annotations

import datetime
import io
import sys
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Barrier

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from db.models import (
    Article,
    PersonalArticleRevision,
    PersonalCapture,
    PersonalEnrichmentTransfer,
    PersonalFeedReceipt,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    Source,
)
from packages.providers.base import RSSItem
from packages.providers.fakes import FakeRSSProvider
from services.ingestion.http_provider import MAX_RSS_RESPONSE_BYTES, HttpRSSProvider
from services.personal.coordinator import (
    _admit_pending,
    _capture_selected_feeds,
    _freeze_enrichment_scope,
    _mark_stage_failure,
)
from services.personal.runs import (
    PersonalRunConflict,
    acquire_run,
    create_daily_run,
    finish_run,
    retry_run,
)
from services.personal.settings import PersonalSettingsUpdate, update_settings
from services.personal.workspace import ensure_workspace
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
START = datetime.datetime(2026, 9, 19, 18, tzinfo=UTC)


@dataclass(frozen=True)
class Attempt:
    run_id: uuid.UUID
    token: uuid.UUID


class Desk:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        with self.session() as session:
            sources = [
                Source(
                    id=uuid.UUID(int=index + 1),
                    name=f"Synthetic Phase 3 source {index}",
                    feed_url=f"https://offline.example/source-{index}.xml",
                )
                for index in range(4)
            ]
            session.add_all(sources)
            workspace, _ = ensure_workspace(session)
            session.flush()
            self.workspace_id = workspace.id
            self.source_ids = tuple(source.id for source in sources)
            session.commit()

    def session(self) -> Session:
        return Session(self.engine, autoflush=False)

    def configure(
        self,
        *,
        sources: Sequence[uuid.UUID] | None = None,
        daily: int = 100,
        admission: int = 100,
        enrichment: int = 100,
        assisted: bool = False,
    ) -> uuid.UUID:
        with self.session() as session:
            workspace = session.get(PersonalWorkspace, self.workspace_id)
            profile = update_settings(
                session,
                workspace,
                PersonalSettingsUpdate(
                    selected_source_ids=list(
                        sources if sources is not None else self.source_ids[:1]
                    ),
                    daily_article_limit=daily,
                    run_article_limit=admission,
                    enrichment_article_limit=enrichment,
                    execution_profile="assisted" if assisted else "raw",
                    model_route={"mode": "offline_fixture"} if assisted else {},
                ),
            )
            profile_id = profile.id
            session.commit()
            return profile_id

    def start(self, now: datetime.datetime = START) -> Attempt:
        with self.session() as session:
            workspace = session.get(PersonalWorkspace, self.workspace_id)
            decision = create_daily_run(session, workspace, now=now)
            assert decision.created
            token = decision.run.ownership_token
            assert token is not None
            attempt = Attempt(decision.run.id, token)
            acquire_run(session, attempt.run_id, token, now=now)
            session.commit()
            return attempt

    def capture(
        self,
        attempt: Attempt,
        items: dict[uuid.UUID, list[RSSItem]],
        *,
        now: datetime.datetime = START,
    ) -> dict:
        return _capture_selected_feeds(
            attempt.run_id,
            attempt.token,
            session_factory=self.session,
            provider_factory=lambda source: FakeRSSProvider(items.get(source.id, [])),
            clock=lambda: now,
        )

    def admit(self, attempt: Attempt, *, now: datetime.datetime = START) -> tuple[uuid.UUID, ...]:
        return _admit_pending(
            attempt.run_id, attempt.token, session_factory=self.session, admitted_at=now
        )

    def freeze(
        self,
        attempt: Attempt,
        admitted: Sequence[uuid.UUID],
        *,
        now: datetime.datetime = START,
    ) -> tuple[uuid.UUID, ...]:
        return _freeze_enrichment_scope(
            attempt.run_id,
            attempt.token,
            admitted,
            session_factory=self.session,
            now=now,
        )

    def finish(self, attempt: Attempt, *, failed: bool = False) -> None:
        with self.session() as session:
            finish_run(
                session,
                attempt.run_id,
                attempt.token,
                state="failed" if failed else "succeeded",
                error={"code": "synthetic_interruption"} if failed else None,
            )
            session.commit()

    def retry(self, attempt: Attempt, *, now: datetime.datetime) -> Attempt:
        with self.session() as session:
            workspace = session.get(PersonalWorkspace, self.workspace_id)
            decision = retry_run(session, workspace, attempt.run_id, now=now)
            token = decision.run.ownership_token
            assert token is not None and token != attempt.token
            acquire_run(session, attempt.run_id, token, now=now)
            session.commit()
            return Attempt(attempt.run_id, token)


@pytest.fixture
def desk() -> Iterator[Desk]:
    with migrated_disposable_engine() as engine:
        yield Desk(engine)


def items(batch: str, count: int, *, published: bool = True) -> list[RSSItem]:
    return [
        RSSItem(
            guid=f"{batch}-{index}",
            title=f"Synthetic article {batch} {index}",
            url=f"https://offline.example/{batch}/{index:04d}",
            summary=f"Retained RSS summary for {batch} article {index}.",
            published_at=START - datetime.timedelta(minutes=index) if published else None,
            provider_name="synthetic-rss",
        )
        for index in range(count)
    ]


def test_p3_01_twenty_admitted_ten_pending_and_twenty_frozen(desk: Desk) -> None:
    profile_id = desk.configure(daily=20, admission=20, enrichment=20, assisted=True)
    attempt = desk.start()
    captured_items = items("p301", 30)
    captured_items[0] = replace(captured_items[0], title="T" * 600, summary="S" * 3_000)
    coverage = desk.capture(attempt, {desk.source_ids[0]: captured_items})
    admitted = desk.admit(attempt)
    enrichment = desk.freeze(attempt, admitted)
    assert coverage["articles_captured"] == 30
    assert len(admitted) == len(enrichment) == 20
    with desk.session() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 20
        assert (
            session.scalar(
                select(func.count())
                .select_from(PersonalCapture)
                .where(PersonalCapture.admitted_at.is_(None))
            )
            == 10
        )
        run = session.get(PersonalRun, attempt.run_id)
        assert run.profile_revision_id == profile_id
        assert run.admitted_article_ids == list(admitted)
        assert run.enrichment_article_ids == list(enrichment)
        assert run.coverage["pending_total"] == 10
        assert run.stage_results["scope"]["articles_selected_for_enrichment"] == 20
        assert "embedding" not in run.stage_results
        bounded_capture = session.scalar(
            select(PersonalCapture).where(PersonalCapture.canonical_url == captured_items[0].url)
        )
        assert bounded_capture.truncated
        assert bounded_capture.receipt["field_truncations"] == ["title", "summary"]
        article = session.get(Article, bounded_capture.article_id)
        assert len(article.title) == 512 and len(article.summary) == 2_000
        assert article.raw_payload["personal_truncated"] is True
        revision = session.scalar(
            select(PersonalArticleRevision).where(
                PersonalArticleRevision.run_id == attempt.run_id,
                PersonalArticleRevision.article_id == article.id,
            )
        )
        assert revision.truncated


def test_p3_02_concurrent_duplicate_capture_and_rolled_back_admission(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.personal.coordinator as coordinator

    desk.configure(daily=2, admission=2)
    attempt = desk.start()
    first, second = items("p302", 2)
    variant = RSSItem(
        guid="variant",
        title="Replacement must not win",
        url=first.url + "/?utm_source=repeat",
        published_at=None,
    )
    batch = {desk.source_ids[0]: [first, variant, second, first]}
    barrier = Barrier(2)

    def capture_again() -> dict:
        barrier.wait(timeout=10)
        return desk.capture(attempt, batch)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(capture_again) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert sum(value["articles_captured"] for value in results) == 2
    original = coordinator._article_for_capture
    call_count = 0

    def fail_after_article_insert(session: Session, candidate: PersonalCapture) -> uuid.UUID:
        nonlocal call_count
        call_count += 1
        article_id = original(session, candidate)
        if call_count == 2:
            raise RuntimeError("synthetic crash before admission commit")
        return article_id

    with monkeypatch.context() as scoped:
        scoped.setattr(coordinator, "_article_for_capture", fail_after_article_insert)
        with pytest.raises(RuntimeError, match="before admission commit"):
            desk.admit(attempt)
    with desk.session() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(PersonalCapture)
                .where(PersonalCapture.admitted_at.is_not(None))
            )
            == 0
        )
    barrier = Barrier(2)

    def admit_again() -> tuple[uuid.UUID, ...]:
        barrier.wait(timeout=10)
        return desk.admit(attempt)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(admit_again) for _ in range(2)]
        admissions = [future.result(timeout=30) for future in futures]
    assert admissions[0] == admissions[1]
    assert len(admissions[0]) == 2
    with desk.session() as session:
        captures = list(session.scalars(select(PersonalCapture)))
        assert len(captures) == 2
        assert len({capture.article_id for capture in captures}) == 2
        assert sum(capture.admission_charged for capture in captures) == 2
        assert sorted(capture.receipt["admission_ordinal"] for capture in captures) == [0, 1]
        assert {capture.title for capture in captures} == {first.title, second.title}
        assert session.scalar(select(func.count()).select_from(Article)) == 2


def test_p3_03_round_robin_order_survives_new_sessions(desk: Desk) -> None:
    a, b = desk.source_ids[:2]
    desk.configure(sources=[b, a], daily=4, admission=4)
    attempt = desk.start()
    desk.capture(attempt, {a: items("p303-a", 20), b: items("p303-b", 2)})
    admitted = desk.admit(attempt)
    with desk.session() as session:
        by_id = {article.id: article.url for article in session.scalars(select(Article))}
        assert [by_id[article_id] for article_id in admitted] == [
            "https://offline.example/p303-a/0000",
            "https://offline.example/p303-b/0000",
            "https://offline.example/p303-a/0001",
            "https://offline.example/p303-b/0001",
        ]
    desk.engine.dispose()
    assert desk.admit(attempt) == admitted
    with desk.session() as session:
        rows = session.scalars(
            select(PersonalCapture).where(PersonalCapture.admitted_at.is_not(None))
        )
        assert {row.article_id: row.receipt["admission_ordinal"] for row in rows} == {
            article_id: index for index, article_id in enumerate(admitted)
        }


def test_p3_03_older_capture_batch_precedes_newer_and_unknown_dates_follow_known(
    desk: Desk,
) -> None:
    source = desk.source_ids[0]
    desk.configure(admission=4)
    attempt = desk.start()
    old_unknown = items("p303-old-unknown", 1, published=False)[0]
    old_known = items("p303-old-known", 1)[0]
    new_known = items("p303-new-known", 1)[0]
    desk.capture(attempt, {source: [old_unknown, old_known]}, now=START)
    desk.capture(attempt, {source: [new_known]}, now=START + datetime.timedelta(minutes=1))
    admitted = desk.admit(attempt)
    with desk.session() as session:
        urls = {article.id: article.url for article in session.scalars(select(Article))}
        assert [urls[article_id] for article_id in admitted] == [
            old_known.url,
            old_unknown.url,
            new_known.url,
        ]


def test_p3_04_retained_candidate_survives_rotation_and_disabled_source(desk: Desk) -> None:
    a, b = desk.source_ids[:2]
    retained = items("p304-retained", 1, published=False)[0]
    desk.configure(sources=[a], admission=0)
    first = desk.start()
    desk.capture(first, {a: [retained]})
    assert desk.admit(first) == ()
    assert desk.freeze(first, ()) == ()
    desk.finish(first)
    desk.configure(sources=[b])
    disabled_day = START + datetime.timedelta(days=1)
    second = desk.start(disabled_day)
    desk.capture(second, {b: []}, now=disabled_day)
    assert desk.admit(second, now=disabled_day) == ()
    desk.freeze(second, (), now=disabled_day)
    desk.finish(second)
    desk.configure(sources=[a])
    enabled_day = START + datetime.timedelta(days=2)
    third = desk.start(enabled_day)
    admitted = desk.admit(third, now=enabled_day)  # drain before the rotated feed is requested
    desk.capture(third, {a: []}, now=enabled_day)
    assert len(admitted) == 1
    with desk.session() as session:
        candidate = session.scalar(select(PersonalCapture))
        article = session.get(Article, admitted[0])
        assert candidate.run_id == first.run_id
        assert candidate.captured_at == START
        assert candidate.admitted_run_id == third.run_id
        assert article.title == retained.title
        assert article.summary == retained.summary
        assert article.published_at is None


def test_p3_05_full_two_thousand_queue_pauses_without_requesting_unknown_input(desk: Desk) -> None:
    desk.configure(sources=desk.source_ids, admission=0)
    attempt = desk.start()
    desk.capture(
        attempt,
        {source: items(f"p305-{index}", 500) for index, source in enumerate(desk.source_ids)},
    )

    def unexpected_request(_source: Source):
        pytest.fail("full pending queue must not invoke its RSS provider")

    coverage = _capture_selected_feeds(
        attempt.run_id,
        attempt.token,
        session_factory=desk.session,
        provider_factory=unexpected_request,
        clock=lambda: START + datetime.timedelta(minutes=1),
    )
    assert coverage["pending_total"] == 2_000
    assert coverage["feeds_attempted"] == coverage["items_fetched"] == 0
    assert coverage["feeds_paused"] == 4
    with desk.session() as session:
        assert session.scalar(select(func.count()).select_from(PersonalCapture)) == 2_000
        paused = list(
            session.scalars(
                select(PersonalFeedReceipt).where(
                    PersonalFeedReceipt.status == "collection_paused_pending_capacity"
                )
            )
        )
        assert len(paused) == 4
        assert all(receipt.details["entries_observed"] is None for receipt in paused)
        assert all(receipt.details["bounds_reached"] == ["pending_capacity"] for receipt in paused)


@pytest.mark.parametrize("oversized", [False, True])
def test_p3_05_transport_and_entry_bounds_persist_truthful_receipts(
    desk: Desk, monkeypatch: pytest.MonkeyPatch, oversized: bool
) -> None:
    desk.configure(admission=0)
    attempt = desk.start()
    payload = (
        b"<rss><channel>"
        + b"".join(
            f"<item><link>https://offline.example/p305/{index}</link></item>".encode()
            for index in range(501)
        )
        + b"</channel></rss>"
    )
    if oversized:
        payload += b" " * MAX_RSS_RESPONSE_BYTES
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: io.BytesIO(payload))
    coverage = _capture_selected_feeds(
        attempt.run_id,
        attempt.token,
        session_factory=desk.session,
        provider_factory=lambda _source: HttpRSSProvider(),
        clock=lambda: START,
    )
    with desk.session() as session:
        receipt = session.scalar(select(PersonalFeedReceipt))
        assert receipt.status == ("response_byte_limit" if oversized else "entry_limit_reached")
        assert receipt.details["total_entries_known"] is False
        assert receipt.details["entries_observed"] == (None if oversized else 501)
        assert receipt.details["entries_processed"] == (0 if oversized else 500)
        assert coverage["pending_total"] == (0 if oversized else 500)
        assert session.scalar(select(func.count()).select_from(Article)) == 0


def test_p3_06_midnight_retry_keeps_run_identity_and_charges_actual_admission_day(
    desk: Desk,
) -> None:
    before = datetime.datetime(2026, 9, 20, 6, 59, tzinfo=UTC)
    after = datetime.datetime(2026, 9, 20, 7, 1, tzinfo=UTC)
    desk.configure(daily=2, admission=3)
    attempt = desk.start(before)
    desk.capture(attempt, {desk.source_ids[0]: items("p306", 5)}, now=before)
    first_ids = desk.admit(attempt, now=before)
    assert len(first_ids) == 2
    desk.finish(attempt, failed=True)
    resumed = desk.retry(attempt, now=after)
    resumed_ids = desk.admit(resumed, now=after)
    assert len(resumed_ids) == 3
    assert resumed_ids[:2] == first_ids
    desk.freeze(resumed, resumed_ids, now=after)
    assert desk.admit(resumed, now=after + datetime.timedelta(days=1)) == resumed_ids
    desk.finish(resumed)
    next_run = desk.start(after + datetime.timedelta(minutes=1))
    next_ids = desk.admit(next_run, now=after + datetime.timedelta(minutes=1))
    assert len(next_ids) == 1
    with desk.session() as session:
        old_run = session.get(PersonalRun, attempt.run_id)
        new_run = session.get(PersonalRun, next_run.run_id)
        assert old_run.local_date == datetime.date(2026, 9, 19)
        assert old_run.attempt == 2
        assert new_run.local_date == datetime.date(2026, 9, 20)
        captures = list(session.scalars(select(PersonalCapture)))
        assert sum(row.admitted_at == before for row in captures) == 2
        assert sum(row.admitted_at is not None for row in captures) == 4
        assert sum(row.admitted_at is None for row in captures) == 1


def test_p3_06_lower_daily_ceiling_applies_now_and_frozen_run_ceiling_cannot_grow(
    desk: Desk,
) -> None:
    source = desk.source_ids[0]
    original_profile = desk.configure(daily=4, admission=3, enrichment=2, assisted=True)
    attempt = desk.start()
    desk.capture(attempt, {source: items("p306-first", 2)})
    initial = desk.admit(attempt)
    assert len(initial) == 2
    desk.configure(daily=1, admission=50, enrichment=50, assisted=True)
    desk.capture(
        attempt, {source: items("p306-later", 5)}, now=START + datetime.timedelta(minutes=1)
    )
    assert desk.admit(attempt) == initial
    desk.configure(daily=50, admission=50, enrichment=50, assisted=True)
    admitted = desk.admit(attempt)
    assert len(admitted) == 3
    assert len(desk.freeze(attempt, admitted)) == 2
    with desk.session() as session:
        run = session.get(PersonalRun, attempt.run_id)
        assert run.profile_revision_id == original_profile
        profile = session.get(PersonalProfileRevision, original_profile)
        assert profile.settings["run_article_limit"] == 3
        assert profile.settings["enrichment_article_limit"] == 2


def test_p3_13_old_raw_scope_does_not_reduce_new_admission_and_capacity_deferral_transfers(
    desk: Desk,
) -> None:
    source = desk.source_ids[0]
    desk.configure()
    old_run = desk.start()
    desk.capture(old_run, {source: items("p313-old", 100)})
    old_ids = desk.admit(old_run)
    assert len(old_ids) == 100 and desk.freeze(old_run, old_ids) == ()
    desk.finish(old_run)
    desk.configure(assisted=True)
    second_day = START + datetime.timedelta(days=1)
    new_run = desk.start(second_day)
    desk.capture(new_run, {source: items("p313-new", 100)}, now=second_day)
    new_ids = desk.admit(new_run, now=second_day)
    scope = desk.freeze(new_run, new_ids, now=second_day)
    assert len(new_ids) == len(scope) == 100
    assert scope == tuple(sorted(old_ids))
    assert not set(new_ids) & set(scope)
    with desk.session() as session:
        old_rows = list(
            session.scalars(select(PersonalCapture).where(PersonalCapture.article_id.in_(old_ids)))
        )
        assert all(
            row.admitted_at == START and row.admitted_run_id == old_run.run_id for row in old_rows
        )
        assert all(row.processing_run_id == new_run.run_id for row in old_rows)
        new_rows = list(
            session.scalars(select(PersonalCapture).where(PersonalCapture.article_id.in_(new_ids)))
        )
        assert all(row.enrichment_state == "deferred_enrichment_capacity" for row in new_rows)
        assert session.scalar(select(func.count()).select_from(PersonalEnrichmentTransfer)) == 100
        assert session.scalar(select(func.count()).select_from(Article)) == 200
    desk.finish(new_run)
    third_day = START + datetime.timedelta(days=2)
    later_run = desk.start(third_day)
    assert desk.admit(later_run, now=third_day) == ()
    later_scope = desk.freeze(later_run, (), now=third_day)
    assert later_scope == tuple(sorted(new_ids))
    with desk.session() as session:
        transfers = list(
            session.scalars(
                select(PersonalEnrichmentTransfer).where(
                    PersonalEnrichmentTransfer.to_run_id == later_run.run_id
                )
            )
        )
        assert len(transfers) == 100
        assert all(row.reason == "deferred_enrichment_capacity" for row in transfers)
        assert session.scalar(select(func.count()).select_from(Article)) == 200


def test_p3_14_failed_retry_scope_and_exhausted_failure_are_never_implicitly_transferred(
    desk: Desk,
) -> None:
    source = desk.source_ids[0]
    desk.configure(assisted=True)
    failed = desk.start()
    desk.capture(failed, {source: items("p314-hard-failure", 1)})
    failed_ids = desk.admit(failed)
    assert desk.freeze(failed, failed_ids) == failed_ids
    _mark_stage_failure(
        failed.run_id, failed.token, session_factory=desk.session, code="synthetic_provider_failure"
    )
    next_day = START + datetime.timedelta(days=1)
    next_run = desk.start(next_day)
    assert desk.freeze(next_run, (), now=next_day) == ()
    desk.finish(next_run)
    for index in range(2):
        failed = desk.retry(failed, now=next_day + datetime.timedelta(minutes=index + 1))
        assert desk.freeze(failed, failed_ids, now=next_day) == failed_ids
        _mark_stage_failure(
            failed.run_id,
            failed.token,
            session_factory=desk.session,
            code="synthetic_provider_failure",
        )
    with pytest.raises(PersonalRunConflict, match="exhausted"):
        desk.retry(failed, now=next_day + datetime.timedelta(minutes=5))
    third_day = START + datetime.timedelta(days=2)
    third_run = desk.start(third_day)
    desk.capture(third_run, {source: items("p314-fresh", 1)}, now=third_day)
    fresh = desk.admit(third_run, now=third_day)
    assert desk.freeze(third_run, fresh, now=third_day) == fresh
    with desk.session() as session:
        assert session.scalar(select(func.count()).select_from(PersonalEnrichmentTransfer)) == 0
        failed_row = session.scalar(
            select(PersonalCapture).where(PersonalCapture.article_id == failed_ids[0])
        )
        assert failed_row.processing_run_id == failed.run_id
        assert failed_row.enrichment_state == "provider_failed"
        assert session.get(PersonalRun, failed.run_id).attempt == 3
        assert session.get(Article, failed_ids[0]) is not None


def test_p3_13_existing_raw_from_disabled_feed_stays_readable_and_outside_new_scope(
    desk: Desk,
) -> None:
    a, b = desk.source_ids[:2]
    desk.configure(sources=[a, b])
    previous = desk.start()
    desk.capture(previous, {a: items("p313-disabled-feed", 1), b: items("p313-enabled-feed", 1)})
    admitted = desk.admit(previous)
    desk.freeze(previous, admitted)
    desk.finish(previous)
    desk.configure(sources=[b], assisted=True)
    next_day = START + datetime.timedelta(days=1)
    next_run = desk.start(next_day)
    scope = desk.freeze(next_run, (), now=next_day)
    with desk.session() as session:
        captures = {row.source_id: row for row in session.scalars(select(PersonalCapture))}
        assert scope == (captures[b].article_id,)
        assert captures[a].processing_run_id == previous.run_id
        assert captures[a].enrichment_state == "disabled_by_profile"
        assert session.get(Article, captures[a].article_id) is not None
        assert session.scalar(select(func.count()).select_from(PersonalEnrichmentTransfer)) == 1
