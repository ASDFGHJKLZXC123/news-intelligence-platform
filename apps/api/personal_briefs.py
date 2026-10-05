"""Published personal brief history, exact-version evidence, and exports."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from db.base import get_session
from services.personal.exports import (
    PersonalBriefDocument,
    PersonalBriefRepository,
    personal_export_filename,
    render_personal_markdown,
    render_personal_pdf,
)
from services.personal.workspace import get_workspace

router = APIRouter(prefix="/api/v1/personal/briefs", tags=["personal"])
SessionDep = Annotated[Session, Depends(get_session)]


def _repository(session: Session) -> PersonalBriefRepository:
    workspace, _ = get_workspace(session)
    if workspace is None:
        raise HTTPException(
            status_code=409,
            detail="personal workspace is not initialized; use the protected setup action",
        )
    return PersonalBriefRepository(session, workspace.id)


def _published(session: Session, report_id: uuid.UUID) -> PersonalBriefDocument:
    document = _repository(session).published_document(report_id)
    if document is None:
        raise HTTPException(status_code=404, detail="published personal brief not found")
    return document


def _section(item) -> dict[str, Any]:  # noqa: ANN001
    return {
        "id": str(item.id),
        "title": item.title,
        "section_order": item.section_order,
        "body": item.body,
        "evidence_refs": [str(value) for value in item.evidence_refs],
        "grounding_status": item.grounding_status,
    }


def _citation(item) -> dict[str, Any]:  # noqa: ANN001
    return {
        "claim_id": str(item.claim_id),
        "text": item.text,
        "evidence_count": len(item.evidence),
    }


@router.get("")
def list_personal_briefs(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    items, total = _repository(session).history(limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{report_id}")
def get_personal_brief(report_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    document = _published(session, report_id)
    report = document.report
    snapshot = document.snapshot
    return {
        "report": {
            "id": str(report.id),
            "title": report.title,
            "brief_date": report.brief_date,
            "version": report.version,
            "status": report.status,
            "created_at": report.created_at,
            "updated_at": report.updated_at,
            "content_policy": report.content_policy,
            "sections": [_section(item) for item in document.sections],
        },
        "snapshot": {
            "id": str(snapshot.id),
            "run_id": str(snapshot.run_id),
            "profile_revision_id": str(snapshot.profile_revision_id),
            "prepared_at": snapshot.prepared_at,
            "input_hash": snapshot.input_hash,
            "input_contract": snapshot.input_contract,
            "capture_started_at": snapshot.capture_started_at,
            "capture_ended_at": snapshot.capture_ended_at,
            "coverage": snapshot.coverage,
            "candidate_event_ids": [str(value) for value in snapshot.candidate_event_ids],
            "selected_event_ids": [str(value) for value in snapshot.selected_event_ids],
        },
        "citations": [_citation(item) for item in document.citations],
    }


@router.get("/{report_id}/claims/{claim_id}/evidence")
def get_personal_brief_evidence(
    report_id: uuid.UUID, claim_id: uuid.UUID, session: SessionDep
) -> dict[str, Any]:
    document = _published(session, report_id)
    citation = next((item for item in document.citations if item.claim_id == claim_id), None)
    if citation is None:
        raise HTTPException(status_code=404, detail="claim not found in published personal brief")
    return {
        "report_id": str(report_id),
        "claim": {"id": str(citation.claim_id), "text": citation.text},
        "evidence": [
            {
                "evidence_item_id": str(item.evidence_item_id),
                "source_type": "article",
                "source_id": str(item.article_id),
                "article_id": str(item.article_id),
                "support_type": "supports",
                "title": item.title,
                "publisher": item.publisher,
                "url": item.url,
                "published_at": item.published_at,
                "snippet": citation.text,
                "excerpt_origin": item.source_field,
                "excerpt_truncated": False,
            }
            for item in citation.evidence
        ],
    }


@router.get("/{report_id}/export.md")
def export_personal_brief_markdown(report_id: uuid.UUID, session: SessionDep) -> Response:
    document = _published(session, report_id)
    return Response(
        content=render_personal_markdown(document).encode("utf-8"),
        media_type="text/markdown",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{personal_export_filename(document, "md")}"'
            )
        },
    )


@router.get("/{report_id}/export.pdf")
def export_personal_brief_pdf(report_id: uuid.UUID, session: SessionDep) -> Response:
    document = _published(session, report_id)
    return Response(
        content=render_personal_pdf(document),
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{personal_export_filename(document, "pdf")}"'
            )
        },
    )


__all__ = ["router"]
