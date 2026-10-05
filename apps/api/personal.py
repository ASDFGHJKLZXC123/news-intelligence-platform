"""Workspace-scoped API for the personal Today, Saved and Briefs workflow."""

from __future__ import annotations

import datetime
import json
import math
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.base import get_session
from db.models import (
    EventArticle,
    Job,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWriterMode,
    Source,
)
from packages.config.settings import get_settings
from services.personal.offline_fixture import offline_fixture_route
from services.personal.repository import PersonalRepository
from services.personal.runs import (
    PersonalOwnershipLost,
    PersonalRunConflict,
    WriterModeConflict,
    create_daily_run,
    record_delivery,
    retry_run,
    retry_status,
)
from services.personal.settings import (
    SETTINGS_SCHEMA,
    PersonalSettingsUpdate,
    PersonalSourceDraft,
    effective_run_allowance,
    feature_readiness,
    register_source_draft,
    safe_model_route,
    update_settings,
    validated_settings,
)
from services.personal.workspace import (
    OwnerBindingConflict,
    OwnerSelectionRequired,
    bind_owner,
    configure_profile,
    ensure_workspace,
    get_workspace,
)

router = APIRouter(prefix="/api/v1/personal", tags=["personal"])
SessionDep = Annotated[Session, Depends(get_session)]


def _configured_owner_id() -> uuid.UUID | None:
    raw = get_settings().personal_owner_id.strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="PERSONAL_OWNER_ID is not a valid UUID"
        ) from exc


def _workspace(session: Session):
    workspace, choices = get_workspace(session)
    if workspace is None:
        raise HTTPException(
            status_code=409,
            detail="personal workspace is not initialized; use the protected setup action",
        )
    return workspace, choices


def _bootstrap_workspace(session: Session, request=None):
    try:
        existing, _ = get_workspace(session)
        configured = (
            None
            if existing is not None and existing.owner_id is not None
            else _configured_owner_id()
        )
        workspace, choices = ensure_workspace(session, configured_owner_id=configured)
        if request is not None:
            configure_profile(
                session,
                workspace,
                selected_source_ids=request.selected_source_ids,
                include_phrases=request.include_phrases,
                exclude_phrases=request.exclude_phrases,
                execution_profile=request.execution_profile,
                settings={
                    "model_route": request.model_route,
                    "authorized_spend_usd": request.authorized_spend_usd,
                },
            )
        session.commit()
        return workspace, choices
    except Exception:
        session.rollback()
        raise


def _run_status(session: Session, run) -> dict[str, Any] | None:
    if run is None:
        return None
    profile = session.get(PersonalProfileRevision, run.profile_revision_id)
    eligible, reason = retry_status(
        run,
        now=_utc_now(),
        profile=profile,
        control=session.get(PersonalWriterMode, True, populate_existing=True),
    )
    grouped_count = (
        session.scalar(
            select(func.count(func.distinct(EventArticle.article_id))).where(
                EventArticle.article_id.in_(run.enrichment_article_ids or [])
            )
        )
        or 0
    )
    return {
        "id": str(run.id),
        "local_date": run.local_date.isoformat(),
        "state": run.state,
        "attempt": run.attempt,
        "profile_revision_id": str(run.profile_revision_id),
        "profile_revision": profile.revision if profile else None,
        "profile_schema_revision": profile.schema_revision if profile else None,
        "scopes_frozen_at": run.scopes_frozen_at,
        "counts": {
            "admitted": len(run.admitted_article_ids or []),
            "selected_for_enrichment": len(run.enrichment_article_ids or []),
            "grouped": int(grouped_count),
        },
        "max_attempts": run.max_attempts,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "lease_expires_at": run.lease_expires_at,
        "capture_started_at": run.capture_started_at,
        "capture_ended_at": run.capture_ended_at,
        "coverage": run.coverage,
        "available_event_ids": [str(value) for value in run.event_ids],
        "snapshot_id": str(run.snapshot_id) if run.snapshot_id else None,
        "report_id": str(run.report_id) if run.report_id else None,
        "stage_results": run.stage_results,
        "result": run.result,
        "error": run.error,
        "retry_eligible": eligible,
        "retry_reason": reason,
        "runtime": {
            "transport": run.delivery_transport,
            "fencing_generation": run.fencing_generation,
            "running_started_at": run.started_at,
            "graceful_deadline_at": run.graceful_deadline_at,
            "hard_deadline_at": run.hard_deadline_at,
            "lease_expires_at": run.lease_expires_at,
        },
    }


