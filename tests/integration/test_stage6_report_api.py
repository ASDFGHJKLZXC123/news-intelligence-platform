"""Stage 6 daily-brief / evidence / reprocess API against a live, disposable Postgres.

The DB-free FastAPI wiring (routing, status codes, serialization, and the enqueue seam) is proven in
tests/unit/test_report_api.py against hand-built fakes. What can only be proven against real SQL
lives here, driven through the *real* ``ReportLifecycleRepository`` and ``IntelligenceRepository``
over a disposable database:

* the daily-brief default serves the latest *published* version only -- a higher failed or still-
  generating version never wins ``latest``/``by-date`` or the ``/reports`` listing, yet the full
  version history (each version's status/stale/change_reason) and any exact prior version with its
  ordered sections stay retrievable;
* the Evidence Drawer's ``claim_evidence`` join returns the real support types, an article's
  summary-first <=200-char snippet with canonical attribution, and never an article body or raw
  payload; and
* ``reprocess-event`` marks only the *dependent published* reports stale -- through the citation
  join and the direct ``event_id`` link -- committing once and mutating nothing else.

Database hygiene (per the Stage 6 workflow rules): never the default ``news`` database. This module
creates and owns a throwaway ``nip_stage6_api_<hex>`` database on ``localhost:55432``, creates only
the FK-closed subset of tables these endpoints touch (no PostGIS, no full migration chain), and
drops it -- with a leak check -- in ``finally``.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from apps.api.intelligence import (
    QueuedBrief,
    get_brief_enqueuer,
    get_crisis_prediction_reads_enabled,
)
from apps.api.main import app
from db.base import get_session
from db.models.core import (
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
from tests.integration._stage6_db import (
    disposable_database,
    require_disposable_postgres,
    url_for,
)

pytestmark = pytest.mark.integration

UTC = datetime.UTC
_API_PREFIX = "nip_stage6_api_"

BRIEF_DATE = datetime.date(2026, 7, 14)
OTHER_DATE = datetime.date(2026, 7, 13)
PUB_AT = datetime.datetime(2026, 7, 14, 4, tzinfo=UTC)

#: FK-closed set of tables these endpoints touch. `users`/`llm_runs` are FK targets only (no rows).
_API_TABLES = [
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


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A throwaway database with only the API tables, dropped and leak-checked on exit."""
    require_disposable_postgres()
    with disposable_database(_API_PREFIX) as name:
        eng = create_engine(url_for(name))
        try:
            from db.base import Base

            Base.metadata.create_all(bind=eng, tables=_API_TABLES)
            yield eng
        finally:
            eng.dispose()


class _RecordingEnqueuer:
    """Stands in for the broker seam: records the date and returns a task identity, no broker."""

    def __init__(self) -> None:
        self.dates: list[datetime.date] = []

    def __call__(self, brief_date: datetime.date) -> QueuedBrief:
        self.dates.append(brief_date)
        return QueuedBrief(task_id="task-int", task_name="generate_daily_brief", queue="pipeline")


@pytest.fixture
def enqueuer() -> _RecordingEnqueuer:
    return _RecordingEnqueuer()


@pytest.fixture
def client(engine: Engine, enqueuer: _RecordingEnqueuer) -> Iterator[TestClient]:
    """A TestClient whose session and enqueue seams are bound to the disposable DB (never `news`).

    Overriding ``get_session`` alone points *both* the lifecycle repository and the intelligence
    repository at the disposable engine (FastAPI resolves each per request from ``get_session``).
    The enqueue seam is overridden so no broker is ever contacted. Loopback client so the API-key
    middleware admits the reprocess/generate mutations (local env, no key configured).
    """

    def _session_override() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_brief_enqueuer] = lambda: enqueuer
    app.dependency_overrides[get_crisis_prediction_reads_enabled] = lambda: True
    try:
        yield TestClient(app, client=("127.0.0.1", 5000))
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------------------
# Seeding (committed once; the endpoints read it back through their own sessions)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seeded:
    event_id: uuid.UUID
    article_id: uuid.UUID
    claim_id: uuid.UUID
    unrelated_claim_id: uuid.UUID
    v1_id: uuid.UUID
    v2_id: uuid.UUID
    v3_id: uuid.UUID
    other_id: uuid.UUID
    event_report_id: uuid.UUID


