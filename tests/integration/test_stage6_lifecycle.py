"""Stage 6 report lifecycle against a live, disposable Postgres.

What can only be checked against a real Postgres lives here: the advisory-lock version allocation
under genuine concurrency, the partial unique indexes, the immutability of a published row across a
regeneration, and the stale-marking join that resolves a claim citation back through
``claim_evidence`` -> ``article`` evidence -> ``event_articles`` to an event.

Database hygiene (per the Stage 6 workflow rules): never the default ``news`` database. This module
creates and owns a throwaway ``nip_stage6_lifecycle_<hex>`` database on ``localhost:55432``, creates
only the tables the lifecycle touches (no PostGIS, no full migration chain), and drops it in a
``finally`` -- then asserts it is gone, so a crashed run cannot leak.
"""

from __future__ import annotations

import datetime
import threading
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.models.core import (
    REPORT_CONTENT_POLICY_PREDICTION_BACKED,
    Article,
    Claim,
    ClaimEvidence,
    Event,
    EventArticle,
    EvidenceItem,
    LLMRun,
    Report,
    ReportSection,
    Source,
    User,
)
from services.reports.composition import DraftBlock
from services.reports.grounding import (
    GateOutcome,
    GroundingGateResult,
    SectionGroundingResult,
    SectionGroundingStatus,
)
from services.reports.lifecycle import (
    SECTION_LIST_LIMIT,
    CannotPublishError,
    ReportLifecycleRepository,
    ReportReadOverflowError,
    ReportStatus,
    SectionValidationError,
    persist_daily_brief,
)
from services.reports.material import FINAL_DISCLAIMER, SectionKind
from tests.integration._stage6_db import (
    disposable_database,
    require_disposable_postgres,
    url_for,
)

pytestmark = pytest.mark.integration

UTC = datetime.UTC
_LC_PREFIX = "nip_stage6_lifecycle_"

#: FK-closed set of tables the lifecycle touches. `users`/`llm_runs` are FK targets only (no rows).
_LIFECYCLE_TABLES = [
    User.__table__,
    LLMRun.__table__,
    Source.__table__,
    Article.__table__,
    Event.__table__,
    EventArticle.__table__,
    EvidenceItem.__table__,
    Claim.__table__,
    ClaimEvidence.__table__,
    Report.__table__,
    ReportSection.__table__,
]

PASSED = SectionGroundingStatus.PASSED
FAILED = SectionGroundingStatus.FAILED


@pytest.fixture
def engine():
    """A throwaway database with only the lifecycle tables, dropped and leak-checked on exit."""
    require_disposable_postgres()
    with disposable_database(_LC_PREFIX) as name:
        engine = create_engine(url_for(name))
        try:
            from db.base import Base

            Base.metadata.create_all(bind=engine, tables=_LIFECYCLE_TABLES)
            yield engine
        finally:
            engine.dispose()


