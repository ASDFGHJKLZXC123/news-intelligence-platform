"""Stage 6 export API wiring: the four export routes, media types, filenames, and read-only-ness.

DB-free -- routing, status codes, media type, ``Content-Disposition``, published-only selection,
and that a request writes nothing are exercised through hand-built fakes. The real SQL (the bulk
attribution join, no body/raw leak) is proven in ``tests/integration/test_stage6_export.py``.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.intelligence import (
    get_report_export_repository,
    get_report_lifecycle_repository,
)
from apps.api.main import app
from db.base import get_session
from services.reports.exports import ReportExportRepository, SourceAttribution
from services.reports.lifecycle import ReportSectionSnapshot, ReportSnapshot, SectionBlock
from services.reports.material import FINAL_DISCLAIMER

NOW = datetime.datetime(2026, 7, 14, 4, tzinfo=datetime.UTC)
BRIEF_DATE = datetime.date(2026, 7, 14)


def _snap(*, version: int, status: str) -> ReportSnapshot:
    return ReportSnapshot(
        id=uuid.uuid4(),
        user_id=None,
        report_type="daily_brief",
        brief_date=BRIEF_DATE,
        event_id=None,
        title="Daily Brief — 2026-07-14",
        status=status,
        version=version,
        change_reason=None,
        stale=False,
        generated_by_run_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _section(order: int, title: str, body: str, *, claim_ids: tuple[uuid.UUID, ...] = ()) -> ReportSectionSnapshot:
    blocks = (SectionBlock(text=body, claim_ids=tuple(str(c) for c in claim_ids)),) if claim_ids else ()
    return ReportSectionSnapshot(
        id=uuid.uuid4(),
        report_id=uuid.uuid4(),
        section_order=order,
        title=title,
        body=body,
        blocks=blocks,
        evidence_refs=tuple(claim_ids),
        grounding_status="passed",
    )


def _attribution() -> SourceAttribution:
    return SourceAttribution(
        evidence_item_id=uuid.uuid4(),
        source_type="article",
        attribution="Reuters",
        title="Bank under pressure",
        url="https://news.example/bank",
        snippet="A concise summary.",
        published_at=NOW,
    )


class FakeLifecycleRepo:
    def __init__(
        self,
        *,
        by_date: ReportSnapshot | None = None,
        version_map: dict[int, ReportSnapshot] | None = None,
        sections: tuple[ReportSectionSnapshot, ...] = (),
    ) -> None:
        self._by_date = by_date
        self._version_map = version_map or {}
        self._sections = sections
        self.by_date_calls: list[datetime.date] = []
        self.version_calls: list[tuple[datetime.date, int]] = []

    def latest_published_daily_brief_by_date(self, brief_date: datetime.date) -> ReportSnapshot | None:
        self.by_date_calls.append(brief_date)
        return self._by_date

    def daily_brief_version(self, brief_date: datetime.date, version: int) -> ReportSnapshot | None:
        self.version_calls.append((brief_date, version))
        return self._version_map.get(version)

    def report_sections(self, report_id: uuid.UUID) -> tuple[ReportSectionSnapshot, ...]:
        return self._sections


class FakeExportRepo:
    def __init__(self, attributions: tuple[SourceAttribution, ...] = ()) -> None:
        self._attributions = attributions
        self.calls: list[tuple[uuid.UUID, ...]] = []

    def source_attributions_for_report(self, claim_ids: Any) -> tuple[SourceAttribution, ...]:
        self.calls.append(tuple(claim_ids))
        return self._attributions


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        yield TestClient(app, client=("127.0.0.1", 5000))
    finally:
        app.dependency_overrides.clear()


def _use(lifecycle: FakeLifecycleRepo, export: FakeExportRepo) -> None:
    app.dependency_overrides[get_report_lifecycle_repository] = lambda: lifecycle
    app.dependency_overrides[get_report_export_repository] = lambda: export


# --------------------------------------------------------------------------------------
# The four routes: media type + Content-Disposition + content
# --------------------------------------------------------------------------------------


def test_latest_markdown_export(client: TestClient) -> None:
    lifecycle = FakeLifecycleRepo(
        by_date=_snap(version=3, status="published"),
        sections=(_section(1, "Executive Summary", "Rates held.", claim_ids=(uuid.uuid4(),)),),
    )
    _use(lifecycle, FakeExportRepo((_attribution(),)))

    resp = client.get("/api/v1/reports/daily-brief/2026-07-14/export.md")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/markdown; charset=utf-8"
    assert resp.headers["content-disposition"] == 'attachment; filename="daily-brief-2026-07-14-v3.md"'
    body = resp.text
    assert body.startswith("# Daily Brief — 2026-07-14")
    assert "- Reuters — Bank under pressure — https://news.example/bank" in body
    assert body.rstrip().endswith(FINAL_DISCLAIMER)
    assert lifecycle.by_date_calls == [BRIEF_DATE]  # latest reads the by-date published selector


def test_latest_pdf_export(client: TestClient) -> None:
    lifecycle = FakeLifecycleRepo(
        by_date=_snap(version=3, status="published"),
        sections=(_section(1, "Executive Summary", "Rates held."),),
    )
    _use(lifecycle, FakeExportRepo((_attribution(),)))

    resp = client.get("/api/v1/reports/daily-brief/2026-07-14/export.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == 'attachment; filename="daily-brief-2026-07-14-v3.pdf"'
    assert resp.content.startswith(b"%PDF-1.4")
    assert resp.content.rstrip().endswith(b"%%EOF")


def test_specific_version_markdown_export(client: TestClient) -> None:
    published = _snap(version=2, status="published")
    lifecycle = FakeLifecycleRepo(
        version_map={2: published}, sections=(_section(1, "Executive Summary", "Rates held."),)
    )
    _use(lifecycle, FakeExportRepo())

    resp = client.get("/api/v1/reports/daily-brief/2026-07-14/versions/2/export.md")

    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="daily-brief-2026-07-14-v2.md"'
    assert lifecycle.version_calls == [(BRIEF_DATE, 2)]


def test_specific_version_pdf_export(client: TestClient) -> None:
    lifecycle = FakeLifecycleRepo(
        version_map={5: _snap(version=5, status="published")},
        sections=(_section(1, "Executive Summary", "Rates held."),),
    )
    _use(lifecycle, FakeExportRepo())

    resp = client.get("/api/v1/reports/daily-brief/2026-07-14/versions/5/export.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == 'attachment; filename="daily-brief-2026-07-14-v5.pdf"'


# --------------------------------------------------------------------------------------
# Only published reports may be exported
# --------------------------------------------------------------------------------------


def test_latest_export_is_404_when_no_published_brief_for_date(client: TestClient) -> None:
    _use(FakeLifecycleRepo(by_date=None), FakeExportRepo())
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/export.md").status_code == 404
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/export.pdf").status_code == 404


def test_specific_version_export_is_404_when_that_version_is_not_published(client: TestClient) -> None:
    # A failed and a still-generating version are inspectable via the metadata routes but never
    # exportable: only a published version may be exported.
    lifecycle = FakeLifecycleRepo(
        version_map={2: _snap(version=2, status="failed"), 3: _snap(version=3, status="generating")}
    )
    _use(lifecycle, FakeExportRepo())
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/versions/2/export.md").status_code == 404
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/versions/3/export.pdf").status_code == 404


def test_specific_version_export_is_404_when_version_absent(client: TestClient) -> None:
    _use(FakeLifecycleRepo(version_map={}), FakeExportRepo())
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/versions/9/export.md").status_code == 404


# --------------------------------------------------------------------------------------
# Invalid typed params -> 422
# --------------------------------------------------------------------------------------


def test_invalid_date_is_422(client: TestClient) -> None:
    _use(FakeLifecycleRepo(), FakeExportRepo())
    assert client.get("/api/v1/reports/daily-brief/not-a-date/export.md").status_code == 422
    assert client.get("/api/v1/reports/daily-brief/not-a-date/export.pdf").status_code == 422


def test_invalid_version_is_422(client: TestClient) -> None:
    _use(FakeLifecycleRepo(), FakeExportRepo())
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/versions/0/export.md").status_code == 422
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/versions/abc/export.pdf").status_code == 422


# --------------------------------------------------------------------------------------
# The export path writes nothing
# --------------------------------------------------------------------------------------


class _RecordingResult:
    def all(self) -> list[Any]:
        return []  # no attribution rows -> the "no citations" path, still no writes

    def scalars(self) -> _RecordingResult:
        return self

    def first(self) -> None:
        return None


class _RecordingSession:
    """A session that answers reads and records any mutating call (there must be none)."""

    def __init__(self) -> None:
        self.commits = 0
        self.flushes = 0
        self.adds = 0

    def execute(self, _stmt: Any) -> _RecordingResult:
        return _RecordingResult()

    def commit(self) -> None:
        self.commits += 1

    def flush(self) -> None:
        self.flushes += 1

    def add(self, _obj: Any) -> None:
        self.adds += 1


def test_export_request_issues_no_writes(client: TestClient) -> None:
    # The lifecycle read is faked; the *real* export repository runs against a recording session,
    # so the attribution query path is exercised and proven to only read.
    session = _RecordingSession()
    lifecycle = FakeLifecycleRepo(
        by_date=_snap(version=1, status="published"),
        sections=(_section(1, "Executive Summary", "Quiet day.", claim_ids=(uuid.uuid4(),)),),
    )
    app.dependency_overrides[get_report_lifecycle_repository] = lambda: lifecycle
    app.dependency_overrides[get_session] = lambda: iter([session])
    app.dependency_overrides[get_report_export_repository] = lambda: ReportExportRepository(session)

    resp = client.get("/api/v1/reports/daily-brief/2026-07-14/export.md")

    assert resp.status_code == 200
    assert (session.commits, session.flushes, session.adds) == (0, 0, 0)