def _brief(
    brief_date: datetime.date,
    *,
    status: str,
    version: int,
    change_reason: str | None = None,
) -> Report:
    return Report(
        user_id=None,
        report_type="daily_brief",
        brief_date=brief_date,
        event_id=None,
        title=f"Daily Brief — {brief_date.isoformat()}",
        status=status,
        version=version,
        change_reason=change_reason,
        stale=False,
    )


def _section(
    report_id: uuid.UUID,
    order: int,
    title: str,
    body: str,
    *,
    grounding: str,
    claim_ids: tuple[uuid.UUID, ...] = (),
) -> ReportSection:
    return ReportSection(
        report_id=report_id,
        section_order=order,
        title=title,
        body=body,
        blocks=(
            [{"text": body, "claim_ids": [str(cid) for cid in claim_ids]}] if claim_ids else None
        ),
        evidence_refs=list(claim_ids) or None,
        grounding_status=grounding,
    )


def _seed(session: Session) -> Seeded:
    # Event -> article -> article-typed EvidenceItem -> Claim, so a brief section citing the claim
    # resolves back through the citation join to the event. The item carries raw_ref/metadata the
    # drawer must never expose, and a non-article contradicting link on the same claim.
    source = Source(name="Reuters", feed_url=f"https://feed/{uuid.uuid4()}", authority_score=0.8)
    session.add(source)
    session.flush()
    article = Article(
        source_id=source.id,
        url=f"https://x/{uuid.uuid4()}",
        url_hash=uuid.uuid4().hex,
        title="Bank under pressure",
        summary="A concise summary of the article.",
        body="SECRET BODY that must never leak whole. " * 20,
        published_at=PUB_AT,
    )
    session.add(article)
    session.flush()
    event = Event(title="Regional bank stress", hotness_score=70.0)
    session.add(event)
    session.flush()
    session.add(EventArticle(event_id=event.id, article_id=article.id))

    article_ev = EvidenceItem(
        source_type="article",
        source_id=str(article.id),
        title="Bank under pressure",
        publisher="Reuters",
        url="https://news.example/bank",
        published_at=PUB_AT,
        credibility_score=0.8,
        raw_ref={"do_not": "expose"},
        evidence_metadata={"internal": "do_not_expose"},
    )
    filing_ev = EvidenceItem(source_type="filing", source_id="filing-1", title="Quarterly filing")
    session.add_all([article_ev, filing_ev])
    session.flush()

    claim = Claim(
        claim_text="The bank faces a liquidity squeeze.",
        claim_type="assertion",
        confidence_score=0.9,
    )
    unrelated_claim = Claim(claim_text="Unrelated claim.", confidence_score=0.5)
    session.add_all([claim, unrelated_claim])
    session.flush()
    session.add_all(
        [
            ClaimEvidence(
                claim_id=claim.id,
                evidence_item_id=article_ev.id,
                support_type="supports",
                confidence_score=0.88,
            ),
            ClaimEvidence(
                claim_id=claim.id, evidence_item_id=filing_ev.id, support_type="contradicts"
            ),
        ]
    )

    # Daily brief BRIEF_DATE: v1 published (cites the claim), v2 failed (also cites it), v3 generating.
    # A second published brief on OTHER_DATE cites an unrelated claim (no link to the event).
    v1 = _brief(BRIEF_DATE, status="published", version=1)
    v2 = _brief(BRIEF_DATE, status="failed", version=2, change_reason="rerun")
    v3 = _brief(BRIEF_DATE, status="generating", version=3)
    other = _brief(OTHER_DATE, status="published", version=1)
    session.add_all([v1, v2, v3, other])
    session.flush()
    session.add_all(
        [
            _section(
                v1.id,
                1,
                "Executive Summary",
                "Rates held.",
                grounding="passed",
                claim_ids=(claim.id,),
            ),
            _section(v1.id, 2, "Disclaimer", "Not advice.", grounding="passed"),
            _section(
                v2.id,
                1,
                "Executive Summary",
                "Blocked body.",
                grounding="failed",
                claim_ids=(claim.id,),
            ),
            _section(v3.id, 1, "Executive Summary", "In progress.", grounding="pending"),
            _section(
                other.id,
                1,
                "Executive Summary",
                "Quiet day.",
                grounding="passed",
                claim_ids=(unrelated_claim.id,),
            ),
        ]
    )

    # A published report directly about the event -> the second stale-dependency path.
    event_report = Report(
        user_id=None,
        report_type="event_report",
        brief_date=None,
        event_id=event.id,
        title="Event report",
        status="published",
        version=1,
        stale=False,
    )
    session.add(event_report)
    session.flush()
    seeded = Seeded(
        event_id=event.id,
        article_id=article.id,
        claim_id=claim.id,
        unrelated_claim_id=unrelated_claim.id,
        v1_id=v1.id,
        v2_id=v2.id,
        v3_id=v3.id,
        other_id=other.id,
        event_report_id=event_report.id,
    )
    session.commit()
    return seeded


