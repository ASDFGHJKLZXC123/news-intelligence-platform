"""Personal daily-run identity, ownership and retry lifecycle."""

from __future__ import annotations

import datetime
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import (
    Job,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    PersonalWriterMode,
)
from services.personal.contracts import personal_local_date
from services.personal.processing import (
    GRACEFUL_DEADLINE,
    HARD_DEADLINE,
    QUEUE_LEASE,
    RUNNING_LEASE,
    PersonalOwnershipLost,
    PersonalRunConflict,
    ProcessingBusy,
    WriterModeConflict,
    fail_owned_launch,
    lock_processing_control,
    queue_control,
    release_control,
    require_idle,
    require_mode,
    utc_now,
    verify_control,
)

MAX_ATTEMPTS = 3
ACTIVE_STATES = frozenset({"queued", "running"})
TERMINAL_STATES = frozenset({"succeeded", "partially_failed", "failed"})


@dataclass(frozen=True)
class RunDecision:
    run: PersonalRun
    created: bool
    should_enqueue: bool
    already_processed: bool = False


def personal_job_key(workspace_id: uuid.UUID, local_date: datetime.date) -> str:
    return f"personal-daily:{workspace_id}:{local_date.isoformat()}"


def _writer_mode(session: Session, *, lock: bool) -> PersonalWriterMode:
    return lock_processing_control(session, lock="exclusive" if lock else "read")


def require_personal_writer_mode(session: Session, *, lock: bool = False) -> None:
    mode = _writer_mode(session, lock=lock)
    if mode.mode != "personal":
        raise WriterModeConflict(
            "personal processing is unavailable while the legacy writer mode is active"
        )
    active_legacy = session.execute(
        select(Job.id)
        .where(Job.job_type != "personal_daily", Job.state.in_(("queued", "running")))
        .limit(1)
    ).first()
    if active_legacy is not None:
        raise WriterModeConflict("a legacy writer is queued or running")


def _queue_transport(selected: str | None) -> str:
    if selected is None:
        from packages.config.settings import get_settings

        selected = get_settings().personal_processing_transport
    if not isinstance(selected, str) or selected not in {"celery", "subprocess"}:
        raise ValueError("personal processing transport must be celery or subprocess")
    return selected


