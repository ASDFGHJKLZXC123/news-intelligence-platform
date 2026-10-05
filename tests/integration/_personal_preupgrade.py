"""Author old-schema fixtures without invoking the current runtime's new fences."""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import MetaData, Table, func, select
from sqlalchemy.orm import Session

from db.models import Job, PersonalProfileRevision, PersonalWorkspace
from services.personal.runs import personal_job_key


def configure_preupgrade_profile(
    session: Session,
    workspace: PersonalWorkspace,
    source_ids: list[uuid.UUID],
    *,
    execution_profile: str = "raw",
    settings: dict | None = None,
    schema_revision: str = "personal-profile.v1",
) -> PersonalProfileRevision:
    revision = (
        session.scalar(
            select(func.max(PersonalProfileRevision.revision)).where(
                PersonalProfileRevision.workspace_id == workspace.id
            )
        )
        or 0
    )
    profile = PersonalProfileRevision(
        workspace_id=workspace.id,
        revision=revision + 1,
        selected_source_ids=source_ids,
        include_phrases=[],
        exclude_phrases=[],
        execution_profile=execution_profile,
        settings=settings or {},
        schema_revision=schema_revision,
    )
    session.add(profile)
    session.flush()
    workspace.active_profile_revision_id = profile.id
    mode = Table("personal_writer_mode", MetaData(), autoload_with=session.connection())
    session.execute(mode.update().where(mode.c.singleton.is_(True)).values(mode="personal"))
    return profile


def create_preupgrade_run(
    session: Session,
    workspace: PersonalWorkspace,
    profile: PersonalProfileRevision,
    *,
    now: dt.datetime,
    state: str = "succeeded",
    token: uuid.UUID | None = None,
    lease_expires_at: dt.datetime | None = None,
    **retained_fields,
) -> SimpleNamespace:
    local_date = now.astimezone(ZoneInfo(workspace.timezone)).date()
    job = Job(
        job_key=personal_job_key(workspace.id, local_date),
        job_type="personal_daily",
        state=state,
        attempt=1,
        max_attempts=3,
        related_ids={"workspace_id": str(workspace.id), "local_date": local_date.isoformat()},
        safe_to_rerun=True,
    )
    session.add(job)
    session.flush()
    values = dict(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        local_date=local_date,
        profile_revision_id=profile.id,
        job_id=job.id,
        execution_mode="personal",
        state=state,
        attempt=1,
        max_attempts=3,
        ownership_token=token,
        lease_expires_at=lease_expires_at,
        admitted_article_ids=[],
        enrichment_article_ids=[],
        event_ids=[],
        coverage={},
        stage_results={},
    )
    values.update(retained_fields)
    table = Table("personal_runs", MetaData(), autoload_with=session.connection())
    row = session.execute(table.insert().values(**values).returning(table)).mappings().one()
    return SimpleNamespace(**row)
