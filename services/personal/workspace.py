"""Idempotent single-workspace bootstrap and explicit local-owner binding."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from db.models import (
    PersonalProfileRevision,
    PersonalWorkspace,
    PersonalWriterMode,
    Source,
    User,
    WatchlistItem,
)
from db.models.personal import PERSONAL_TIMEZONE
from packages.config.settings import get_settings
from services.personal.contracts import validate_phrases
from services.personal.processing import (
    ProcessingBusy,
    lock_processing_control,
    switch_processing_mode,
)


class OwnerSelectionRequired(RuntimeError):
    pass


class OwnerBindingConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnerChoice:
    id: uuid.UUID
    label: str
    legacy_saved_count: int


def _activate_personal_writer_mode(session: Session) -> bool:
    """Activate personal writes only while no durable legacy job is active."""

    # Legacy transport retains its explicit setup compatibility. Saving preferences in
    # subprocess mode never activates processing; switch-mode is a separate idle action.
    if getattr(get_settings(), "personal_processing_transport", "celery") == "subprocess":
        return False
    if (
        session.scalar(
            select(PersonalWriterMode.mode).where(PersonalWriterMode.singleton.is_(True))
        )
        == "personal"
    ):
        return True
    try:
        switch_processing_mode(session, "personal")
    except ProcessingBusy:
        return False
    return True


def _owner_choices(session: Session) -> tuple[OwnerChoice, ...]:
    rows = session.execute(
        select(User, func.count(WatchlistItem.id))
        .outerjoin(WatchlistItem, WatchlistItem.user_id == User.id)
        .group_by(User.id)
        .order_by(User.created_at, User.id)
    ).all()
    return tuple(
        OwnerChoice(
            id=user.id,
            label=user.display_name or user.email,
            legacy_saved_count=int(count),
        )
        for user, count in rows
    )


def ensure_workspace(
    session: Session,
    *,
    configured_owner_id: uuid.UUID | None = None,
    timezone: str = PERSONAL_TIMEZONE,
) -> tuple[PersonalWorkspace, tuple[OwnerChoice, ...]]:
    """Return/create the singleton workspace, preserving every existing user/save.

    The caller owns commit.  Concurrent first callers converge on the database singleton.
    """

    # SELECT FOR UPDATE cannot lock a singleton row which does not exist yet.  The advisory
    # transaction lock makes first-use creation converge without weakening ordinary reads.
    session.execute(select(func.pg_advisory_xact_lock(1_884_720_002)))
    workspace = session.execute(select(PersonalWorkspace).with_for_update()).scalars().first()
    choices = _owner_choices(session)
    if workspace is not None:
        if workspace.owner_id is not None:
            return workspace, choices
        if configured_owner_id is not None:
            workspace = bind_owner(session, workspace, configured_owner_id)
        elif len(choices) == 1:
            workspace = bind_owner(session, workspace, choices[0].id)
            workspace.setup_provenance = {
                **(workspace.setup_provenance or {}),
                "owner_resolution": "only_existing_user",
            }
        elif not choices:
            owner = User(
                email=f"personal-{workspace.id}@local.invalid",
                display_name="Local personal owner",
                role="local_owner",
            )
            session.add(owner)
            session.flush()
            workspace = bind_owner(session, workspace, owner.id)
            workspace.setup_provenance = {
                **(workspace.setup_provenance or {}),
                "owner_resolution": "created_reserved_local_owner",
            }
        return workspace, choices

    owner_id: uuid.UUID | None = None
    provenance: dict[str, object] = {"schema": "personal-workspace-setup.v1"}
    if configured_owner_id is not None:
        owner = session.get(User, configured_owner_id)
        if owner is None:
            raise ValueError("configured personal owner does not exist")
        owner_id = owner.id
        provenance["owner_resolution"] = "explicit_config"
    elif len(choices) == 1:
        owner_id = choices[0].id
        provenance["owner_resolution"] = "only_existing_user"
    elif not choices:
        workspace_id = uuid.uuid4()
        owner = User(
            email=f"personal-{workspace_id}@local.invalid",
            display_name="Local personal owner",
            role="local_owner",
        )
        session.add(owner)
        session.flush()
        owner_id = owner.id
        provenance["owner_resolution"] = "created_reserved_local_owner"
        choices = (
            OwnerChoice(id=owner.id, label=owner.display_name or owner.email, legacy_saved_count=0),
        )
    else:
        workspace_id = uuid.uuid4()
        provenance["owner_resolution"] = "selection_required"

    workspace_id = locals().get("workspace_id", uuid.uuid4())
    workspace = PersonalWorkspace(
        id=workspace_id,
        singleton=True,
        owner_id=owner_id,
        timezone=timezone,
        setup_provenance=provenance,
    )
    session.add(workspace)
    session.flush()
    profile = PersonalProfileRevision(
        workspace_id=workspace.id,
        revision=1,
        selected_source_ids=[],
        include_phrases=[],
        exclude_phrases=[],
        execution_profile="assisted",
        settings={
            "source": "selection_required",
            "observed_active_source_count": session.scalar(
                select(func.count()).select_from(Source).where(Source.active.is_(True))
            )
            or 0,
        },
        schema_revision="personal-profile.v1",
    )
    session.add(profile)
    session.flush()
    workspace.active_profile_revision_id = profile.id
    return workspace, choices


def configure_profile(
    session: Session,
    workspace: PersonalWorkspace,
    *,
    selected_source_ids: list[uuid.UUID] | tuple[uuid.UUID, ...],
    include_phrases: list[str] | tuple[str, ...] = (),
    exclude_phrases: list[str] | tuple[str, ...] = (),
    execution_profile: str = "assisted",
    settings: dict[str, object] | None = None,
) -> PersonalProfileRevision:
    """Create an immutable, explicitly configured profile for later runs."""

    selected = tuple(dict.fromkeys(selected_source_ids))
    profile_settings = {"source": "explicit_configuration", **(settings or {})}
    if not 1 <= len(selected) <= 10:
        raise ValueError("select between 1 and 10 personal feed sources")
    if execution_profile not in {"assisted", "raw"}:
        raise ValueError("execution_profile must be assisted or raw")
    if execution_profile == "assisted" and not profile_settings.get("model_route"):
        raise ValueError("assisted personal processing requires an explicit model route")
    route = profile_settings.get("model_route")
    try:
        authorized_spend = float(profile_settings.get("authorized_spend_usd") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("authorized spending allowance must be a finite number") from exc
    if not math.isfinite(authorized_spend) or authorized_spend < 0:
        raise ValueError("authorized spending allowance must be a finite non-negative number")
    if (
        execution_profile == "assisted"
        and isinstance(route, dict)
        and route.get("mode") != "offline_fixture"
        and authorized_spend <= 0
    ):
        raise ValueError(
            "live assisted processing requires an explicit positive spending allowance"
        )
    validate_phrases(include_phrases, name="include_phrases")
    validate_phrases(exclude_phrases, name="exclude_phrases")
    if getattr(get_settings(), "personal_processing_transport", "celery") == "celery":
        lock_processing_control(session)
    found = set(
        session.execute(
            select(Source.id).where(Source.id.in_(selected), Source.active.is_(True))
        ).scalars()
    )
    if found != set(selected):
        raise ValueError("every selected personal feed source must exist and be active")
    locked = session.execute(
        select(PersonalWorkspace)
        .where(PersonalWorkspace.id == workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    revision = (
        session.scalar(
            select(func.coalesce(func.max(PersonalProfileRevision.revision), 0)).where(
                PersonalProfileRevision.workspace_id == locked.id
            )
        )
        + 1
    )
    profile = PersonalProfileRevision(
        workspace_id=locked.id,
        revision=revision,
        selected_source_ids=list(selected),
        include_phrases=list(include_phrases),
        exclude_phrases=list(exclude_phrases),
        execution_profile=execution_profile,
        settings=profile_settings,
        schema_revision="personal-profile.v1",
    )
    session.add(profile)
    session.flush()
    locked.active_profile_revision_id = profile.id
    _activate_personal_writer_mode(session)
    session.flush()
    return profile


def bind_owner(
    session: Session, workspace: PersonalWorkspace, owner_id: uuid.UUID
) -> PersonalWorkspace:
    if session.get(User, owner_id) is None:
        raise ValueError("owner does not exist")
    updated = session.execute(
        update(PersonalWorkspace)
        .where(PersonalWorkspace.id == workspace.id, PersonalWorkspace.owner_id.is_(None))
        .values(
            owner_id=owner_id,
            setup_provenance={
                **(workspace.setup_provenance or {}),
                "owner_resolution": "explicit_selection",
            },
        )
        .returning(PersonalWorkspace.id)
    ).scalar_one_or_none()
    session.expire(workspace)
    session.refresh(workspace)
    if updated is not None or workspace.owner_id == owner_id:
        return workspace
    raise OwnerBindingConflict(
        "personal owner is already bound; reassignment requires a future explicit migration"
    )


def get_workspace(session: Session) -> tuple[PersonalWorkspace | None, tuple[OwnerChoice, ...]]:
    """Read setup state without making a GET-triggered database change."""

    return session.execute(select(PersonalWorkspace)).scalars().first(), _owner_choices(session)


def require_owner(workspace: PersonalWorkspace) -> uuid.UUID:
    if workspace.owner_id is None:
        raise OwnerSelectionRequired("select the personal workspace owner before saving")
    return workspace.owner_id