@pytest.fixture
def seeded(engine: Engine) -> Seeded:
    with Session(engine) as session:
        return _seed(session)


# --------------------------------------------------------------------------------------
# Daily brief: latest / by-date default to the latest PUBLISHED version
# --------------------------------------------------------------------------------------


def test_latest_and_by_date_serve_the_published_version_over_higher_unpublished(
    client: TestClient, seeded: Seeded
) -> None:
    latest = client.get("/api/v1/reports/daily-brief/latest").json()["report"]
    # v2 (failed) and v3 (generating) have higher versions, but latest serves the published one.
    assert latest["id"] == str(seeded.v1_id)
    assert (latest["version"], latest["status"]) == (1, "published")
    assert latest["brief_date"] == "2026-07-14"
    assert latest["user_id"] is None  # the daily brief is a global artifact
    assert [s["section_order"] for s in latest["sections"]] == [1, 2]
    first = latest["sections"][0]
    assert first["body"] == "Rates held."
    assert first["evidence_refs"] == [str(seeded.claim_id)]
    assert first["blocks"][0]["claim_ids"] == [str(seeded.claim_id)]
    assert latest["sections"][1]["evidence_refs"] == []  # deterministic section cites nothing

    by_date = client.get("/api/v1/reports/daily-brief/2026-07-14").json()["report"]
    assert by_date["id"] == str(seeded.v1_id)
    assert by_date["version"] == 1


def test_versions_lists_all_three_newest_first_and_an_exact_prior_version(
    client: TestClient, seeded: Seeded
) -> None:
    payload = client.get("/api/v1/reports/daily-brief/2026-07-14/versions").json()
    assert payload["count"] == 3
    assert [item["version"] for item in payload["items"]] == [3, 2, 1]
    # Each prior version exposes its own status/stale/change_reason for inspection...
    assert [item["status"] for item in payload["items"]] == ["generating", "failed", "published"]
    assert payload["items"][1]["change_reason"] == "rerun"
    assert all(item["stale"] is False for item in payload["items"])
    # ...but the listing is metadata only -- no section bodies.
    assert all("sections" not in item for item in payload["items"])

    v2 = client.get("/api/v1/reports/daily-brief/2026-07-14/versions/2").json()["report"]
    assert v2["id"] == str(seeded.v2_id)
    assert (v2["version"], v2["status"]) == (2, "failed")  # a prior non-published version, explicit
    assert v2["sections"][0]["grounding_status"] == "failed"
    assert v2["sections"][0]["body"] == "Blocked body."


def test_reports_listing_default_serves_only_the_latest_published_daily_brief(
    client: TestClient, seeded: Seeded
) -> None:
    items = client.get("/api/v1/reports").json()["items"]
    briefs = {
        (item["brief_date"], item["version"], item["status"])
        for item in items
        if item["report_type"] == "daily_brief"
    }
    # Only the latest PUBLISHED version per date; the failed v2 and generating v3 never appear.
    assert briefs == {("2026-07-14", 1, "published"), ("2026-07-13", 1, "published")}
    assert all(
        item["status"] == "published" for item in items if item["report_type"] == "daily_brief"
    )


# --------------------------------------------------------------------------------------
# Evidence Drawer
# --------------------------------------------------------------------------------------