def create_daily_run(
    session: Session,
    workspace: PersonalWorkspace,
    *,
    now: datetime.datetime,
    allow_bounded_live: bool = False,
    transport: str | None = None,
) -> RunDecision:
    selected_transport = _queue_transport(transport)
    require_personal_writer_mode(session, lock=True)
    # Serialize the first run with settings/timezone edits. Neither a caller date nor a
    # concurrent profile change can move this run into a different calendar identity.
    workspace = session.execute(
        select(PersonalWorkspace)
        .where(PersonalWorkspace.id == workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if workspace.active_profile_revision_id is None:
        raise PersonalRunConflict("personal profile is not configured")
    profile = session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
    if profile is None:
        raise PersonalRunConflict("active personal profile revision is unavailable")
    if not profile.selected_source_ids:
        raise PersonalRunConflict("select at least one configured feed before starting")
    if profile.execution_profile == "assisted" and profile.schema_revision != "personal-profile.v2":
        profile_settings = profile.settings or {}
        route = profile_settings.get("model_route")
        try:
            authorized = float(profile_settings.get("authorized_spend_usd") or 0)
        except (TypeError, ValueError) as exc:
            raise PersonalRunConflict(
                "configure a finite live spending allowance before assisted processing"
            ) from exc
        if not isinstance(route, dict) or not route:
            raise PersonalRunConflict("configure an explicit assisted model route before starting")
        if route.get("mode") != "offline_fixture":
            if not allow_bounded_live:
                raise PersonalRunConflict(
                    "live assisted processing requires the dedicated bounded smoke harness"
                )
            if route.get("mode") != "live":
                raise PersonalRunConflict("the assisted model route mode is unsupported")
            if not math.isfinite(authorized) or authorized <= 0:
                raise PersonalRunConflict(
                    "configure a positive live spending allowance before assisted processing"
                )
    local_date = personal_local_date(now, workspace.timezone)
    existing = session.execute(
        select(PersonalRun)
        .where(PersonalRun.workspace_id == workspace.id, PersonalRun.local_date == local_date)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.state == "succeeded":
            return RunDecision(
                existing, created=False, should_enqueue=False, already_processed=True
            )
        if existing.state in ACTIVE_STATES:
            return RunDecision(existing, created=False, should_enqueue=False)
        raise PersonalRunConflict(
            f"run {existing.id} is {existing.state}; use its explicit retry action"
        )

    control = _writer_mode(session, lock=True)
    require_idle(session, control, now=now)
    now_utc = now.astimezone(datetime.UTC)
    token = uuid.uuid4()
    job = Job(
        job_key=personal_job_key(workspace.id, local_date),
        job_type="personal_daily",
        state="queued",
        attempt=1,
        max_attempts=MAX_ATTEMPTS,
        related_ids={"workspace_id": str(workspace.id), "local_date": local_date.isoformat()},
        safe_to_rerun=True,
    )
    session.add(job)
    session.flush()
    run = PersonalRun(
        workspace_id=workspace.id,
        local_date=local_date,
        profile_revision_id=profile.id,
        job_id=job.id,
        state="queued",
        attempt=1,
        max_attempts=MAX_ATTEMPTS,
        ownership_token=token,
        lease_expires_at=now_utc + QUEUE_LEASE,
        delivery_transport=selected_transport,
        admitted_article_ids=[],
        enrichment_article_ids=[],
        event_ids=[],
        coverage={},
        stage_results={},
    )
    session.add(run)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = session.execute(
            select(PersonalRun).where(
                PersonalRun.workspace_id == workspace.id, PersonalRun.local_date == local_date
            )
        ).scalar_one()
        return RunDecision(existing, created=False, should_enqueue=False)
    queue_control(session, control, run, now=now_utc)
    session.flush()
    return RunDecision(run, created=True, should_enqueue=True)


def retry_run(
    session: Session,
    workspace: PersonalWorkspace,
    run_id: uuid.UUID,
    *,
    now: datetime.datetime,
    transport: str | None = None,
) -> RunDecision:
    selected_transport = _queue_transport(transport)
    require_personal_writer_mode(session, lock=True)
    run = session.execute(
        select(PersonalRun)
        .where(PersonalRun.id == run_id, PersonalRun.workspace_id == workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if run is None:
        raise LookupError("personal run not found")
    profile = session.get(
        PersonalProfileRevision,
        run.profile_revision_id,
        populate_existing=True,
    )
    if _is_dedicated_live_profile(profile):
        raise PersonalRunConflict(
            "dedicated live smoke runs cannot be retried through the ordinary API"
        )
    now_utc = now.astimezone(datetime.UTC)
    expired = run.state in ACTIVE_STATES and (
        run.lease_expires_at is None or run.lease_expires_at <= now_utc
    )
    retryable_terminal = run.state in {"failed", "partially_failed"}
    if not expired and not retryable_terminal:
        raise PersonalRunConflict(f"run in state {run.state} is not eligible for retry")
    if run.attempt >= run.max_attempts:
        raise PersonalRunConflict("personal run exhausted its three attempts")
    control = _writer_mode(session, lock=True)
    if control.active_run_id not in {None, run.id}:
        raise ProcessingBusy(control.active_run_id)
    if control.active_run_id is None:
        require_idle(session, control, now=now_utc)
    run.attempt += 1
    run.state = "queued"
    run.ownership_token = uuid.uuid4()
    run.lease_expires_at = now_utc + QUEUE_LEASE
    run.celery_task_id = None
    run.delivery_transport = selected_transport
    run.error = None
    job = session.get(Job, run.job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise RuntimeError("personal run job is unavailable")
    job.attempt = run.attempt
    job.state = "queued"
    job.error = None
    queue_control(session, control, run, now=now_utc)
    session.flush()
    return RunDecision(run, created=False, should_enqueue=True)


def record_delivery(
    session: Session,
    run_id: uuid.UUID,
    token: uuid.UUID,
    celery_task_id: str,
    *,
    now: datetime.datetime | None = None,
    transport: str = "celery",
) -> PersonalRun:
    if transport not in {"celery", "subprocess"}:
        raise ValueError("unsupported personal processing transport")
    run = lock_owned_run(session, run_id, token, allow_queued=True, now=now)
    if run.state != "queued":
        raise PersonalOwnershipLost("queued delivery is owned by another attempt")
    run.delivery_transport = transport
    run.celery_task_id = celery_task_id if transport == "celery" else None
    session.flush()
    return run


def acquire_run(
    session: Session,
    run_id: uuid.UUID,
    token: uuid.UUID,
    *,
    now: datetime.datetime,
    generation: int | None = None,
    rotate_token: bool = False,
) -> PersonalRun:
    control = lock_processing_control(session)
    require_mode(control, "personal")
    run = session.execute(
        select(PersonalRun)
        .where(PersonalRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if run is None or run.state != "queued":
        raise PersonalOwnershipLost("personal run delivery is stale or missing")
    verify_control(control, run, token, now=now, allow_queued=True, generation=generation)
    run.state = "running"
    if rotate_token:
        run.ownership_token = uuid.uuid4()
    stamp = now.astimezone(datetime.UTC)
    run.started_at = stamp
    run.graceful_deadline_at = stamp + GRACEFUL_DEADLINE
    run.hard_deadline_at = stamp + HARD_DEADLINE
    run.lease_expires_at = stamp + RUNNING_LEASE
    control.ownership_token = run.ownership_token
    control.started_at = run.started_at
    control.graceful_deadline_at = run.graceful_deadline_at
    control.hard_deadline_at = run.hard_deadline_at
    control.lease_expires_at = run.lease_expires_at
    history = {name: dict(entry) for name, entry in (control.child_history or {}).items()}
    for entry in history.values():
        if entry.get("generation") == control.generation and entry.get("run_id") == str(run.id):
            entry["running_lease_expires_at"] = run.lease_expires_at.isoformat()
            entry["hard_deadline_at"] = run.hard_deadline_at.isoformat()
    control.child_history = history
    job = session.get(Job, run.job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise RuntimeError("personal run job is unavailable")
    job.state = "running"
    session.flush()
    return run


def lock_owned_run(
    session: Session,
    run_id: uuid.UUID,
    token: uuid.UUID,
    *,
    allow_queued: bool = False,
    now: datetime.datetime | None = None,
    generation: int | None = None,
    dispatch: bool = False,
) -> PersonalRun:
    control = lock_processing_control(session, lock="dispatch" if dispatch else "business")
    require_mode(control, "personal")
    statement = select(PersonalRun).where(PersonalRun.id == run_id)
    statement = (
        statement.with_for_update(read=True, key_share=True)
        if dispatch
        else statement.with_for_update(key_share=True)
    )
    run = session.scalar(statement.execution_options(populate_existing=True))
    if run is None:
        raise PersonalOwnershipLost("personal write refused because attempt ownership changed")
    verify_control(
        control, run, token, now=now or utc_now(), allow_queued=allow_queued, generation=generation
    )
    return run


def finish_run(
    session: Session,
    run_id: uuid.UUID,
    token: uuid.UUID,
    *,
    state: str,
    result: dict | None = None,
    error: dict | None = None,
    now: datetime.datetime | None = None,
) -> PersonalRun:
    if state not in TERMINAL_STATES:
        raise ValueError("terminal personal state required")
    # Terminal release takes the exclusive global fence first, before the run lock.
    control = lock_processing_control(session)
    run = lock_owned_run(session, run_id, token, now=now)
    session.execute(select(PersonalRun.id).where(PersonalRun.id == run.id).with_for_update())
    from services.personal.snapshots import refresh_terminal_manifest

    refresh_terminal_manifest(session, run)
    run.state, run.result, run.error = state, result, error
    run.lease_expires_at = None
    job = session.get(Job, run.job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise RuntimeError("personal run job is unavailable")
    job.state = "succeeded" if state == "succeeded" else "failed"
    job.error = error
    release_control(control)
    session.flush()
    return run


def mark_delivery_failed(
    session: Session,
    run_id: uuid.UUID,
    token: uuid.UUID,
    *,
    now: datetime.datetime | None = None,
    error_code: str = "dispatch_failed",
    generation: int | None = None,
) -> bool:
    control = lock_processing_control(session)
    return fail_owned_launch(
        session,
        run_id,
        token,
        control.generation if generation is None else generation,
        code=error_code,
        now=now,
    )


def _is_dedicated_live_profile(profile: PersonalProfileRevision | None) -> bool:
    if (
        profile is None
        or profile.execution_profile != "assisted"
        or profile.schema_revision == "personal-profile.v2"
    ):
        return False
    route = (profile.settings or {}).get("model_route")
    return isinstance(route, dict) and route.get("mode") == "live"


def retry_status(
    run: PersonalRun,
    *,
    now: datetime.datetime,
    profile: PersonalProfileRevision | None = None,
    control: PersonalWriterMode | None = None,
) -> tuple[bool, str]:
    if _is_dedicated_live_profile(profile):
        return False, "dedicated live smoke runs require a separately authorized verification"
    if run.attempt >= run.max_attempts:
        return False, "maximum attempts reached"
    if control is not None:
        if control.mode != "personal":
            return False, "personal processing mode is not active"
        if control.active_run_id not in {None, run.id}:
            return False, f"processing_busy; active run {control.active_run_id}"
        uncertain_children = control.unconfirmed_child or any(
            not entry.get("exited", False) for entry in (control.child_history or {}).values()
        )
        if (
            run.state in {"failed", "partially_failed"}
            and control.active_run_id is None
            and uncertain_children
        ):
            return False, "awaiting confirmed child exit or explicit reconciliation"
    if run.state in {"failed", "partially_failed"}:
        return True, "explicit retry available"
    if run.state in ACTIVE_STATES and (
        run.lease_expires_at is None or run.lease_expires_at <= now.astimezone(datetime.UTC)
    ):
        if (
            control is not None
            and control.lease_expires_at is not None
            and control.lease_expires_at > now.astimezone(datetime.UTC)
        ):
            return False, "global ownership lease has not expired"
        return True, "ownership lease expired"
    return False, f"run in state {run.state} is not retryable"
