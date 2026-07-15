"""Stage 6 daily-brief export against a live, disposable Postgres.

The DB-free renderer/attribution logic and the FastAPI wiring are proven in
tests/unit/test_report_export*.py. What only real SQL can prove lives here, driven through the
*real* ``ReportExportRepository`` and ``ReportLifecycleRepository`` over a throwaway database:

* the bulk attribution join resolves every cited claim's sources in one query (no N+1), across
  several articles and a non-article source;
* an article snippet is summary-first and falls back to a bounded body head, and never carries the
  article body whole, ``EvidenceItem.raw_ref`` or its ``metadata``;
* only the latest *published* version is exportable by default, and a specific version exports only
  when it is published (a failed version is a 404); and
* an export request mutates nothing.

Database hygiene (Stage 6 rules): never the default ``news`` database. This module owns a throwaway
``nip_stage6_export_<hex>`` database on ``localhost:55432``, creates only the FK-closed subset of
tables the export touches, and drops it -- with a leak check -- in ``finally``.
"""

from __future__ import annotations

import datetime
import io
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from apps.api.main import app
from db.base import get_session
from db.models.core import (
    Article,
    Claim,
    ClaimEvidence,
    Event,
    EvidenceItem,
    LLMRun,
    Report,
    ReportSection,
    Source,
    User,
)
from services.reports.material import FINAL_DISCLAIMER
from tests.integration._stage6_db import (
    disposable_database,
    require_disposable_postgres,
    url_for,
)

pytestmark = pytest.mark.integration

UTC = datetime.UTC
_EXPORT_PREFIX = "nip_stage6_export_"

BRIEF_DATE = datetime.date(2026, 7, 14)
PUB_AT = datetime.datetime(2026, 7, 14, 4, tzinfo=UTC)
OLDER_AT = datetime.datetime(2026, 7, 12, 4, tzinfo=UTC)

_SECRET_BODY = "SECRET BODY alpha that must never leak whole. " * 20
# Long (>200 chars), so the body-fallback snippet is a bounded head, never the whole body.
_BETA_BODY = "Beta body-fallback detail sentence. " * 20

# Non-WinAnsi report content (Cyrillic, CJK, Arabic) seeded into the published v1 so the PDF
# export has to embed a real Unicode font: the old WinAnsi/base-14 writer rendered every one of
# these code points as '?'. The extraction test below asserts they survive round-trip exactly.
# Arabic sits on its own line (LTR and RTL runs kept apart) so a bidi-naive text extractor reads
# each line back cleanly -- the Arabic code points still round-trip, in visual order.
_UNICODE_TITLE = "Мировые рынки 全球市场"
_UNICODE_BODY = "Ставки удержаны. 利率维持不变.\nنص العربية."

#: FK-closed set the export touches. ``events`` is a FK target of ``reports.event_id`` only (no
#: rows); ``users``/``llm_runs`` are FK targets only.
_EXPORT_TABLES = [
    User.__table__,
    LLMRun.__table__,
    Event.__table__,
    Source.__table__,
    Article.__table__,
    EvidenceItem.__table__,
    Claim.__table__,
    ClaimEvidence.__table__,
    Report.__table__,
    ReportSection.__table__,
]


@pytest.fixture
def engine() -> Iterator[Engine]:
    require_disposable_postgres()
    with disposable_database(_EXPORT_PREFIX) as name:
        eng = create_engine(url_for(name))
        try:
            from db.base import Base

            Base.metadata.create_all(bind=eng, tables=_EXPORT_TABLES)
            yield eng
        finally:
            eng.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    def _session_override() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = _session_override
    try:
        yield TestClient(app, client=("127.0.0.1", 5000))
    finally:
        app.dependency_overrides.clear()


@dataclass(frozen=True)
class Seeded:
    v1_id: uuid.UUID
    v2_id: uuid.UUID
    article_alpha_id: uuid.UUID
    article_beta_id: uuid.UUID


def _brief(status: str, version: int) -> Report:
    return Report(
        user_id=None,
        report_type="daily_brief",
        brief_date=BRIEF_DATE,
        event_id=None,
        title=f"Daily Brief — {BRIEF_DATE.isoformat()}",
        status=status,
        version=version,
        change_reason="rerun" if version > 1 else None,
        stale=False,
    )


def _section(
    report_id: uuid.UUID,
    order: int,
    title: str,
    body: str,
    *,
    claim_ids: tuple[uuid.UUID, ...] = (),
) -> ReportSection:
    return ReportSection(
        report_id=report_id,
        section_order=order,
        title=title,
        body=body,
        blocks=([{"text": body, "claim_ids": [str(c) for c in claim_ids]}] if claim_ids else None),
        evidence_refs=list(claim_ids) or None,
        grounding_status="passed" if claim_ids or title == "Disclaimer" else "passed",
    )


