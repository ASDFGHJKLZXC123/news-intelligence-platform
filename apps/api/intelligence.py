"""Frontend intelligence query APIs, plus the ADR 0010 alert operator endpoints.

Most of this module is the database-backed read contract the static frontend can cut over
to page by page while keeping fixture JSON as a fallback during development. The alert
mutation endpoints near the bottom (`acknowledge`, `acknowledge-all-clear`, `supersede`) are
the one place this module writes: they are thin HTTP wrappers over
`services.alerts.AlertLifecycleService` and `services.alerts.supersession`, translating the
service's typed exceptions into the 404 (unknown alert) / 409 (terminal or business-rule
conflict) responses FastAPI callers expect. They hold no alert policy themselves -- the
service decides, this module only serializes and maps errors.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, Text, and_, any_, case, cast, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from db.base import get_session
from db.models import (
    ACTIVE_ALERT_STATES,
    REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY,
    Alert,
    Article,
    Claim,
    ClaimEvidence,
    Company,
    CompanyRiskRollup,
    CrisisPrediction,
    DailyIntelligenceSummary,
    Event,
    EventCompany,
    EventIndustry,
    EventLocation,
    EventTimelineItem,
    EvidenceItem,
    IndustryRiskRollup,
    Job,
    LLMRun,
    PersonalClaimPreparation,
    Report,
    ReportSection,
    RiskLevel,
    RiskScoreObservation,
    Source,
    SourceHealthSnapshot,
    WatchlistItem,
    risk_level_for_score,
)
from db.models.personal import PERSONAL_REPORT_TYPE
from packages.config.settings import get_settings
from services.alerts import AlertLifecycleService, SQLAlchemyAlertRepository
from services.alerts.supersession import SupersessionError, supersede_with_broader_alert
from services.reports.context import (
    ARTICLE_EVIDENCE_SOURCE_TYPE,
    MAX_EXCERPT_CHARS,
    build_source_excerpt,
)
from services.reports.exports import (
    ReportExportRepository,
    collect_claim_refs,
    export_filename,
    render_markdown,
    render_pdf,
)
from services.reports.lifecycle import (
    ReportLifecycleRepository,
    ReportSectionSnapshot,
    ReportSnapshot,
)
from services.reports.repository import DAILY_BRIEF_REPORT_TYPE, EVENT_RISK_TARGET_TYPE
from services.reports.selection import PUBLISHED_STATUS
from services.writer_mode import (
    LegacyWriterModeConflict,
    mark_legacy_daily_brief_delivery_failed,
    queue_legacy_daily_brief,
    require_legacy_maintenance_mode,
    require_legacy_writer_mode,
)

router = APIRouter(tags=["intelligence"])


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


SessionDep = Annotated[Session, Depends(get_session)]


def _iso(value: datetime.date | datetime.datetime | None) -> str | None:
    """Render a date/datetime for the wire: UTC ISO 8601 with a trailing `Z`.

    Naive datetimes are stored as UTC, so they are stamped as UTC rather than emitted
    without an offset -- the adapter cannot guess a timezone (api-adapter-contract).
    """
    if value is None:
        return None
    if not isinstance(value, datetime.datetime):
        return value.isoformat()
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.date | datetime.datetime):
        return _iso(value)
    return value


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _page(items: list[Any], *, total: int, limit: int, offset: int) -> dict[str, Any]:
    """The consumed-list wire envelope (api-adapter-contract): exactly these four keys.

    ``total`` is the real filtered relation size before LIMIT/OFFSET -- never ``len(items)`` --
    so a partial, offset, or empty page still reports how much the caller can page through.
    """
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _serialize_event(event: Event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "title": event.title,
        "summary": event.summary,
        "event_type": event.event_type,
        "country": event.country,
        "region": event.region,
        "severity_score": _json_value(event.severity_score),
        "article_count": event.article_count,
        "source_count": event.source_count,
        "first_seen_at": _iso(event.first_seen_at),
        "last_seen_at": _iso(event.last_seen_at),
        "created_at": _iso(event.created_at),
        "updated_at": _iso(event.updated_at),
    }


class EventStatus(StrEnum):
    """The event-list wire status vocabulary (fixtures' ``EventCard.status``).

    The Event table has no status column; this value is *derived* from persisted lifecycle
    facts (see :func:`_event_status_case`), never stored. Reused as the ``status`` query-filter
    type so an unknown value is rejected as a 422 rather than silently returning an empty page.
    """

    NEW = "new"
    DEVELOPING = "developing"
    UPDATED = "updated"
    RESOLVED = "resolved"


#: An event's alerting lifecycle is "closed" once a linked alert has resolved and none is live.
_RESOLVED_ALERT_STATE = "resolved"


@dataclass(frozen=True)
class EventCompanyView:
    """One event->company link joined to the company's identity, for embedding on an event.

    Carries the company's real name/ticker (killing the bare-UUID N+1) alongside the
    persisted ``event_companies`` relationship fields. Never fabricated.
    """

    company_id: uuid.UUID
    display_name: str
    primary_ticker: str | None
    exchange: str | None
    industry: str | None
    impact_direction: str | None
    impact_score: Any
    risk_score: Any
    confidence_score: Any
    exposure_explanation: str | None


@dataclass(frozen=True)
class EventListRow:
    """A single event-list item assembled query-bound: the event plus its derived risk/status
    and its bulk-loaded company/industry/location identities.

    ``risk_score``/``confidence_score`` are the values computed by :func:`_event_risk_expr` in
    the same statement the ``risk_level`` filter uses, so a filtered page and its serialization
    agree by construction. ``status`` is the derived :class:`EventStatus`.
    """

    event: Event
    risk_score: Any
    confidence_score: Any
    status: str
    companies: tuple[EventCompanyView, ...]
    industries: tuple[EventIndustry, ...]
    locations: tuple[EventLocation, ...]


def _event_risk_expr(*, prediction_backed_outputs_enabled: bool = False) -> tuple[Any, Any]:
    """SQL ``(risk_score, confidence_score)`` for an event, from persisted sources only.

    ``risk_score`` prefers the latest ``RiskScoreObservation`` targeting the event itself
    (``target_type='event'``, the same semantics the daily brief reads) and falls back to the
    maximum persisted linked ``EventCompany``/``EventIndustry`` risk_score; it is NULL only when
    the event has no real score anywhere. ``confidence_score`` is that same latest observation's
    confidence -- a persisted direct-observation value, never a constant -- and is NULL when no
    direct observation exists. Both are correlated scalar subqueries, so they never multiply the
    event's row.
    """
    obs_filter = (
        RiskScoreObservation.target_type == EVENT_RISK_TARGET_TYPE,
        RiskScoreObservation.target_id == cast(Event.id, Text),
    )
    obs_order = (RiskScoreObservation.as_of.desc(), RiskScoreObservation.id.desc())
    latest_score = (
        select(RiskScoreObservation.score)
        .where(*obs_filter)
        .order_by(*obs_order)
        .limit(1)
        .correlate(Event)
        .scalar_subquery()
    )
    latest_confidence = (
        select(RiskScoreObservation.confidence_score)
        .where(*obs_filter)
        .order_by(*obs_order)
        .limit(1)
        .correlate(Event)
        .scalar_subquery()
    )
    if prediction_backed_outputs_enabled:
        company_risk = (
            select(func.max(EventCompany.risk_score))
            .where(EventCompany.event_id == Event.id)
            .correlate(Event)
            .scalar_subquery()
        )
        industry_risk = (
            select(func.max(EventIndustry.risk_score))
            .where(EventIndustry.event_id == Event.id)
            .correlate(Event)
            .scalar_subquery()
        )
        risk_score = func.coalesce(latest_score, func.greatest(company_risk, industry_risk))
    else:
        # Direct RiskScoreObservation rows remain visible descriptive observations. Persisted
        # event-company/industry scores are prediction-backed rollups and are not referenced.
        risk_score = latest_score
    return risk_score, latest_confidence


def _risk_level_case(risk_score: Any) -> Any:
    """The canonical 0-100 risk band as SQL, mirroring :func:`risk_level_for_score` exactly.

    Only used to *filter* by ``risk_level``; the serialized ``risk_level`` calls the canonical
    Python function on the same computed ``risk_score``, so filter and payload share one rule.
    A NULL score yields a NULL band (an unscored event never matches a ``risk_level`` filter).
    """
    return case(
        (risk_score <= 30, RiskLevel.LOW.value),
        (risk_score <= 55, RiskLevel.MEDIUM.value),
        (risk_score <= 75, RiskLevel.HIGH.value),
        (risk_score > 75, RiskLevel.CRITICAL.value),
        else_=None,
    )


def _event_status_case(*, composite_alerts_enabled: bool = False) -> Any:
    """The derived :class:`EventStatus` as SQL, from persisted lifecycle facts only.

    Deterministic and total, evaluated top-down: ``resolved`` when the event's alerting
    lifecycle has closed (a linked alert resolved and none still live); else ``developing``
    while coverage is still widening (last_seen_at after first_seen_at); else ``updated`` when
    the record was revised after creation; else ``new``. The identical expression is used for
    the ``status`` filter and for serialization, so they cannot diverge.
    """
    lifecycle_cases: list[tuple[Any, str]] = []
    if composite_alerts_enabled:
        resolved_alert = (
            select(Alert.id)
            .where(Alert.related_event_id == Event.id, Alert.state == _RESOLVED_ALERT_STATE)
            .correlate(Event)
            .exists()
        )
        active_alert = (
            select(Alert.id)
            .where(Alert.related_event_id == Event.id, Alert.state.in_(ACTIVE_ALERT_STATES))
            .correlate(Event)
            .exists()
        )
        lifecycle_cases.append((and_(resolved_alert, ~active_alert), EventStatus.RESOLVED.value))
    lifecycle_cases.extend(
        [
            (
                and_(
                    Event.last_seen_at.isnot(None),
                    Event.first_seen_at.isnot(None),
                    Event.last_seen_at > Event.first_seen_at,
                ),
                EventStatus.DEVELOPING.value,
            ),
            (Event.updated_at > Event.created_at, EventStatus.UPDATED.value),
        ]
    )
    return case(*lifecycle_cases, else_=EventStatus.NEW.value)


def _event_company_filter(company: str) -> Any:
    """A non-multiplying EXISTS matching an event that links a company by UUID or ticker/name."""
    company_uuid = _uuid_or_none(company)
    if company_uuid is not None:
        subquery = select(EventCompany.company_id).where(
            EventCompany.event_id == Event.id, EventCompany.company_id == company_uuid
        )
    else:
        like = f"%{company}%"
        subquery = (
            select(EventCompany.company_id)
            .join(Company, Company.id == EventCompany.company_id)
            .where(
                EventCompany.event_id == Event.id,
                or_(
                    func.upper(Company.primary_ticker) == company.upper(),
                    Company.display_name.ilike(like),
                    Company.legal_name.ilike(like),
                ),
            )
        )
    return subquery.correlate(Event).exists()


def _event_industry_filter(industry: str) -> Any:
    """A non-multiplying EXISTS matching an event linked to an industry by its persisted id/name."""
    like = f"%{industry}%"
    return (
        select(EventIndustry.industry_id)
        .where(
            EventIndustry.event_id == Event.id,
            or_(
                func.upper(EventIndustry.industry_id) == industry.upper(),
                EventIndustry.industry_id.ilike(like),
            ),
        )
        .correlate(Event)
        .exists()
    )


def _event_company_view(link: EventCompany, company: Company) -> EventCompanyView:
    return EventCompanyView(
        company_id=link.company_id,
        display_name=company.display_name,
        primary_ticker=company.primary_ticker,
        exchange=company.exchange,
        industry=company.industry,
        impact_direction=link.impact_direction,
        impact_score=link.impact_score,
        risk_score=link.risk_score,
        confidence_score=link.confidence_score,
        exposure_explanation=link.exposure_explanation,
    )


def _serialize_event_company(
    view: EventCompanyView,
    *,
    prediction_backed_outputs_enabled: bool = False,
) -> dict[str, Any]:
    return {
        "company_id": str(view.company_id),
        "display_name": view.display_name,
        "primary_ticker": view.primary_ticker,
        "exchange": view.exchange,
        "industry": view.industry,
        "impact_direction": (view.impact_direction if prediction_backed_outputs_enabled else None),
        "impact_score": (
            _json_value(view.impact_score) if prediction_backed_outputs_enabled else None
        ),
        "risk_score": (_json_value(view.risk_score) if prediction_backed_outputs_enabled else None),
        "confidence_score": _json_value(view.confidence_score),
        "exposure_explanation": (
            view.exposure_explanation if prediction_backed_outputs_enabled else None
        ),
    }


def _serialize_event_industry(
    industry: EventIndustry,
    *,
    prediction_backed_outputs_enabled: bool = False,
) -> dict[str, Any]:
    return {
        "industry_id": industry.industry_id,
        "impact_direction": (
            industry.impact_direction if prediction_backed_outputs_enabled else None
        ),
        "impact_score": (
            _json_value(industry.impact_score) if prediction_backed_outputs_enabled else None
        ),
        "risk_score": (
            _json_value(industry.risk_score) if prediction_backed_outputs_enabled else None
        ),
        "opportunity_score": (
            _json_value(industry.opportunity_score) if prediction_backed_outputs_enabled else None
        ),
    }


def _serialize_event_list_item(
    row: EventListRow,
    *,
    prediction_backed_outputs_enabled: bool = False,
) -> dict[str, Any]:
    """One expanded event-list item: event core + derived scores/status + embedded identities."""
    risk_score = _json_value(row.risk_score)
    payload = _serialize_event(row.event)
    payload.update(
        {
            "hotness_score": _json_value(row.event.hotness_score),
            "risk_score": risk_score,
            # Canonical band from the real score only; NULL score -> NULL level (never guessed).
            "risk_level": None if risk_score is None else risk_level_for_score(risk_score).value,
            "confidence_score": _json_value(row.confidence_score),
            "status": row.status,
            "companies": [
                _serialize_event_company(
                    view,
                    prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
                )
                for view in row.companies
            ],
            "industries": [
                _serialize_event_industry(
                    industry,
                    prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
                )
                for industry in row.industries
            ],
            "locations": [_serialize_event_location(location) for location in row.locations],
        }
    )
    return payload


def _serialize_event_location(
    location: EventLocation, event: Event | None = None
) -> dict[str, Any]:
    payload = {
        "id": str(location.id),
        "event_id": str(location.event_id),
        "location_name": location.location_name,
        "country_code": location.country_code,
        "region": location.region,
        "latitude": _json_value(location.latitude),
        "longitude": _json_value(location.longitude),
        "location_type": location.location_type,
        "confidence_score": _json_value(location.confidence_score),
        "created_at": _iso(location.created_at),
    }
    if event is not None:
        payload["event"] = _serialize_event(event)
    return payload


def _serialize_company(company: Company, rollup: CompanyRiskRollup | None = None) -> dict[str, Any]:
    payload = {
        "id": str(company.id),
        "entity_profile_id": str(company.entity_profile_id),
        "display_name": company.display_name,
        "legal_name": company.legal_name,
        "primary_ticker": company.primary_ticker,
        "exchange": company.exchange,
        "country": company.country,
        "sector": company.sector,
        "industry": company.industry,
        "website": company.website,
        "logo_url": company.logo_url,
        "logo_source": company.logo_source,
        "active": company.active,
        "created_at": _iso(company.created_at),
        "updated_at": _iso(company.updated_at),
    }
    if rollup is not None:
        payload["risk_rollup"] = _serialize_company_rollup(rollup)
    return payload


def _serialize_company_rollup(rollup: CompanyRiskRollup) -> dict[str, Any]:
    return {
        "id": str(rollup.id),
        "company_id": str(rollup.company_id),
        "as_of": _iso(rollup.as_of),
        "impact_score": _json_value(rollup.impact_score),
        "risk_score": _json_value(rollup.risk_score),
        "opportunity_score": _json_value(rollup.opportunity_score),
        "related_event_count": rollup.related_event_count,
        "top_driver": rollup.top_driver,
        "confidence_score": _json_value(rollup.confidence_score),
    }


def _serialize_industry_rollup(rollup: IndustryRiskRollup) -> dict[str, Any]:
    return {
        "id": str(rollup.id),
        "industry_id": rollup.industry_id,
        "as_of": _iso(rollup.as_of),
        "impact_score": _json_value(rollup.impact_score),
        "risk_score": _json_value(rollup.risk_score),
        "opportunity_score": _json_value(rollup.opportunity_score),
        "news_velocity_score": _json_value(rollup.news_velocity_score),
        "related_event_count": rollup.related_event_count,
        "summary": rollup.summary,
    }


def _serialize_risk_score(score: RiskScoreObservation) -> dict[str, Any]:
    return {
        "id": str(score.id),
        "target_type": score.target_type,
        "target_id": score.target_id,
        "risk_type": score.risk_type,
        "score": _json_value(score.score),
        "level": score.level,
        "confidence_score": _json_value(score.confidence_score),
        "as_of": _iso(score.as_of),
        "model_version": score.model_version,
        "evidence_refs": score.evidence_refs,
        "driver_refs": score.driver_refs,
    }


# --------------------------------------------------------------------------------------
# Risk-radar detail (api-adapter-contract, shape gap 5): GET /risk-radar/{risk_type} returns
# a snake_case RiskDetail (types.ts), never raw observation rows. Every field is a persisted
# fact or a transparent extraction from one: the latest real RiskScoreObservation supplies the
# score/severity/confidence/as_of/model metadata, and a matching real CrisisPrediction supplies
# the horizon probabilities, full model_rating, drivers, analogies, and invalidation signals.
# A field with no real source is [] / null -- never a fixture backfill or an invented label.
# --------------------------------------------------------------------------------------

#: Canonical horizon tokens (db.models.enums.Horizon) paired with their CrisisPrediction column.
_HORIZON_PROBABILITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("0_6m", "probability_0_6m"),
    ("6_12m", "probability_6_12m"),
    ("12_18m", "probability_12_18m"),
    ("within_18m", "probability_within_18m"),
)

#: The string keys a persisted top_driver / driver_ref may carry a real label under, in
#: preference order. A driver lacking every one of them is unlabeled and is skipped, never
#: given an invented name (risk-detail truth rules).
_DRIVER_LABEL_KEYS: tuple[str, ...] = ("name", "signal", "component")
#: A persisted historical_analogy is a driver dict titled by its case (services.crisis_model).
_ANALOGY_LABEL_KEYS: tuple[str, ...] = ("title", "name", "signal", "component")

#: RiskScoreObservation target types that map onto a RiskDetail related-id list. Others a
#: risk_type may target (country/region) have no RiskDetail field and are simply not carried.
RISK_TARGET_EVENT = EVENT_RISK_TARGET_TYPE
RISK_TARGET_INDUSTRY = "industry"
RISK_TARGET_COMPANY = "company"

#: The detail's related-id read is bounded: at most this many distinct (target_type, target_id)
#: rows for the risk_type. A risk_type observed against more targets than this is truncated by
#: the deterministic (target_type, target_id) order rather than silently unbounded.
RISK_DETAIL_RELATED_TARGET_LIMIT = 500


def _serialize_crisis_rating(prediction: CrisisPrediction) -> dict[str, Any]:
    """Every CrisisRating field (types.ts) from one real prediction. Never partial or fabricated.

    JSONB columns (``model_versions``/``top_drivers``/``evidence_refs``) are already JSON values;
    a null one becomes its empty container so the shape is total. ``as_of_date`` is date-only.
    """
    return {
        "target_type": prediction.target_type,
        "target_id": prediction.target_id,
        "risk_type": prediction.risk_type,
        "as_of_date": _iso(prediction.as_of_date),
        "probability_0_6m": _json_value(prediction.probability_0_6m),
        "probability_6_12m": _json_value(prediction.probability_6_12m),
        "probability_12_18m": _json_value(prediction.probability_12_18m),
        "probability_within_18m": _json_value(prediction.probability_within_18m),
        "risk_score": _json_value(prediction.risk_score),
        "risk_level": prediction.risk_level,
        "confidence_score": _json_value(prediction.confidence_score),
        "model_versions": prediction.model_versions or {},
        "top_drivers": prediction.top_drivers or [],
        "evidence_refs": prediction.evidence_refs or [],
        "what_could_escalate": _string_list(prediction.what_could_escalate),
        "what_could_reduce_risk": _string_list(prediction.what_could_reduce_risk),
    }


def _probability_by_horizon(prediction: CrisisPrediction | None) -> list[dict[str, Any]]:
    """The four canonical-horizon probabilities from a real prediction; ``[]`` without one.

    Horizons are never fabricated: only a matching CrisisPrediction carries them, and its
    probabilities stay in [0, 1] exactly as persisted.
    """
    if prediction is None:
        return []
    return [
        {"horizon": horizon, "probability": _json_value(getattr(prediction, column))}
        for horizon, column in _HORIZON_PROBABILITY_FIELDS
    ]


def _string_list(values: Any) -> list[str]:
    """The real non-empty strings in a persisted text array, in order. Never invents entries.

    A null column, a non-list, or a non-string/blank element contributes nothing rather than a
    placeholder -- so ``invalidation_signals``/``what_could_*`` carry only real reduce/escalate
    strings.
    """
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value.strip()]


def _string_labels(entries: Any, keys: Sequence[str]) -> list[str]:
    """Real labels extracted from a persisted driver/analogy array, deduplicated, in order.

    Each entry contributes a label only if it is a non-blank string itself or a mapping carrying
    a non-blank string under one of ``keys`` (first match wins). A malformed or unlabeled entry
    is skipped, never assigned a synthesized name (risk-detail truth rules).
    """
    if not isinstance(entries, list):
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        label: str | None = None
        if isinstance(entry, str) and entry.strip():
            label = entry
        elif isinstance(entry, Mapping):
            for key in keys:
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    label = value
                    break
        if label is not None and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def _group_related_targets(rows: Sequence[tuple[str, str]]) -> dict[str, list[str]]:
    """Fold distinct ``(target_type, target_id)`` rows into per-type, order-preserving id lists.

    The query already emits distinct pairs in a deterministic order; the per-type dedup here is
    defensive so a related-id list is unique even if the same id appears under two types.
    """
    grouped: dict[str, list[str]] = {}
    for target_type, target_id in rows:
        bucket = grouped.setdefault(target_type, [])
        if target_id not in bucket:
            bucket.append(target_id)
    return grouped


def _serialize_risk_detail(
    observation: RiskScoreObservation,
    prediction: CrisisPrediction | None,
    related: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    """Assemble the snake_case RiskDetail from one real observation and its matching prediction.

    The observation is the required spine (score/severity/confidence/as_of/model/target); the
    optional prediction adds the horizon probabilities, full ``model_rating``, drivers, historical
    comparisons and invalidation signals. ``main_drivers`` reads the prediction's ``top_drivers``
    when present, otherwise the observation's own ``driver_refs`` -- both real. ``signals`` and
    ``leading_indicators`` are ``[]``: no persisted row carries the fields a truthful RiskSignal or
    leading indicator needs, and neither is fabricated. Related ids are the real observation targets
    for this risk_type, split by target type; an unsupported relationship is never inferred.
    """
    drivers_source = prediction.top_drivers if prediction is not None else observation.driver_refs
    analogies_source = prediction.historical_analogies if prediction is not None else None
    invalidation_source = prediction.what_could_reduce_risk if prediction is not None else None
    grouped = _group_related_targets(related)
    return {
        "risk_type": observation.risk_type,
        "score": _json_value(observation.score),
        # The persisted band on the latest real observation -- not re-derived from the score.
        "severity": observation.level,
        "confidence_score": _json_value(observation.confidence_score),
        "as_of": _iso(observation.as_of),
        "model_version": observation.model_version,
        "target_type": observation.target_type,
        "target_id": observation.target_id,
        # Full real CrisisRating when a matching prediction exists, else null (never a stub).
        "model_rating": _serialize_crisis_rating(prediction) if prediction is not None else None,
        "probability_by_horizon": _probability_by_horizon(prediction),
        "main_drivers": _string_labels(drivers_source, _DRIVER_LABEL_KEYS),
        # No persisted source carries name+value+status+explanation+source+last_updated_at for a
        # truthful RiskSignal, so this is [] rather than prose/source invented from a driver row.
        "signals": [],
        "historical_comparisons": _string_labels(analogies_source, _ANALOGY_LABEL_KEYS),
        # No persisted leading-indicator source exists; [] rather than a fabricated list.
        "leading_indicators": [],
        "invalidation_signals": _string_list(invalidation_source),
        "related_event_ids": grouped.get(RISK_TARGET_EVENT, []),
        "related_industry_ids": grouped.get(RISK_TARGET_INDUSTRY, []),
        "related_company_ids": grouped.get(RISK_TARGET_COMPANY, []),
    }


def _serialize_daily_summary(
    summary: DailyIntelligenceSummary | None,
    *,
    prediction_backed_outputs_enabled: bool = False,
) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "id": str(summary.id),
        "summary_date": _iso(summary.summary_date),
        "overall_risk_level": summary.overall_risk_level,
        "confidence_score": _json_value(summary.confidence_score),
        "summary": summary.summary,
        "key_points": summary.key_points,
        "model_rating_prediction_id": (
            _json_value(summary.model_rating_prediction_id)
            if prediction_backed_outputs_enabled
            else None
        ),
        "generated_by_run_id": _json_value(summary.generated_by_run_id),
        "created_at": _iso(summary.created_at),
    }


def _serialize_alert(alert: Alert) -> dict[str, Any]:
    return {
        "id": str(alert.id),
        "user_id": str(alert.user_id),
        "alert_rule_id": _json_value(alert.alert_rule_id),
        "title": alert.title,
        "message": alert.message,
        "severity": alert.severity,
        "risk_score": _json_value(alert.risk_score),
        "alert_type": alert.alert_type,
        "related_event_id": _json_value(alert.related_event_id),
        "related_company_id": _json_value(alert.related_company_id),
        "related_industry_id": alert.related_industry_id,
        "evidence_refs": alert.evidence_refs,
        "evidence_signal_ids": [str(value) for value in alert.evidence_signal_ids or []],
        # The wire field `status` carries the ADR 0010 lifecycle state; the adapter maps it
        # down to the UI triad (api-adapter-contract, "Enums").
        "status": alert.state,
        "state": alert.state,
        "dedupe_key": alert.dedupe_key,
        "score_version": alert.score_version,
        "what_could_reduce_risk": alert.what_could_reduce_risk,
        "news_driven": _json_value(alert.news_driven),
        "experimental": alert.experimental,
        "superseded_by": _json_value(alert.superseded_by),
        "created_at": _iso(alert.created_at),
        "updated_at": _iso(alert.updated_at),
        "resolved_at": _iso(alert.resolved_at),
    }


def _serialize_watchlist_item(item: WatchlistItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "user_id": str(item.user_id),
        "item_type": item.item_type,
        "item_id": item.item_id,
        "label": item.label,
        "metadata": item.item_metadata,
        "alert_enabled": item.alert_enabled,
        "created_at": _iso(item.created_at),
    }


_PREDICTION_BACKED_REPORT_SECTION_TITLES = frozenset({"forecasts", "alerts"})


def _prediction_safe_report_sections(
    sections: Sequence[Any],
    *,
    prediction_backed_outputs_enabled: bool,
) -> tuple[Any, ...]:
    """Drop legacy deterministic forecast/alert sections while Gate G is closed."""
    if prediction_backed_outputs_enabled:
        return tuple(sections)
    return tuple(
        section
        for section in sections
        if section.title.strip().casefold() not in _PREDICTION_BACKED_REPORT_SECTION_TITLES
    )


def _serialize_report(
    report: Report,
    sections: list[ReportSection] | None = None,
    *,
    prediction_backed_outputs_enabled: bool = False,
) -> dict[str, Any]:
    payload = {
        "id": str(report.id),
        # The daily brief is global; only user-scoped reports carry an owner.
        "user_id": _json_value(report.user_id),
        "report_type": report.report_type,
        "brief_date": _iso(report.brief_date),
        "event_id": _json_value(report.event_id),
        "title": report.title,
        "status": report.status,
        "version": report.version,
        "change_reason": report.change_reason,
        "stale": report.stale,
        "confidence_score": _json_value(report.confidence_score),
        "generated_by_run_id": _json_value(report.generated_by_run_id),
        "created_at": _iso(report.created_at),
        "updated_at": _iso(report.updated_at),
    }
    if sections is not None:
        visible_sections = _prediction_safe_report_sections(
            sections,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        )
        payload["sections"] = [
            {
                "id": str(section.id),
                "section_order": section.section_order,
                "title": section.title,
                "body": section.body,
                "blocks": section.blocks,
                # Claim-level citations: the drawer resolves these through claim_evidence.
                "evidence_refs": [str(value) for value in section.evidence_refs or []],
                "grounding_status": section.grounding_status,
            }
            for section in visible_sections
        ]
    return payload


def _serialize_brief_snapshot(
    report: ReportSnapshot,
    sections: tuple[ReportSectionSnapshot, ...] | None = None,
    *,
    prediction_backed_outputs_enabled: bool = False,
) -> dict[str, Any]:
    """Serialize a detached daily-brief snapshot from the lifecycle repository.

    The dedicated daily-brief endpoints read through ``ReportLifecycleRepository``, whose reads
    return frozen, session-detached snapshots (bounded, totally ordered, no ORM row). The wire
    shape mirrors :func:`_serialize_report`; ``confidence_score`` is omitted because it is not part
    of the snapshot (the daily brief never sets it). Section order/body/blocks/evidence_refs/
    grounding_status are preserved exactly.
    """
    payload: dict[str, Any] = {
        "id": str(report.id),
        # The daily brief is a global artifact (ADR 0009): it has no owner.
        "user_id": _json_value(report.user_id),
        "report_type": report.report_type,
        "brief_date": _iso(report.brief_date),
        "event_id": _json_value(report.event_id),
        "title": report.title,
        "status": report.status,
        "version": report.version,
        "change_reason": report.change_reason,
        "stale": report.stale,
        "generated_by_run_id": _json_value(report.generated_by_run_id),
        "created_at": _iso(report.created_at),
        "updated_at": _iso(report.updated_at),
    }
    if sections is not None:
        visible_sections = _prediction_safe_report_sections(
            sections,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        )
        payload["sections"] = [
            {
                "id": str(section.id),
                "section_order": section.section_order,
                "title": section.title,
                "body": section.body,
                # A withheld/deterministic section has no blocks -> `[]`, faithfully.
                "blocks": [
                    {"text": block.text, "claim_ids": list(block.claim_ids)}
                    for block in section.blocks
                ],
                "evidence_refs": [str(ref) for ref in section.evidence_refs],
                "grounding_status": section.grounding_status,
            }
            for section in visible_sections
        ]
    return payload


#: The Evidence Drawer resolves one claim's evidence links. A single claim carrying more links than
#: this is an upstream extraction fault, not a citation; the read over-reads by one and fails loud
#: rather than returning a silently truncated list (report-generation spec / requirement 6).
EVIDENCE_LINK_LIMIT = 200


class EvidenceReadOverflowError(RuntimeError):
    """A claim's evidence-link read exceeded :data:`EVIDENCE_LINK_LIMIT`; fail loud, do not truncate."""


@dataclass(frozen=True)
class EvidenceDrawerLink:
    """One resolved ``claim_evidence`` row for the drawer. Detached: never an ORM row or full text.

    ``snippet`` is a bounded (<= :data:`MAX_EXCERPT_CHARS`) excerpt derived deterministically from an
    article's summary (then body); it is present only for article-typed evidence whose article
    resolves. Article full text and ``EvidenceItem.raw_ref``/``metadata`` are never carried.
    """

    evidence_item_id: uuid.UUID
    support_type: str
    support_confidence: Any
    source_type: str
    source_id: str
    title: str
    publisher: str | None
    url: str | None
    published_at: datetime.datetime | None
    credibility: Any
    snippet: str | None
    snippet_origin: str | None
    snippet_truncated: bool


def _serialize_evidence_link(link: EvidenceDrawerLink) -> dict[str, Any]:
    return {
        "evidence_item_id": str(link.evidence_item_id),
        # The real claim_evidence support_type (supports/contradicts/...), never fabricated.
        "support_type": link.support_type,
        "confidence": _json_value(link.support_confidence),
        "source_type": link.source_type,
        "source_id": link.source_id,
        "title": link.title,
        "publisher": link.publisher,
        "url": link.url,
        "published_at": _iso(link.published_at),
        "credibility": _json_value(link.credibility),
        "snippet": link.snippet,
        "snippet_origin": link.snippet_origin,
        "snippet_truncated": link.snippet_truncated,
    }


# --------------------------------------------------------------------------------------
# Dashboard snapshot (api-adapter-contract, shape gap 2): the expanded GET /dashboard adds
# metrics / upcoming_triggers / event_map / company_ranking / industry_summary alongside the
# existing summary / risk_scores / alerts. Every value below is a persisted fact or a
# transparent deterministic derivation (a count, a latest-per-key rollup, the canonical risk
# band, an equirectangular lon/lat projection) -- nothing is fabricated, and missing live data
# yields [] or null rather than a fixture backfill.
# --------------------------------------------------------------------------------------

#: An event counts as "high risk" once its real (item-3) risk clears the High-band floor, i.e.
#: it is High (56-75) or Critical (>75); an event with no persisted score is never counted.
HIGH_RISK_EVENT_THRESHOLD = 55

#: Whole-snapshot MVP bounds (ADR 0007): the dashboard reads the full dataset, but each block is
#: still capped so a data spike cannot produce an unbounded payload or scan.
UPCOMING_TRIGGER_LIMIT = 50
MAP_LOCATION_ROW_LIMIT = 500
COMPANY_RANKING_LIMIT = 100
INDUSTRY_SUMMARY_LIMIT = 100


@dataclass(frozen=True)
class DashboardMetricCounts:
    """The dashboard's real bounded counts, one COUNT read each. Never derived from fixtures."""

    events_today: int
    high_risk_events: int
    affected_industries: int
    affected_companies: int


@dataclass(frozen=True)
class DashboardMapPoint:
    """One aggregated map location: the distinct events at a real coordinate, their strongest
    persisted risk, and the deterministically-dominant event type. ``related_event_ids`` is unique.
    """

    location_name: str
    latitude: Any
    longitude: Any
    event_count: int
    max_risk_score: Any
    dominant_event_type: str | None
    related_event_ids: tuple[str, ...]


def _map_x(longitude: Any) -> float:
    """Deterministic equirectangular projection of longitude to 0..1 (lon -180..180 -> 0..1)."""
    return (float(longitude) + 180.0) / 360.0


def _map_y(latitude: Any) -> float:
    """Deterministic equirectangular projection of latitude to 0..1, north at the top."""
    return (90.0 - float(latitude)) / 180.0


def _dominant_event_type(counts: dict[str, int]) -> str | None:
    """The most frequent real event type at a point; ties broken by type name. None if all null."""
    if not counts:
        return None
    return min(counts.items(), key=lambda item: (-item[1], item[0]))[0]


def _aggregate_map_points(rows: list[Any]) -> list[DashboardMapPoint]:
    """Fold ``(location_name, latitude, longitude, event_id, event_type, risk_score)`` rows into
    map points.

    Grouped by the exact ``(location_name, latitude, longitude)`` triple so a point is a real
    coordinate, never a guessed centroid. Within a point, ``related_event_ids`` are the distinct
    events (deduped, kept in the query's ``event_id`` order), ``max_risk_score`` is the strongest
    non-null persisted risk (NULL only when nothing there is scored), and the dominant type is the
    most frequent real event type. Points are ordered by event_count, then risk, then name.
    """
    grouped: dict[tuple[str, Any, Any], dict[str, Any]] = {}
    for row in rows:
        key = (row.location_name, row.latitude, row.longitude)
        point = grouped.get(key)
        if point is None:
            point = {"event_ids": [], "seen": set(), "max_risk": None, "types": {}}
            grouped[key] = point
        event_id = str(row.event_id)
        if event_id not in point["seen"]:
            point["seen"].add(event_id)
            point["event_ids"].append(event_id)
        if row.risk_score is not None and (
            point["max_risk"] is None or row.risk_score > point["max_risk"]
        ):
            point["max_risk"] = row.risk_score
        if row.event_type is not None:
            point["types"][row.event_type] = point["types"].get(row.event_type, 0) + 1
    points = [
        DashboardMapPoint(
            location_name=key[0],
            latitude=key[1],
            longitude=key[2],
            event_count=len(point["event_ids"]),
            max_risk_score=point["max_risk"],
            dominant_event_type=_dominant_event_type(point["types"]),
            related_event_ids=tuple(point["event_ids"]),
        )
        for key, point in grouped.items()
    ]
    points.sort(
        key=lambda p: (
            -p.event_count,
            -float(p.max_risk_score) if p.max_risk_score is not None else float("inf"),
            p.location_name,
        )
    )
    return points


def _dashboard_metric(
    label: str, value: int | str, *, severity: str | None = None
) -> dict[str, Any]:
    """One DashboardMetric wire object. Optional ``previous_value``/``change`` are omitted rather
    than invented -- no real comparison window is computed -- and ``severity`` is set only when it
    is a persisted risk band (the overall-risk metric), never guessed for a bare count.
    """
    metric: dict[str, Any] = {"label": label, "value": value}
    if severity is not None:
        metric["severity"] = severity
    return metric


def _dashboard_metrics(
    counts: DashboardMetricCounts,
    *,
    open_alerts: int,
    summary: DailyIntelligenceSummary | None,
) -> list[dict[str, Any]]:
    metrics = [
        _dashboard_metric("Events Today", counts.events_today),
        _dashboard_metric("High-Risk Events", counts.high_risk_events),
        _dashboard_metric("Affected Industries", counts.affected_industries),
        _dashboard_metric("Affected Companies", counts.affected_companies),
        # The same live "open" count the alerts block reports (ACTIVE_ALERT_STATES).
        _dashboard_metric("Open Alerts", open_alerts),
    ]
    if summary is not None:
        # The one real severity on the dashboard: the latest daily summary's overall band.
        metrics.append(
            _dashboard_metric(
                "Overall Risk",
                summary.overall_risk_level,
                severity=summary.overall_risk_level,
            )
        )
    return metrics


def _serialize_upcoming_trigger(item: EventTimelineItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "title": item.title,
        "expected_at": _iso(item.timestamp),
        "related_event_id": str(item.event_id),
        # A timeline item persists no company link; the field stays null rather than invented.
        "related_company_id": None,
        "importance": item.importance,
        # The real persisted description is the reason -- never a placeholder.
        "reason": item.description,
    }


def _serialize_event_map_point(point: DashboardMapPoint) -> dict[str, Any]:
    return {
        "location_name": point.location_name,
        "latitude": _json_value(point.latitude),
        "longitude": _json_value(point.longitude),
        "x": _map_x(point.longitude),
        "y": _map_y(point.latitude),
        "event_count": point.event_count,
        "max_risk_score": _json_value(point.max_risk_score),
        "dominant_event_type": point.dominant_event_type,
        "related_event_ids": list(point.related_event_ids),
    }


def _serialize_company_ranking(company: Company, rollup: CompanyRiskRollup) -> dict[str, Any]:
    return {
        "company_id": str(company.id),
        "name": company.display_name,
        "ticker": company.primary_ticker,
        "exchange": company.exchange,
        "industry": company.industry,
        "country": company.country,
        # CompanyRiskRollup persists no direction; null, never guessed from the score sign.
        "impact_direction": None,
        "impact_score": _json_value(rollup.impact_score),
        "risk_score": _json_value(rollup.risk_score),
        "related_event_count": rollup.related_event_count,
        "top_driver": rollup.top_driver,
        "confidence_score": _json_value(rollup.confidence_score),
        "last_updated_at": _iso(rollup.as_of),
    }


def _serialize_industry_summary(rollup: IndustryRiskRollup) -> dict[str, Any]:
    return {
        "industry_id": rollup.industry_id,
        # No industry catalog exists: the persisted industry_id is the display identity.
        "industry_name": rollup.industry_id,
        "impact_score": _json_value(rollup.impact_score),
        "risk_score": _json_value(rollup.risk_score),
        "opportunity_score": _json_value(rollup.opportunity_score),
        "news_velocity_score": _json_value(rollup.news_velocity_score),
        "related_event_count": rollup.related_event_count,
        # IndustryRiskRollup persists no direction; null rather than fabricated.
        "direction": None,
        "summary": rollup.summary,
    }


class IntelligenceRepository:
    """SQLAlchemy-backed read model for frontend-facing intelligence endpoints."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _count(self, filtered: Select[Any]) -> int:
        """COUNT over the full filtered relation, before any LIMIT/OFFSET.

        The page query and this count derive from the *same* filtered ``select``, so their
        predicates are identical by construction (api-adapter-contract: real ``total``).
        """
        return int(
            self.session.execute(select(func.count()).select_from(filtered.subquery())).scalar_one()
        )

    def latest_daily_summary(self) -> DailyIntelligenceSummary | None:
        stmt = (
            select(DailyIntelligenceSummary)
            .order_by(DailyIntelligenceSummary.summary_date.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def count_open_alerts(self) -> int:
        # "Open" means live: everything that has not been resolved or superseded.
        stmt = select(func.count(Alert.id)).where(Alert.state.in_(ACTIVE_ALERT_STATES))
        return int(self.session.execute(stmt).scalar_one())

    def dashboard_metric_counts(
        self,
        *,
        now: datetime.datetime,
        prediction_backed_outputs_enabled: bool = False,
    ) -> DashboardMetricCounts:
        """The dashboard's four real bounded counts, one fixed COUNT read each (no per-row work).

        "Today" is the UTC calendar day of ``now`` (the injected clock seam), bounding the event's
        most recent coverage time (``last_seen_at``, falling back to ``created_at``) with inclusive
        UTC-day boundaries -- the same activity expression the event list filters on. "High-risk"
        reuses the item-3 persisted event risk (:func:`_event_risk_expr`); an event with no real
        score is never counted. Affected industries/companies count the distinct linked entities.
        """
        activity = func.coalesce(Event.last_seen_at, Event.created_at)
        day_start = datetime.datetime.combine(now.date(), datetime.time.min, tzinfo=datetime.UTC)
        day_end = day_start + datetime.timedelta(days=1)
        events_today = int(
            self.session.execute(
                select(func.count(Event.id)).where(activity >= day_start, activity < day_end)
            ).scalar_one()
        )
        risk_score_expr, _ = _event_risk_expr(
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled
        )
        high_risk_events = int(
            self.session.execute(
                select(func.count(Event.id)).where(risk_score_expr > HIGH_RISK_EVENT_THRESHOLD)
            ).scalar_one()
        )
        affected_industries = int(
            self.session.execute(
                select(func.count(EventIndustry.industry_id.distinct()))
            ).scalar_one()
        )
        affected_companies = int(
            self.session.execute(
                select(func.count(EventCompany.company_id.distinct()))
            ).scalar_one()
        )
        return DashboardMetricCounts(
            events_today=events_today,
            high_risk_events=high_risk_events,
            affected_industries=affected_industries,
            affected_companies=affected_companies,
        )

    def upcoming_triggers(self, *, now: datetime.datetime, limit: int) -> list[EventTimelineItem]:
        """Genuinely future timeline entries -- the one persisted forward-looking schedule the data
        model carries. Ordered by ``(timestamp, id)`` and bounded; empty when nothing is scheduled.
        """
        stmt = (
            select(EventTimelineItem)
            .where(EventTimelineItem.timestamp > now)
            .order_by(EventTimelineItem.timestamp, EventTimelineItem.id)
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def event_map(
        self,
        *,
        limit: int,
        prediction_backed_outputs_enabled: bool = False,
    ) -> list[DashboardMapPoint]:
        """Real geocoded event locations aggregated into map points, in one bounded read.

        A single query joins each ``EventLocation`` (with real coordinates) to its ``Event`` and
        carries the item-3 persisted event risk as a correlated column; the rows are folded into
        points in Python (:func:`_aggregate_map_points`), so the query count is constant regardless
        of how many locations or events are on the page. Locations without coordinates cannot be
        placed and are excluded. Bounded by ``limit`` source rows (declared truncation).
        """
        risk_score_expr, _ = _event_risk_expr(
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled
        )
        rows = self.session.execute(
            select(
                EventLocation.location_name,
                EventLocation.latitude,
                EventLocation.longitude,
                Event.id.label("event_id"),
                Event.event_type,
                risk_score_expr.label("risk_score"),
            )
            .join(Event, EventLocation.event_id == Event.id)
            .where(EventLocation.latitude.isnot(None), EventLocation.longitude.isnot(None))
            .order_by(
                EventLocation.location_name,
                EventLocation.latitude,
                EventLocation.longitude,
                Event.id,
            )
            .limit(limit)
        ).all()
        return _aggregate_map_points(list(rows))

    def company_ranking(self, *, limit: int) -> list[tuple[Company, CompanyRiskRollup]]:
        """Each active company joined to its latest risk rollup, in one bounded ``DISTINCT ON`` read.

        A company with no rollup has no ranking row (nothing is fabricated). Ranked by the rollup's
        risk then impact, with ``companies.id`` the unique tie-breaker; bounded by ``limit``.
        """
        latest_sq = (
            select(CompanyRiskRollup)
            .distinct(CompanyRiskRollup.company_id)
            .order_by(CompanyRiskRollup.company_id, CompanyRiskRollup.as_of.desc())
            .subquery()
        )
        latest = aliased(CompanyRiskRollup, latest_sq)
        rows = self.session.execute(
            select(Company, latest)
            .join(latest, latest.company_id == Company.id)
            .where(Company.active.is_(True))
            .order_by(
                latest.risk_score.desc().nullslast(),
                latest.impact_score.desc().nullslast(),
                Company.id,
            )
            .limit(limit)
        ).all()
        return [(company, rollup) for company, rollup in rows]

    def dashboard_industry_summary(self, *, limit: int) -> list[IndustryRiskRollup]:
        """The latest rollup per industry -- the same dedup ``list_industries`` uses (one row per
        industry, newest ``as_of``), ranked by risk then ``industry_id``. One bounded read.
        """
        latest_sq = (
            select(IndustryRiskRollup)
            .distinct(IndustryRiskRollup.industry_id)
            .order_by(IndustryRiskRollup.industry_id, IndustryRiskRollup.as_of.desc())
            .subquery()
        )
        latest = aliased(IndustryRiskRollup, latest_sq)
        rows = (
            self.session.execute(
                select(latest)
                .order_by(latest.risk_score.desc().nullslast(), latest.industry_id)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return list(rows)

    def list_risk_scores(
        self,
        *,
        risk_type: str | None,
        target_type: str | None,
        limit: int,
    ) -> list[RiskScoreObservation]:
        stmt = select(RiskScoreObservation).order_by(RiskScoreObservation.as_of.desc()).limit(limit)
        if risk_type:
            stmt = stmt.where(RiskScoreObservation.risk_type == risk_type)
        if target_type:
            stmt = stmt.where(RiskScoreObservation.target_type == target_type)
        return list(self.session.execute(stmt).scalars().all())

    def list_risk_radar_scores(
        self,
        *,
        target_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[RiskScoreObservation], int]:
        # The consumed `/risk-radar` list. Kept separate from `list_risk_scores` so the
        # dashboard and the `/risk-radar/{risk_type}` detail (a later item's RiskDetail shape)
        # are untouched. `id` is the unique tie-breaker for a stable offset across equal `as_of`.
        base = select(RiskScoreObservation)
        if target_type:
            base = base.where(RiskScoreObservation.target_type == target_type)
        total = self._count(base)
        stmt = (
            base.order_by(RiskScoreObservation.as_of.desc(), RiskScoreObservation.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all()), total

    def latest_risk_observation(self, *, risk_type: str) -> RiskScoreObservation | None:
        """The single newest real observation for a risk_type -- the spine of its RiskDetail.

        ``id`` breaks ties under an equal ``as_of`` so the choice is deterministic. ``None`` when
        the risk_type has no observation at all (the endpoint turns that into a 404 rather than
        substituting zeros or a fixture).
        """
        stmt = (
            select(RiskScoreObservation)
            .where(RiskScoreObservation.risk_type == risk_type)
            .order_by(RiskScoreObservation.as_of.desc(), RiskScoreObservation.id.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def latest_crisis_prediction(
        self, *, risk_type: str, target_type: str | None, target_id: str | None
    ) -> CrisisPrediction | None:
        """The prediction that supplies a RiskDetail's model_rating -- one query, never cross-type.

        The ``risk_type`` predicate is absolute: a prediction of a different family is never
        returned. Among same-risk_type rows, one whose target matches the chosen observation is
        preferred (``match_rank`` first), then the most recent by ``as_of_date`` then ``created_at``
        then ``id``. Encoding the preference as a single ORDER BY keeps this a bounded single read
        rather than a match-then-fallback pair of queries.
        """
        order: list[Any] = [
            CrisisPrediction.as_of_date.desc(),
            CrisisPrediction.created_at.desc(),
            CrisisPrediction.id.desc(),
        ]
        if target_type is not None and target_id is not None:
            match_rank = case(
                (
                    and_(
                        CrisisPrediction.target_type == target_type,
                        CrisisPrediction.target_id == target_id,
                    ),
                    1,
                ),
                else_=0,
            )
            order.insert(0, match_rank.desc())
        stmt = (
            select(CrisisPrediction)
            .where(CrisisPrediction.risk_type == risk_type)
            .order_by(*order)
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def related_risk_targets(self, *, risk_type: str, limit: int) -> list[tuple[str, str]]:
        """Distinct ``(target_type, target_id)`` the risk_type is really observed against, bounded.

        One DISTINCT read -- no per-target follow-up. The RiskDetail related-id lists are derived
        purely from these real observation targets, so no unsupported event/industry/company
        relationship is ever inferred. Deterministically ordered and capped by ``limit`` (declared
        truncation) so a heavily-observed risk_type cannot produce an unbounded scan.
        """
        stmt = (
            select(RiskScoreObservation.target_type, RiskScoreObservation.target_id)
            .where(RiskScoreObservation.risk_type == risk_type)
            .distinct()
            .order_by(RiskScoreObservation.target_type, RiskScoreObservation.target_id)
            .limit(limit)
        )
        return [(row.target_type, row.target_id) for row in self.session.execute(stmt).all()]

    def risk_observation_history(
        self, *, risk_type: str, cutoff: datetime.datetime, limit: int, offset: int
    ) -> tuple[list[RiskScoreObservation], int]:
        """One risk_type's real time series since ``cutoff``: count + page on identical predicates.

        The count and the page both derive from the same ``base`` (risk_type and the ``as_of >=
        cutoff`` window), so ``total`` is the real filtered size before LIMIT/OFFSET. Rows are
        chronological (ascending ``as_of``) with ``id`` as the unique tie-breaker -- the order the
        UI charts consume -- and the page applies the requested LIMIT/OFFSET.
        """
        base = select(RiskScoreObservation).where(
            RiskScoreObservation.risk_type == risk_type,
            RiskScoreObservation.as_of >= cutoff,
        )
        total = self._count(base)
        stmt = (
            base.order_by(RiskScoreObservation.as_of, RiskScoreObservation.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all()), total

    def list_events(
        self,
        *,
        q: str | None = None,
        country: str | None = None,
        event_type: str | None = None,
        date_from: datetime.date | None = None,
        date_to: datetime.date | None = None,
        risk_level: str | None = None,
        industry: str | None = None,
        company: str | None = None,
        status: str | None = None,
        limit: int,
        offset: int,
        prediction_backed_outputs_enabled: bool = False,
    ) -> tuple[list[EventListRow], int]:
        """One expanded event-list page: count + page + a fixed set of bulk attachments.

        The six new filters are folded into one ``base`` select from which the count and page
        both derive, so their predicates are identical by construction. Derived ``risk_score``/
        ``confidence_score``/``status`` are computed as correlated columns on the page (so they
        never multiply the event row and match the filter expressions), while company/industry/
        location identities are attached with a constant three bulk reads keyed by the page's
        event ids -- never one read per event. ``date_from``/``date_to`` bound the event's most
        recent coverage time (``last_seen_at``, falling back to ``created_at``) with inclusive
        UTC day boundaries; the caller validates ``date_from <= date_to``.
        """
        risk_score_expr, confidence_expr = _event_risk_expr(
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled
        )
        status_expr = _event_status_case(composite_alerts_enabled=prediction_backed_outputs_enabled)
        base = select(Event)
        if country:
            base = base.where(func.upper(Event.country) == country.upper())
        if event_type:
            base = base.where(Event.event_type == event_type)
        if q:
            like_q = f"%{q}%"
            base = base.where(or_(Event.title.ilike(like_q), Event.summary.ilike(like_q)))
        if date_from is not None or date_to is not None:
            activity = func.coalesce(Event.last_seen_at, Event.created_at)
            if date_from is not None:
                base = base.where(
                    activity
                    >= datetime.datetime.combine(date_from, datetime.time.min, tzinfo=datetime.UTC)
                )
            if date_to is not None:
                base = base.where(
                    activity
                    < datetime.datetime.combine(date_to, datetime.time.min, tzinfo=datetime.UTC)
                    + datetime.timedelta(days=1)
                )
        if risk_level is not None:
            base = base.where(_risk_level_case(risk_score_expr) == risk_level)
        if status is not None:
            base = base.where(status_expr == status)
        if company:
            base = base.where(_event_company_filter(company))
        if industry:
            base = base.where(_event_industry_filter(industry))
        total = self._count(base)
        page_stmt = (
            base.add_columns(
                risk_score_expr.label("risk_score"),
                confidence_expr.label("confidence_score"),
                status_expr.label("status"),
            )
            .order_by(Event.last_seen_at.desc().nullslast(), Event.created_at.desc(), Event.id)
            .limit(limit)
            .offset(offset)
        )
        page = [(row[0], row[1], row[2], row[3]) for row in self.session.execute(page_stmt).all()]
        event_ids = [event.id for event, *_ in page]
        companies = self._load_event_companies(
            event_ids,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        )
        industries = self._load_event_industries(
            event_ids,
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        )
        locations = self._load_event_locations(event_ids)
        rows = [
            EventListRow(
                event=event,
                risk_score=risk_score,
                confidence_score=confidence,
                status=status_value,
                companies=tuple(companies.get(event.id, ())),
                industries=tuple(industries.get(event.id, ())),
                locations=tuple(locations.get(event.id, ())),
            )
            for event, risk_score, confidence, status_value in page
        ]
        return rows, total

    def _load_event_companies(
        self,
        event_ids: list[uuid.UUID],
        *,
        prediction_backed_outputs_enabled: bool = False,
    ) -> dict[uuid.UUID, list[EventCompanyView]]:
        """Every page event's company links joined to company identity, in one bulk read.

        Empty page -> no ids -> no query at all. Ordered by event, then strongest risk/impact,
        then ``company_id`` as the unique tie-breaker for a stable order within an event.
        """
        if not event_ids:
            return {}
        order: list[Any] = [EventCompany.event_id]
        if prediction_backed_outputs_enabled:
            order.extend(
                [
                    EventCompany.risk_score.desc().nullslast(),
                    EventCompany.impact_score.desc().nullslast(),
                ]
            )
        order.append(EventCompany.company_id)
        rows = self.session.execute(
            select(EventCompany, Company)
            .join(Company, Company.id == EventCompany.company_id)
            .where(EventCompany.event_id.in_(event_ids))
            .order_by(*order)
        ).all()
        grouped: dict[uuid.UUID, list[EventCompanyView]] = {}
        for link, company in rows:
            grouped.setdefault(link.event_id, []).append(_event_company_view(link, company))
        return grouped

    def _load_event_industries(
        self,
        event_ids: list[uuid.UUID],
        *,
        prediction_backed_outputs_enabled: bool = False,
    ) -> dict[uuid.UUID, list[EventIndustry]]:
        if not event_ids:
            return {}
        order: list[Any] = [EventIndustry.event_id]
        if prediction_backed_outputs_enabled:
            order.append(EventIndustry.risk_score.desc().nullslast())
        order.append(EventIndustry.industry_id)
        rows = (
            self.session.execute(
                select(EventIndustry).where(EventIndustry.event_id.in_(event_ids)).order_by(*order)
            )
            .scalars()
            .all()
        )
        grouped: dict[uuid.UUID, list[EventIndustry]] = {}
        for industry in rows:
            grouped.setdefault(industry.event_id, []).append(industry)
        return grouped

    def _load_event_locations(
        self, event_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[EventLocation]]:
        if not event_ids:
            return {}
        rows = (
            self.session.execute(
                select(EventLocation)
                .where(EventLocation.event_id.in_(event_ids))
                .order_by(
                    EventLocation.event_id,
                    EventLocation.confidence_score.desc().nullslast(),
                    EventLocation.id,
                )
            )
            .scalars()
            .all()
        )
        grouped: dict[uuid.UUID, list[EventLocation]] = {}
        for location in rows:
            grouped.setdefault(location.event_id, []).append(location)
        return grouped

    def get_event_detail(
        self,
        event_id: uuid.UUID,
        *,
        prediction_backed_outputs_enabled: bool = False,
    ) -> tuple[
        Event | None,
        list[EventTimelineItem],
        list[EventCompanyView],
        list[EventIndustry],
        list[EventLocation],
    ]:
        event = self.session.execute(select(Event).where(Event.id == event_id)).scalars().first()
        if event is None:
            return None, [], [], [], []
        timeline = list(
            self.session.execute(
                select(EventTimelineItem)
                .where(EventTimelineItem.event_id == event_id)
                .order_by(EventTimelineItem.timestamp)
            )
            .scalars()
            .all()
        )
        # The detail companies embed name/ticker via the same bulk identity join the list uses,
        # so the adapter never issues a per-company UUID lookup.
        companies = self._load_event_companies(
            [event_id],
            prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
        ).get(event_id, [])
        industries = list(
            self.session.execute(select(EventIndustry).where(EventIndustry.event_id == event_id))
            .scalars()
            .all()
        )
        locations = list(
            self.session.execute(select(EventLocation).where(EventLocation.event_id == event_id))
            .scalars()
            .all()
        )
        return event, timeline, companies, industries, locations

    def list_geo_events(
        self, *, country_code: str | None, limit: int, offset: int
    ) -> tuple[list[tuple[EventLocation, Event]], int]:
        base = select(EventLocation, Event).join(Event, EventLocation.event_id == Event.id)
        if country_code:
            base = base.where(func.upper(EventLocation.country_code) == country_code.upper())
        total = self._count(base)
        stmt = (
            base.order_by(
                Event.last_seen_at.desc().nullslast(),
                EventLocation.created_at.desc(),
                EventLocation.id,
            )
            .limit(limit)
            .offset(offset)
        )
        return [(location, event) for location, event in self.session.execute(stmt).all()], total

    def list_companies(
        self,
        *,
        q: str | None,
        sector: str | None,
        country: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Company], int]:
        base = select(Company).where(Company.active.is_(True))
        if sector:
            base = base.where(Company.sector == sector)
        if country:
            base = base.where(func.upper(Company.country) == country.upper())
        if q:
            like_q = f"%{q}%"
            base = base.where(
                or_(
                    Company.display_name.ilike(like_q),
                    Company.legal_name.ilike(like_q),
                    Company.primary_ticker.ilike(like_q),
                )
            )
        total = self._count(base)
        stmt = base.order_by(Company.display_name, Company.id).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all()), total

    def get_company(
        self,
        identifier: str,
        *,
        prediction_backed_outputs_enabled: bool = False,
    ) -> tuple[Company | None, CompanyRiskRollup | None]:
        company_id = _uuid_or_none(identifier)
        if company_id is not None:
            company_stmt = select(Company).where(Company.id == company_id)
        else:
            company_stmt = select(Company).where(
                func.upper(Company.primary_ticker) == identifier.upper()
            )
        company = self.session.execute(company_stmt).scalars().first()
        if company is None:
            return None, None
        if not prediction_backed_outputs_enabled:
            return company, None
        rollup = (
            self.session.execute(
                select(CompanyRiskRollup)
                .where(CompanyRiskRollup.company_id == company.id)
                .order_by(CompanyRiskRollup.as_of.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        return company, rollup

    def list_industries(self, *, limit: int, offset: int) -> tuple[list[IndustryRiskRollup], int]:
        # One row per industry -- its latest rollup. Ordering by `as_of` alone returned the
        # same industry once per snapshot (api-adapter-contract, shape gap 6). The total counts
        # distinct industries (rows of the deduplicated relation), not raw rollup snapshots.
        latest_sq = (
            select(IndustryRiskRollup)
            .distinct(IndustryRiskRollup.industry_id)
            .order_by(IndustryRiskRollup.industry_id, IndustryRiskRollup.as_of.desc())
            .subquery()
        )
        latest = aliased(IndustryRiskRollup, latest_sq)
        total = int(self.session.execute(select(func.count()).select_from(latest_sq)).scalar_one())
        # `industry_id` is unique in the deduplicated set: a stable tie-breaker under equal `as_of`.
        rollups = (
            self.session.execute(
                select(latest)
                .order_by(latest.as_of.desc(), latest.industry_id)
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rollups), total

    def get_industry(self, industry_id: str) -> IndustryRiskRollup | None:
        stmt = (
            select(IndustryRiskRollup)
            .where(IndustryRiskRollup.industry_id == industry_id)
            .order_by(IndustryRiskRollup.as_of.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def list_historical(
        self, *, target_type: str | None, risk_type: str | None, limit: int
    ) -> list[CrisisPrediction]:
        stmt = select(CrisisPrediction).order_by(CrisisPrediction.as_of_date.desc()).limit(limit)
        if target_type:
            stmt = stmt.where(CrisisPrediction.target_type == target_type)
        if risk_type:
            stmt = stmt.where(CrisisPrediction.risk_type == risk_type)
        return list(self.session.execute(stmt).scalars().all())

    def list_alerts(
        self, *, user_id: uuid.UUID | None, status: str | None, limit: int, offset: int
    ) -> tuple[list[Alert], int]:
        # `status` is the wire name for the ADR 0010 lifecycle state.
        base = select(Alert)
        if user_id is not None:
            base = base.where(Alert.user_id == user_id)
        if status:
            base = base.where(Alert.state == status)
        total = self._count(base)
        stmt = base.order_by(Alert.created_at.desc(), Alert.id).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all()), total

    def list_watchlist(
        self, *, user_id: uuid.UUID | None, limit: int, offset: int
    ) -> tuple[list[WatchlistItem], int]:
        base = select(WatchlistItem)
        if user_id is not None:
            base = base.where(WatchlistItem.user_id == user_id)
        total = self._count(base)
        stmt = (
            base.order_by(WatchlistItem.created_at.desc(), WatchlistItem.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all()), total

    def list_reports(
        self,
        *,
        user_id: uuid.UUID | None,
        limit: int,
        prediction_backed_outputs_enabled: bool = False,
    ) -> list[tuple[Report, list[ReportSection]]]:
        stmt = select(Report).order_by(Report.created_at.desc()).limit(limit)
        if user_id is not None:
            stmt = stmt.where(Report.user_id == user_id)
        if not prediction_backed_outputs_enabled:
            stmt = stmt.where(Report.content_policy == REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY)
        # Daily briefs are versioned and global; the default listing serves only the latest
        # PUBLISHED version per brief_date -- never a generating/failed/superseded one
        # (report-generation spec, "Lifecycle and versioning"; the dedicated
        # /reports/daily-brief endpoints expose full history). Every other report type
        # (event/user reports) is returned unchanged. The predicate is a no-op on the
        # user-scoped path: daily briefs are user_id IS NULL, so a user_id filter already
        # excludes them.
        newer_published = aliased(Report)
        has_newer_published = select(newer_published.id).where(
            newer_published.report_type == DAILY_BRIEF_REPORT_TYPE,
            newer_published.brief_date == Report.brief_date,
            newer_published.status == PUBLISHED_STATUS,
            newer_published.version > Report.version,
        )
        if not prediction_backed_outputs_enabled:
            has_newer_published = has_newer_published.where(
                newer_published.content_policy == REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
            )
        has_newer_published = has_newer_published.exists()
        stmt = stmt.where(
            Report.report_type != PERSONAL_REPORT_TYPE,
            or_(
                Report.report_type != DAILY_BRIEF_REPORT_TYPE,
                and_(Report.status == PUBLISHED_STATUS, ~has_newer_published),
            ),
        )
        reports = list(self.session.execute(stmt).scalars().all())
        if not reports:
            return []
        report_ids = [report.id for report in reports]
        sections = list(
            self.session.execute(
                select(ReportSection)
                .where(ReportSection.report_id.in_(report_ids))
                .order_by(ReportSection.report_id, ReportSection.section_order)
            )
            .scalars()
            .all()
        )
        sections_by_report: dict[uuid.UUID, list[ReportSection]] = {
            report.id: [] for report in reports
        }
        for section in sections:
            sections_by_report[section.report_id].append(section)
        return [(report, sections_by_report[report.id]) for report in reports]

    def claim_is_referenced_by_descriptive_report(
        self, claim_id: uuid.UUID, *, descriptive_only: bool = True
    ) -> bool:
        """Whether a published legacy report is allowed to expose ``claim_id``."""

        filters = [
            Report.status == PUBLISHED_STATUS,
            Report.report_type != PERSONAL_REPORT_TYPE,
            claim_id == any_(ReportSection.evidence_refs),
        ]
        if descriptive_only:
            filters.append(Report.content_policy == REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY)
        stmt = (
            select(ReportSection.id)
            .join(Report, Report.id == ReportSection.report_id)
            .where(*filters)
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first() is not None

    def get_claim(self, claim_id: uuid.UUID) -> Claim | None:
        return self.session.get(Claim, claim_id)

    def claim_evidence_links(self, claim_id: uuid.UUID) -> tuple[EvidenceDrawerLink, ...]:
        """Every evidence item linked to a claim, resolved for the Evidence Drawer. Bounded, ordered.

        One deterministic query joins ``claim_evidence -> evidence_items`` and *left*-joins the
        backing ``articles``/``sources`` only for article-typed items (source_type = 'article',
        source_id = the article UUID) -- the same discriminator the composition context uses. The
        article body/summary never cross the boundary whole: only ``left(col, MAX+1)`` and the true
        column length are read, from which a deterministic <= ``MAX_EXCERPT_CHARS`` snippet is
        derived (summary first, then body). ``raw_ref``/``metadata`` are never selected. All
        Legacy support types are returned (supports/contradicts/...). Support pairs created by the
        personal workflow are excluded because their immutable citation surface is report-scoped.
        """
        summary_head = func.left(Article.summary, MAX_EXCERPT_CHARS + 1)
        body_head = func.left(Article.body, MAX_EXCERPT_CHARS + 1)
        personal_owned_evidence = exists(
            select(PersonalClaimPreparation.id).where(
                PersonalClaimPreparation.claim_id == ClaimEvidence.claim_id,
                PersonalClaimPreparation.evidence_item_id == ClaimEvidence.evidence_item_id,
                # CORE-01 creates only a ``supports`` pair. A legacy contradiction or other
                # independently authored relationship may share the same claim/evidence IDs and
                # must remain visible under its own support type.
                ClaimEvidence.support_type == "supports",
                or_(
                    PersonalClaimPreparation.validation["support_created_by_personal"].astext
                    == "true",
                    # Rows made by the first Phase 2 implementation predate the explicit
                    # support-pair marker. A newly-created evidence item could not already have a
                    # legacy ClaimEvidence pair while the single writer fence was held, so this
                    # retained marker is a safe compatibility signal for those rows.
                    PersonalClaimPreparation.validation["evidence_created_by_personal"].astext
                    == "true",
                ),
            )
        )
        stmt = (
            select(
                ClaimEvidence.support_type,
                ClaimEvidence.confidence_score.label("support_confidence"),
                EvidenceItem.id.label("evidence_item_id"),
                EvidenceItem.source_type,
                EvidenceItem.source_id,
                EvidenceItem.title,
                EvidenceItem.publisher,
                EvidenceItem.url,
                EvidenceItem.published_at,
                EvidenceItem.credibility_score,
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
            .where(ClaimEvidence.claim_id == claim_id, ~personal_owned_evidence)
            .order_by(
                EvidenceItem.published_at.desc().nullslast(),
                EvidenceItem.id,
                ClaimEvidence.support_type,
            )
            # Over-read by one so an overflow fails loud rather than truncating silently.
            .limit(EVIDENCE_LINK_LIMIT + 1)
        )
        rows = self.session.execute(stmt).all()
        if len(rows) > EVIDENCE_LINK_LIMIT:
            raise EvidenceReadOverflowError(
                f"claim {claim_id} has more than {EVIDENCE_LINK_LIMIT} evidence links; "
                "refusing to return a truncated list"
            )
        links: list[EvidenceDrawerLink] = []
        for row in rows:
            is_article = (
                row.source_type == ARTICLE_EVIDENCE_SOURCE_TYPE and row.article_title is not None
            )
            snippet: str | None = None
            snippet_origin: str | None = None
            snippet_truncated = False
            if is_article:
                excerpt = build_source_excerpt(
                    row.summary_head,
                    row.body_head,
                    summary_length=row.summary_length,
                    body_length=row.body_length,
                )
                if not excerpt.is_empty:
                    snippet = excerpt.text
                    snippet_origin = excerpt.origin.value
                    snippet_truncated = excerpt.truncated
            links.append(
                EvidenceDrawerLink(
                    evidence_item_id=row.evidence_item_id,
                    support_type=row.support_type,
                    support_confidence=row.support_confidence,
                    source_type=row.source_type,
                    source_id=row.source_id,
                    # For article evidence, prefer the citation snapshot but fall back to the
                    # article's canonical link/publisher when the item did not store them.
                    title=row.title,
                    publisher=row.publisher or (row.source_name if is_article else None),
                    url=row.url or (row.article_url if is_article else None),
                    published_at=row.published_at,
                    credibility=row.credibility_score,
                    snippet=snippet,
                    snippet_origin=snippet_origin,
                    snippet_truncated=snippet_truncated,
                )
            )
        return tuple(links)

    def list_jobs(self, *, state: str | None, limit: int, offset: int) -> tuple[list[Job], int]:
        base = select(Job)
        if state:
            base = base.where(Job.state == state)
        total = self._count(base)
        stmt = base.order_by(Job.created_at.desc(), Job.id).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all()), total

    def list_sources(
        self, *, active: bool | None, limit: int, offset: int
    ) -> tuple[list[tuple[Source, SourceHealthSnapshot | None]], int]:
        """One page of sources, each merged with its own latest health snapshot.

        The latest health for the whole page is read in a single bounded ``DISTINCT ON`` query
        keyed by ``source_id`` -- never one read per source (no N+1). ``total`` is the count of
        sources matching the ``active`` filter, independent of the page size.
        """
        base = select(Source)
        if active is not None:
            base = base.where(Source.active.is_(active))
        total = self._count(base)
        sources = list(
            self.session.execute(base.order_by(Source.name, Source.id).limit(limit).offset(offset))
            .scalars()
            .all()
        )
        if not sources:
            return [], total
        source_ids = [source.id for source in sources]
        latest_health = (
            self.session.execute(
                select(SourceHealthSnapshot)
                .where(SourceHealthSnapshot.source_id.in_(source_ids))
                .order_by(
                    SourceHealthSnapshot.source_id,
                    SourceHealthSnapshot.checked_at.desc(),
                    SourceHealthSnapshot.id.desc(),
                )
                .distinct(SourceHealthSnapshot.source_id)
            )
            .scalars()
            .all()
        )
        health_by_source = {item.source_id: item for item in latest_health}
        return [(source, health_by_source.get(source.id)) for source in sources], total

    def list_model_runs(self, *, limit: int, offset: int) -> tuple[list[LLMRun], int]:
        base = select(LLMRun)
        total = self._count(base)
        stmt = base.order_by(LLMRun.created_at.desc(), LLMRun.id).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars().all()), total


def get_intelligence_repository(
    session: Annotated[Session, Depends(get_session)],
) -> IntelligenceRepository:
    return IntelligenceRepository(session)


RepositoryDep = Annotated[IntelligenceRepository, Depends(get_intelligence_repository)]


def get_crisis_prediction_reads_enabled() -> bool:
    """Return the explicit Gate G switch for public CrisisPrediction reads."""
    return get_settings().crisis_prediction_reads_enabled


CrisisPredictionReadsDep = Annotated[bool, Depends(get_crisis_prediction_reads_enabled)]


def get_report_lifecycle_repository(session: SessionDep) -> ReportLifecycleRepository:
    """The accepted Stage 6 lifecycle read/stale repository over the request's session.

    Its reads return detached, bounded, fail-loud snapshots; its one write here is the
    stale-marking used by ``reprocess-event``. FastAPI caches ``get_session`` within a request,
    so an endpoint taking both ``SessionDep`` and this dependency shares one session (and thus one
    transaction) -- which is what lets ``reprocess-event`` mark stale and commit exactly once.
    """
    return ReportLifecycleRepository(session)


LifecycleRepoDep = Annotated[ReportLifecycleRepository, Depends(get_report_lifecycle_repository)]


def get_report_export_repository(session: SessionDep) -> ReportExportRepository:
    """The Stage 6 export attribution repository over the request's session (read-only).

    Shares ``get_session`` with the lifecycle repository (FastAPI caches it per request), so a
    single export request reads the report snapshot, its sections and its source attribution over
    one session -- and writes nothing.
    """
    return ReportExportRepository(session)


ExportRepoDep = Annotated[ReportExportRepository, Depends(get_report_export_repository)]


@dataclass(frozen=True)
class QueuedBrief:
    """The identity of a daily-brief generation task handed to the broker."""

    task_id: str
    task_name: str
    queue: str


def enqueue_generate_daily_brief(
    brief_date: datetime.date, *, task_id: uuid.UUID, legacy_job_id: uuid.UUID
) -> QueuedBrief:
    """Enqueue the accepted ``generate_daily_brief`` Celery task (ADR 0009) by name, asynchronously.

    Opens no database and no Redis and never calls the coordinator: after the API has committed a
    durable queue row, it hands the canonical
    ``YYYY-MM-DD`` to the broker on ``QUEUE_PIPELINE`` and returns the task identity. The
    coordinator (run by the worker) allocates a *new* version through its existing lifecycle
    semantics; this never mutates or reuses a prior report. Imported lazily so importing this
    module opens no broker connection and so the enqueue seam is monkeypatchable in tests.
    """
    from workers.celery_app import QUEUE_PIPELINE, celery_app
    from workers.report_tasks import TASK_NAME

    async_result = celery_app.send_task(
        TASK_NAME,
        args=[brief_date.isoformat(), str(legacy_job_id)],
        queue=QUEUE_PIPELINE,
        task_id=str(task_id),
    )
    return QueuedBrief(task_id=str(async_result.id), task_name=TASK_NAME, queue=QUEUE_PIPELINE)


def get_brief_enqueuer() -> Callable[..., QueuedBrief]:
    return enqueue_generate_daily_brief


BriefEnqueuerDep = Annotated[Callable[..., QueuedBrief], Depends(get_brief_enqueuer)]


@router.get("/api/v1/dashboard")
def get_dashboard(
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    """The whole-snapshot dashboard (api-adapter-contract, shape gap 2).

    Retains ``summary``/``risk_scores``/``alerts`` and adds the five fixture-compatible blocks
    ``metrics``/``upcoming_triggers``/``event_map``/``company_ranking``/``industry_summary``. Every
    value is persisted or a transparent derivation; missing live data is ``[]`` or ``null``, never a
    fixture backfill. ``now`` (the monkeypatchable ``_utc_now`` seam) fixes UTC "today"/"future" so
    the metric and trigger reads are deterministic under test. The reads are a fixed, bounded set --
    no query multiplies with data size.
    """
    now = _utc_now()
    # DailyIntelligenceSummary predates the durable report content-policy marker. Even a
    # row with no prediction FK may contain model-generated predictive prose, so closed Gate G
    # neither reads nor serializes it.
    visible_summary = repo.latest_daily_summary() if crisis_prediction_reads_enabled else None
    open_alerts = repo.count_open_alerts() if crisis_prediction_reads_enabled else 0
    counts = repo.dashboard_metric_counts(
        now=now,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )
    triggers = repo.upcoming_triggers(now=now, limit=UPCOMING_TRIGGER_LIMIT)
    map_points = repo.event_map(
        limit=MAP_LOCATION_ROW_LIMIT,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )
    ranking = (
        repo.company_ranking(limit=COMPANY_RANKING_LIMIT) if crisis_prediction_reads_enabled else []
    )
    industry_summary = (
        repo.dashboard_industry_summary(limit=INDUSTRY_SUMMARY_LIMIT)
        if crisis_prediction_reads_enabled
        else []
    )
    return {
        "summary": _serialize_daily_summary(
            visible_summary,
            prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
        ),
        "risk_scores": [
            _serialize_risk_score(score)
            for score in repo.list_risk_scores(risk_type=None, target_type=None, limit=12)
        ],
        "alerts": {"open_count": open_alerts},
        "metrics": _dashboard_metrics(
            counts,
            open_alerts=open_alerts,
            summary=visible_summary,
        ),
        "upcoming_triggers": [_serialize_upcoming_trigger(item) for item in triggers],
        "event_map": [_serialize_event_map_point(point) for point in map_points],
        "company_ranking": [
            _serialize_company_ranking(company, rollup) for company, rollup in ranking
        ],
        "industry_summary": [_serialize_industry_summary(rollup) for rollup in industry_summary],
    }


@router.get("/api/v1/events")
def list_events(
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
    q: str | None = None,
    country: str | None = None,
    event_type: str | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    risk_level: RiskLevel | None = None,
    industry: str | None = None,
    company: str | None = None,
    status: EventStatus | None = None,
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """The expanded event-list contract (api-adapter-contract, shape gap 1).

    ``risk_level``/``status`` are typed enums, so an unknown value is a 422 (not a silent empty
    page); ``date_from``/``date_to`` are UTC-day bounds on the event's coverage time and an
    inverted range is a 422. Every filter narrows the real ``total``. Each item carries the
    derived risk/confidence/status and embedded company/industry/location identities.
    """
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    rows, total = repo.list_events(
        q=q,
        country=country,
        event_type=event_type,
        date_from=date_from,
        date_to=date_to,
        risk_level=risk_level.value if risk_level is not None else None,
        industry=industry,
        company=company,
        status=status.value if status is not None else None,
        limit=limit,
        offset=offset,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )
    return _page(
        [
            _serialize_event_list_item(
                row,
                prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/events/{event_id}")
def get_event(
    event_id: uuid.UUID,
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    event, timeline, companies, industries, locations = repo.get_event_detail(
        event_id,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return {
        "event": _serialize_event(event),
        "timeline": [
            {
                "id": str(item.id),
                "timestamp": _iso(item.timestamp),
                "title": item.title,
                "description": item.description,
                "importance": item.importance,
                "evidence_refs": item.evidence_refs,
            }
            for item in timeline
        ],
        # Company identity (name/ticker) is embedded, so the adapter reads no bare UUIDs.
        "companies": [
            _serialize_event_company(
                company,
                prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
            )
            for company in companies
        ],
        "industries": [
            _serialize_event_industry(
                industry,
                prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
            )
            for industry in industries
        ],
        "locations": [_serialize_event_location(location) for location in locations],
    }


@router.get("/api/v1/geo/events")
def list_geo_events(
    repo: RepositoryDep,
    country_code: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows, total = repo.list_geo_events(country_code=country_code, limit=limit, offset=offset)
    return _page(
        [_serialize_event_location(location, event) for location, event in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/risk-radar")
def list_risk_radar(
    repo: RepositoryDep,
    target_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    scores, total = repo.list_risk_radar_scores(target_type=target_type, limit=limit, offset=offset)
    return _page(
        [_serialize_risk_score(score) for score in scores], total=total, limit=limit, offset=offset
    )


# `/{risk_type}/history` is declared before `/{risk_type}` so the more specific path wins; a
# str `{risk_type}` cannot in any case absorb the extra `/history` segment, but the order keeps
# the intent explicit (mirrors the daily-brief `/latest` vs `/{brief_date}` ordering above).
@router.get("/api/v1/risk-radar/{risk_type}/history")
def get_risk_radar_history(
    risk_type: str,
    repo: RepositoryDep,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """One risk_type's real RiskScoreObservation time series (api-adapter-contract, shape gap 3).

    ``days`` is a bounded 1..365 look-back (an out-of-range value is a 422, never silently
    clamped); ``_utc_now`` is the fixed UTC seam so the ``as_of >= now - days`` cutoff is
    deterministic under test. The consumed-list envelope carries the real filtered ``total`` (the
    count shares the page's risk_type + cutoff predicates), and rows are chronological UTC-``Z``.
    """
    cutoff = _utc_now() - datetime.timedelta(days=days)
    observations, total = repo.risk_observation_history(
        risk_type=risk_type, cutoff=cutoff, limit=limit, offset=offset
    )
    return _page(
        [_serialize_risk_score(observation) for observation in observations],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/risk-radar/{risk_type}")
def get_risk_radar_detail(
    risk_type: str,
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    """The RiskDetail for one risk_type (api-adapter-contract, shape gap 5), not raw observations.

    The latest real observation is the required spine: without one this is a 404 (no fixture/zero
    substitute). While Gate G is closed, no CrisisPrediction query runs and prediction-backed
    fields stay ``[]``/``null``; the observation still supplies the descriptive score, severity,
    metadata, and drivers. When explicitly enabled, a matching prediction (same risk_type, target
    preferred) adds horizon probabilities, full ``model_rating`` and
    drivers/analogies/invalidation signals.
    """
    observation = repo.latest_risk_observation(risk_type=risk_type)
    if observation is None:
        raise HTTPException(status_code=404, detail="no risk observation for that risk type")
    prediction: CrisisPrediction | None = None
    if crisis_prediction_reads_enabled:
        prediction = repo.latest_crisis_prediction(
            risk_type=risk_type,
            target_type=observation.target_type,
            target_id=observation.target_id,
        )
    related = repo.related_risk_targets(risk_type=risk_type, limit=RISK_DETAIL_RELATED_TARGET_LIMIT)
    return {"risk": _serialize_risk_detail(observation, prediction, related)}


@router.get("/api/v1/industries")
def list_industries(
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if not crisis_prediction_reads_enabled:
        return _page([], total=0, limit=limit, offset=offset)
    rollups, total = repo.list_industries(limit=limit, offset=offset)
    return _page(
        [_serialize_industry_rollup(rollup) for rollup in rollups],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/industries/{industry_id}")
def get_industry(
    industry_id: str,
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    if not crisis_prediction_reads_enabled:
        raise HTTPException(status_code=404, detail="industry not found")
    rollup = repo.get_industry(industry_id)
    if rollup is None:
        raise HTTPException(status_code=404, detail="industry not found")
    return {"industry": _serialize_industry_rollup(rollup)}


@router.get("/api/v1/companies")
def list_companies(
    repo: RepositoryDep,
    q: str | None = None,
    sector: str | None = None,
    country: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    companies, total = repo.list_companies(
        q=q, sector=sector, country=country, limit=limit, offset=offset
    )
    return _page(
        [_serialize_company(company) for company in companies],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/companies/{company_id}")
def get_company(
    company_id: str,
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    company, rollup = repo.get_company(
        company_id,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")
    return {"company": _serialize_company(company, rollup)}


@router.get("/api/v1/historical")
def list_historical(
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
    target_type: str | None = None,
    risk_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    """List prediction history only while the explicit Gate G read switch is open."""
    if not crisis_prediction_reads_enabled:
        return {"items": [], "count": 0}
    predictions = repo.list_historical(target_type=target_type, risk_type=risk_type, limit=limit)
    return {
        "items": [
            {
                "id": str(prediction.id),
                "target_type": prediction.target_type,
                "target_id": prediction.target_id,
                "risk_type": prediction.risk_type,
                "as_of_date": _iso(prediction.as_of_date),
                "risk_score": _json_value(prediction.risk_score),
                "risk_level": prediction.risk_level,
                "confidence_score": _json_value(prediction.confidence_score),
                "top_drivers": prediction.top_drivers,
                "evidence_refs": prediction.evidence_refs,
            }
            for prediction in predictions
        ],
        "count": len(predictions),
    }


@router.get("/api/v1/alerts")
def list_alerts(
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
    user_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if not crisis_prediction_reads_enabled:
        return _page([], total=0, limit=limit, offset=offset)
    alerts, total = repo.list_alerts(
        user_id=_uuid_or_none(user_id), status=status, limit=limit, offset=offset
    )
    return _page(
        [_serialize_alert(alert) for alert in alerts], total=total, limit=limit, offset=offset
    )


class SupersedeAlertRequest(BaseModel):
    """Body for `POST /api/v1/alerts/{alert_id}/supersede`.

    The path's ``alert_id`` is always one of the narrower alerts being replaced; this body
    names the broader alert absorbing it and, optionally, further narrower alerts to fold
    into the same replacement in one call.
    """

    broader_alert_id: str = Field(min_length=1)
    additional_narrower_alert_ids: list[str] = Field(default_factory=list)


def _require_alert_for_write(session: Session, alert_id: uuid.UUID) -> None:
    """Raise 404 up front for a plainly-unknown alert, before touching the service.

    Not strictly required -- the service raises ``LookupError`` for the same case -- but it
    keeps the 404 path a single, obvious read rather than relying on exception-message
    sniffing for the common case.
    """
    if session.get(Alert, alert_id) is None:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")


@router.post("/api/v1/alerts/{alert_id}/acknowledge")
def acknowledge_alert(
    alert_id: uuid.UUID,
    session: SessionDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    """Record that a notification for this alert was actually delivered (ADR 0010).

    Escalations re-notify by design, so this always records the *latest* delivery -- there is
    no terminal-state conflict here, only "does this alert exist".
    """
    if not crisis_prediction_reads_enabled:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
    try:
        require_legacy_writer_mode(session, lock=True)
    except LegacyWriterModeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _require_alert_for_write(session, alert_id)
    service = AlertLifecycleService(SQLAlchemyAlertRepository(session))
    try:
        service.acknowledge_notification(alert_id, at=_utc_now())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    alert = session.get(Alert, alert_id)
    session.commit()
    return {"alert": _serialize_alert(alert)}


@router.post("/api/v1/alerts/{alert_id}/acknowledge-all-clear")
def acknowledge_alert_all_clear(
    alert_id: uuid.UUID,
    session: SessionDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    """Record that the all-clear for this alert was delivered (ADR 0010).

    An all-clear is sent once and only for a *resolved* alert: acknowledging one on a live
    (open/escalated/downgraded) or already-superseded alert is a 409, not a 404 -- the alert
    exists, it is simply not in the one state this action is valid for. Re-acknowledging an
    already-acknowledged resolution is not an error: it is reported back as a no-op so a
    retried request is idempotent.
    """
    if not crisis_prediction_reads_enabled:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
    try:
        require_legacy_writer_mode(session, lock=True)
    except LegacyWriterModeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _require_alert_for_write(session, alert_id)
    service = AlertLifecycleService(SQLAlchemyAlertRepository(session))
    try:
        acknowledged = service.acknowledge_all_clear(alert_id, at=_utc_now())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    alert = session.get(Alert, alert_id)
    session.commit()
    return {"alert": _serialize_alert(alert), "acknowledged": acknowledged}


@router.post("/api/v1/alerts/{alert_id}/supersede")
def supersede_alert(
    alert_id: uuid.UUID,
    request: SupersedeAlertRequest,
    session: SessionDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    """Replace this alert (and any additional narrower alerts named) with a broader one.

    Wraps `services.alerts.supersession.supersede_with_broader_alert`. Its
    ``SupersessionError`` covers several distinct failures that this endpoint splits by HTTP
    status: an unknown broader or narrower alert id is a 404 (nothing to act on); a malformed
    id, a terminal (already resolved/superseded) alert, an alert owned by a different user, or
    a narrower alert that is actually the broader alert's own dedupe key are all 409s -- the
    named alerts exist, but the replacement as asked for is not a valid one.
    """
    if not crisis_prediction_reads_enabled:
        raise HTTPException(status_code=404, detail=f"alert {alert_id} not found")
    try:
        require_legacy_writer_mode(session, lock=True)
    except LegacyWriterModeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    narrower_ids = [str(alert_id), *request.additional_narrower_alert_ids]
    repository = SQLAlchemyAlertRepository(session)
    try:
        result = supersede_with_broader_alert(
            repository,
            broader_alert_id=request.broader_alert_id,
            narrower_alert_ids=narrower_ids,
            now=_utc_now(),
        )
    except SupersessionError as exc:
        message = str(exc)
        status_code = 404 if message.startswith("unknown alert") else 409
        raise HTTPException(status_code=status_code, detail=message) from exc
    broader = session.get(Alert, result.replacement_id)
    session.commit()
    return {
        "alert": _serialize_alert(broader),
        "superseded_ids": [str(value) for value in result.superseded_ids],
    }


@router.get("/api/v1/watchlist")
def list_watchlist(
    repo: RepositoryDep,
    user_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    items, total = repo.list_watchlist(user_id=_uuid_or_none(user_id), limit=limit, offset=offset)
    return _page(
        [_serialize_watchlist_item(item) for item in items], total=total, limit=limit, offset=offset
    )


@router.get("/api/v1/reports")
def list_reports(
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
    user_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    rows = repo.list_reports(
        user_id=_uuid_or_none(user_id),
        limit=limit,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )
    if not crisis_prediction_reads_enabled:
        # Defense in depth for alternate repository implementations: a closed endpoint never
        # trusts section names or prose and accepts only the durable descriptive policy.
        rows = [
            (report, sections)
            for report, sections in rows
            if getattr(report, "content_policy", None) == REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
        ]
    rows = [
        (report, sections)
        for report, sections in rows
        if report.report_type != PERSONAL_REPORT_TYPE
    ]
    return {
        "items": [
            _serialize_report(
                report,
                sections,
                prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
            )
            for report, sections in rows
        ],
        "count": len(rows),
    }


# --------------------------------------------------------------------------------------
# Daily brief (ADR 0009): the latest PUBLISHED version is served by default; prior versions
# -- including generating/failed ones, with their status/stale/change_reason -- are explicit.
# `/latest` is declared before `/{brief_date}` so the literal wins over the typed date param.
# --------------------------------------------------------------------------------------


def _brief_with_sections(
    repo: ReportLifecycleRepository,
    report: ReportSnapshot,
    *,
    prediction_backed_outputs_enabled: bool,
) -> dict[str, Any]:
    if (
        not prediction_backed_outputs_enabled
        and report.content_policy != REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
    ):
        raise HTTPException(status_code=404, detail="no descriptive-only daily brief")
    return _serialize_brief_snapshot(
        report,
        repo.report_sections(report.id),
        prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
    )


@router.get("/api/v1/reports/daily-brief/latest")
def get_daily_brief_latest(
    repo: LifecycleRepoDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    report = repo.latest_published_daily_brief(
        content_policy=(
            None if crisis_prediction_reads_enabled else REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
        )
    )
    if report is None:
        raise HTTPException(status_code=404, detail="no published daily brief")
    return {
        "report": _brief_with_sections(
            repo,
            report,
            prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
        )
    }


@router.get("/api/v1/reports/daily-brief/{brief_date}")
def get_daily_brief_by_date(
    brief_date: datetime.date,
    repo: LifecycleRepoDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    report = repo.latest_published_daily_brief_by_date(
        brief_date,
        content_policy=(
            None if crisis_prediction_reads_enabled else REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
        ),
    )
    if report is None:
        raise HTTPException(status_code=404, detail="no published daily brief for that date")
    return {
        "report": _brief_with_sections(
            repo,
            report,
            prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
        )
    }


@router.get("/api/v1/reports/daily-brief/{brief_date}/versions")
def list_daily_brief_versions(
    brief_date: datetime.date,
    repo: LifecycleRepoDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    versions = repo.daily_brief_versions(
        brief_date,
        content_policy=(
            None if crisis_prediction_reads_enabled else REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
        ),
    )
    if not versions:
        raise HTTPException(status_code=404, detail="no daily brief for that date")
    # Newest first, bounded (the repository fails loud past its version bound). Metadata only:
    # each entry carries status/stale/change_reason so a prior version is fully inspectable.
    return {
        "items": [
            _serialize_brief_snapshot(
                report,
                prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
            )
            for report in versions
        ],
        "count": len(versions),
    }


@router.get("/api/v1/reports/daily-brief/{brief_date}/versions/{version}")
def get_daily_brief_version(
    brief_date: datetime.date,
    version: Annotated[int, Path(ge=1)],
    repo: LifecycleRepoDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    report = repo.daily_brief_version(
        brief_date,
        version,
        content_policy=(
            None if crisis_prediction_reads_enabled else REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
        ),
    )
    if report is None:
        raise HTTPException(status_code=404, detail="no such daily brief version")
    return {
        "report": _brief_with_sections(
            repo,
            report,
            prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
        )
    }


# --------------------------------------------------------------------------------------
# Daily brief export (report-generation spec, S23.2): deterministic Markdown/PDF of a
# *published* brief, with per-source attribution before the fixed disclaimer. Only published
# reports may be exported: `latest` is the latest PUBLISHED version for a date; a specific
# version exports only if that version is published (a failed/generating version is a 404).
# Paths carry a `.md`/`.pdf` suffix -- a distinct literal segment that cannot shadow, and is
# never shadowed by, the typed `{brief_date}`/`{version}` metadata routes above.
# --------------------------------------------------------------------------------------

#: Media types (RFC 7763 for Markdown); UTF-8 so the em dash in every brief title survives.
_MARKDOWN_MEDIA_TYPE = "text/markdown; charset=utf-8"
_PDF_MEDIA_TYPE = "application/pdf"


def _published_or_404(report: ReportSnapshot | None) -> ReportSnapshot:
    """Return a report only if it exists and is published; otherwise 404. Never exports a draft.

    Guards the specific-version path in particular: ``daily_brief_version`` returns any status, so
    a failed or still-generating version -- inspectable via the metadata endpoints -- must not be
    exportable and is refused here.
    """
    if report is None or report.status != PUBLISHED_STATUS:
        raise HTTPException(status_code=404, detail="no published daily brief to export")
    return report


def _export_response(
    lifecycle_repo: ReportLifecycleRepository,
    export_repo: ReportExportRepository,
    report: ReportSnapshot,
    *,
    pdf: bool,
    prediction_backed_outputs_enabled: bool,
) -> Response:
    """Render a published brief to Markdown or PDF with a safe, deterministic download filename.

    Reads only: the ordered sections, the distinct cited claims, and one bulk attribution query.
    """
    if (
        not prediction_backed_outputs_enabled
        and report.content_policy != REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
    ):
        raise HTTPException(status_code=404, detail="no descriptive-only daily brief to export")
    sections = _prediction_safe_report_sections(
        lifecycle_repo.report_sections(report.id),
        prediction_backed_outputs_enabled=prediction_backed_outputs_enabled,
    )
    attributions = export_repo.source_attributions_for_report(collect_claim_refs(sections))
    if pdf:
        content: bytes = render_pdf(report, sections, attributions)
        media_type, filename = _PDF_MEDIA_TYPE, export_filename(report, "pdf")
    else:
        content = render_markdown(report, sections, attributions).encode("utf-8")
        media_type, filename = _MARKDOWN_MEDIA_TYPE, export_filename(report, "md")
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/v1/reports/daily-brief/{brief_date}/export.md")
def export_daily_brief_latest_markdown(
    brief_date: datetime.date,
    repo: LifecycleRepoDep,
    export_repo: ExportRepoDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> Response:
    report = _published_or_404(
        repo.latest_published_daily_brief_by_date(
            brief_date,
            content_policy=(
                None if crisis_prediction_reads_enabled else REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
            ),
        )
    )
    return _export_response(
        repo,
        export_repo,
        report,
        pdf=False,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )


@router.get("/api/v1/reports/daily-brief/{brief_date}/export.pdf")
def export_daily_brief_latest_pdf(
    brief_date: datetime.date,
    repo: LifecycleRepoDep,
    export_repo: ExportRepoDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> Response:
    report = _published_or_404(
        repo.latest_published_daily_brief_by_date(
            brief_date,
            content_policy=(
                None if crisis_prediction_reads_enabled else REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
            ),
        )
    )
    return _export_response(
        repo,
        export_repo,
        report,
        pdf=True,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )


@router.get("/api/v1/reports/daily-brief/{brief_date}/versions/{version}/export.md")
def export_daily_brief_version_markdown(
    brief_date: datetime.date,
    version: Annotated[int, Path(ge=1)],
    repo: LifecycleRepoDep,
    export_repo: ExportRepoDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> Response:
    report = _published_or_404(
        repo.daily_brief_version(
            brief_date,
            version,
            content_policy=(
                None if crisis_prediction_reads_enabled else REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
            ),
        )
    )
    return _export_response(
        repo,
        export_repo,
        report,
        pdf=False,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )


@router.get("/api/v1/reports/daily-brief/{brief_date}/versions/{version}/export.pdf")
def export_daily_brief_version_pdf(
    brief_date: datetime.date,
    version: Annotated[int, Path(ge=1)],
    repo: LifecycleRepoDep,
    export_repo: ExportRepoDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> Response:
    report = _published_or_404(
        repo.daily_brief_version(
            brief_date,
            version,
            content_policy=(
                None if crisis_prediction_reads_enabled else REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
            ),
        )
    )
    return _export_response(
        repo,
        export_repo,
        report,
        pdf=True,
        prediction_backed_outputs_enabled=crisis_prediction_reads_enabled,
    )


@router.get("/api/v1/evidence/{claim_id}")
def get_evidence(
    claim_id: uuid.UUID,
    repo: RepositoryDep,
    crisis_prediction_reads_enabled: CrisisPredictionReadsDep,
) -> dict[str, Any]:
    """Resolve one claim id (from ``report_sections.evidence_refs``) to its evidence for the drawer.

    A malformed id is rejected at routing (422 via the typed ``uuid.UUID`` path param); an unknown
    claim is 404. Evidence is a bounded, deterministic list carrying the real support_type and, for
    article evidence, a <= 200-char snippet plus canonical link/title/source attribution -- never
    article full text or raw payload.
    """
    if not crisis_prediction_reads_enabled and not repo.claim_is_referenced_by_descriptive_report(
        claim_id
    ):
        # Unknown and legacy-only IDs share the same response so this check cannot be used
        # to enumerate claims from prediction-backed reports.
        raise HTTPException(status_code=404, detail="claim not found")
    claim = repo.get_claim(claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="claim not found")
    links = repo.claim_evidence_links(claim_id)
    if crisis_prediction_reads_enabled and not links:
        # Gate G may expose standalone legacy claims, but a claim whose only support was prepared
        # by the personal workflow remains inside the report-scoped personal citation namespace.
        raise HTTPException(status_code=404, detail="claim not found")
    return {
        "claim": {
            "id": str(claim.id),
            "text": claim.claim_text,
            "type": claim.claim_type,
            "confidence": _json_value(claim.confidence_score),
        },
        "evidence": [_serialize_evidence_link(link) for link in links],
    }


@router.get("/api/v1/admin/jobs")
def list_admin_jobs(
    repo: RepositoryDep,
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    jobs, total = repo.list_jobs(state=state, limit=limit, offset=offset)
    return _page(
        [
            {
                "id": str(job.id),
                "job_key": job.job_key,
                "job_type": job.job_type,
                "state": job.state,
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
                "safe_to_rerun": job.safe_to_rerun,
                "created_at": _iso(job.created_at),
                "updated_at": _iso(job.updated_at),
            }
            for job in jobs
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


def _serialize_source_health(item: SourceHealthSnapshot) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "source_id": _json_value(item.source_id),
        "provider": item.provider,
        "checked_at": _iso(item.checked_at),
        "status": item.status,
        "latency_ms": item.latency_ms,
        "error_rate": _json_value(item.error_rate),
        "items_fetched": item.items_fetched,
    }


@router.get("/api/v1/admin/sources")
def list_admin_sources(
    repo: RepositoryDep,
    active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    # Each paginated item carries the source plus its own latest health snapshot (or null),
    # so the adapter can map an item straight to `SourceStatus` without a second call.
    rows, total = repo.list_sources(active=active, limit=limit, offset=offset)
    return _page(
        [
            {
                "id": str(source.id),
                "name": source.name,
                "source_type": source.source_type,
                "feed_url": source.feed_url,
                "homepage_url": source.homepage_url,
                "active": source.active,
                "created_at": _iso(source.created_at),
                "updated_at": _iso(source.updated_at),
                "latest_health": _serialize_source_health(health) if health is not None else None,
            }
            for source, health in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/admin/models")
def list_admin_models(
    repo: RepositoryDep,
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    runs, total = repo.list_model_runs(limit=limit, offset=offset)
    return _page(
        [
            {
                "id": str(run.id),
                "prompt_name": run.prompt_name,
                "prompt_version": run.prompt_version,
                "provider": run.provider,
                "model": run.model,
                "output_schema_version": run.output_schema_version,
                "cost_usd": _json_value(run.cost_usd),
                "latency_ms": run.latency_ms,
                "created_at": _iso(run.created_at),
            }
            for run in runs
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


class GenerateDailyBriefRequest(BaseModel):
    """Body for ``POST /api/v1/internal/jobs/generate-daily-brief`` (ADR 0009 manual regeneration).

    ``brief_date`` is required and explicit -- a manual regeneration always names the calendar date
    it is for; an unparseable date is a 422 from the typed field. The ET-cutoff default is the
    scheduled beat's job, not this endpoint's.
    """

    brief_date: datetime.date


@router.post("/api/v1/internal/jobs/generate-daily-brief", status_code=202)
def generate_daily_brief_job(
    request: GenerateDailyBriefRequest, session: SessionDep, enqueue: BriefEnqueuerDep
) -> dict[str, Any]:
    """Queue the accepted ``generate_daily_brief`` task for one ``brief_date``; return 202 (ADR 0009).

    Asynchronous only: it commits a durable queued legacy job before publishing that exact identity
    to the pipeline queue, then returns the task identity. It opens no Redis and never runs the
    coordinator inline. The queued row prevents personal-mode activation during broker handoff;
    the worker owns generation and mints a new version through the existing lifecycle semantics.
    """
    task_id = uuid.uuid4()
    try:
        job = queue_legacy_daily_brief(session, brief_date=request.brief_date, task_id=task_id)
    except LegacyWriterModeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        queued = enqueue(request.brief_date, task_id=task_id, legacy_job_id=job.id)
    except Exception as exc:
        mark_legacy_daily_brief_delivery_failed(session, job.id)
        raise HTTPException(
            status_code=503,
            detail="daily brief job could not be delivered to the worker",
        ) from exc
    return {
        "status": "queued",
        "task_id": queued.task_id,
        "task_name": queued.task_name,
        "queue": queued.queue,
        "brief_date": request.brief_date.isoformat(),
        "job_id": str(job.id),
    }


@router.post("/api/v1/admin/reprocess-event/{event_id}")
def reprocess_event(
    event_id: uuid.UUID, session: SessionDep, repo: LifecycleRepoDep
) -> dict[str, Any]:
    """Mark every published report that depends on an event stale (spec: reprocess never mutates).

    404 if the event does not exist. Otherwise this only flips ``stale`` on dependent *published*
    reports -- it never changes their content, status, or version, and does not touch Stage 4
    observations. The stale-marking and the single commit share the request's session (``repo``
    wraps the same ``session``), so it commits exactly once.
    """
    if session.get(Event, event_id) is None:
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")
    try:
        require_legacy_maintenance_mode(session)
    except LegacyWriterModeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    count = repo.mark_published_reports_stale_for_event(event_id)
    session.commit()
    return {"event_id": str(event_id), "dependent_published_report_count": count}


__all__ = [
    "DashboardMapPoint",
    "DashboardMetricCounts",
    "EventCompanyView",
    "EventListRow",
    "EventStatus",
    "EvidenceDrawerLink",
    "IntelligenceRepository",
    "QueuedBrief",
    "enqueue_generate_daily_brief",
    "get_brief_enqueuer",
    "get_crisis_prediction_reads_enabled",
    "get_intelligence_repository",
    "get_report_export_repository",
    "get_report_lifecycle_repository",
    "router",
]