class CountingSession(Session):
    """A Session that counts commits/rollbacks, to prove the repository issues neither itself."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.commit_calls = 0
        self.rollback_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1
        super().commit()

    def rollback(self) -> None:
        self.rollback_calls += 1
        super().rollback()


# --------------------------------------------------------------------------------------
# Gate-result builders (DB-free, hand-built for full control over version-to-version text)
# --------------------------------------------------------------------------------------


def _block(text: str, *claim_ids: uuid.UUID) -> DraftBlock:
    return DraftBlock(text=text, claim_ids=tuple(claim_ids))


def _sect(
    kind: SectionKind,
    order: int,
    title: str,
    *,
    status: SectionGroundingStatus = PASSED,
    blocks: tuple[DraftBlock, ...] = (),
    final_text: str | None = None,
    blocks_publication: bool = False,
) -> SectionGroundingResult:
    text = final_text if final_text is not None else "\n\n".join(b.text for b in blocks)
    return SectionGroundingResult(
        kind=kind,
        order=order,
        title=title,
        status=status,
        final_text=text,
        final_blocks=blocks,
        verdicts=(),
        copyright_findings=(),
        consistency_findings=(),
        composition_attempts=(),
        regeneration_attempts=(),
        grounding_attempts=(),
        claims_before=0,
        claims_after=0,
        regenerated=False,
        blocks_publication=blocks_publication,
        transformations=(),
    )


def _disc(order: int) -> SectionGroundingResult:
    return _sect(SectionKind.DISCLAIMER, order, "Disclaimer", final_text=FINAL_DISCLAIMER)


def _gate(
    brief_date: datetime.date,
    *sections: SectionGroundingResult,
    outcome: GateOutcome = GateOutcome.PASS,
) -> GroundingGateResult:
    return GroundingGateResult(brief_date=brief_date, sections=sections, outcome=outcome)


def _passing_gate(brief_date: datetime.date, body: str, *claim_ids: uuid.UUID) -> GroundingGateResult:
    return _gate(
        brief_date,
        _sect(SectionKind.EXECUTIVE_SUMMARY, 1, "Executive Summary", blocks=(_block(body, *claim_ids),)),
        _disc(2),
    )


def _blocked_gate(brief_date: datetime.date) -> GroundingGateResult:
    return _gate(
        brief_date,
        _sect(
            SectionKind.EXECUTIVE_SUMMARY,
            1,
            "Executive Summary",
            status=FAILED,
            final_text="[grounding_failed] unsupported claim",
            blocks_publication=True,
        ),
        _disc(2),
        outcome=GateOutcome.BLOCKED,
    )


def _seed_claims(session: Session, *claim_ids: uuid.UUID) -> None:
    """Insert real Claim rows for every id a successful persistence will cite.

    The persistence boundary now requires every evidence ref to be a real Claim id (``evidence_refs``
    is ``ARRAY(UUID)`` with no FK to enforce it), so these hand-built passing gates -- which cite
    ad-hoc UUIDs -- must have their claims present in the same transaction before they persist.
    """
    for claim_id in claim_ids:
        session.add(Claim(id=claim_id, claim_text="Seeded claim.", confidence_score=0.9))
    session.flush()


# --------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------


def test_full_publish_sequence_persists_and_never_commits(engine) -> None:
    brief_date = datetime.date(2026, 7, 14)
    claim_id = uuid.uuid4()
    gate = _passing_gate(brief_date, "Rates held steady.", claim_id)

    with CountingSession(engine) as session:
        _seed_claims(session, claim_id)
        snap = persist_daily_brief(
            session, gate, content_policy=REPORT_CONTENT_POLICY_PREDICTION_BACKED
        )
        # The repository flushes but issues no commit/rollback of its own.
        assert session.commit_calls == 0
        assert session.rollback_calls == 0
        session.commit()

    assert snap.status == "published"
    assert snap.version == 1
    assert snap.user_id is None
    assert snap.brief_date == brief_date

    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        sections = repo.report_sections(snap.id)
    assert [s.section_order for s in sections] == [1, 2]
    assert sections[0].body == "Rates held steady."
    assert sections[0].evidence_refs == (claim_id,)
    assert sections[0].grounding_status == "passed"
    assert sections[1].body == FINAL_DISCLAIMER
    assert sections[1].evidence_refs == ()  # deterministic section fabricates no citation


def test_concurrent_version_allocation_yields_1_and_2_without_collision(engine) -> None:
    brief_date = datetime.date(2026, 7, 20)
    barrier = threading.Barrier(2)
    results: dict[int, int] = {}
    errors: dict[int, BaseException] = {}

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=15)
            with Session(engine) as session:
                repo = ReportLifecycleRepository(session)
                snap = repo.create_generating_daily_brief(
                    brief_date=brief_date, title="brief", change_reason="raced rerun"
                )
                session.commit()
                results[idx] = snap.version
        except BaseException as exc:  # noqa: BLE001 -- surfaced via the errors dict
            errors[idx] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert set(results.values()) == {1, 2}  # serialised by the advisory lock, no collision

    with Session(engine) as session:
        versions = ReportLifecycleRepository(session).daily_brief_versions(brief_date)
    assert [v.version for v in versions] == [2, 1]  # both rows exist, deterministic desc order


def test_a_published_version_is_byte_for_byte_immutable_through_regeneration(engine) -> None:
    brief_date = datetime.date(2026, 7, 15)
    v1_claim, v2_claim = uuid.uuid4(), uuid.uuid4()
    with Session(engine) as session:
        _seed_claims(session, v1_claim)
        v1 = persist_daily_brief(
            session,
            _passing_gate(brief_date, "First body.", v1_claim),
            content_policy=REPORT_CONTENT_POLICY_PREDICTION_BACKED,
        )
        session.commit()

    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        v1_before = repo.daily_brief_version(brief_date, 1)
        v1_sections_before = repo.report_sections(v1.id)

    with Session(engine) as session:
        _seed_claims(session, v2_claim)
        v2 = persist_daily_brief(
            session,
            _passing_gate(brief_date, "A completely different second body.", v2_claim),
            change_reason="reprocessed upstream event",
            content_policy=REPORT_CONTENT_POLICY_PREDICTION_BACKED,
        )
        session.commit()

    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        v1_after = repo.daily_brief_version(brief_date, 1)
        v1_sections_after = repo.report_sections(v1.id)
        latest = repo.latest_published_daily_brief_by_date(brief_date)

    assert v2.version == 2
    assert v2.change_reason == "reprocessed upstream event"
    # The prior published row is untouched -- status, version, reason, and every section byte.
    assert v1_after == v1_before
    assert v1_after.status == "published"
    assert v1_after.version == 1
    assert v1_after.change_reason is None
    assert v1_sections_after == v1_sections_before
    assert latest.version == 2  # the newer published version is now served by default


def test_latest_published_ignores_a_higher_failed_or_in_flight_version(engine) -> None:
    brief_date = datetime.date(2026, 7, 16)
    pub_claim = uuid.uuid4()
    with Session(engine) as session:
        _seed_claims(session, pub_claim)
        persist_daily_brief(
            session,
            _passing_gate(brief_date, "Published one.", pub_claim),
            content_policy=REPORT_CONTENT_POLICY_PREDICTION_BACKED,
        )
        session.commit()

    with Session(engine) as session:  # version 2: blocked -> failed
        v2 = persist_daily_brief(
            session,
            _blocked_gate(brief_date),
            change_reason="rerun",
            content_policy=REPORT_CONTENT_POLICY_PREDICTION_BACKED,
        )
        session.commit()
    assert v2.status == "failed"

    with Session(engine) as session:  # version 3: still generating (in-flight)
        repo = ReportLifecycleRepository(session)
        v3 = repo.create_generating_daily_brief(
            brief_date=brief_date, title="brief", change_reason="third"
        )
        session.commit()
    assert v3.version == 3 and v3.status == "generating"

    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        assert repo.latest_published_daily_brief_by_date(brief_date).version == 1
        assert repo.latest_published_daily_brief().version == 1
        assert [v.version for v in repo.daily_brief_versions(brief_date)] == [3, 2, 1]


def test_a_blocked_report_persists_its_sections_but_cannot_publish(engine) -> None:
    brief_date = datetime.date(2026, 7, 17)
    blocked = _blocked_gate(brief_date)
    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        snap = repo.create_generating_daily_brief(brief_date=brief_date, title="brief")
        repo.transition(snap.id, ReportStatus.GROUNDING_CHECK)
        repo.persist_sections(snap.id, blocked)  # audit-safe outcomes are retained
        with pytest.raises(CannotPublishError):
            repo.publish(snap.id, blocked)  # fail closed
        failed = repo.transition(snap.id, ReportStatus.FAILED)
        session.commit()

    assert failed.status == "failed"
    with Session(engine) as session:
        sections = ReportLifecycleRepository(session).report_sections(snap.id)
    assert [s.grounding_status for s in sections] == ["failed", "passed"]
    # And a failed version is not served by default.
    with Session(engine) as session:
        assert (
            ReportLifecycleRepository(session).latest_published_daily_brief_by_date(brief_date)
            is None
        )


def test_persisted_failed_sections_cannot_be_published_by_a_different_passing_gate(engine) -> None:
    # publish() trusts the persisted rows, not the gate argument: a blocked report's failed sections
    # stay unpublishable even if a fabricated passing gate with the same orders is handed to it.
    brief_date = datetime.date(2026, 7, 18)
    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        snap = repo.create_generating_daily_brief(brief_date=brief_date, title="brief")
        repo.transition(snap.id, ReportStatus.GROUNDING_CHECK)
        repo.persist_sections(snap.id, _blocked_gate(brief_date))
        passing_lie = _passing_gate(brief_date, "Looks fine.", uuid.uuid4())
        with pytest.raises(CannotPublishError):
            repo.publish(snap.id, passing_lie)
        session.rollback()


def _seed_event_with_cited_claim(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """Event -> article -> article EvidenceItem -> Claim, wired so a brief can cite the claim."""
    source = Source(name="Reuters", feed_url=f"https://feed/{uuid.uuid4()}", authority_score=0.8)
    session.add(source)
    session.flush()
    article = Article(
        source_id=source.id,
        url=f"https://x/{uuid.uuid4()}",
        url_hash=uuid.uuid4().hex,
        title="Bank under pressure",
        summary="A concise summary.",
        body="Body text.",
        published_at=datetime.datetime(2026, 7, 14, 4, tzinfo=UTC),
    )
    session.add(article)
    session.flush()
    event = Event(title="Regional bank stress", hotness_score=70.0)
    session.add(event)
    session.flush()
    session.add(EventArticle(event_id=event.id, article_id=article.id))
    evidence = EvidenceItem(
        source_type="article", source_id=str(article.id), title="Bank under pressure"
    )
    session.add(evidence)
    session.flush()
    claim = Claim(claim_text="The bank faces a liquidity squeeze.", confidence_score=0.9)
    session.add(claim)
    session.flush()
    session.add(
        ClaimEvidence(claim_id=claim.id, evidence_item_id=evidence.id, support_type="supports")
    )
    session.commit()
    return event.id, claim.id


def test_stale_marking_follows_the_citation_join_and_spares_unrelated_rows(engine) -> None:
    with Session(engine) as session:
        event_id, claim_id = _seed_event_with_cited_claim(session)

    # A published brief that cites the event's claim.
    with Session(engine) as session:
        citing = persist_daily_brief(
            session,
            _passing_gate(datetime.date(2026, 7, 14), "Cites the claim.", claim_id),
            content_policy=REPORT_CONTENT_POLICY_PREDICTION_BACKED,
        )
        session.commit()

    # A published brief that cites something else entirely.
    unrelated_claim = uuid.uuid4()
    with Session(engine) as session:
        _seed_claims(session, unrelated_claim)
        unrelated = persist_daily_brief(
            session,
            _passing_gate(datetime.date(2026, 7, 15), "Unrelated.", unrelated_claim),
            content_policy=REPORT_CONTENT_POLICY_PREDICTION_BACKED,
        )
        session.commit()

    # An unpublished (grounding_check) brief that DOES cite the claim -- must be spared.
    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        inflight = repo.create_generating_daily_brief(
            brief_date=datetime.date(2026, 7, 16), title="brief"
        )
        repo.transition(inflight.id, ReportStatus.GROUNDING_CHECK)
        repo.persist_sections(
            inflight.id, _passing_gate(datetime.date(2026, 7, 16), "Cites the claim.", claim_id)
        )
        session.commit()

    # A published report directly about the event.
    with Session(engine) as session:
        event_report = Report(
            user_id=None,
            report_type="event_report",
            event_id=event_id,
            title="Event report",
            status="published",
            version=1,
            stale=False,
        )
        session.add(event_report)
        session.commit()
        event_report_id = event_report.id

    with CountingSession(engine) as session:
        repo = ReportLifecycleRepository(session)
        count = repo.mark_published_reports_stale_for_event(event_id)
        assert session.commit_calls == 0 and session.rollback_calls == 0
        session.commit()
    assert count == 2  # the citing brief and the direct event report

    # Idempotent: a second call marks nothing new and returns the same deterministic count.
    with Session(engine) as session:
        assert (
            ReportLifecycleRepository(session).mark_published_reports_stale_for_event(event_id) == 2
        )

    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        assert repo.get_report(citing.id).stale is True
        assert repo.get_report(event_report_id).stale is True
        assert repo.get_report(unrelated.id).stale is False  # cites a different claim
        assert repo.get_report(inflight.id).stale is False  # unpublished, untouched


def test_a_non_claim_evidence_ref_cannot_persist_but_real_claims_do(engine) -> None:
    # ReportSection.evidence_refs is ARRAY(UUID) with no FK, so absent a boundary check a
    # historical-episode-style UUID would persist into a citation. The check refuses it -- with no
    # partial section write and no status change -- while the same section citing a real Claim ships.
    brief_date = datetime.date(2026, 7, 19)
    real_claim = uuid.uuid4()
    episode_like = uuid.uuid4()  # a UUID that is not a Claim (e.g. a historical episode id)

    with Session(engine) as session:
        _seed_claims(session, real_claim)
        repo = ReportLifecycleRepository(session)
        snap = repo.create_generating_daily_brief(brief_date=brief_date, title="brief")
        repo.transition(snap.id, ReportStatus.GROUNDING_CHECK)
        session.commit()

    # The non-Claim ref is refused; the rollback undoes nothing that was written (there was nothing).
    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        bad = _gate(
            brief_date,
            _sect(
                SectionKind.EXECUTIVE_SUMMARY,
                1,
                "Executive Summary",
                blocks=(_block("Body.", real_claim, episode_like),),
            ),
            _disc(2),
        )
        with pytest.raises(SectionValidationError):
            repo.persist_sections(snap.id, bad)
        session.rollback()

    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        assert repo.report_sections(snap.id) == ()  # nothing persisted
        assert repo.get_report(snap.id).status == "grounding_check"  # status untouched

    # The same section citing only the real claim persists cleanly.
    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        good = _gate(
            brief_date,
            _sect(
                SectionKind.EXECUTIVE_SUMMARY,
                1,
                "Executive Summary",
                blocks=(_block("Body.", real_claim),),
            ),
            _disc(2),
        )
        sections = repo.persist_sections(snap.id, good)
        session.commit()
    assert [s.section_order for s in sections] == [1, 2]
    assert sections[0].evidence_refs == (real_claim,)


def test_report_sections_read_fails_loud_on_a_live_overflow(engine) -> None:
    # A report with more than SECTION_LIST_LIMIT sections is an upstream fault; the bounded read
    # over-reads by one and refuses rather than returning a silently truncated body.
    brief_date = datetime.date(2026, 7, 21)
    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        snap = repo.create_generating_daily_brief(brief_date=brief_date, title="brief")
        for order in range(SECTION_LIST_LIMIT + 1):
            session.add(
                ReportSection(
                    report_id=snap.id,
                    section_order=order,
                    title="t",
                    body="b",
                    grounding_status="passed",
                )
            )
        session.commit()
        with pytest.raises(ReportReadOverflowError):
            repo.report_sections(snap.id)

    # Exactly the limit is returned in full, not refused.
    other_date = datetime.date(2026, 7, 22)
    with Session(engine) as session:
        repo = ReportLifecycleRepository(session)
        other = repo.create_generating_daily_brief(brief_date=other_date, title="brief")
        for order in range(SECTION_LIST_LIMIT):
            session.add(
                ReportSection(
                    report_id=other.id,
                    section_order=order,
                    title="t",
                    body="b",
                    grounding_status="passed",
                )
            )
        session.commit()
        assert len(repo.report_sections(other.id)) == SECTION_LIST_LIMIT
