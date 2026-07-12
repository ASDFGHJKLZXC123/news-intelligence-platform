"""Read-only frontend intelligence query APIs.

These endpoints are the database-backed contract the static frontend can cut over to
page by page while keeping fixture JSON as a fallback during development.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from db.base import get_session
from db.models import (
    ACTIVE_ALERT_STATES,
    Alert,
    Company,
    CompanyRiskRollup,
    CrisisPrediction,
    DailyIntelligenceSummary,
    Event,
    EventCompany,
    EventIndustry,
    EventLocation,
    EventTimelineItem,
    IndustryRiskRollup,
    Job,
    LLMRun,
    Report,
    ReportSection,
    RiskScoreObservation,
    Source,
    SourceHealthSnapshot,
    WatchlistItem,
)

router = APIRouter(tags=["intelligence"])


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


def _serialize_event_location(location: EventLocation, event: Event | None = None) -> dict[str, Any]:
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


def _serialize_daily_summary(summary: DailyIntelligenceSummary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "id": str(summary.id),
        "summary_date": _iso(summary.summary_date),
        "overall_risk_level": summary.overall_risk_level,
        "confidence_score": _json_value(summary.confidence_score),
        "summary": summary.summary,
        "key_points": summary.key_points,
        "model_rating_prediction_id": _json_value(summary.model_rating_prediction_id),
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


def _serialize_report(report: Report, sections: list[ReportSection] | None = None) -> dict[str, Any]:
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
            for section in sections
        ]
    return payload


class IntelligenceRepository:
    """SQLAlchemy-backed read model for frontend-facing intelligence endpoints."""

    def __init__(self, session: Session) -> None:
        self.session = session

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

    def list_events(
        self,
        *,
        q: str | None,
        country: str | None,
        event_type: str | None,
        limit: int,
    ) -> list[Event]:
        stmt = select(Event).order_by(Event.last_seen_at.desc().nullslast(), Event.created_at.desc()).limit(limit)
        if country:
            stmt = stmt.where(func.upper(Event.country) == country.upper())
        if event_type:
            stmt = stmt.where(Event.event_type == event_type)
        if q:
            like_q = f"%{q}%"
            stmt = stmt.where(or_(Event.title.ilike(like_q), Event.summary.ilike(like_q)))
        return list(self.session.execute(stmt).scalars().all())

    def get_event_detail(
        self,
        event_id: uuid.UUID,
    ) -> tuple[Event | None, list[EventTimelineItem], list[EventCompany], list[EventIndustry], list[EventLocation]]:
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
        companies = list(
            self.session.execute(select(EventCompany).where(EventCompany.event_id == event_id))
            .scalars()
            .all()
        )
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

    def list_geo_events(self, *, country_code: str | None, limit: int) -> list[tuple[EventLocation, Event]]:
        stmt = (
            select(EventLocation, Event)
            .join(Event, EventLocation.event_id == Event.id)
            .order_by(Event.last_seen_at.desc().nullslast(), EventLocation.created_at.desc())
            .limit(limit)
        )
        if country_code:
            stmt = stmt.where(func.upper(EventLocation.country_code) == country_code.upper())
        return [(location, event) for location, event in self.session.execute(stmt).all()]

    def list_companies(
        self,
        *,
        q: str | None,
        sector: str | None,
        country: str | None,
        limit: int,
    ) -> list[Company]:
        stmt = select(Company).where(Company.active.is_(True)).order_by(Company.display_name).limit(limit)
        if sector:
            stmt = stmt.where(Company.sector == sector)
        if country:
            stmt = stmt.where(func.upper(Company.country) == country.upper())
        if q:
            like_q = f"%{q}%"
            stmt = stmt.where(
                or_(
                    Company.display_name.ilike(like_q),
                    Company.legal_name.ilike(like_q),
                    Company.primary_ticker.ilike(like_q),
                )
            )
        return list(self.session.execute(stmt).scalars().all())

    def get_company(self, identifier: str) -> tuple[Company | None, CompanyRiskRollup | None]:
        company_id = _uuid_or_none(identifier)
        if company_id is not None:
            company_stmt = select(Company).where(Company.id == company_id)
        else:
            company_stmt = select(Company).where(func.upper(Company.primary_ticker) == identifier.upper())
        company = self.session.execute(company_stmt).scalars().first()
        if company is None:
            return None, None
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

    def list_industries(self, *, limit: int) -> list[IndustryRiskRollup]:
        # One row per industry -- its latest rollup. Ordering by `as_of` alone returned the
        # same industry once per snapshot (api-adapter-contract, shape gap 6).
        stmt = (
            select(IndustryRiskRollup)
            .distinct(IndustryRiskRollup.industry_id)
            .order_by(IndustryRiskRollup.industry_id, IndustryRiskRollup.as_of.desc())
            .subquery()
        )
        latest = aliased(IndustryRiskRollup, stmt)
        rollups = (
            self.session.execute(select(latest).order_by(latest.as_of.desc()).limit(limit))
            .scalars()
            .all()
        )
        return list(rollups)

    def get_industry(self, industry_id: str) -> IndustryRiskRollup | None:
        stmt = (
            select(IndustryRiskRollup)
            .where(IndustryRiskRollup.industry_id == industry_id)
            .order_by(IndustryRiskRollup.as_of.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def list_historical(self, *, target_type: str | None, risk_type: str | None, limit: int) -> list[CrisisPrediction]:
        stmt = select(CrisisPrediction).order_by(CrisisPrediction.as_of_date.desc()).limit(limit)
        if target_type:
            stmt = stmt.where(CrisisPrediction.target_type == target_type)
        if risk_type:
            stmt = stmt.where(CrisisPrediction.risk_type == risk_type)
        return list(self.session.execute(stmt).scalars().all())

    def list_alerts(self, *, user_id: uuid.UUID | None, status: str | None, limit: int) -> list[Alert]:
        # `status` is the wire name for the ADR 0010 lifecycle state.
        stmt = select(Alert).order_by(Alert.created_at.desc()).limit(limit)
        if user_id is not None:
            stmt = stmt.where(Alert.user_id == user_id)
        if status:
            stmt = stmt.where(Alert.state == status)
        return list(self.session.execute(stmt).scalars().all())

    def list_watchlist(self, *, user_id: uuid.UUID | None, limit: int) -> list[WatchlistItem]:
        stmt = select(WatchlistItem).order_by(WatchlistItem.created_at.desc()).limit(limit)
        if user_id is not None:
            stmt = stmt.where(WatchlistItem.user_id == user_id)
        return list(self.session.execute(stmt).scalars().all())

    def list_reports(self, *, user_id: uuid.UUID | None, limit: int) -> list[tuple[Report, list[ReportSection]]]:
        stmt = select(Report).order_by(Report.created_at.desc()).limit(limit)
        if user_id is not None:
            stmt = stmt.where(Report.user_id == user_id)
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
        sections_by_report: dict[uuid.UUID, list[ReportSection]] = {report.id: [] for report in reports}
        for section in sections:
            sections_by_report[section.report_id].append(section)
        return [(report, sections_by_report[report.id]) for report in reports]

    def list_jobs(self, *, state: str | None, limit: int) -> list[Job]:
        stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if state:
            stmt = stmt.where(Job.state == state)
        return list(self.session.execute(stmt).scalars().all())

    def list_sources(self, *, active: bool | None, limit: int) -> tuple[list[Source], list[SourceHealthSnapshot]]:
        source_stmt = select(Source).order_by(Source.name).limit(limit)
        if active is not None:
            source_stmt = source_stmt.where(Source.active.is_(active))
        sources = list(self.session.execute(source_stmt).scalars().all())
        health = list(
            self.session.execute(
                select(SourceHealthSnapshot)
                .order_by(SourceHealthSnapshot.checked_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return sources, health

    def list_model_runs(self, *, limit: int) -> list[LLMRun]:
        stmt = select(LLMRun).order_by(LLMRun.created_at.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())


def get_intelligence_repository(
    session: Annotated[Session, Depends(get_session)],
) -> IntelligenceRepository:
    return IntelligenceRepository(session)


RepositoryDep = Annotated[IntelligenceRepository, Depends(get_intelligence_repository)]


@router.get("/api/v1/dashboard")
def get_dashboard(repo: RepositoryDep) -> dict[str, Any]:
    return {
        "summary": _serialize_daily_summary(repo.latest_daily_summary()),
        "risk_scores": [_serialize_risk_score(score) for score in repo.list_risk_scores(risk_type=None, target_type=None, limit=12)],
        "alerts": {"open_count": repo.count_open_alerts()},
    }


@router.get("/api/v1/events")
def list_events(
    repo: RepositoryDep,
    q: str | None = None,
    country: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    events = repo.list_events(q=q, country=country, event_type=event_type, limit=limit)
    return {"items": [_serialize_event(event) for event in events], "count": len(events)}


@router.get("/api/v1/events/{event_id}")
def get_event(event_id: uuid.UUID, repo: RepositoryDep) -> dict[str, Any]:
    event, timeline, companies, industries, locations = repo.get_event_detail(event_id)
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
        "companies": [
            {
                "company_id": str(company.company_id),
                "impact_direction": company.impact_direction,
                "impact_score": _json_value(company.impact_score),
                "risk_score": _json_value(company.risk_score),
                "confidence_score": _json_value(company.confidence_score),
            }
            for company in companies
        ],
        "industries": [
            {
                "industry_id": industry.industry_id,
                "impact_direction": industry.impact_direction,
                "impact_score": _json_value(industry.impact_score),
                "risk_score": _json_value(industry.risk_score),
                "opportunity_score": _json_value(industry.opportunity_score),
            }
            for industry in industries
        ],
        "locations": [_serialize_event_location(location) for location in locations],
    }


@router.get("/api/v1/geo/events")
def list_geo_events(
    repo: RepositoryDep,
    country_code: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    rows = repo.list_geo_events(country_code=country_code, limit=limit)
    return {
        "items": [_serialize_event_location(location, event) for location, event in rows],
        "count": len(rows),
    }


@router.get("/api/v1/risk-radar")
def list_risk_radar(
    repo: RepositoryDep,
    target_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    scores = repo.list_risk_scores(risk_type=None, target_type=target_type, limit=limit)
    return {"items": [_serialize_risk_score(score) for score in scores], "count": len(scores)}


@router.get("/api/v1/risk-radar/{risk_type}")
def list_risk_radar_by_type(
    risk_type: str,
    repo: RepositoryDep,
    target_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    scores = repo.list_risk_scores(risk_type=risk_type, target_type=target_type, limit=limit)
    return {"items": [_serialize_risk_score(score) for score in scores], "count": len(scores)}


@router.get("/api/v1/industries")
def list_industries(
    repo: RepositoryDep,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    rollups = repo.list_industries(limit=limit)
    return {"items": [_serialize_industry_rollup(rollup) for rollup in rollups], "count": len(rollups)}


@router.get("/api/v1/industries/{industry_id}")
def get_industry(industry_id: str, repo: RepositoryDep) -> dict[str, Any]:
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
) -> dict[str, Any]:
    companies = repo.list_companies(q=q, sector=sector, country=country, limit=limit)
    return {"items": [_serialize_company(company) for company in companies], "count": len(companies)}


@router.get("/api/v1/companies/{company_id}")
def get_company(company_id: str, repo: RepositoryDep) -> dict[str, Any]:
    company, rollup = repo.get_company(company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")
    return {"company": _serialize_company(company, rollup)}


@router.get("/api/v1/historical")
def list_historical(
    repo: RepositoryDep,
    target_type: str | None = None,
    risk_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
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
    user_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    alerts = repo.list_alerts(user_id=_uuid_or_none(user_id), status=status, limit=limit)
    return {"items": [_serialize_alert(alert) for alert in alerts], "count": len(alerts)}


@router.get("/api/v1/watchlist")
def list_watchlist(
    repo: RepositoryDep,
    user_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    items = repo.list_watchlist(user_id=_uuid_or_none(user_id), limit=limit)
    return {"items": [_serialize_watchlist_item(item) for item in items], "count": len(items)}


@router.get("/api/v1/reports")
def list_reports(
    repo: RepositoryDep,
    user_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    rows = repo.list_reports(user_id=_uuid_or_none(user_id), limit=limit)
    return {
        "items": [_serialize_report(report, sections) for report, sections in rows],
        "count": len(rows),
    }


@router.get("/api/v1/admin/jobs")
def list_admin_jobs(
    repo: RepositoryDep,
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    jobs = repo.list_jobs(state=state, limit=limit)
    return {
        "items": [
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
        "count": len(jobs),
    }


@router.get("/api/v1/admin/sources")
def list_admin_sources(
    repo: RepositoryDep,
    active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    sources, health = repo.list_sources(active=active, limit=limit)
    return {
        "sources": [
            {
                "id": str(source.id),
                "name": source.name,
                "source_type": source.source_type,
                "feed_url": source.feed_url,
                "homepage_url": source.homepage_url,
                "active": source.active,
                "created_at": _iso(source.created_at),
                "updated_at": _iso(source.updated_at),
            }
            for source in sources
        ],
        "health": [
            {
                "id": str(item.id),
                "source_id": _json_value(item.source_id),
                "provider": item.provider,
                "checked_at": _iso(item.checked_at),
                "status": item.status,
                "latency_ms": item.latency_ms,
                "error_rate": _json_value(item.error_rate),
                "items_fetched": item.items_fetched,
            }
            for item in health
        ],
    }


@router.get("/api/v1/admin/models")
def list_admin_models(
    repo: RepositoryDep,
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    runs = repo.list_model_runs(limit=limit)
    return {
        "items": [
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
        "count": len(runs),
    }


__all__ = [
    "IntelligenceRepository",
    "get_intelligence_repository",
    "router",
]