def _profile_run_ready(profile: PersonalProfileRevision | None) -> bool:
    if profile is None or not profile.selected_source_ids:
        return False
    if profile.schema_revision == SETTINGS_SCHEMA:
        # Optional AI readiness never blocks free, bounded RSS capture and reading.
        return True
    if profile.execution_profile == "raw":
        return True
    settings = profile.settings or {}
    route = settings.get("model_route")
    if not isinstance(route, dict) or not route:
        return False
    runtime = get_settings()
    if route.get("mode") == "offline_fixture":
        fixture_id = route.get("fixture_id")
        return bool(
            runtime.app_env in {"local", "test"}
            and runtime.personal_offline_fixture_path
            and isinstance(fixture_id, str)
            and fixture_id
            and route == offline_fixture_route(fixture_id)
        )
    # Phase 2 live execution is deliberately absent from the ordinary API/worker path. The
    # dedicated smoke harness independently validates explicit authorization, a disposable
    # database, exact captures, route prices and a durable all-dispatch ledger.
    return False


def _readiness(
    workspace, profile: PersonalProfileRevision | None, mode: str | None, *, legacy_active: bool
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if workspace.owner_id is None:
        reasons.append("owner_selection_required")
    if profile is None:
        reasons.append("profile_missing")
    else:
        if not profile.selected_source_ids:
            reasons.append("feeds_not_configured")
        if profile.execution_profile == "assisted" and profile.schema_revision != SETTINGS_SCHEMA:
            settings = profile.settings or {}
            route = settings.get("model_route")
            if not isinstance(route, dict) or not route:
                reasons.append("model_route_not_configured")
            else:
                try:
                    authorized = float(settings.get("authorized_spend_usd") or 0)
                except (TypeError, ValueError):
                    authorized = float("nan")
            if (
                isinstance(route, dict)
                and route
                and route.get("mode") != "offline_fixture"
                and (not math.isfinite(authorized) or authorized <= 0)
            ):
                reasons.append("live_spending_not_authorized")
            elif not _profile_run_ready(profile):
                reasons.append("execution_route_unavailable")
    runtime = get_settings()
    if runtime.personal_processing_transport == "subprocess":
        from apps.api.deps import check_config

        if not check_config().ok:
            reasons.append("launcher_configuration_invalid")
    if mode != runtime.personal_processing_mode:
        reasons.append("processing_mode_mismatch")
    if legacy_active:
        reasons.append("legacy_writer_active")
    elif mode != "personal":
        reasons.append("personal_writer_inactive")
    return not reasons, reasons


def _profile_status(profile: PersonalProfileRevision | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    settings = profile.settings or {}
    safe_route = safe_model_route(settings.get("model_route"))
    try:
        authorized = float(settings.get("authorized_spend_usd") or 0)
    except (TypeError, ValueError):
        authorized = float("nan")
    return {
        "id": str(profile.id),
        "revision": profile.revision,
        "schema_revision": profile.schema_revision,
        "selected_source_ids": [str(value) for value in profile.selected_source_ids],
        "include_phrases": list(profile.include_phrases),
        "exclude_phrases": list(profile.exclude_phrases),
        "execution_profile": profile.execution_profile,
        "model_route": safe_route,
        "live_spending_authorized": (
            effective_run_allowance(profile) > 0
            if profile.schema_revision == SETTINGS_SCHEMA
            else math.isfinite(authorized) and authorized > 0
        ),
        "settings": validated_settings(profile),
    }


def _settings_status(session: Session, workspace) -> dict[str, Any]:
    profile = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
    mode = session.scalar(
        select(PersonalWriterMode.mode).where(PersonalWriterMode.singleton.is_(True))
    )
    legacy_active = (
        session.scalar(
            select(Job.id)
            .where(Job.job_type != "personal_daily", Job.state.in_(("queued", "running")))
            .limit(1)
        )
        is not None
    )
    features = feature_readiness(
        profile,
        owner_ready=workspace.owner_id is not None,
        writer_ready=mode == "personal" and not legacy_active,
        runtime=get_settings(),
    )
    has_run = (
        session.scalar(
            select(PersonalRun.id).where(PersonalRun.workspace_id == workspace.id).limit(1)
        )
        is not None
    )
    suggestion = None
    path = (
        Path(__file__).resolve().parents[2]
        / "personal-project-conversion/config/selected-preferences.json"
    )
    try:
        saved = json.loads(path.read_text())
        suggestion = {
            "status": "saved_preferences_not_applied",
            "sources": [
                {key: item.get(key) for key in ("name", "feed_url")}
                for item in saved.get("sources", [])
            ],
            "interest": saved.get("interest", {}),
            "model_route": safe_model_route(saved.get("preferred_model_route")),
            "monthly_allowance_authorized": False,
            "note": "Saved choices are not active settings. The completed Phase 2 verification allowance is not a recurring allowance.",
        }
    except (OSError, ValueError, TypeError):
        pass
    values = validated_settings(profile)
    return {
        "workspace_id": str(workspace.id),
        "timezone": workspace.timezone,
        "timezone_editable": not has_run,
        "profile": _profile_status(profile),
        "settings": values,
        "features": features,
        "effective_run_allowance_usd": format(effective_run_allowance(profile), "f"),
        "available_sources": [
            {
                "id": str(source.id),
                "name": source.name,
                "feed_url": source.feed_url,
                "active": source.active,
                "draft": str(source.id)
                in (workspace.setup_provenance or {}).get("draft_source_ids", []),
                "source_type": source.source_type,
            }
            for source in session.scalars(
                select(Source).where(Source.source_type == "rss").order_by(Source.name, Source.id)
            )
        ],
        "saved_suggestion": suggestion,
        "application": {
            "feed_interest_route_and_run_limits": "next_run",
            "lower_daily_and_spending_limits": "immediately_for_new_admissions_and_requests",
            "existing_run_limits": "never_expanded",
        },
    }


@router.get("/settings")
def get_personal_settings(session: SessionDep) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    return _settings_status(session, workspace)


@router.post("/sources", status_code=201)
def post_personal_source(request: PersonalSourceDraft, session: SessionDep) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    try:
        source = register_source_draft(session, workspace, request)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "source": {
            "id": str(source.id),
            "name": source.name,
            "feed_url": source.feed_url,
            "active": source.active,
            "draft": str(source.id)
            in (workspace.setup_provenance or {}).get("draft_source_ids", []),
            "source_type": source.source_type,
        },
    }


@router.put("/settings")
def put_personal_settings(request: PersonalSettingsUpdate, session: SessionDep) -> dict[str, Any]:
    from services.personal.spending import PaidWorkBlocked

    workspace, _ = _workspace(session)
    try:
        update_settings(session, workspace, request)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PaidWorkBlocked as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        session.rollback()
        raise
    return _settings_status(session, workspace)


@router.get("/readiness")
def get_personal_readiness(session: SessionDep) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    result = _settings_status(session, workspace)
    return {"workspace_id": result["workspace_id"], "features": result["features"]}


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def enqueue_personal_run(run_id: uuid.UUID, ownership_token: uuid.UUID, task_id: str) -> None:
    if get_settings().personal_processing_transport == "subprocess":
        from db.base import SessionLocal
        from services.personal.supervisor import get_supervisor

        with SessionLocal() as session:
            run = session.get(PersonalRun, run_id)
            if run is None:
                raise RuntimeError("queued personal run is unavailable")
            generation = run.fencing_generation
        get_supervisor().launch(run_id, ownership_token, generation)
        return
    from workers.celery_app import QUEUE_PIPELINE, celery_app

    celery_app.send_task(
        "workers.personal_tasks.run_personal_daily",
        args=[str(run_id), str(ownership_token)],
        queue=QUEUE_PIPELINE,
        task_id=task_id,
    )


def _require_runtime_mode(session: Session) -> None:
    runtime = get_settings()
    from services.personal.processing import assert_mode_agreement

    if runtime.personal_processing_transport == "subprocess":
        from apps.api.deps import check_config

        config = check_config()
        if not config.ok:
            raise HTTPException(status_code=409, detail=config.detail)
    try:
        assert_mode_agreement(session, runtime.personal_processing_mode)
    except WriterModeConflict as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def get_personal_enqueuer() -> Callable[[uuid.UUID, uuid.UUID, str], None]:
    return enqueue_personal_run


EnqueuerDep = Annotated[Callable[[uuid.UUID, uuid.UUID, str], None], Depends(get_personal_enqueuer)]


class OwnerBindingRequest(BaseModel):
    owner_id: uuid.UUID


class WorkspaceSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    selected_source_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)
    include_phrases: list[str] = Field(default_factory=list, max_length=20)
    exclude_phrases: list[str] = Field(default_factory=list, max_length=20)
    execution_profile: str = "assisted"
    model_route: dict[str, Any] = Field(default_factory=dict)
    authorized_spend_usd: float | None = Field(default=None, ge=0)