def _seed(session: Session) -> Seeded:
    source = Source(name="Reuters", feed_url=f"https://feed/{uuid.uuid4()}", authority_score=0.8)
    session.add(source)
    session.flush()

    # Article alpha: has a summary, so its (secret) body is never the snippet, only the summary is.
    alpha = Article(
        source_id=source.id,
        url=f"https://x/{uuid.uuid4()}",
        url_hash=uuid.uuid4().hex,
        title="Alpha headline",
        summary="A concise summary of alpha.",
        body=_SECRET_BODY,
        published_at=PUB_AT,
    )
    # Article beta: no summary, so the snippet falls back to a bounded head of the body.
    beta = Article(
        source_id=source.id,
        url=f"https://x/{uuid.uuid4()}",
        url_hash=uuid.uuid4().hex,
        title="Beta headline",
        summary=None,
        body=_BETA_BODY,
        published_at=OLDER_AT,
    )
    session.add_all([alpha, beta])
    session.flush()

    alpha_item = EvidenceItem(
        source_type="article",
        source_id=str(alpha.id),
        title="Alpha headline",
        publisher="Reuters",
        url="https://news.example/alpha",
        published_at=PUB_AT,
        credibility_score=0.8,
        raw_ref={"do_not": "expose"},
        evidence_metadata={"internal": "do_not_expose"},
    )
    beta_item = EvidenceItem(
        source_type="article",
        source_id=str(beta.id),
        title="Beta headline",
        publisher="Reuters",
        url="https://news.example/beta",
        published_at=OLDER_AT,
    )
    filing_item = EvidenceItem(source_type="filing", source_id="filing-1", title="Quarterly filing")
    session.add_all([alpha_item, beta_item, filing_item])
    session.flush()

    claim_a = Claim(claim_text="Alpha claim.", claim_type="assertion", confidence_score=0.9)
    claim_b = Claim(claim_text="Beta claim.", claim_type="assertion", confidence_score=0.8)
    claim_c = Claim(claim_text="Filing claim.", claim_type="assertion", confidence_score=0.7)
    session.add_all([claim_a, claim_b, claim_c])
    session.flush()
    session.add_all(
        [
            ClaimEvidence(
                claim_id=claim_a.id,
                evidence_item_id=alpha_item.id,
                support_type="supports",
                confidence_score=0.9,
            ),
            ClaimEvidence(
                claim_id=claim_b.id,
                evidence_item_id=beta_item.id,
                support_type="supports",
                confidence_score=0.8,
            ),
            ClaimEvidence(
                claim_id=claim_c.id, evidence_item_id=filing_item.id, support_type="supports"
            ),
        ]
    )

    # v1 published (cites all three claims across two sections + a persisted disclaimer); v2 failed.
    v1 = _brief("published", 1)
    v2 = _brief("failed", 2)
    session.add_all([v1, v2])
    session.flush()
    session.add_all(
        [
            _section(
                v1.id,
                1,
                "Executive Summary",
                "Rates held; markets steady.",
                claim_ids=(claim_a.id, claim_b.id),
            ),
            _section(v1.id, 2, "Top Event", "A regional development.", claim_ids=(claim_c.id,)),
            _section(v1.id, 3, _UNICODE_TITLE, _UNICODE_BODY),  # non-WinAnsi Cyrillic/CJK/Arabic
            _section(v1.id, 4, "Disclaimer", FINAL_DISCLAIMER),
            _section(v2.id, 1, "Executive Summary", "Blocked body.", claim_ids=(claim_a.id,)),
        ]
    )
    session.commit()
    return Seeded(v1_id=v1.id, v2_id=v2.id, article_alpha_id=alpha.id, article_beta_id=beta.id)


@pytest.fixture
def seeded(engine: Engine) -> Seeded:
    with Session(engine) as session:
        return _seed(session)


# --------------------------------------------------------------------------------------
# Bulk attribution, summary/body fallback, and no secret leak
# --------------------------------------------------------------------------------------


