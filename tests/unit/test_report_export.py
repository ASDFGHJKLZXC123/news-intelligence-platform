"""Stage 6 export service: deterministic Markdown/PDF, safe attribution, and its bounds.

No database and no web framework here -- the renderers and the pure attribution builder are driven
against hand-built snapshots/rows. What can only be proven against real SQL (the bulk join, the
summary/body fallback, that no article body or raw payload is ever selected) is in
``tests/integration/test_stage6_export.py``; the DB-free API wiring is in
``tests/unit/test_report_export_api.py``.
"""

from __future__ import annotations

import datetime
import io
import re
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfReader

from services.reports.exports import (
    ATTRIBUTION_ROW_LIMIT,
    MAX_REPORT_CLAIM_REFS,
    MAX_SNIPPET_CHARS,
    AttributionOverflowError,
    AttributionRow,
    ClaimReferenceOverflowError,
    ReportExportRepository,
    SourceAttribution,
    build_source_attributions,
    collect_claim_refs,
    export_filename,
    render_markdown,
    render_pdf,
)
from services.reports.lifecycle import ReportSectionSnapshot, ReportSnapshot, SectionBlock
from services.reports.material import FINAL_DISCLAIMER

NOW = datetime.datetime(2026, 7, 14, 4, tzinfo=datetime.UTC)
BRIEF_DATE = datetime.date(2026, 7, 14)


# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def _snap(
    *, version: int = 1, status: str = "published", title: str | None = None
) -> ReportSnapshot:
    return ReportSnapshot(
        id=uuid.uuid4(),
        user_id=None,
        report_type="daily_brief",
        brief_date=BRIEF_DATE,
        event_id=None,
        title=title or "Daily Brief — 2026-07-14",
        status=status,
        version=version,
        change_reason=None,
        stale=False,
        generated_by_run_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _section(
    order: int, title: str, body: str, *, claim_ids: tuple[uuid.UUID, ...] = ()
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
        grounding_status="passed",
    )


def _disclaimer_section(order: int) -> ReportSectionSnapshot:
    return _section(order, "Disclaimer", FINAL_DISCLAIMER)


def _article_row(
    *,
    item_id: uuid.UUID | None = None,
    publisher: str | None = "Reuters",
    title: str = "Bank under pressure",
    url: str | None = "https://news.example/bank",
    published_at: datetime.datetime | None = NOW,
    summary_head: str | None = "A concise summary of the article.",
    body_head: str | None = None,
    summary_length: int | None = None,
    body_length: int | None = None,
    source_name: str | None = "Reuters",
    article_title: str | None = "Bank under pressure",
    article_url: str | None = "https://news.example/bank",
) -> AttributionRow:
    return AttributionRow(
        evidence_item_id=item_id or uuid.uuid4(),
        source_type="article",
        item_title=title,
        item_publisher=publisher,
        item_url=url,
        published_at=published_at,
        article_title=article_title,
        article_url=article_url,
        source_name=source_name,
        summary_head=summary_head,
        body_head=body_head,
        summary_length=summary_length
        if summary_length is not None
        else (len(summary_head) if summary_head else None),
        body_length=body_length
        if body_length is not None
        else (len(body_head) if body_head else None),
    )


def _filing_row(
    *, item_id: uuid.UUID | None = None, title: str = "Quarterly filing"
) -> AttributionRow:
    return AttributionRow(
        evidence_item_id=item_id or uuid.uuid4(),
        source_type="filing",
        item_title=title,
        item_publisher=None,
        item_url=None,
        published_at=None,
        article_title=None,
        article_url=None,
        source_name=None,
        summary_head=None,
        body_head=None,
        summary_length=None,
        body_length=None,
    )


# --------------------------------------------------------------------------------------
# PDF text extraction helper (a real parser reads the embedded-font text back out)
# --------------------------------------------------------------------------------------


def _pdf_text(pdf: bytes) -> str:
    """Extract the drawn text of a rendered PDF via ``pypdf`` (a real extractor), pages joined.

    The exporter now embeds a Unicode font and draws text as glyph ids resolved through the
    font's ``/ToUnicode`` map, so the only faithful way to read it back is a real PDF parser --
    which is exactly what proves the original code points survived (no WinAnsi ``?`` fallback).
    """
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() for page in reader.pages)