def _deliver_personal_run(
    session: Session, run: PersonalRun, enqueuer: Callable[[uuid.UUID, uuid.UUID, str], None]
) -> None:
    if run.ownership_token is None:
        raise RuntimeError("queued personal run is missing its ownership token")
    run_id = run.id
    ownership_token = run.ownership_token
    task_id = str(uuid.uuid4())
    transport = get_settings().personal_processing_transport
    record_delivery(session, run_id, ownership_token, task_id, transport=transport)
    session.commit()
    try:
        enqueuer(run_id, ownership_token, task_id)
    except Exception as exc:
        session.rollback()
        from services.personal.runs import mark_delivery_failed

        mark_delivery_failed(
            session, run_id, ownership_token, now=_utc_now(), error_code="dispatch_failed"
        )
        session.commit()
        raise HTTPException(status_code=503, detail="personal run could not be delivered") from exc


@router.get("/workspace")
def get_personal_workspace(session: SessionDep) -> dict[str, Any]:
    workspace, choices = get_workspace(session)
    if workspace is None:
        return {
            "workspace": None,
            "setup_required": True,
            "owner_choices": [
                {
                    "id": str(choice.id),
                    "label": choice.label,
                    "legacy_saved_count": choice.legacy_saved_count,
                }
                for choice in choices
            ],
            "latest_run": None,
            "actions": {"can_setup": True, "can_save": False, "can_start": False},
        }
    repo = PersonalRepository(session, workspace)
    profile = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
    mode = session.scalar(
        select(PersonalWriterMode.mode).where(PersonalWriterMode.singleton.is_(True))
    )
    legacy_active = (
        session.execute(
            select(Job.id)
            .where(Job.job_type != "personal_daily", Job.state.in_(("queued", "running")))
            .limit(1)
        ).first()
        is not None
    )
    can_start, readiness_reasons = _readiness(workspace, profile, mode, legacy_active=legacy_active)
    last_successful = repo.latest_successful_run()
    from services.personal.processing import diagnostics

    return {
        "runtime": diagnostics(session),
        "workspace": {
            "id": str(workspace.id),
            "timezone": workspace.timezone,
            "owner_id": str(workspace.owner_id) if workspace.owner_id else None,
            "owner_selection_required": workspace.owner_id is None,
            "active_profile_revision_id": (
                str(workspace.active_profile_revision_id)
                if workspace.active_profile_revision_id
                else None
            ),
            "setup_provenance": workspace.setup_provenance,
        },
        "owner_choices": [
            {
                "id": str(choice.id),
                "label": choice.label,
                "legacy_saved_count": choice.legacy_saved_count,
            }
            for choice in choices
        ],
        "latest_run": _run_status(session, repo.latest_run()),
        "last_successful_update_at": (
            last_successful.updated_at if last_successful is not None else None
        ),
        "profile": _profile_status(profile),
        "readiness": {"ready": can_start, "reasons": readiness_reasons},
        "actions": {
            "can_setup": False,
            "can_save": workspace.owner_id is not None,
            "can_start": can_start,
        },
    }


