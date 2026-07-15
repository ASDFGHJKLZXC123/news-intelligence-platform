"""Deterministic Markdown/PDF export of a *published* daily brief (report-generation spec, S23).

The last read-only step of Stage 6. Given an immutable published-report snapshot and its ordered
sections (both from :mod:`services.reports.lifecycle`, which this never mutates), plus the sources
behind the brief's cited claims, this renders two byte-stable artifacts:

* **Markdown** and **PDF** that preserve the report's title, date, version and its sections'
  exact order and content, and that carry per-source attribution lines followed by the fixed
  financial disclaimer -- always last, always verbatim.

Three disciplines are enforced by construction, not by the caller's care:

* **No article text ever leaves.** Attribution is built only from bounded columns -- the evidence
  item's own title/publisher/url, the article's canonical title/url, and a summary-first excerpt
  read as ``left(col, 200+1)`` -- exactly the Evidence Drawer's rule. ``Article.body`` whole,
  ``EvidenceItem.raw_ref``/``metadata`` and any provider payload are never selected, so they can
  never render. A non-article source invents no article content: it gets no snippet at all.
* **The disclaimer is the canonical template, once, last.** Any persisted "disclaimer" section is
  dropped from the body and :data:`~services.reports.material.FINAL_DISCLAIMER` is appended after
  the source attribution -- never a persisted, possibly-paraphrased copy.
* **Untrusted text cannot forge structure.** Article-derived attribution fields are stripped of
  newlines/control characters (so a title cannot open a second source line) and Markdown-escaped
  (so it cannot open a link or emphasis); the PDF writer neutralises the same for its own syntax.

One bounded bulk query resolves every cited claim's sources at once (no N+1); both the claim-ref
count and the returned attribution rows are bounded and fail loud rather than truncate silently.

Framework-agnostic: it holds no web framework and no ORM rows -- it takes detached snapshots and a
:class:`~sqlalchemy.orm.Session`-backed repository, exactly like :mod:`services.reports.lifecycle`.
"""

from __future__ import annotations

import datetime
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Text, and_, cast, func, select
from sqlalchemy.orm import Session

from db.models.core import Article, ClaimEvidence, EvidenceItem, Source
from services.reports._pdf import PdfDocument, is_safe_url
from services.reports.context import (
    ARTICLE_EVIDENCE_SOURCE_TYPE,
    MAX_EXCERPT_CHARS,
    build_source_excerpt,
)
from services.reports.lifecycle import ReportSectionSnapshot, ReportSnapshot
from services.reports.material import FINAL_DISCLAIMER

#: The snippet cap the copyright rule fixes for exports (spec S23.2: "snippets <= 200 chars").
#: Reused from the composition context so the boundary read and the export agree on one number.
MAX_SNIPPET_CHARS = MAX_EXCERPT_CHARS

#: A published brief citing more distinct claims than this is an upstream fault, not a long brief;
#: the ref set is refused whole rather than passed truncated into the ``IN`` list (fail loud).
MAX_REPORT_CLAIM_REFS = 500

#: The attribution query over-reads by one and refuses more than this many distinct sources, so a
#: truncated source list can never be mistaken for the complete one.
ATTRIBUTION_ROW_LIMIT = 500


class ReportExportError(RuntimeError):
    """Base class for an export fault the caller must surface, never paper over with partial data."""


class ClaimReferenceOverflowError(ReportExportError):
    """A brief cited more distinct claims than :data:`MAX_REPORT_CLAIM_REFS`; refused, not truncated."""


class AttributionOverflowError(ReportExportError):
    """The attribution query found more sources than :data:`ATTRIBUTION_ROW_LIMIT`; fail loud."""


# --------------------------------------------------------------------------------------
# Immutable records
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributionRow:
    """One ``claim_evidence -> evidence_item (-> article/source)`` join row, as loaded.

    Carries only attribution-safe columns: never ``Article.body`` whole, never ``raw_ref`` or
    ``metadata``. The body/summary arrive as bounded ``left(col, MAX+1)`` heads plus their true
    lengths, exactly as the Evidence Drawer reads them.
    """

    evidence_item_id: uuid.UUID
    source_type: str
    item_title: str
    item_publisher: str | None
    item_url: str | None
    published_at: datetime.datetime | None
    article_title: str | None
    article_url: str | None
    source_name: str | None
    summary_head: str | None
    body_head: str | None
    summary_length: int | None
    body_length: int | None