# --------------------------------------------------------------------------------------
# Markdown: exact, deterministic, ordered, disclaimer last
# --------------------------------------------------------------------------------------


def test_markdown_is_exact_and_deterministic() -> None:
    report = _snap(version=2)
    sections = (
        _section(1, "Executive Summary", "Rates held."),
        _section(2, "Risk Radar", "Risk eased."),
        _disclaimer_section(3),  # a persisted disclaimer -> dropped from the body
    )
    attributions = build_source_attributions([_article_row(summary_head="A concise summary.")])

    markdown = render_markdown(report, sections, attributions)

    expected = (
        "# Daily Brief — 2026-07-14\n"
        "\n"
        "**Brief date:** 2026-07-14 · **Version:** 2 · **Status:** published\n"
        "\n"
        "## Executive Summary\n"
        "\n"
        "Rates held.\n"
        "\n"
        "## Risk Radar\n"
        "\n"
        "Risk eased.\n"
        "\n"
        "## Sources\n"
        "\n"
        '- Reuters — Bank under pressure — https://news.example/bank — "A concise summary."\n'
        "\n"
        "## Disclaimer\n"
        "\n"
        f"{FINAL_DISCLAIMER}\n"
    )
    assert markdown == expected
    assert render_markdown(report, sections, attributions) == markdown  # deterministic


def test_markdown_preserves_section_order_and_content() -> None:
    report = _snap()
    sections = tuple(_section(i, f"Section {i}", f"Body {i}.") for i in range(1, 6))
    markdown = render_markdown(report, sections, ())
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]
    # The report's own sections, in order, then Sources, then Disclaimer -- always last.
    assert headings == [
        "## Section 1",
        "## Section 2",
        "## Section 3",
        "## Section 4",
        "## Section 5",
        "## Sources",
        "## Disclaimer",
    ]


def test_disclaimer_is_verbatim_exactly_once_and_final() -> None:
    report = _snap()
    # Two persisted disclaimer copies (verbatim body, and a "Disclaimer"-titled one) are both dropped.
    sections = (
        _section(1, "Executive Summary", "Rates held."),
        _disclaimer_section(2),
        _section(3, "Disclaimer", "some paraphrased disclaimer text"),
    )
    markdown = render_markdown(report, sections, ())
    assert markdown.count(FINAL_DISCLAIMER) == 1
    assert "paraphrased" not in markdown  # a persisted paraphrase never ships
    assert markdown.rstrip().endswith(FINAL_DISCLAIMER)
    # Sources come strictly before the final disclaimer.
    assert markdown.index("## Sources") < markdown.index("## Disclaimer")


def test_markdown_empty_citations_states_it_plainly() -> None:
    markdown = render_markdown(_snap(), (_section(1, "Executive Summary", "Quiet day."),), ())
    sources = markdown.split("## Sources")[1].split("## Disclaimer")[0]
    assert "_No source citations are recorded for this brief._" in sources
    assert not [line for line in sources.splitlines() if line.startswith("- ")]


# --------------------------------------------------------------------------------------
# Source attribution: dedupe, order, snippet
# --------------------------------------------------------------------------------------


def test_attribution_dedupes_by_source_and_orders_newest_first_undated_last() -> None:
    shared = uuid.uuid4()
    older = _article_row(
        item_id=uuid.uuid4(),
        title="Older",
        published_at=datetime.datetime(2026, 7, 10, tzinfo=datetime.UTC),
    )
    newer = _article_row(
        item_id=uuid.uuid4(),
        title="Newer",
        published_at=datetime.datetime(2026, 7, 13, tzinfo=datetime.UTC),
    )
    # The same source appears twice (two claims cited it); it collapses to one line.
    dup_a = _article_row(item_id=shared, title="Shared")
    dup_b = _article_row(item_id=shared, title="Shared")
    undated = _filing_row(title="Undated filing")

    built = build_source_attributions([undated, older, dup_a, newer, dup_b])

    assert len(built) == 4  # the duplicate source collapsed
    titles = [item.title for item in built]
    assert titles[-1] == "Undated filing"  # undated sorts last
    dated = titles[:-1]
    assert dated.index("Newer") < dated.index("Older")  # newest first


