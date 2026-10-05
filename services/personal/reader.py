"""Database-only reading of retained RSS content and collection receipts."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    EventArticle,
    PersonalCapture,
    PersonalFeedReceipt,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    Report,
    Source,
)
from services.personal.contracts import article_matches_profile


def _profile(session: Session, workspace: PersonalWorkspace, run_id: uuid.UUID | None):
    profile_id = workspace.active_profile_revision_id
    if run_id is not None:
        run = session.get(PersonalRun, run_id)
        if run is None or run.workspace_id != workspace.id:
            raise LookupError("personal run not found")
        profile_id = run.profile_revision_id
    return session.get(PersonalProfileRevision, profile_id) if profile_id else None


def _article_item(capture, source, event_id, run, profile) -> dict[str, Any]:
    matches = article_matches_profile(
        capture.title,
        capture.rss_summary,
        include_phrases=profile.include_phrases if profile else [],
        exclude_phrases=profile.exclude_phrases if profile else [],
    )
    error = None
    state = "grouped" if event_id else capture.enrichment_state
    if run is not None and run.state in {"failed", "partially_failed"}:
        error = (run.error or {}).get(
            "message"
        ) or "This update failed; retained RSS content is available."
        if not event_id and state == "selected":
            state = "enrichment_failed"
    return {
        "id": str(capture.article_id),
        "title": capture.title,
        "snippet": capture.rss_summary,
        "source_id": str(source.id),
        "source_name": source.name,
        "url": capture.original_url or capture.canonical_url,
        "canonical_url": capture.canonical_url,
        "published_at": capture.published_at,
        "captured_at": capture.captured_at,
        "admitted_at": capture.admitted_at,
        "enrichment_state": state,
        "summary_state": (
            "run_brief_available"
            if run is not None and (run.result or {}).get("published")
            else (run.stage_results or {}).get("report", {}).get("reason", "not_available")
            if run is not None
            else "not_available"
        ),
        "event_id": str(event_id) if event_id else None,
        "error": error,
        "from_backlog": capture.processing_run_id != capture.admitted_run_id,
        "matches_interest": matches,
        "truncated": capture.truncated,
        "admitted_run_id": str(capture.admitted_run_id),
        "processing_run_id": str(capture.processing_run_id) if capture.processing_run_id else None,
    }


def raw_articles(
    session: Session,
    workspace: PersonalWorkspace,
    *,
    limit: int = 20,
    offset: int = 0,
    ungrouped: bool = True,
    interest_only: bool = False,
    run_id: uuid.UUID | None = None,
    q: str | None = None,
    article_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    profile = _profile(session, workspace, run_id)
    event = (
        select(EventArticle.event_id)
        .where(EventArticle.article_id == PersonalCapture.article_id)
        .order_by(EventArticle.event_id)
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        select(PersonalCapture, Source, event.label("event_id"), PersonalRun)
        .join(Source, Source.id == PersonalCapture.source_id)
        .outerjoin(PersonalRun, PersonalRun.id == PersonalCapture.processing_run_id)
        .where(
            PersonalCapture.workspace_id == workspace.id,
            PersonalCapture.admitted_at.is_not(None),
            PersonalCapture.article_id.is_not(None),
        )
    )
    if article_id is not None:
        stmt = stmt.where(PersonalCapture.article_id == article_id)
    elif ungrouped:
        stmt = stmt.where(event.is_(None))
    if run_id is not None:
        stmt = stmt.where(PersonalCapture.admitted_run_id == run_id)
    if q:
        escaped = q.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(
            func.lower(PersonalCapture.title).like(f"%{escaped}%", escape="\\")
            | func.lower(PersonalCapture.rss_summary).like(f"%{escaped}%", escape="\\")
        )
    stmt = stmt.order_by(PersonalCapture.admitted_at.desc(), PersonalCapture.article_id)
    # Interest normalization is exactly the shared Unicode predicate. Stream bounded RSS
    # rows to count exact matches without building an unrestricted in-memory article list.
    items: list[dict[str, Any]] = []
    total = 0
    for capture, source, event_id, run in session.execute(stmt.execution_options(yield_per=200)):
        item = _article_item(capture, source, event_id, run, profile)
        if interest_only and not item["matches_interest"]:
            continue
        if offset <= total < offset + limit:
            items.append(item)
        total += 1
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def pending_backlog(
    session: Session,
    workspace: PersonalWorkspace,
    *,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    profile = _profile(session, workspace, None)
    selected = set(profile.selected_source_ids if profile else [])
    stmt = (
        select(PersonalCapture, Source)
        .join(Source, Source.id == PersonalCapture.source_id)
        .where(
            PersonalCapture.workspace_id == workspace.id,
            PersonalCapture.admitted_at.is_(None),
        )
        .order_by(PersonalCapture.captured_at, PersonalCapture.source_id, PersonalCapture.id)
    )
    items = []
    total = eligible_total = 0
    for capture, source in session.execute(stmt):
        eligible = source.active and source.id in selected
        eligible_total += int(eligible)
        if offset <= total < offset + limit:
            items.append(
                {
                    "id": str(capture.id),
                    "title": capture.title,
                    "snippet": capture.rss_summary,
                    "source_id": str(source.id),
                    "source_name": source.name,
                    "url": capture.original_url or capture.canonical_url,
                    "published_at": capture.published_at,
                    "captured_at": capture.captured_at,
                    "eligible": eligible,
                    "disabled_reason": None if eligible else "source_disabled",
                    "truncated": capture.truncated,
                }
            )
        total += 1
    return {
        "items": items,
        "total": total,
        "eligible_total": eligible_total,
        "disabled_source_total": total - eligible_total,
        "limit": limit,
        "offset": offset,
    }


def collection_status(session: Session, workspace: PersonalWorkspace) -> dict[str, Any]:
    captures = (
        select(PersonalCapture).where(PersonalCapture.workspace_id == workspace.id).subquery()
    )

    def count(condition):
        return int(session.scalar(select(func.count()).select_from(captures).where(condition)) or 0)

    observed = int(session.scalar(select(func.count()).select_from(captures)) or 0)
    pending = count(captures.c.admitted_at.is_(None))
    grouped_condition = (
        select(EventArticle.article_id)
        .where(EventArticle.article_id == captures.c.article_id)
        .exists()
    )
    grouped = count(grouped_condition)
    completed = count(
        grouped_condition
        & select(PersonalRun.id)
        .join(Report, Report.id == PersonalRun.report_id)
        .where(PersonalRun.id == captures.c.processing_run_id, Report.status == "published")
        .exists()
    )
    latest = session.scalar(
        select(PersonalRun)
        .where(PersonalRun.workspace_id == workspace.id)
        .order_by(PersonalRun.local_date.desc(), PersonalRun.created_at.desc())
        .limit(1)
    )
    receipts = []
    if latest:
        for receipt in session.scalars(
            select(PersonalFeedReceipt)
            .where(PersonalFeedReceipt.run_id == latest.id)
            .order_by(PersonalFeedReceipt.started_at, PersonalFeedReceipt.id)
        ):
            receipts.append(
                {
                    "id": str(receipt.id),
                    "source_id": str(receipt.source_id),
                    "run_id": str(receipt.run_id),
                    "attempt": receipt.attempt,
                    "started_at": receipt.started_at,
                    "ended_at": receipt.ended_at,
                    "status": receipt.status,
                    "details": receipt.details,
                }
            )
    return {
        "observed_candidates": observed,
        "pending_candidates": pending,
        "admitted_articles": observed - pending,
        "grouped_articles": grouped,
        "completed_enrichment": completed,
        "receipts": receipts,
        "count_scope": "retained_database_records",
    }
