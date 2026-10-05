"""Immutable reads and exports for one workspace's personal briefs.

Personal report citations deliberately resolve from ``PersonalBriefSnapshot.input_payload``.
They never join back to the mutable article, claim, or evidence tables after publication.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    PersonalBriefSnapshot,
    PersonalReportLink,
    PersonalRun,
    PersonalWorkspace,
    Report,
)
from db.models.core import REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
from db.models.personal import PERSONAL_REPORT_TYPE
from services.reports._pdf import is_safe_url
from services.reports.exports import (
    MAX_SNIPPET_CHARS,
    SourceAttribution,
    collect_claim_refs,
    render_markdown,
    render_pdf,
)
from services.reports.lifecycle import (
    ReportLifecycleRepository,
    ReportSectionSnapshot,
    ReportSnapshot,
)
from services.reports.material import FINAL_DISCLAIMER

MAX_HISTORY_ROWS = 100
MAX_SOURCE_TITLE_CHARS = 512
MAX_SOURCE_PUBLISHER_CHARS = 512
MAX_SOURCE_URL_CHARS = 4_096

_COVERAGE_COUNT_FIELDS = (
    "feeds_configured",
    "feeds_attempted",
    "feeds_succeeded",
    "feeds_failed",
    "feeds_paused",
    "items_fetched",
    "articles_captured",
    "pending_total",
    "pending_capacity",
)
_COVERAGE_DETAIL_FIELDS = ("feed_failures", "feed_pauses")

_EXPORT_SECTION_NAMESPACE = uuid.UUID("898fd4c9-447c-4c36-b180-41065e9bb9bb")


class PersonalBriefReadError(RuntimeError):
    """A linked personal report has an internally inconsistent immutable contract."""


@dataclass(frozen=True)
class PersonalEvidence:
    evidence_item_id: uuid.UUID
    article_id: uuid.UUID
    source_field: str
    title: str
    publisher: str | None
    url: str | None
    published_at: datetime.datetime | None


@dataclass(frozen=True)
class PersonalCitation:
    claim_id: uuid.UUID
    text: str
    evidence: tuple[PersonalEvidence, ...]


@dataclass(frozen=True)
class PersonalSnapshotView:
    id: uuid.UUID
    run_id: uuid.UUID
    profile_revision_id: uuid.UUID
    prepared_at: datetime.datetime
    input_hash: str
    input_contract: str
    capture_started_at: datetime.datetime | None
    capture_ended_at: datetime.datetime | None
    coverage: dict[str, Any]
    candidate_event_ids: tuple[uuid.UUID, ...]
    selected_event_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class PersonalBriefDocument:
    report: ReportSnapshot
    sections: tuple[ReportSectionSnapshot, ...]
    snapshot: PersonalSnapshotView
    citations: tuple[PersonalCitation, ...]


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _instant(value: object, *, field: str, optional: bool = False) -> datetime.datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise PersonalBriefReadError(f"personal snapshot {field} is invalid")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersonalBriefReadError(f"personal snapshot {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersonalBriefReadError(f"personal snapshot {field} is not timezone-aware")
    return parsed


def _uuid(value: object, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise PersonalBriefReadError(f"personal snapshot {field} is invalid") from exc


def _safe_error(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    result = {
        key: item.strip()
        for key in ("code", "message")
        if isinstance((item := value.get(key)), str) and item.strip()
    }
    return result or None


def _safe_coverage(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PersonalBriefReadError("personal snapshot capture coverage is invalid")
    result: dict[str, Any] = {}
    for key in _COVERAGE_COUNT_FIELDS:
        if key not in value:
            continue
        count = value[key]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PersonalBriefReadError(f"personal snapshot coverage field {key} is invalid")
        result[key] = count
    for key in _COVERAGE_DETAIL_FIELDS:
        if key not in value:
            continue
        rows = value[key]
        if not isinstance(rows, list) or len(rows) > 100:
            raise PersonalBriefReadError(f"personal snapshot coverage field {key} is invalid")
        safe_rows: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise PersonalBriefReadError(
                    f"personal snapshot coverage field {key} is invalid"
                )
            source_id = row.get("source_id")
            code = row.get("code")
            if (
                not isinstance(source_id, str)
                or not source_id.strip()
                or len(source_id) > 128
                or not isinstance(code, str)
                or not code.strip()
                or len(code) > 128
            ):
                raise PersonalBriefReadError(
                    f"personal snapshot coverage field {key} is invalid"
                )
            safe_rows.append({"source_id": source_id, "code": code})
        result[key] = safe_rows
    return result


def _validate_report_identity(
    report: ReportSnapshot,
    link: PersonalReportLink,
    snapshot: PersonalBriefSnapshot,
    run: PersonalRun,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> None:
    if (
        report.report_type != PERSONAL_REPORT_TYPE
        or report.content_policy != REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
        or link.workspace_id != workspace_id
        or link.report_id != report.id
        or report.brief_date != link.brief_date
        or report.version != link.version
        or snapshot.id != link.snapshot_id
        or snapshot.workspace_id != workspace_id
        or snapshot.run_id != link.run_id
        or run.id != link.run_id
        or run.workspace_id != workspace_id
        or run.local_date != link.brief_date
        or run.profile_revision_id != snapshot.profile_revision_id
        or run.snapshot_id != snapshot.id
        or report.user_id != owner_id
    ):
        raise PersonalBriefReadError("personal report linkage is inconsistent")


def _selected_articles(payload: dict[str, Any], selected_event_ids: tuple[uuid.UUID, ...]):
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise PersonalBriefReadError("personal snapshot candidates are invalid")
    selected = set(selected_event_ids)
    articles: dict[uuid.UUID, dict[str, Any]] = {}
    candidate_ids: list[uuid.UUID] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise PersonalBriefReadError("personal snapshot candidate is invalid")
        event_id = _uuid(candidate.get("event_id"), field="candidate event identity")
        candidate_ids.append(event_id)
        if event_id not in selected:
            continue
        source_inputs = candidate.get("source_inputs")
        rows = source_inputs.get("articles") if isinstance(source_inputs, dict) else None
        if not isinstance(rows, list):
            raise PersonalBriefReadError("personal snapshot source articles are invalid")
        for row in rows:
            if not isinstance(row, dict):
                raise PersonalBriefReadError("personal snapshot source article is invalid")
            article_id = _uuid(row.get("article_id"), field="source article identity")
            previous = articles.setdefault(article_id, row)
            if previous != row:
                raise PersonalBriefReadError(
                    "personal snapshot repeats an article with different source data"
                )
    return tuple(candidate_ids), articles


def build_snapshot_view_and_citations(
    snapshot: PersonalBriefSnapshot,
    sections: tuple[ReportSectionSnapshot, ...],
) -> tuple[PersonalSnapshotView, tuple[PersonalCitation, ...]]:
    payload = snapshot.input_payload
    if (
        not isinstance(payload, dict)
        or snapshot.input_contract != "personal-brief-input.v1"
        or payload.get("schema") != snapshot.input_contract
        or _canonical_hash(payload) != snapshot.input_hash
        or _uuid(payload.get("run_id"), field="run identity") != snapshot.run_id
        or _uuid(payload.get("workspace_id"), field="workspace identity")
        != snapshot.workspace_id
        or _uuid(payload.get("profile_revision_id"), field="profile identity")
        != snapshot.profile_revision_id
        or payload.get("model_route") != snapshot.model_route
    ):
        raise PersonalBriefReadError("personal snapshot contract or hash is invalid")
    try:
        payload_date = datetime.date.fromisoformat(str(payload.get("local_date")))
    except ValueError as exc:
        raise PersonalBriefReadError("personal snapshot local date is invalid") from exc
    if not payload_date:
        raise PersonalBriefReadError("personal snapshot local date is invalid")
    captured = payload.get("captured")
    coverage = payload.get("coverage")
    if not isinstance(captured, dict):
        raise PersonalBriefReadError("personal snapshot capture coverage is invalid")
    safe_coverage = _safe_coverage(coverage)
    started = _instant(captured.get("start"), field="capture start", optional=True)
    ended = _instant(captured.get("end"), field="capture end", optional=True)
    payload_prepared = _instant(payload.get("prepared_at"), field="preparation time")
    if payload_prepared != snapshot.prepared_at:
        raise PersonalBriefReadError("personal snapshot preparation time is inconsistent")

    candidate_ids, articles = _selected_articles(payload, tuple(snapshot.selected_event_ids))
    if candidate_ids != tuple(snapshot.candidate_event_ids):
        raise PersonalBriefReadError("personal snapshot candidate ordering is inconsistent")
    if tuple(candidate_ids[:5]) != tuple(snapshot.selected_event_ids):
        raise PersonalBriefReadError("personal snapshot selected ordering is inconsistent")

    claims = payload.get("claims")
    if not isinstance(claims, list):
        raise PersonalBriefReadError("personal snapshot claims are invalid")
    by_id: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("citable") is not True:
            continue
        claim_id = _uuid(claim.get("claim_id"), field="claim identity")
        by_id.setdefault(claim_id, []).append(claim)

    citations: list[PersonalCitation] = []
    for claim_id in collect_claim_refs(sections):
        claim_rows = by_id.get(claim_id)
        if not claim_rows:
            raise PersonalBriefReadError(
                "published report cites a claim outside its immutable snapshot"
            )
        texts = {item.get("exact_excerpt") for item in claim_rows}
        if len(texts) != 1:
            raise PersonalBriefReadError("canonical snapshot claim text is inconsistent")
        text = next(iter(texts))
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_SNIPPET_CHARS:
            raise PersonalBriefReadError("personal snapshot citation text is invalid")
        evidence: list[PersonalEvidence] = []
        seen_evidence: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
        for claim in claim_rows:
            evidence_item_id = _uuid(
                claim.get("evidence_item_id"), field="evidence identity"
            )
            article_id = _uuid(claim.get("article_id"), field="claim article identity")
            source_field = claim.get("source_field")
            article = articles.get(article_id)
            if article is None:
                raise PersonalBriefReadError(
                    "published report cites an article outside its selected snapshot"
                )
            title = article.get("title")
            if source_field not in {"title", "summary"} or not isinstance(title, str) or not title.strip():
                raise PersonalBriefReadError("personal snapshot citation source is invalid")
            identity = (article_id, source_field)
            previous = seen_evidence.setdefault(evidence_item_id, identity)
            if previous != identity:
                raise PersonalBriefReadError(
                    "personal snapshot evidence identity is reused inconsistently"
                )
            if any(item.evidence_item_id == evidence_item_id for item in evidence):
                continue
            raw_url = article.get("url")
            url = raw_url if isinstance(raw_url, str) and is_safe_url(raw_url) else None
            publisher = article.get("publisher")
            if (
                len(title) > MAX_SOURCE_TITLE_CHARS
                or (publisher is not None and not isinstance(publisher, str))
                or (isinstance(publisher, str) and len(publisher) > MAX_SOURCE_PUBLISHER_CHARS)
                or (isinstance(raw_url, str) and len(raw_url) > MAX_SOURCE_URL_CHARS)
            ):
                raise PersonalBriefReadError("personal snapshot citation metadata is invalid")
            evidence.append(
                PersonalEvidence(
                    evidence_item_id=evidence_item_id,
                    article_id=article_id,
                    source_field=source_field,
                    title=title,
                    publisher=publisher,
                    url=url,
                    published_at=_instant(
                        article.get("published_at"),
                        field="source publication time",
                        optional=True,
                    ),
                )
            )
        citations.append(PersonalCitation(claim_id=claim_id, text=text, evidence=tuple(evidence)))
    view = PersonalSnapshotView(
        id=snapshot.id,
        run_id=snapshot.run_id,
        profile_revision_id=snapshot.profile_revision_id,
        prepared_at=snapshot.prepared_at,
        input_hash=snapshot.input_hash,
        input_contract=snapshot.input_contract,
        capture_started_at=started,
        capture_ended_at=ended,
        coverage=json.loads(json.dumps(safe_coverage)),
        candidate_event_ids=tuple(snapshot.candidate_event_ids),
        selected_event_ids=tuple(snapshot.selected_event_ids),
    )
    return view, tuple(citations)


class PersonalBriefRepository:
    """Workspace-scoped personal report reads over a caller-owned session."""

    def __init__(self, session: Session, workspace_id: uuid.UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._owner_id = session.scalar(
            select(PersonalWorkspace.owner_id).where(PersonalWorkspace.id == workspace_id)
        )

    def history(self, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        if limit < 1 or limit > MAX_HISTORY_ROWS or offset < 0:
            raise ValueError("personal brief pagination is invalid")
        if self._owner_id is None:
            return [], 0
        where = (
            PersonalReportLink.workspace_id == self._workspace_id,
            PersonalRun.workspace_id == self._workspace_id,
            Report.report_type == PERSONAL_REPORT_TYPE,
            Report.content_policy == REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY,
            Report.user_id == self._owner_id,
        )
        total = int(
            self._session.scalar(
                select(func.count())
                .select_from(PersonalReportLink)
                .join(Report, Report.id == PersonalReportLink.report_id)
                .join(PersonalRun, PersonalRun.id == PersonalReportLink.run_id)
                .where(*where)
            )
            or 0
        )
        rows = self._session.execute(
            select(Report, PersonalReportLink, PersonalRun)
            .join(PersonalReportLink, PersonalReportLink.report_id == Report.id)
            .join(PersonalRun, PersonalRun.id == PersonalReportLink.run_id)
            .where(*where)
            .order_by(
                PersonalReportLink.brief_date.desc(),
                PersonalReportLink.version.desc(),
                PersonalReportLink.report_id,
            )
            .limit(limit)
            .offset(offset)
        ).all()
        items: list[dict[str, Any]] = []
        for report, link, run in rows:
            if report.brief_date != link.brief_date or report.version != link.version:
                raise PersonalBriefReadError("personal report history linkage is inconsistent")
            items.append(
                {
                    "id": str(report.id),
                    "title": report.title,
                    "brief_date": link.brief_date,
                    "version": link.version,
                    "status": report.status,
                    "created_at": report.created_at,
                    "updated_at": report.updated_at,
                    "snapshot_id": str(link.snapshot_id),
                    "run_id": str(link.run_id),
                    "error": _safe_error(run.error) if run.report_id == report.id else None,
                }
            )
        return items, total

    def published_document(self, report_id: uuid.UUID) -> PersonalBriefDocument | None:
        if self._owner_id is None:
            return None
        row = self._session.execute(
            select(Report, PersonalReportLink, PersonalBriefSnapshot, PersonalRun)
            .join(PersonalReportLink, PersonalReportLink.report_id == Report.id)
            .join(PersonalBriefSnapshot, PersonalBriefSnapshot.id == PersonalReportLink.snapshot_id)
            .join(PersonalRun, PersonalRun.id == PersonalReportLink.run_id)
            .where(
                Report.id == report_id,
                PersonalReportLink.workspace_id == self._workspace_id,
                Report.report_type == PERSONAL_REPORT_TYPE,
                Report.content_policy == REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY,
                Report.status == "published",
                Report.user_id == self._owner_id,
                PersonalRun.workspace_id == self._workspace_id,
            )
            .limit(1)
        ).first()
        if row is None:
            return None
        report_row, link, snapshot, run = row
        lifecycle = ReportLifecycleRepository(self._session)
        report = lifecycle.get_report(report_row.id)
        if report is None:
            return None
        _validate_report_identity(
            report, link, snapshot, run, self._workspace_id, self._owner_id
        )
        sections = lifecycle.report_sections(report.id)
        if (
            not sections
            or sections[-1].title.strip().casefold() != "disclaimer"
            or sections[-1].body.strip() != FINAL_DISCLAIMER
            or any(item.grounding_status not in {"passed", "data_quality_note"} for item in sections)
        ):
            raise PersonalBriefReadError("published personal report sections are invalid")
        snapshot_view, citations = build_snapshot_view_and_citations(snapshot, sections)
        if report.brief_date is None or report.brief_date.isoformat() != snapshot.input_payload.get(
            "local_date"
        ):
            raise PersonalBriefReadError("personal report date differs from its snapshot")
        return PersonalBriefDocument(report, sections, snapshot_view, citations)


def _export_metadata_section(document: PersonalBriefDocument) -> ReportSectionSnapshot:
    snapshot = document.snapshot
    coverage = snapshot.coverage
    count_fields = (
        ("Feeds configured", "feeds_configured"),
        ("Feeds attempted", "feeds_attempted"),
        ("Feeds succeeded", "feeds_succeeded"),
        ("Feeds failed", "feeds_failed"),
        ("Feeds paused", "feeds_paused"),
        ("Articles captured", "articles_captured"),
        ("Items fetched", "items_fetched"),
        ("Pending items", "pending_total"),
        ("Pending capacity", "pending_capacity"),
    )
    lines = [
        f"Report UUID: {document.report.id}",
        f"Snapshot UUID: {snapshot.id}",
        f"Run UUID: {snapshot.run_id}",
        f"Profile revision UUID: {snapshot.profile_revision_id}",
        f"Capture started: {snapshot.capture_started_at.isoformat() if snapshot.capture_started_at else 'not recorded'}",
        f"Capture ended: {snapshot.capture_ended_at.isoformat() if snapshot.capture_ended_at else 'not recorded'}",
    ]
    lines.extend(
        f"{label}: {coverage[key] if key in coverage else 'Not recorded'}"
        for label, key in count_fields
    )
    return ReportSectionSnapshot(
        id=uuid.uuid5(_EXPORT_SECTION_NAMESPACE, str(document.report.id)),
        report_id=document.report.id,
        section_order=-1,
        title="Report and capture details",
        body="\n".join(lines),
        blocks=(),
        evidence_refs=(),
        grounding_status="data_quality_note",
    )


def _source_attributions(document: PersonalBriefDocument) -> tuple[SourceAttribution, ...]:
    sources: list[SourceAttribution] = []
    for citation in document.citations:
        for evidence in citation.evidence:
            identity = [evidence.publisher] if evidence.publisher else []
            if evidence.published_at is not None:
                identity.append(f"published {evidence.published_at.isoformat()}")
            identity.append(f"claim {citation.claim_id}")
            sources.append(
                SourceAttribution(
                    evidence_item_id=evidence.evidence_item_id,
                    source_type="article",
                    attribution=" · ".join(identity),
                    title=evidence.title,
                    url=evidence.url,
                    snippet=citation.text,
                    published_at=evidence.published_at,
                )
            )
    return tuple(sources)


def render_personal_markdown(document: PersonalBriefDocument) -> str:
    sections = (_export_metadata_section(document), *document.sections)
    return render_markdown(document.report, sections, _source_attributions(document))


def render_personal_pdf(document: PersonalBriefDocument) -> bytes:
    sections = (_export_metadata_section(document), *document.sections)
    return render_pdf(document.report, sections, _source_attributions(document))


def personal_export_filename(document: PersonalBriefDocument, extension: str) -> str:
    if extension not in {"md", "pdf"}:
        raise ValueError("unsupported personal export extension")
    brief_date = document.report.brief_date.isoformat() if document.report.brief_date else "unknown"
    return (
        f"personal-news-brief-{brief_date}-v{document.report.version}-"
        f"{document.report.id}.{extension}"
    )


__all__ = [
    "MAX_HISTORY_ROWS",
    "build_snapshot_view_and_citations",
    "PersonalBriefDocument",
    "PersonalBriefReadError",
    "PersonalBriefRepository",
    "PersonalCitation",
    "PersonalEvidence",
    "PersonalSnapshotView",
    "personal_export_filename",
    "render_personal_markdown",
    "render_personal_pdf",
]