def test_article_snippet_is_summary_first_then_body_and_capped() -> None:
    summary_only = build_source_attributions(
        [_article_row(summary_head="The summary.", body_head="The body.")]
    )
    assert summary_only[0].snippet == "The summary."

    body_fallback = build_source_attributions(
        [_article_row(summary_head=None, body_head="The body head.")]
    )
    assert body_fallback[0].snippet == "The body head."

    long_body = "x" * (MAX_SNIPPET_CHARS + 50)
    capped = build_source_attributions(
        [
            _article_row(
                summary_head=None,
                body_head=long_body[: MAX_SNIPPET_CHARS + 1],
                body_length=len(long_body),
            )
        ]
    )
    assert capped[0].snippet is not None and len(capped[0].snippet) <= MAX_SNIPPET_CHARS


def test_non_article_source_never_invents_a_snippet() -> None:
    built = build_source_attributions([_filing_row()])
    assert built[0].snippet is None
    assert built[0].attribution is None  # no publisher, and no article to borrow a source name from
    assert built[0].title == "Quarterly filing"


def test_article_typed_row_without_a_resolved_article_gets_no_snippet() -> None:
    # source_type=article but the LEFT JOIN found no article (article_title is None): no snippet,
    # and it never borrows the article's canonical name/url it does not have.
    orphan = AttributionRow(
        evidence_item_id=uuid.uuid4(),
        source_type="article",
        item_title="Orphaned item",
        item_publisher=None,
        item_url=None,
        published_at=None,
        article_title=None,
        article_url=None,
        source_name=None,
        summary_head="should not be used",
        body_head=None,
        summary_length=18,
        body_length=None,
    )
    built = build_source_attributions([orphan])
    assert built[0].snippet is None
    assert built[0].attribution is None
    assert built[0].url is None


# --------------------------------------------------------------------------------------
# Safety: injection cannot forge records or leak, control chars neutralised
# --------------------------------------------------------------------------------------


def test_malicious_attribution_cannot_forge_extra_markdown_records() -> None:
    evil = _article_row(
        title="Evil\n- FAKE SOURCE\n\n## Injected Heading",
        publisher="Bad\r\nActor",
        summary_head="line one\nline two\t[x](javascript:alert(1))",
    )
    markdown = render_markdown(
        _snap(), (_section(1, "Executive Summary", "Body."),), build_source_attributions([evil])
    )
    sources = markdown.split("## Sources")[1].split("## Disclaimer")[0]
    # Exactly one source bullet: the injected newline/bullet/heading did not create records.
    assert len([line for line in sources.splitlines() if line.startswith("- ")]) == 1
    # The injected "## Injected Heading"/"- FAKE SOURCE" survive only as inert mid-line text --
    # never at a line start, where they would be a real heading or a forged bullet.
    assert not any(line.lstrip().startswith("## Injected") for line in markdown.splitlines())
    assert not any(line.strip() == "- FAKE SOURCE" for line in markdown.splitlines())
    # The link/emphasis metacharacters are escaped, so no live link forms.
    assert "[x](javascript:alert(1))" not in sources
    assert "\\[x\\]" in sources


def test_malicious_attribution_cannot_break_the_pdf() -> None:
    evil = _article_row(
        title="A) forged (obj) \\ stream endobj",
        publisher="X\nY",
        summary_head="ctrl\x00\x07chars\nand newline",
    )
    pdf = render_pdf(
        _snap(), (_section(1, "Executive Summary", "Body."),), build_source_attributions([evil])
    )
    # Still a single valid PDF that a real parser accepts: the drawn text is embedded-font glyph
    # ids, never a literal PDF string, so the parens/backslash/PDF keywords cannot forge structure.
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    assert pdf.count(b"/Type /Catalog") == 1
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1  # the injected "stream"/"endobj" did not fabricate objects/pages
    # The forged tokens never appear as literal bytes: they were drawn as glyph ids, not syntax.
    assert b"forged (obj)" not in pdf
    assert b"stream endobj" not in pdf
    assert (
        render_pdf(
            _snap(), (_section(1, "Executive Summary", "Body."),), build_source_attributions([evil])
        )
        == pdf
    )


