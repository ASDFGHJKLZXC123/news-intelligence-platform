"""Stage 6 report/evidence/job API tests with dependency fakes (no database, no broker).

These exercise the FastAPI wiring only -- routing, status codes, serialization, and the enqueue
seam -- through hand-built fakes. The behaviour that can only be proven against real SQL (the
latest-published-wins version filtering, the citation-join stale marking, the article-snippet
bound) is proven in ``tests/integration/test_stage6_report_api.py``.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.intelligence import (
    EvidenceDrawerLink,
    QueuedBrief,
    enqueue_generate_daily_brief,
    get_brief_enqueuer,
    get_intelligence_repository,
    get_report_lifecycle_repository,
)
from apps.api.main import app
from db.base import get_session
from db.models import Event
from services.reports.lifecycle import ReportSectionSnapshot, ReportSnapshot, SectionBlock

NOW = datetime.datetime(2026, 7, 14, 10, 30, tzinfo=datetime.UTC)
BRIEF_DATE = datetime.date(2026, 7, 14)


# --------------------------------------------------------------------------------------
# Snapshot / link builders (the real detached dataclasses the endpoints serialize)
# --------------------------------------------------------------------------------------


def _snap(
    *,
    version: int,
    status: str,
    stale: bool = False,
    change_reason: str | None = None,
    rid: uuid.UUID | None = None,
    brief_date: datetime.date = BRIEF_DATE,
) -> ReportSnapshot:
    return ReportSnapshot(
        id=rid or uuid.uuid4(),
        user_id=None,
        report_type="daily_brief",
        brief_date=brief_date,
        event_id=None,
        title=f"Daily Brief — {brief_date.isoformat()}",
        status=status,
        version=version,
        change_reason=change_reason,
        stale=stale,
        generated_by_run_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _section(
    order: int,
    title: str,
    body: str,
    *,
    claim_ids: tuple[uuid.UUID, ...] = (),
    grounding: str = "passed",
) -> ReportSectionSnapshot:
    blocks = (
        (SectionBlock(text=body, claim_ids=tuple(str(c) for c in claim_ids)),) if claim_ids else ()
    )
    return ReportSectionSnapshot(
        id=uuid.uuid4(),
        report_id=uuid.uuid4(),
        section_order=order,
        title=title,
        body=body,
        blocks=blocks,
        evidence_refs=tuple(claim_ids),
        grounding_status=grounding,
    )


class FakeLifecycleRepo:
    """A stand-in for ``ReportLifecycleRepository`` returning pre-built snapshots."""

    def __init__(
        self,
        *,
        latest: ReportSnapshot | None = None,
        by_date: ReportSnapshot | None = None,
        versions: tuple[ReportSnapshot, ...] = (),
        version_map: dict[int, ReportSnapshot] | None = None,
        sections: tuple[ReportSectionSnapshot, ...] = (),
    ) -> None:
        self._latest = latest
        self._by_date = by_date
        self._versions = versions
        self._version_map = version_map or {}
        self._sections = sections

    def latest_published_daily_brief(self) -> ReportSnapshot | None:
        return self._latest

    def latest_published_daily_brief_by_date(self, brief_date: datetime.date) -> ReportSnapshot | None:
        return self._by_date

    def daily_brief_versions(self, brief_date: datetime.date) -> tuple[ReportSnapshot, ...]:
        return self._versions

    def daily_brief_version(self, brief_date: datetime.date, version: int) -> ReportSnapshot | None:
        return self._version_map.get(version)

    def report_sections(self, report_id: uuid.UUID) -> tuple[ReportSectionSnapshot, ...]:
        return self._sections


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Loopback client so the API-key middleware admits mutations (local, no key configured).
    try:
        yield TestClient(app, client=("127.0.0.1", 5000))
    finally:
        app.dependency_overrides.clear()


def _use_lifecycle(repo: FakeLifecycleRepo) -> None:
    app.dependency_overrides[get_report_lifecycle_repository] = lambda: repo


# --------------------------------------------------------------------------------------
# Daily brief: latest / by-date
# --------------------------------------------------------------------------------------


def test_latest_returns_the_published_snapshot_with_ordered_sections(client: TestClient) -> None:
    claim_id = uuid.uuid4()
    published = _snap(version=3, status="published", stale=True, change_reason="reprocessed")
    _use_lifecycle(
        FakeLifecycleRepo(
            latest=published,
            sections=(
                _section(1, "Executive Summary", "Rates held.", claim_ids=(claim_id,)),
                _section(2, "Disclaimer", "Not advice.", grounding="passed"),
            ),
        )
    )
    body = client.get("/api/v1/reports/daily-brief/latest").json()["report"]

    assert body["version"] == 3
    assert body["status"] == "published"
    assert body["stale"] is True
    assert body["change_reason"] == "reprocessed"
    assert body["user_id"] is None  # the daily brief is a global artifact
    assert body["brief_date"] == "2026-07-14"
    assert [s["section_order"] for s in body["sections"]] == [1, 2]
    first = body["sections"][0]
    assert first["body"] == "Rates held."
    assert first["evidence_refs"] == [str(claim_id)]
    assert first["blocks"][0]["claim_ids"] == [str(claim_id)]
    assert first["grounding_status"] == "passed"
    # A deterministic/withheld section carries no fabricated citation.
    assert body["sections"][1]["evidence_refs"] == []


def test_latest_is_404_when_no_brief_is_published(client: TestClient) -> None:
    _use_lifecycle(FakeLifecycleRepo(latest=None))
    assert client.get("/api/v1/reports/daily-brief/latest").status_code == 404


def test_by_date_returns_latest_published_for_that_date(client: TestClient) -> None:
    _use_lifecycle(
        FakeLifecycleRepo(
            by_date=_snap(version=1, status="published"),
            sections=(_section(1, "Executive Summary", "Quiet day."),),
        )
    )
    resp = client.get("/api/v1/reports/daily-brief/2026-07-14")
    assert resp.status_code == 200
    assert resp.json()["report"]["version"] == 1


def test_by_date_is_404_when_that_date_has_no_published_brief(client: TestClient) -> None:
    _use_lifecycle(FakeLifecycleRepo(by_date=None))
    assert client.get("/api/v1/reports/daily-brief/2026-07-14").status_code == 404


def test_invalid_brief_date_is_422_via_typed_param(client: TestClient) -> None:
    _use_lifecycle(FakeLifecycleRepo())
    # "latest" is matched by its own literal route; a non-date path segment is a typed 422.
    assert client.get("/api/v1/reports/daily-brief/not-a-date").status_code == 422


# --------------------------------------------------------------------------------------
# Daily brief: versions (newest first, metadata only) and specific version (with sections)
# --------------------------------------------------------------------------------------


def test_versions_are_newest_first_metadata_only(client: TestClient) -> None:
    _use_lifecycle(
        FakeLifecycleRepo(
            versions=(
                _snap(version=3, status="generating"),
                _snap(version=2, status="failed", change_reason="rerun"),
                _snap(version=1, status="published"),
            )
        )
    )
    payload = client.get("/api/v1/reports/daily-brief/2026-07-14/versions").json()

    assert payload["count"] == 3
    assert [item["version"] for item in payload["items"]] == [3, 2, 1]
    # Prior versions expose their status/stale/change_reason for explicit inspection...
    assert [item["status"] for item in payload["items"]] == ["generating", "failed", "published"]
    assert payload["items"][1]["change_reason"] == "rerun"
    # ...but the list is metadata only -- no section bodies.
    assert all("sections" not in item for item in payload["items"])


def test_versions_is_404_when_the_date_has_no_versions(client: TestClient) -> None:
    _use_lifecycle(FakeLifecycleRepo(versions=()))
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/versions").status_code == 404


def test_specific_version_returns_that_version_with_sections(client: TestClient) -> None:
    failed_v2 = _snap(version=2, status="failed", change_reason="rerun")
    _use_lifecycle(
        FakeLifecycleRepo(
            version_map={2: failed_v2},
            sections=(_section(1, "Executive Summary", "Blocked body.", grounding="failed"),),
        )
    )
    body = client.get("/api/v1/reports/daily-brief/2026-07-14/versions/2").json()["report"]

    assert body["version"] == 2
    assert body["status"] == "failed"  # a prior non-published version is retrievable explicitly
    assert body["sections"][0]["grounding_status"] == "failed"


def test_specific_version_is_404_when_absent(client: TestClient) -> None:
    _use_lifecycle(FakeLifecycleRepo(version_map={}))
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/versions/9").status_code == 404


def test_non_positive_version_is_422(client: TestClient) -> None:
    _use_lifecycle(FakeLifecycleRepo())
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/versions/0").status_code == 422
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/versions/abc").status_code == 422


# --------------------------------------------------------------------------------------
# Evidence Drawer
# --------------------------------------------------------------------------------------


class FakeEvidenceRepo:
    def __init__(self, claim: Any, links: tuple[EvidenceDrawerLink, ...]) -> None:
        self._claim = claim
        self._links = links

    def get_claim(self, claim_id: uuid.UUID) -> Any:
        return self._claim

    def claim_evidence_links(self, claim_id: uuid.UUID) -> tuple[EvidenceDrawerLink, ...]:
        return self._links


_EXPECTED_EVIDENCE_KEYS = {
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


def test_evidence_drawer_shape_carries_real_support_types_and_a_safe_snippet(
    client: TestClient,
) -> None:
    claim_id = uuid.uuid4()
    claim = SimpleNamespace(
        id=claim_id,
        claim_text="The bank faces a liquidity squeeze.",
        claim_type="assertion",
        confidence_score=decimal.Decimal("0.9"),
    )
    article_id = uuid.uuid4()
    supports = EvidenceDrawerLink(
        evidence_item_id=uuid.uuid4(),
        support_type="supports",
        support_confidence=decimal.Decimal("0.88"),
        source_type="article",
        source_id=str(article_id),
        title="Bank under pressure",
        publisher="Reuters",
        url="https://news.example/bank",
        published_at=NOW,
        credibility=decimal.Decimal("0.8"),
        snippet="A" * 200,
        snippet_origin="summary",
        snippet_truncated=True,
    )
    contradicts = EvidenceDrawerLink(
        evidence_item_id=uuid.uuid4(),
        support_type="contradicts",
        support_confidence=None,
        source_type="filing",
        source_id="filing-1",
        title="Quarterly filing",
        publisher=None,
        url=None,
        published_at=None,
        credibility=None,
        snippet=None,
        snippet_origin=None,
        snippet_truncated=False,
    )
    app.dependency_overrides[get_intelligence_repository] = lambda: FakeEvidenceRepo(
        claim, (supports, contradicts)
    )

    payload = client.get(f"/api/v1/evidence/{claim_id}").json()

    assert payload["claim"] == {
        "id": str(claim_id),
        "text": "The bank faces a liquidity squeeze.",
        "type": "assertion",
        "confidence": 0.9,
    }
    evidence = payload["evidence"]
    # Real support types are serialized as-is; a contradicting link is not dropped or relabelled.
    assert [e["support_type"] for e in evidence] == ["supports", "contradicts"]
    art = evidence[0]
    assert art["source_type"] == "article"
    assert art["source_id"] == str(article_id)
    assert art["publisher"] == "Reuters"
    assert art["url"] == "https://news.example/bank"
    assert art["credibility"] == 0.8
    assert art["confidence"] == 0.88
    assert art["published_at"] == "2026-07-14T10:30:00Z"
    assert len(art["snippet"]) <= 200 and art["snippet_origin"] == "summary"
    # No article full text or raw payload ever leaks: only the documented keys are present.
    for item in evidence:
        assert set(item) == _EXPECTED_EVIDENCE_KEYS
    assert evidence[1]["snippet"] is None  # non-article evidence has no snippet


def test_evidence_drawer_missing_claim_is_404(client: TestClient) -> None:
    app.dependency_overrides[get_intelligence_repository] = lambda: FakeEvidenceRepo(None, ())
    assert client.get(f"/api/v1/evidence/{uuid.uuid4()}").status_code == 404


def test_evidence_drawer_malformed_uuid_is_422_at_routing(client: TestClient) -> None:
    assert client.get("/api/v1/evidence/not-a-uuid").status_code == 422


# --------------------------------------------------------------------------------------
# generate-daily-brief (internal job)
# --------------------------------------------------------------------------------------


class RecordingEnqueuer:
    def __init__(self) -> None:
        self.dates: list[datetime.date] = []

    def __call__(self, brief_date: datetime.date) -> QueuedBrief:
        self.dates.append(brief_date)
        return QueuedBrief(task_id="task-123", task_name="generate_daily_brief", queue="pipeline")


def test_generate_daily_brief_enqueues_and_returns_202(client: TestClient) -> None:
    enqueuer = RecordingEnqueuer()
    app.dependency_overrides[get_brief_enqueuer] = lambda: enqueuer

    resp = client.post(
        "/api/v1/internal/jobs/generate-daily-brief", json={"brief_date": "2026-07-14"}
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["task_id"] == "task-123"
    assert body["task_name"] == "generate_daily_brief"
    assert body["queue"] == "pipeline"
    assert body["brief_date"] == "2026-07-14"
    # The endpoint hands the coordinator a canonical calendar date, not a rolling window.
    assert enqueuer.dates == [datetime.date(2026, 7, 14)]


def test_generate_daily_brief_requires_an_explicit_brief_date(client: TestClient) -> None:
    app.dependency_overrides[get_brief_enqueuer] = lambda: RecordingEnqueuer()
    assert client.post("/api/v1/internal/jobs/generate-daily-brief", json={}).status_code == 422


def test_generate_daily_brief_rejects_an_unparseable_date(client: TestClient) -> None:
    app.dependency_overrides[get_brief_enqueuer] = lambda: RecordingEnqueuer()
    resp = client.post(
        "/api/v1/internal/jobs/generate-daily-brief", json={"brief_date": "not-a-date"}
    )
    assert resp.status_code == 422


def test_enqueue_helper_sends_the_exact_task_args_and_queue_without_a_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workers.celery_app import QUEUE_PIPELINE
    from workers.report_tasks import TASK_NAME

    # Pin the accepted contract values so a rename cannot silently pass.
    assert TASK_NAME == "generate_daily_brief"
    assert QUEUE_PIPELINE == "pipeline"

    calls: list[dict[str, Any]] = []

    class FakeCelery:
        def send_task(self, name: str, *, args: Any, queue: str) -> Any:
            calls.append({"name": name, "args": args, "queue": queue})
            return SimpleNamespace(id="async-1")

    # Patch the module attribute the lazily-imported enqueuer resolves -- no broker is contacted.
    monkeypatch.setattr("workers.celery_app.celery_app", FakeCelery())

    queued = enqueue_generate_daily_brief(datetime.date(2026, 7, 14))

    assert queued == QueuedBrief(task_id="async-1", task_name=TASK_NAME, queue=QUEUE_PIPELINE)
    assert calls == [{"name": TASK_NAME, "args": ["2026-07-14"], "queue": QUEUE_PIPELINE}]


# --------------------------------------------------------------------------------------
# reprocess-event (admin)
# --------------------------------------------------------------------------------------


class FakeReprocessSession:
    def __init__(self, event_exists: bool) -> None:
        self._event = SimpleNamespace(id="e") if event_exists else None
        self.commits = 0

    def get(self, model: Any, key: Any, **_kwargs: Any) -> Any:
        return self._event if model is Event else None

    def commit(self) -> None:
        self.commits += 1


class FakeStaleRepo:
    def __init__(self, count: int) -> None:
        self._count = count
        self.stale_calls: list[uuid.UUID] = []

    def mark_published_reports_stale_for_event(self, event_id: uuid.UUID) -> int:
        self.stale_calls.append(event_id)
        return self._count


def _use_reprocess(session: FakeReprocessSession, repo: FakeStaleRepo) -> None:
    def _session() -> Iterator[Any]:
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_report_lifecycle_repository] = lambda: repo


def test_reprocess_event_marks_stale_commits_once_and_returns_the_count(client: TestClient) -> None:
    session = FakeReprocessSession(event_exists=True)
    repo = FakeStaleRepo(count=2)
    _use_reprocess(session, repo)
    event_id = uuid.uuid4()

    resp = client.post(f"/api/v1/admin/reprocess-event/{event_id}")

    assert resp.status_code == 200
    assert resp.json() == {
        "event_id": str(event_id),
        "dependent_published_report_count": 2,
    }
    # It only marks stale (no content/status/version mutation path exists here) and commits once.
    assert repo.stale_calls == [event_id]
    assert session.commits == 1


def test_reprocess_unknown_event_is_404_and_marks_nothing(client: TestClient) -> None:
    session = FakeReprocessSession(event_exists=False)
    repo = FakeStaleRepo(count=0)
    _use_reprocess(session, repo)

    resp = client.post(f"/api/v1/admin/reprocess-event/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert repo.stale_calls == []  # the stale scan never runs for a missing event
    assert session.commits == 0