@dataclass(frozen=True)
class SourceAttribution:
    """One deduplicated, render-ready source line. Fields are already single-lined and safe.

    ``snippet`` is present only for an article-typed source whose article resolved, is derived
    summary-first then body (:func:`~services.reports.context.build_source_excerpt`), and is
    ``<= MAX_SNIPPET_CHARS``. A non-article source never carries one.
    """

    evidence_item_id: uuid.UUID
    source_type: str
    attribution: str | None
    title: str
    url: str | None
    snippet: str | None
    published_at: datetime.datetime | None


# --------------------------------------------------------------------------------------
# Sanitisation (untrusted, article-derived text)
# --------------------------------------------------------------------------------------

#: Markdown inline metacharacters escaped in attribution fields so article-derived text renders as
#: literal text -- never a link, emphasis, code span, raw HTML, strikethrough or table cell. The
#: line-structural characters (``#``/``-``/``+``/``.``) are omitted deliberately: a sanitised field
#: is always single-lined and sits *after* a prefix, so it is never at a line start where they act.
_MARKDOWN_ESCAPE = set("\\`*_[]<>|~")


def _sanitize_inline(text: str) -> str:
    """Collapse a value to a single safe line: every control/format/whitespace run becomes a space.

    This is the guarantee that no article title, publisher or snippet can inject a newline and so
    forge a second source record, and that no zero-width/format character survives. It only ever
    shortens, so a ``<= MAX_SNIPPET_CHARS`` snippet stays within bound.
    """
    cleaned = "".join(
        " " if (ch.isspace() or unicodedata.category(ch).startswith("C")) else ch for ch in text
    )
    return " ".join(cleaned.split())


def _markdown_escape(text: str) -> str:
    return "".join("\\" + ch if ch in _MARKDOWN_ESCAPE else ch for ch in text)


# --------------------------------------------------------------------------------------
# Pure builder: rows -> deduplicated, ordered attribution
# --------------------------------------------------------------------------------------


def _order_key(item: SourceAttribution) -> tuple[int, float, str]:
    """Newest source first, undated last, ties broken by the (unique) evidence-item id.

    Timezone-safe and machine-independent: a naive timestamp is read as UTC and an aware one is
    normalised to UTC before ``.timestamp()``, so the order never depends on the host's timezone.
    """
    published = item.published_at
    if published is None:
        return (1, 0.0, str(item.evidence_item_id))
    if published.tzinfo is None:
        published = published.replace(tzinfo=datetime.UTC)
    return (0, -published.astimezone(datetime.UTC).timestamp(), str(item.evidence_item_id))


def build_source_attributions(rows: Sequence[AttributionRow]) -> tuple[SourceAttribution, ...]:
    """Reduce flat join rows to one safe, ordered line per distinct source. Pure; no session.

    De-duplicates by ``evidence_item_id`` (one source, one line, however many claims cited it),
    derives the article-only summary-first snippet, and folds the item's own attribution together
    with the article's canonical publisher/url the way the Evidence Drawer does -- then sanitises
    every field so nothing article-derived can break out of its line.
    """
    seen: dict[uuid.UUID, SourceAttribution] = {}
    for row in rows:
        if row.evidence_item_id in seen:
            continue
        is_article = (
            row.source_type == ARTICLE_EVIDENCE_SOURCE_TYPE and row.article_title is not None
        )
        snippet: str | None = None
        if is_article:
            excerpt = build_source_excerpt(
                row.summary_head,
                row.body_head,
                summary_length=row.summary_length,
                body_length=row.body_length,
            )
            if not excerpt.is_empty:
                snippet = _sanitize_inline(excerpt.text)[:MAX_SNIPPET_CHARS]
        attribution = row.item_publisher or (row.source_name if is_article else None)
        url = row.item_url or (row.article_url if is_article else None)
        seen[row.evidence_item_id] = SourceAttribution(
            evidence_item_id=row.evidence_item_id,
            source_type=row.source_type,
            attribution=_sanitize_inline(attribution) if attribution else None,
            title=_sanitize_inline(row.item_title),
            url=_sanitize_inline(url) if url else None,
            snippet=snippet or None,
            published_at=row.published_at,
        )
    return tuple(sorted(seen.values(), key=_order_key))


# --------------------------------------------------------------------------------------
# Claim-ref collection (bounded)
# --------------------------------------------------------------------------------------


def collect_claim_refs(
    sections: Sequence[ReportSectionSnapshot],
) -> tuple[uuid.UUID, ...]:
    """Every distinct claim id the report cites, first-occurrence order. Bounded, fail loud.

    The ids come from each section's ``evidence_refs`` (already claim ids by construction). More
    than :data:`MAX_REPORT_CLAIM_REFS` distinct ids is an upstream fault; the whole set is refused
    rather than a truncated ``IN`` list silently under-attributing the brief.
    """
    seen: dict[uuid.UUID, None] = {}
    for section in sections:
        for ref in section.evidence_refs:
            seen.setdefault(ref, None)
    if len(seen) > MAX_REPORT_CLAIM_REFS:
        raise ClaimReferenceOverflowError(
            f"report cites {len(seen)} distinct claims, over the {MAX_REPORT_CLAIM_REFS} bound"
        )
    return tuple(seen)