_EVIDENCE_KEYS = {
    "evidence_item_id",
    "support_type",
    "confidence",
    "source_type",
    "source_id",
    "title",
    "publisher",
    "url",
    "published_at",
    "credibility",
    "snippet",
    "snippet_origin",
    "snippet_truncated",
}


def test_evidence_drawer_returns_real_support_types_and_a_safe_article_snippet(
    client: TestClient, seeded: Seeded
) -> None:
    resp = client.get(f"/api/v1/evidence/{seeded.claim_id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["claim"] == {
        "id": str(seeded.claim_id),
        "text": "The bank faces a liquidity squeeze.",
        "type": "assertion",
        "confidence": 0.9,
    }
    evidence = payload["evidence"]
    # Deterministic order (published_at desc nullslast): the dated article link precedes the filing.
    assert [e["support_type"] for e in evidence] == ["supports", "contradicts"]

    art = evidence[0]
    assert art["source_type"] == "article"
    assert art["source_id"] == str(seeded.article_id)
    assert art["publisher"] == "Reuters"
    assert art["url"] == "https://news.example/bank"
    assert art["credibility"] == 0.8
    assert art["confidence"] == 0.88
    # Summary-first, bounded, and derived (not the raw body).
    assert art["snippet"] == "A concise summary of the article."
    assert art["snippet_origin"] == "summary" and len(art["snippet"]) <= 200
    assert evidence[1]["snippet"] is None  # non-article evidence has no snippet

    # No article body, raw_ref, or metadata ever leaks, and only the documented keys are present.
    for item in evidence:
        assert set(item) == _EVIDENCE_KEYS
    assert "SECRET BODY" not in resp.text
    assert "do_not_expose" not in resp.text and "do_not" not in resp.text


def test_evidence_drawer_missing_claim_is_404(client: TestClient, seeded: Seeded) -> None:
    assert client.get(f"/api/v1/evidence/{uuid.uuid4()}").status_code == 404


def test_evidence_drawer_malformed_uuid_is_422_at_routing(client: TestClient) -> None:
    assert client.get("/api/v1/evidence/not-a-uuid").status_code == 422


# --------------------------------------------------------------------------------------
# reprocess-event: stale only the dependent published reports, commit once
# --------------------------------------------------------------------------------------


def test_reprocess_event_stales_only_dependent_published_reports(
    client: TestClient, seeded: Seeded, engine: Engine
) -> None:
    resp = client.post(f"/api/v1/admin/reprocess-event/{seeded.event_id}")
    assert resp.status_code == 200
    # The citing published brief (v1) and the direct event report -- both dependency paths.
    assert resp.json() == {
        "event_id": str(seeded.event_id),
        "dependent_published_report_count": 2,
    }

    with Session(engine) as session:
        stale = {r.id: r.stale for r in session.query(Report).all()}
    assert stale[seeded.v1_id] is True  # published, cites a claim resolving to the event
    assert stale[seeded.event_report_id] is True  # published, directly about the event
    assert stale[seeded.v2_id] is False  # failed -- cites the claim but is not published
    assert stale[seeded.v3_id] is False  # generating -- not published
    assert stale[seeded.other_id] is False  # published, but cites an unrelated claim

    # Idempotent: a second reprocess reports the same count and marks nothing new.
    again = client.post(f"/api/v1/admin/reprocess-event/{seeded.event_id}")
    assert again.json()["dependent_published_report_count"] == 2


def test_reprocess_unknown_event_is_404(client: TestClient, seeded: Seeded) -> None:
    assert client.post(f"/api/v1/admin/reprocess-event/{uuid.uuid4()}").status_code == 404


# --------------------------------------------------------------------------------------
# generate-daily-brief: end-to-end through the app, no broker
# --------------------------------------------------------------------------------------


def test_generate_daily_brief_enqueues_through_the_app_without_a_broker(
    client: TestClient, enqueuer: _RecordingEnqueuer
) -> None:
    resp = client.post(
        "/api/v1/internal/jobs/generate-daily-brief", json={"brief_date": "2026-07-14"}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["task_id"] == "task-int"
    assert body["queue"] == "pipeline"
    assert body["brief_date"] == "2026-07-14"
    # The endpoint hands the broker seam a canonical calendar date, and no broker was contacted.
    assert enqueuer.dates == [datetime.date(2026, 7, 14)]