def test_markdown_export_resolves_all_sources_safely(client: TestClient, seeded: Seeded) -> None:
    resp = client.get("/api/v1/reports/daily-brief/2026-07-14/export.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/markdown; charset=utf-8"
    body = resp.text

    # Bulk resolution: all three cited sources are attributed (two articles + one filing).
    source_lines = [
        line
        for line in body.split("## Sources")[1].split("## Disclaimer")[0].splitlines()
        if line.startswith("- ")
    ]
    assert len(source_lines) == 3
    assert "Reuters — Alpha headline — https://news.example/alpha" in body
    assert "Reuters — Beta headline — https://news.example/beta" in body
    assert "Quarterly filing" in body  # the non-article source, with no invented snippet

    # Summary-first for alpha; bounded body fallback for beta (no summary).
    assert '"A concise summary of alpha."' in body
    assert "Beta body-fallback detail sentence" in body

    # No article body whole, no raw_ref, no metadata ever leaks.
    assert "SECRET BODY" not in resp.text
    assert "do_not_expose" not in resp.text and '"do_not"' not in resp.text
    assert _BETA_BODY not in resp.text  # only a <=200-char head of beta's body, never the whole

    # The disclaimer is verbatim, exactly once, and last.
    assert resp.text.count(FINAL_DISCLAIMER) == 1
    assert resp.text.rstrip().endswith(FINAL_DISCLAIMER)


def test_pdf_export_is_valid_deterministic_and_leaks_nothing(
    client: TestClient, seeded: Seeded
) -> None:
    first = client.get("/api/v1/reports/daily-brief/2026-07-14/export.pdf")
    assert first.status_code == 200
    assert first.headers["content-type"] == "application/pdf"
    assert (
        first.headers["content-disposition"]
        == 'attachment; filename="daily-brief-2026-07-14-v1.pdf"'
    )
    pdf = first.content
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    assert pdf.count(b"/S /URI") == 2  # alpha and beta canonical links are clickable
    assert b"SECRET BODY" not in pdf and b"do_not_expose" not in pdf

    # Deterministic: the same published snapshot exports byte-identically.
    assert client.get("/api/v1/reports/daily-brief/2026-07-14/export.pdf").content == pdf


def test_pdf_export_retains_non_winansi_unicode(client: TestClient, seeded: Seeded) -> None:
    # The published brief carries a Cyrillic/CJK/Arabic section; the PDF response must embed a
    # real Unicode font and extract back to the exact code points -- never the old WinAnsi '?'.
    resp = client.get("/api/v1/reports/daily-brief/2026-07-14/export.pdf")
    assert resp.status_code == 200
    reader = PdfReader(io.BytesIO(resp.content))
    text = "\n".join(page.extract_text() for page in reader.pages)

    # Cyrillic and CJK survive round-trip exactly (real glyphs resolved through /ToUnicode).
    for needle in ["Мировые рынки", "全球市场", "Ставки удержаны", "利率维持不变"]:
        assert needle in text, needle
    # Arabic code points are retained (visual order, no contextual shaping/RTL reordering).
    assert set("العربية") <= set(text)
    # No script was silently substituted with the WinAnsi '?' the base-14 writer produced.
    assert "?" not in text


# --------------------------------------------------------------------------------------
# Published-only and version behaviour
# --------------------------------------------------------------------------------------


def test_latest_export_serves_the_published_version_not_the_higher_failed_one(
    client: TestClient, seeded: Seeded
) -> None:
    resp = client.get("/api/v1/reports/daily-brief/2026-07-14/export.md")
    assert resp.status_code == 200
    # v2 (failed) has a higher version but latest exports the published v1.
    assert (
        resp.headers["content-disposition"] == 'attachment; filename="daily-brief-2026-07-14-v1.md"'
    )


def test_specific_published_version_exports_but_a_failed_version_is_404(
    client: TestClient, seeded: Seeded
) -> None:
    ok = client.get("/api/v1/reports/daily-brief/2026-07-14/versions/1/export.pdf")
    assert ok.status_code == 200
    assert (
        ok.headers["content-disposition"] == 'attachment; filename="daily-brief-2026-07-14-v1.pdf"'
    )

    blocked = client.get("/api/v1/reports/daily-brief/2026-07-14/versions/2/export.md")
    assert blocked.status_code == 404  # v2 is failed: inspectable via metadata, never exportable


def test_missing_date_and_version_are_404(client: TestClient, seeded: Seeded) -> None:
    assert client.get("/api/v1/reports/daily-brief/2020-01-01/export.md").status_code == 404
    assert (
        client.get("/api/v1/reports/daily-brief/2026-07-14/versions/9/export.pdf").status_code
        == 404
    )


# --------------------------------------------------------------------------------------
# An export writes nothing
# --------------------------------------------------------------------------------------


def test_export_mutates_nothing(client: TestClient, seeded: Seeded, engine: Engine) -> None:
    def _counts() -> tuple[int, int, int]:
        with Session(engine) as session:
            return (
                session.execute(select(func.count()).select_from(Report)).scalar_one(),
                session.execute(select(func.count()).select_from(ReportSection)).scalar_one(),
                session.execute(select(func.count()).select_from(ClaimEvidence)).scalar_one(),
            )

    before = _counts()
    client.get("/api/v1/reports/daily-brief/2026-07-14/export.md")
    client.get("/api/v1/reports/daily-brief/2026-07-14/export.pdf")
    client.get("/api/v1/reports/daily-brief/2026-07-14/versions/1/export.pdf")
    assert _counts() == before

    with Session(engine) as session:
        report = session.get(Report, seeded.v1_id)
        assert report.status == "published" and report.stale is False  # untouched