# --------------------------------------------------------------------------------------
# PDF: valid, deterministic, multi-page, unicode, links, disclaimer last
# --------------------------------------------------------------------------------------


def test_pdf_is_valid_and_deterministic() -> None:
    report = _snap()
    sections = (_section(1, "Executive Summary", "Rates held."),)
    attributions = build_source_attributions([_article_row()])
    a = render_pdf(report, sections, attributions)
    b = render_pdf(report, sections, attributions)
    assert a == b
    assert a.startswith(b"%PDF-")
    assert a.rstrip().endswith(b"%%EOF")
    assert a.count(b"/Type /Catalog") == 1
    # xref offset points at the classic cross-reference table.
    offset = int(re.search(rb"startxref\s+(\d+)", a).group(1))
    assert a[offset : offset + 4] == b"xref"


def test_pdf_paginates_across_multiple_pages() -> None:
    sections = tuple(
        _section(i, f"Section {i}", ("The quick brown fox jumps over the lazy dog. " * 8))
        for i in range(1, 40)
    )
    pdf = render_pdf(_snap(), sections, ())
    # Count the /Type /Page objects (excluding the single /Type /Pages tree node).
    page_objs = len(re.findall(rb"/Type\s*/Page(?!s)", pdf))
    count_in_tree = int(re.search(rb"/Count\s+(\d+)", pdf).group(1))
    assert page_objs >= 2
    assert count_in_tree == page_objs  # the page tree agrees with the number of page objects
    assert len(PdfReader(io.BytesIO(pdf)).pages) == page_objs  # and a real parser sees the same


def test_pdf_retains_cyrillic_and_cjk_exactly_with_no_question_marks() -> None:
    # Cyrillic and CJK in the title, a section heading/body, and a source attribution -- every
    # one must extract back to its exact original code points (real glyphs, never a WinAnsi '?').
    report = _snap(title="Ежедневный обзор — 每日简报 — café")
    sections = (
        _section(1, "Сводка — 概要", "Ставки удержаны. 利率维持不变. Naïve café — coöperate."),
    )
    rows = [
        _article_row(
            title="Банк 银行 под давлением",
            url="https://news.example/bank",
            publisher="Рейтер",
            summary_head="краткое изложение 概要",
        )
    ]
    text = _pdf_text(render_pdf(report, sections, build_source_attributions(rows)))
    for needle in [
        "Ежедневный обзор",
        "每日简报",
        "café",
        "Сводка",
        "概要",
        "Ставки удержаны",
        "利率维持不变",
        "Банк",
        "银行",
        "краткое изложение",
    ]:
        assert needle in text, needle
    assert "?" not in text  # the old WinAnsi '?' substitution for non-Latin scripts is gone


def test_pdf_retains_arabic_code_points_even_without_shaping() -> None:
    # Arabic is drawn in visual/logical order without contextual shaping or RTL reordering, so
    # extraction may reorder the run -- but every Arabic code point must survive, not become '?'.
    arabic = "العربية"
    pdf = render_pdf(_snap(title="Brief"), (_section(1, "خبر عربي", f"نص {arabic} مهم"),), ())
    text = _pdf_text(pdf)
    assert set(arabic) <= set(text)  # all code points retained (order may be visual, not logical)
    assert set("خبر عربي") <= set(text)
    assert "?" not in text