@router.post("/workspace/setup")
def setup_personal_workspace(
    session: SessionDep, request: WorkspaceSetupRequest | None = None
) -> dict[str, Any]:
    """Initialize the singleton through an API-key-protected mutation."""

    try:
        workspace, choices = _bootstrap_workspace(session, request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    profile = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
    mode = session.scalar(
        select(PersonalWriterMode.mode).where(PersonalWriterMode.singleton.is_(True))
    )
    legacy_active = (
        session.execute(
            select(Job.id)
            .where(Job.job_type != "personal_daily", Job.state.in_(("queued", "running")))
            .limit(1)
        ).first()
        is not None
    )
    run_ready, readiness_reasons = _readiness(workspace, profile, mode, legacy_active=legacy_active)
    return {
        "workspace_id": str(workspace.id),
        "owner_id": str(workspace.owner_id) if workspace.owner_id else None,
        "owner_selection_required": workspace.owner_id is None,
        "profile_revision_id": str(profile.id) if profile is not None else None,
        "run_ready": run_ready,
        "profile": _profile_status(profile),
        "readiness": {"ready": run_ready, "reasons": readiness_reasons},
        "owner_choices": [
            {
                "id": str(choice.id),
                "label": choice.label,
                "legacy_saved_count": choice.legacy_saved_count,
            }
            for choice in choices
        ],
    }


@router.put("/workspace/owner")
def put_personal_workspace_owner(
    request: OwnerBindingRequest, session: SessionDep
) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    try:
        workspace = bind_owner(session, workspace, request.owner_id)
        session.commit()
    except OwnerBindingConflict as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"workspace_id": str(workspace.id), "owner_id": str(workspace.owner_id), "bound": True}