# --------------------------------------------------------------------------------------
# Renderers (share one block sequence so Markdown and PDF stay in step)
# --------------------------------------------------------------------------------------


def _is_disclaimer_section(section: ReportSectionSnapshot) -> bool:
    """A persisted disclaimer section, matched by its verbatim body or its title. Dropped from body.

    Matched robustly rather than trusted: the canonical template is appended by the exporter, so a
    persisted disclaimer (verbatim or paraphrased) is never the one that ships.
    """
    return (
        section.body.strip() == FINAL_DISCLAIMER
        or section.title.strip().casefold() == "disclaimer"
    )


def _meta_line(report: ReportSnapshot) -> tuple[str, str, str]:
    brief_date = report.brief_date.isoformat() if report.brief_date else "n/a"
    return (brief_date, str(report.version), report.status)


def _body_sections(
    sections: Sequence[ReportSectionSnapshot],
) -> tuple[ReportSectionSnapshot, ...]:
    """The report's sections in order, with any persisted disclaimer dropped from the body."""
    return tuple(section for section in sections if not _is_disclaimer_section(section))


def render_markdown(
    report: ReportSnapshot,
    sections: Sequence[ReportSectionSnapshot],
    attributions: Sequence[SourceAttribution],
) -> str:
    """Deterministic Markdown for a published brief. Title/date/version and section order preserved.

    Section bodies render verbatim (they are the report's own grounded content). Only the
    attribution lines carry article-derived text, and those are sanitised and escaped. Sources come
    before the disclaimer so the disclaimer stays last, and the disclaimer is the fixed template.
    """
    brief_date, version, status = _meta_line(report)
    lines: list[str] = [
        f"# {_sanitize_inline(report.title)}",
        "",
        f"**Brief date:** {brief_date} · **Version:** {version} · **Status:** {status}",
        "",
    ]
    for section in _body_sections(sections):
        lines.append(f"## {_sanitize_inline(section.title)}")
        lines.append("")
        lines.append(section.body)
        lines.append("")
    lines.append("## Sources")
    lines.append("")
    if attributions:
        lines.extend(_markdown_source_line(item) for item in attributions)
    else:
        lines.append("_No source citations are recorded for this brief._")
    lines.append("")
    lines.append("## Disclaimer")
    lines.append("")
    lines.append(FINAL_DISCLAIMER)
    lines.append("")
    return "\n".join(lines)


def _markdown_source_line(item: SourceAttribution) -> str:
    segments: list[str] = []
    if item.attribution:
        segments.append(_markdown_escape(item.attribution))
    segments.append(_markdown_escape(item.title))
    if item.url:
        segments.append(_markdown_escape(item.url))
    line = "- " + " — ".join(segments)
    if item.snippet:
        line += f' — "{_markdown_escape(item.snippet)}"'
    return line


def render_pdf(
    report: ReportSnapshot,
    sections: Sequence[ReportSectionSnapshot],
    attributions: Sequence[SourceAttribution],
) -> bytes:
    """Deterministic PDF for a published brief -- the same content as :func:`render_markdown`.

    Uses the no-dependency base-14 writer (:mod:`services.reports._pdf`): safe ``http(s)`` source
    URLs become clickable link annotations; everything else is inert text.
    """
    title = _sanitize_inline(report.title)
    brief_date, version, status = _meta_line(report)
    doc = PdfDocument(title=title)
    doc.title(title)
    doc.paragraph(f"Brief date: {brief_date} · Version: {version} · Status: {status}")
    for section in _body_sections(sections):
        doc.heading(_sanitize_inline(section.title))
        doc.paragraph(section.body)
    doc.heading("Sources")
    if attributions:
        for item in attributions:
            doc.bullet(_pdf_source_text(item))
            if item.url and is_safe_url(item.url):
                doc.link(item.url)
    else:
        doc.paragraph("No source citations are recorded for this brief.")
    doc.heading("Disclaimer")
    doc.paragraph(FINAL_DISCLAIMER)
    return doc.render()


def _pdf_source_text(item: SourceAttribution) -> str:
    segments: list[str] = []
    if item.attribution:
        segments.append(item.attribution)
    segments.append(item.title)
    # A safe URL is rendered as its own clickable line; only an unsafe one is inlined as text.
    if item.url and not is_safe_url(item.url):
        segments.append(item.url)
    text = " — ".join(segments)
    if item.snippet:
        text += f' — "{item.snippet}"'
    return text


