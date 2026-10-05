"""Read retained RSS/backlog/spending without initializing any optional AI runtime."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db.base import get_session
from services.personal.reader import collection_status, pending_backlog, raw_articles
from services.personal.workspace import get_workspace

router = APIRouter(prefix="/api/v1/personal", tags=["personal"])
SessionDep = Annotated[Session, Depends(get_session)]


def _workspace(session: Session):
    workspace, _ = get_workspace(session)
    if workspace is None:
        raise HTTPException(status_code=409, detail="personal workspace setup required")
    return workspace


@router.get("/articles")
def get_articles(
    session: SessionDep,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    ungrouped: bool = True,
    interest_only: bool = False,
    run_id: uuid.UUID | None = None,
    q: str | None = Query(None, max_length=160),
) -> dict[str, Any]:
    try:
        return raw_articles(
            session,
            _workspace(session),
            limit=limit,
            offset=offset,
            ungrouped=ungrouped,
            interest_only=interest_only,
            run_id=run_id,
            q=q,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/articles/{article_id}")
def get_article(article_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    result = raw_articles(session, _workspace(session), article_id=article_id, ungrouped=False)
    if not result["items"]:
        raise HTTPException(status_code=404, detail="personal article not found")
    return {"article": result["items"][0]}


@router.get("/backlog")
def get_backlog(
    session: SessionDep,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return pending_backlog(session, _workspace(session), limit=limit, offset=offset)


@router.get("/collection")
def get_collection(session: SessionDep) -> dict[str, Any]:
    return collection_status(session, _workspace(session))


@router.get("/spending")
def get_spending(session: SessionDep) -> dict[str, Any]:
    from services.personal.spending import spending_summary

    return spending_summary(session, _workspace(session).id)
