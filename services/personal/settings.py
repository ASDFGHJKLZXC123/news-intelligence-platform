"""Validated immutable settings and feature-specific, network-free readiness."""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db.models import (
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    PersonalWriterMode,
    Source,
)
from services.ingestion.capture_bounds import validate_http_url
from services.personal.contracts import validate_phrases

SETTINGS_SCHEMA = "personal-profile.v2"
ROUTE_FIELDS = {
    "provider",
    "model",
    "model_version",
    "price_revision",
    "price_source_url",
    "input_usd_per_million_tokens",
    "output_usd_per_million_tokens",
    "max_input_tokens",
    "max_output_tokens",
    "deadline_seconds",
}


def usd_string(value: Any) -> str | None:
    """Do not accept binary floats, non-finite amounts, or unbounded decimal exponents."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("USD allowance must be a finite decimal string or null")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("USD allowance must be a finite decimal string") from exc
    if not amount.is_finite() or amount < 0 or amount >= Decimal("100000000"):
        raise ValueError("USD allowance must be finite and between 0 and 100000000")
    if amount.as_tuple().exponent < -12:
        raise ValueError("USD allowance supports at most 12 decimal places")
    return format(amount, "f")


def safe_model_route(route: Any) -> dict[str, Any]:
    """Return the public route contract, never credentials or arbitrary stored extras."""
    if not isinstance(route, dict):
        return {}
    result = {key: route[key] for key in ("mode", "fixture_id") if isinstance(route.get(key), str)}
    for role in ("generation", "embedding"):
        if isinstance(route.get(role), dict):
            result[role] = {
                key: value
                for key, value in route[role].items()
                if key in ROUTE_FIELDS
                and isinstance(value, (str, int))
                and not isinstance(value, bool)
            }
    if route.get("fallbacks") == []:
        result["fallbacks"] = []
    return result


class PersonalSettingsValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_article_limit: int = Field(default=100, strict=True, ge=0, le=100000)
    run_article_limit: int = Field(default=100, strict=True, ge=0, le=100000)
    enrichment_article_limit: int = Field(default=100, strict=True, ge=0, le=100000)
    max_enabled_feeds: int = Field(default=10, strict=True, ge=1, le=100)
    ai_enabled: bool = Field(default=False, strict=True)
    monthly_allowance_usd: str | None = None
    run_allowance_usd: str | None = None
    model_route: dict[str, Any] = Field(default_factory=dict)

    @field_validator("monthly_allowance_usd", "run_allowance_usd", mode="before")
    @classmethod
    def validate_usd(cls, value: Any) -> str | None:
        return usd_string(value)

    @field_validator("model_route")
    @classmethod
    def validate_route(cls, route: dict[str, Any]) -> dict[str, Any]:
        if not route:
            return {}
        if set(route) - {"mode", "fixture_id", "generation", "embedding", "fallbacks"}:
            raise ValueError(
                "model route contains unsupported fields; secrets remain environment-only"
            )
        if route.get("mode") not in {"offline_fixture", "live"}:
            raise ValueError("model route mode must be offline_fixture or live")
        if route.get("fallbacks", []) != []:
            raise ValueError("fallback model routes are not enabled for the personal profile")
        for role in ("generation", "embedding"):
            value = route.get(role)
            if value is not None and (not isinstance(value, dict) or set(value) - ROUTE_FIELDS):
                raise ValueError(
                    "model route fields are invalid; credentials remain environment-only"
                )
        if route != safe_model_route(route):
            raise ValueError("model route values must use the public bounded route contract")
        if route.get("mode") == "live":
            from services.personal.spending import validate_model_route

            return validate_model_route(route)
        return copy.deepcopy(route)


class PersonalSettingsUpdate(PersonalSettingsValues):
    selected_source_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    include_phrases: list[str] = Field(default_factory=list, max_length=20)
    exclude_phrases: list[str] = Field(default_factory=list, max_length=20)
    execution_profile: Literal["raw", "assisted"] = "raw"
    timezone: str = "America/Los_Angeles"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("include_phrases", "exclude_phrases")
    @classmethod
    def validate_interest(cls, values: list[str]) -> list[str]:
        validate_phrases(values, name="interest phrases")
        return values

    @model_validator(mode="after")
    def validate_selection(self):
        self.selected_source_ids = list(dict.fromkeys(self.selected_source_ids))
        if len(self.selected_source_ids) > self.max_enabled_feeds:
            raise ValueError("selected sources exceed max_enabled_feeds")
        if self.ai_enabled and self.execution_profile == "raw":
            raise ValueError("paid AI enablement requires the assisted processing profile")
        return self


class PersonalSourceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    feed_url: str = Field(max_length=2048)
    source_type: Literal["rss"] = "rss"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("source name must contain visible text without control characters")
        return value

    @field_validator("feed_url")
    @classmethod
    def validate_feed_url(cls, value: str) -> str:
        return validate_http_url(value)


def register_source_draft(
    session: Session, workspace: PersonalWorkspace, request: PersonalSourceDraft
) -> Source:
    """Record a feed without fetching it or making it eligible for either writer."""
    locked = session.execute(
        select(PersonalWorkspace)
        .where(PersonalWorkspace.id == workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    provenance = dict(locked.setup_provenance or {})
    drafts = list(provenance.get("draft_source_ids", []))
    existing = session.scalar(select(Source).where(Source.feed_url == request.feed_url))
    if existing is not None:
        return existing
    if len(drafts) >= 100:
        raise ValueError("at most 100 unselected source drafts may be retained")
    source_id = session.execute(
        insert(Source)
        .values(
            id=uuid.uuid4(),
            name=request.name,
            feed_url=request.feed_url,
            source_type="rss",
            active=False,
        )
        .on_conflict_do_nothing(index_elements=[Source.feed_url])
        .returning(Source.id)
    ).scalar_one_or_none()
    if source_id is not None:
        drafts.append(str(source_id))
        locked.setup_provenance = {**provenance, "draft_source_ids": drafts}
        session.flush()
        return session.get(Source, source_id)
    return session.scalar(select(Source).where(Source.feed_url == request.feed_url))


def validated_settings(profile: PersonalProfileRevision | None) -> dict[str, Any]:
    """Read v1 compatibly without promoting a completed test allowance into live credit."""
    stored = (profile.settings or {}) if profile is not None else {}
    fields = PersonalSettingsValues.model_fields
    values = {key: stored[key] for key in fields if key in stored}
    # Existing test routes may use older metadata; preserve them only within their fixture.
    route = safe_model_route(values.pop("model_route", {}))
    result = PersonalSettingsValues.model_validate(values).model_dump()
    result["model_route"] = route
    return result


def effective_run_allowance(profile: PersonalProfileRevision | None) -> Decimal:
    values = validated_settings(profile)
    if not values["ai_enabled"] or (profile is not None and profile.execution_profile == "raw"):
        return Decimal("0")
    month = Decimal(values["monthly_allowance_usd"] or "0")
    run = values["run_allowance_usd"]
    return min(month, Decimal(run)) if run is not None else month


def processing_block_reason(profile: PersonalProfileRevision | None) -> str | None:
    if profile is None:
        return "profile_missing"
    if profile.execution_profile == "raw":
        return "disabled_by_profile"
    values = validated_settings(profile)
    route = values["model_route"]
    if route.get("mode") == "offline_fixture":
        return None
    if not values["ai_enabled"]:
        return "ai_disabled"
    if not route or not values["monthly_allowance_usd"]:
        return "configuration_missing"
    if effective_run_allowance(profile) <= 0:
        return "allowance_reached"
    try:
        from services.personal.spending import PaidRoute

        for role in ("generation", "embedding"):
            PaidRoute.from_mapping(route.get(role, {}), role=role)
    except (ValueError, TypeError, KeyError):
        return "configuration_missing"
    return None


def processing_ai_enabled(profile: PersonalProfileRevision | None) -> bool:
    return processing_block_reason(profile) is None


def feature_readiness(
    profile: PersonalProfileRevision | None,
    *,
    owner_ready: bool,
    writer_ready: bool,
    runtime: Any = None,
) -> dict[str, Any]:
    """Inspect configuration only; raw reads never initialize providers, Redis, or workers."""
    intake_reason = (
        "owner_selection_required"
        if not owner_ready
        else "feeds_not_configured"
        if profile is None or not profile.selected_source_ids
        else "personal_writer_unavailable"
        if not writer_ready
        else None
    )
    reason = processing_block_reason(profile)
    route = validated_settings(profile)["model_route"]
    if reason is None:
        if route.get("mode") == "offline_fixture" and runtime is not None:
            from services.personal.offline_fixture import offline_fixture_route

            fixture_id = route.get("fixture_id")
            if not (
                runtime.app_env in {"local", "test"}
                and runtime.personal_offline_fixture_path
                and isinstance(fixture_id, str)
                and fixture_id
                and route == offline_fixture_route(fixture_id)
            ):
                reason = "offline_fixture_unavailable"
        elif route.get("mode") == "live":
            if profile is not None and profile.schema_revision == SETTINGS_SCHEMA:
                from services.personal.paid_runtime import runtime_readiness

                # Public redaction must not make unsupported stored execution settings ready.
                reason = runtime_readiness(runtime, (profile.settings or {}).get("model_route"))
            else:
                if runtime is not None:
                    for role in ("generation", "embedding"):
                        provider = route[role].get("provider")
                        if not getattr(runtime, f"{provider}_api_key", ""):
                            reason = "provider_credentials_missing"
                reason = reason or "execution_route_unavailable"
    disabled = reason in {"ai_disabled", "disabled_by_profile", "paid_runtime_disabled"}
    ai_state = "disabled" if disabled else "blocked" if reason or intake_reason else "ready"
    ai_reason = reason or intake_reason
    return {
        "raw_reader": {"state": "ready", "reason": None},
        "saved_and_briefs": {"state": "ready", "reason": None},
        "raw_collection": {
            "state": "blocked" if intake_reason else "ready",
            "reason": intake_reason,
        },
        "embeddings": {"state": ai_state, "reason": ai_reason},
        "brief_generation": {"state": ai_state, "reason": ai_reason},
        "entity_linking": {"state": "disabled", "reason": "disabled_by_profile"},
        "event_embeddings": {"state": "disabled", "reason": "disabled_by_profile"},
        "analogies": {"state": "disabled", "reason": "disabled_by_profile"},
        "forecasting": {"state": "disabled", "reason": "disabled_by_profile"},
    }


def update_settings(
    session: Session,
    workspace: PersonalWorkspace,
    request: PersonalSettingsUpdate,
) -> PersonalProfileRevision:
    """Append one revision; callers own commit. Existing run revisions are never mutated."""
    from services.personal.spending import (
        acquire_workspace_spending_lock,
        assert_legacy_usage_reconciled,
    )
    from services.personal.workspace import _activate_personal_writer_mode

    # Ordinary updates must not wait on the coordinator's writer-mode lock while holding
    # the spending lock it needs for its next request. First activation alone takes mode
    # before spending/workspace; an already personal workspace needs no mode mutation.
    mode = session.scalar(
        select(PersonalWriterMode.mode).where(PersonalWriterMode.singleton.is_(True))
    )
    if mode != "personal":
        _activate_personal_writer_mode(session)
    acquire_workspace_spending_lock(session, workspace.id)
    if request.ai_enabled:
        assert_legacy_usage_reconciled(session, workspace.id)
    locked = session.execute(
        select(PersonalWorkspace)
        .where(PersonalWorkspace.id == workspace.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if request.timezone != locked.timezone:
        has_run = session.scalar(
            select(PersonalRun.id).where(PersonalRun.workspace_id == locked.id).limit(1)
        )
        if has_run:
            raise ValueError(
                "timezone cannot change after the first run; an explicit future migration is required"
            )
    selected = set(request.selected_source_ids)
    drafts = set((locked.setup_provenance or {}).get("draft_source_ids", []))
    if selected:
        rows = list(session.scalars(select(Source).where(Source.id.in_(selected))))
        found = {
            row.id
            for row in rows
            if row.source_type == "rss" and (row.active or str(row.id) in drafts)
        }
        if found != selected:
            raise ValueError(
                "every selected RSS source must exist and be active or explicitly drafted here"
            )
        activated = {str(row.id) for row in rows if not row.active}
        if (
            activated
            and session.scalar(
                select(PersonalWriterMode.mode).where(PersonalWriterMode.singleton.is_(True))
            )
            != "personal"
        ):
            raise ValueError("draft feeds cannot be enabled while a legacy writer is active")
        for row in rows:
            row.active = True
        locked.setup_provenance = {
            **(locked.setup_provenance or {}),
            "draft_source_ids": sorted(drafts - activated),
        }
    values = request.model_dump(include=set(PersonalSettingsValues.model_fields))
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
        selected_source_ids=request.selected_source_ids,
        include_phrases=request.include_phrases,
        exclude_phrases=request.exclude_phrases,
        execution_profile=request.execution_profile,
        settings={**values, "source": "explicit_configuration", "timezone": request.timezone},
        schema_revision=SETTINGS_SCHEMA,
    )
    session.add(profile)
    session.flush()
    locked.timezone = request.timezone
    locked.active_profile_revision_id = profile.id
    session.flush()
    return profile