@router.get("/events")
def list_personal_events(
    session: SessionDep,
    run_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=160),
    saved: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    repo = PersonalRepository(session, workspace)
    rows, total, displayed_run, latest_run = repo.list_events(
        run_id=run_id, query=q, saved_only=saved, limit=limit, offset=offset
    )
    if run_id is not None and displayed_run is None:
        raise HTTPException(status_code=404, detail="personal run not found")
    return {
        "items": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "displayed_run": _run_status(session, displayed_run),
        "newer_run": (
            _run_status(session, latest_run)
            if latest_run is not None
            and (displayed_run is None or latest_run.id != displayed_run.id)
            else None
        ),
        "last_successful_update_at": (
            latest_success.updated_at if (latest_success := repo.latest_successful_run()) else None
        ),
    }


@router.get("/events/{event_id}")
def get_personal_event(
    event_id: uuid.UUID, session: SessionDep, run_id: uuid.UUID | None = None
) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    try:
        detail = PersonalRepository(session, workspace).event_detail(event_id, run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="event not found")
    return {"event": detail}


@router.get("/events/{event_id}/sources")
def get_personal_event_sources(
    event_id: uuid.UUID,
    session: SessionDep,
    run_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    try:
        result = PersonalRepository(session, workspace).event_sources(
            event_id, run_id=run_id, limit=limit, offset=offset
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="event not found")
    items, total = result
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/saved")
def list_personal_saved(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    try:
        items, total = PersonalRepository(session, workspace).saved(limit=limit, offset=offset)
    except OwnerSelectionRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.put("/saved/{event_id}")
def save_personal_event(event_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    try:
        item = PersonalRepository(session, workspace).save(event_id)
        if item is None:
            raise HTTPException(status_code=404, detail="event not found")
        session.commit()
    except OwnerSelectionRequired as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "saved": True,
        "entry": {
            "id": str(item.id),
            "event_id": item.item_id,
            "label": item.label,
            "saved_at": item.created_at,
        },
    }


@router.delete("/saved/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def unsave_personal_event(event_id: uuid.UUID, session: SessionDep) -> Response:
    workspace, _ = _workspace(session)
    try:
        PersonalRepository(session, workspace).unsave(event_id)
        session.commit()
    except OwnerSelectionRequired as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/saved/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def unsave_personal_entry(entry_id: uuid.UUID, session: SessionDep) -> Response:
    """Remove an unavailable/malformed legacy event pointer by its stable entry identity."""

    workspace, _ = _workspace(session)
    try:
        PersonalRepository(session, workspace).unsave_entry(entry_id)
        session.commit()
    except OwnerSelectionRequired as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def start_personal_run(session: SessionDep, enqueuer: EnqueuerDep) -> Response:
    workspace, _ = _workspace(session)
    if workspace.owner_id is None:
        raise HTTPException(status_code=409, detail="select the personal workspace owner first")
    profile = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
    if not _profile_run_ready(profile):
        raise HTTPException(
            status_code=409,
            detail="personal feeds and an authorized execution route must be configured first",
        )
    _require_runtime_mode(session)
    try:
        decision = create_daily_run(session, workspace, now=_utc_now())
        session.commit()
    except (PersonalRunConflict, WriterModeConflict) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if decision.should_enqueue:
        _deliver_personal_run(session, decision.run, enqueuer)
    code = 200 if decision.already_processed else 202
    return Response(
        content=PersonalRunResponse(
            run=_run_status(session, decision.run), already_processed=decision.already_processed
        ).model_dump_json(),
        status_code=code,
        media_type="application/json",
    )


class PersonalRunResponse(BaseModel):
    run: dict[str, Any]
    already_processed: bool = False


@router.get("/runs")
def list_personal_runs(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    base = select(PersonalRun).where(PersonalRun.workspace_id == workspace.id)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.execute(
        base.order_by(PersonalRun.local_date.desc(), PersonalRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).scalars()
    return {
        "items": [_run_status(session, run) for run in rows],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


@router.get("/runs/{run_id}")
def get_personal_run(run_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    run = session.execute(
        select(PersonalRun).where(
            PersonalRun.id == run_id, PersonalRun.workspace_id == workspace.id
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="personal run not found")
    return {"run": _run_status(session, run)}


@router.post("/runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_personal_run(
    run_id: uuid.UUID, session: SessionDep, enqueuer: EnqueuerDep
) -> dict[str, Any]:
    workspace, _ = _workspace(session)
    _require_runtime_mode(session)
    try:
        decision = retry_run(session, workspace, run_id, now=_utc_now())
        session.commit()
        _deliver_personal_run(session, decision.run, enqueuer)
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PersonalRunConflict, WriterModeConflict, PersonalOwnershipLost) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": _run_status(session, decision.run), "already_processed": False}