def test_pdf_makes_safe_source_urls_clickable_links() -> None:
    rows = [
        _article_row(item_id=uuid.uuid4(), url="https://news.example/one"),
        _article_row(item_id=uuid.uuid4(), url="https://news.example/two"),
        # An unsafe scheme is never turned into a link annotation.
        _article_row(
            item_id=uuid.uuid4(), url="javascript:alert(1)", article_url="javascript:alert(1)"
        ),
    ]
    pdf = render_pdf(
        _snap(), (_section(1, "Executive Summary", "Body."),), build_source_attributions(rows)
    )
    assert pdf.count(b"/S /URI") == 2  # only the two http(s) URLs are clickable
    assert b"javascript:alert(1)" not in pdf.split(b"/S /URI")[0] or pdf.count(b"/S /URI") == 2


def test_pdf_disclaimer_is_present_once_and_after_sources() -> None:
    pdf = render_pdf(_snap(), (_section(1, "Executive Summary", "Rates held."),), ())
    text = re.sub(r"\s+", " ", _pdf_text(pdf))
    assert text.count(re.sub(r"\s+", " ", FINAL_DISCLAIMER)) == 1
    assert text.index("Sources") < text.index("informational purposes only")


# --------------------------------------------------------------------------------------
# Filenames and bounds
# --------------------------------------------------------------------------------------


def test_export_filename_is_safe_and_deterministic() -> None:
    report = _snap(version=7)
    assert export_filename(report, "md") == "daily-brief-2026-07-14-v7.md"
    assert export_filename(report, "pdf") == "daily-brief-2026-07-14-v7.pdf"


def test_collect_claim_refs_dedupes_in_first_occurrence_order() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sections = (
        _section(1, "Executive Summary", "x", claim_ids=(a, b)),
        _section(2, "Top Event", "y", claim_ids=(b, c, a)),
    )
    assert collect_claim_refs(sections) == (a, b, c)


def test_collect_claim_refs_fails_loud_past_its_bound() -> None:
    sections = tuple(
        _section(i, f"S{i}", "x", claim_ids=(uuid.uuid4(),))
        for i in range(MAX_REPORT_CLAIM_REFS + 1)
    )
    with pytest.raises(ClaimReferenceOverflowError):
        collect_claim_refs(sections)


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Returns a fixed row set for any statement -- exercises the repository's bound, not SQL."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def execute(self, _stmt: Any) -> _FakeResult:
        return _FakeResult(self._rows)


def _fake_db_row(item_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        evidence_item_id=item_id,
        source_type="filing",
        item_title="Filing",
        item_publisher=None,
        item_url=None,
        published_at=None,
        article_title=None,
        article_url=None,
        source_name=None,
        summary_head=None,
        body_head=None,
        summary_length=None,
        body_length=None,
    )


def test_attribution_query_fails_loud_past_the_row_bound() -> None:
    rows = [_fake_db_row(uuid.uuid4()) for _ in range(ATTRIBUTION_ROW_LIMIT + 1)]
    repo = ReportExportRepository(_FakeSession(rows))
    with pytest.raises(AttributionOverflowError):
        repo.source_attributions_for_report([uuid.uuid4()])


def test_attribution_query_short_circuits_with_no_claim_refs() -> None:
    # No refs -> no query is issued at all (the fake would raise if it were).
    class _BoomSession:
        def execute(self, _stmt: Any) -> Any:
            raise AssertionError("no query should run for an empty ref set")

    assert ReportExportRepository(_BoomSession()).source_attributions_for_report([]) == ()


def test_attribution_query_rejects_an_oversize_ref_set_before_querying() -> None:
    class _BoomSession:
        def execute(self, _stmt: Any) -> Any:
            raise AssertionError("the ref set is over-bound; no query should run")

    refs = [uuid.uuid4() for _ in range(MAX_REPORT_CLAIM_REFS + 1)]
    with pytest.raises(ClaimReferenceOverflowError):
        ReportExportRepository(_BoomSession()).source_attributions_for_report(refs)


def test_source_attribution_carries_no_body_or_raw_channel() -> None:
    # The record the renderer sees exposes only attribution-safe fields -- there is no field a
    # full article body, raw_ref or metadata could ever travel in.
    fields = set(SourceAttribution.__dataclass_fields__)
    assert fields == {
        "evidence_item_id",
        "source_type",
        "attribution",
        "title",
        "url",
        "snippet",
        "published_at",
    }