def export_filename(report: ReportSnapshot, extension: str) -> str:
    """A safe, deterministic download filename: ``daily-brief-YYYY-MM-DD-vN.<ext>``.

    Built only from the typed ``brief_date`` and integer ``version``, so it is all digits, hyphens
    and the fixed stem -- nothing user- or article-controlled can reach the ``Content-Disposition``.
    """
    brief_date = report.brief_date.isoformat() if report.brief_date else "unknown-date"
    return f"daily-brief-{brief_date}-v{report.version}.{extension}"


# --------------------------------------------------------------------------------------
# Repository -- one bounded bulk query, reads only
# --------------------------------------------------------------------------------------


class ReportExportRepository:
    """Resolves a report's cited claims to their sources over a caller-owned session. Read-only."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def source_attributions_for_report(
        self, claim_ids: Sequence[uuid.UUID]
    ) -> tuple[SourceAttribution, ...]:
        """Every distinct source behind ``claim_ids``, resolved in one bounded query, then reduced.

        A single ``claim_evidence -> evidence_items`` join (left-joining ``articles``/``sources``
        only for article-typed items, the same discriminator the composition context uses) fetches
        every cited claim's sources at once -- never one query per claim. ``DISTINCT`` collapses the
        many claims that may share a source; an over-read of one row past the bound makes an
        oversize source set fail loud instead of truncating.
        """
        refs = list(dict.fromkeys(claim_ids))
        if not refs:
            return ()
        if len(refs) > MAX_REPORT_CLAIM_REFS:
            raise ClaimReferenceOverflowError(
                f"report cites {len(refs)} distinct claims, over the {MAX_REPORT_CLAIM_REFS} bound"
            )

        summary_head = func.left(Article.summary, MAX_SNIPPET_CHARS + 1)
        body_head = func.left(Article.body, MAX_SNIPPET_CHARS + 1)
        stmt = (
            select(
                EvidenceItem.id.label("evidence_item_id"),
                EvidenceItem.source_type,
                EvidenceItem.title.label("item_title"),
                EvidenceItem.publisher.label("item_publisher"),
                EvidenceItem.url.label("item_url"),
                EvidenceItem.published_at,
                Article.title.label("article_title"),
                Article.url.label("article_url"),
                Source.name.label("source_name"),
                summary_head.label("summary_head"),
                body_head.label("body_head"),
                func.length(Article.summary).label("summary_length"),
                func.length(Article.body).label("body_length"),
            )
            .select_from(ClaimEvidence)
            .join(EvidenceItem, EvidenceItem.id == ClaimEvidence.evidence_item_id)
            .outerjoin(
                Article,
                and_(
                    EvidenceItem.source_type == ARTICLE_EVIDENCE_SOURCE_TYPE,
                    EvidenceItem.source_id == cast(Article.id, Text),
                ),
            )
            .outerjoin(Source, Source.id == Article.source_id)
            .where(ClaimEvidence.claim_id.in_(refs))
            .distinct()
            .order_by(EvidenceItem.id)
            # Over-read by one so an oversize source set fails loud rather than truncating.
            .limit(ATTRIBUTION_ROW_LIMIT + 1)
        )
        result = self._session.execute(stmt).all()
        if len(result) > ATTRIBUTION_ROW_LIMIT:
            raise AttributionOverflowError(
                f"report resolved more than {ATTRIBUTION_ROW_LIMIT} sources; refusing a "
                "truncated attribution list"
            )
        rows = [
            AttributionRow(
                evidence_item_id=row.evidence_item_id,
                source_type=row.source_type,
                item_title=row.item_title,
                item_publisher=row.item_publisher,
                item_url=row.item_url,
                published_at=row.published_at,
                article_title=row.article_title,
                article_url=row.article_url,
                source_name=row.source_name,
                summary_head=row.summary_head,
                body_head=row.body_head,
                summary_length=row.summary_length,
                body_length=row.body_length,
            )
            for row in result
        ]
        return build_source_attributions(rows)


__all__ = [
    "ATTRIBUTION_ROW_LIMIT",
    "MAX_REPORT_CLAIM_REFS",
    "MAX_SNIPPET_CHARS",
    "AttributionOverflowError",
    "AttributionRow",
    "ClaimReferenceOverflowError",
    "ReportExportError",
    "ReportExportRepository",
    "SourceAttribution",
    "build_source_attributions",
    "collect_claim_refs",
    "export_filename",
    "render_markdown",
    "render_pdf",
]
