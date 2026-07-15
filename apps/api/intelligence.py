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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import Text, and_, cast, func, or_, select
from sqlalchemy.orm import Session, aliased

from db.base import get_session
from db.models import (
    ACTIVE_ALERT_STATES,
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
    Report,
    ReportSection,
    RiskScoreObservation,
    Source,
    SourceHealthSnapshot,
    WatchlistItem,
)
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
from services.reports.repository import DAILY_BRIEF_REPORT_TYPE
from services.reports.selection import PUBLISHED_STATUS

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


def _serialize_brief_snapshot(
    report: ReportSnapshot, sections: tuple[ReportSectionSnapshot, ...] | None = None
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
            for section in sections
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
        # Daily briefs are versioned and global; the default listing serves only the latest
        # PUBLISHED version per brief_date -- never a generating/failed/superseded one
        # (report-generation spec, "Lifecycle and versioning"; the dedicated
        # /reports/daily-brief endpoints expose full history). Every other report type
        # (event/user reports) is returned unchanged. The predicate is a no-op on the
        # user-scoped path: daily briefs are user_id IS NULL, so a user_id filter already
        # excludes them.
        newer_published = aliased(Report)
        has_newer_published = (
            select(newer_published.id)
            .where(
                newer_published.report_type == DAILY_BRIEF_REPORT_TYPE,
                newer_published.brief_date == Report.brief_date,
                newer_published.status == PUBLISHED_STATUS,
                newer_published.version > Report.version,
            )
            .exists()
        )
        stmt = stmt.where(
            or_(
                Report.report_type != DAILY_BRIEF_REPORT_TYPE,
                and_(Report.status == PUBLISHED_STATUS, ~has_newer_published),
            )
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
        sections_by_report: dict[uuid.UUID, list[ReportSection]] = {report.id: [] for report in reports}
        for section in sections:
            sections_by_report[section.report_id].append(section)
        return [(report, sections_by_report[report.id]) for report in reports]

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
        support types are returned (supports/contradicts/...); nothing is filtered or fabricated.
        """
        summary_head = func.left(Article.summary, MAX_EXCERPT_CHARS + 1)
        body_head = func.left(Article.body, MAX_EXCERPT_CHARS + 1)
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
            .where(ClaimEvidence.claim_id == claim_id)
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


def enqueue_generate_daily_brief(brief_date: datetime.date) -> QueuedBrief:
    """Enqueue the accepted ``generate_daily_brief`` Celery task (ADR 0009) by name, asynchronously.

    Opens no database and no Redis and never calls the coordinator: it hands the canonical
    ``YYYY-MM-DD`` to the broker on ``QUEUE_PIPELINE`` and returns the task identity. The
    coordinator (run by the worker) allocates a *new* version through its existing lifecycle
    semantics; this never mutates or reuses a prior report. Imported lazily so importing this
    module opens no broker connection and so the enqueue seam is monkeypatchable in tests.
    """
    from workers.celery_app import QUEUE_PIPELINE, celery_app
    from workers.report_tasks import TASK_NAME

    async_result = celery_app.send_task(
        TASK_NAME, args=[brief_date.isoformat()], queue=QUEUE_PIPELINE
    )
    return QueuedBrief(task_id=str(async_result.id), task_name=TASK_NAME, queue=QUEUE_PIPELINE)


def get_brief_enqueuer() -> Callable[[datetime.date], QueuedBrief]:
    return enqueue_generate_daily_brief


BriefEnqueuerDep = Annotated[Callable[[datetime.date], QueuedBrief], Depends(get_brief_enqueuer)]


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
def acknowledge_alert(alert_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    """Record that a notification for this alert was actually delivered (ADR 0010).

    Escalations re-notify by design, so this always records the *latest* delivery -- there is
    no terminal-state conflict here, only "does this alert exist".
    """
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
def acknowledge_alert_all_clear(alert_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    """Record that the all-clear for this alert was delivered (ADR 0010).

    An all-clear is sent once and only for a *resolved* alert: acknowledging one on a live
    (open/escalated/downgraded) or already-superseded alert is a 409, not a 404 -- the alert
    exists, it is simply not in the one state this action is valid for. Re-acknowledging an
    already-acknowledged resolution is not an error: it is reported back as a no-op so a
    retried request is idempotent.
    """
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
) -> dict[str, Any]:
    """Replace this alert (and any additional narrower alerts named) with a broader one.

    Wraps `services.alerts.supersession.supersede_with_broader_alert`. Its
    ``SupersessionError`` covers several distinct failures that this endpoint splits by HTTP
    status: an unknown broader or narrower alert id is a 404 (nothing to act on); a malformed
    id, a terminal (already resolved/superseded) alert, an alert owned by a different user, or
    a narrower alert that is actually the broader alert's own dedupe key are all 409s -- the
    named alerts exist, but the replacement as asked for is not a valid one.
    """
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


# --------------------------------------------------------------------------------------
# Daily brief (ADR 0009): the latest PUBLISHED version is served by default; prior versions
# -- including generating/failed ones, with their status/stale/change_reason -- are explicit.
# `/latest` is declared before `/{brief_date}` so the literal wins over the typed date param.
# --------------------------------------------------------------------------------------


def _brief_with_sections(repo: ReportLifecycleRepository, report: ReportSnapshot) -> dict[str, Any]:
    return _serialize_brief_snapshot(report, repo.report_sections(report.id))


@router.get("/api/v1/reports/daily-brief/latest")
def get_daily_brief_latest(repo: LifecycleRepoDep) -> dict[str, Any]:
    report = repo.latest_published_daily_brief()
    if report is None:
        raise HTTPException(status_code=404, detail="no published daily brief")
    return {"report": _brief_with_sections(repo, report)}


@router.get("/api/v1/reports/daily-brief/{brief_date}")
def get_daily_brief_by_date(brief_date: datetime.date, repo: LifecycleRepoDep) -> dict[str, Any]:
    report = repo.latest_published_daily_brief_by_date(brief_date)
    if report is None:
        raise HTTPException(status_code=404, detail="no published daily brief for that date")
    return {"report": _brief_with_sections(repo, report)}


@router.get("/api/v1/reports/daily-brief/{brief_date}/versions")
def list_daily_brief_versions(brief_date: datetime.date, repo: LifecycleRepoDep) -> dict[str, Any]:
    versions = repo.daily_brief_versions(brief_date)
    if not versions:
        raise HTTPException(status_code=404, detail="no daily brief for that date")
    # Newest first, bounded (the repository fails loud past its version bound). Metadata only:
    # each entry carries status/stale/change_reason so a prior version is fully inspectable.
    return {
        "items": [_serialize_brief_snapshot(report) for report in versions],
        "count": len(versions),
    }


@router.get("/api/v1/reports/daily-brief/{brief_date}/versions/{version}")
def get_daily_brief_version(
    brief_date: datetime.date,
    version: Annotated[int, Path(ge=1)],
    repo: LifecycleRepoDep,
) -> dict[str, Any]:
    report = repo.daily_brief_version(brief_date, version)
    if report is None:
        raise HTTPException(status_code=404, detail="no such daily brief version")
    return {"report": _brief_with_sections(repo, report)}


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
) -> Response:
    """Render a published brief to Markdown or PDF with a safe, deterministic download filename.

    Reads only: the ordered sections, the distinct cited claims, and one bulk attribution query.
    """
    sections = lifecycle_repo.report_sections(report.id)
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
    brief_date: datetime.date, repo: LifecycleRepoDep, export_repo: ExportRepoDep
) -> Response:
    report = _published_or_404(repo.latest_published_daily_brief_by_date(brief_date))
    return _export_response(repo, export_repo, report, pdf=False)


@router.get("/api/v1/reports/daily-brief/{brief_date}/export.pdf")
def export_daily_brief_latest_pdf(
    brief_date: datetime.date, repo: LifecycleRepoDep, export_repo: ExportRepoDep
) -> Response:
    report = _published_or_404(repo.latest_published_daily_brief_by_date(brief_date))
    return _export_response(repo, export_repo, report, pdf=True)


@router.get("/api/v1/reports/daily-brief/{brief_date}/versions/{version}/export.md")
def export_daily_brief_version_markdown(
    brief_date: datetime.date,
    version: Annotated[int, Path(ge=1)],
    repo: LifecycleRepoDep,
    export_repo: ExportRepoDep,
) -> Response:
    report = _published_or_404(repo.daily_brief_version(brief_date, version))
    return _export_response(repo, export_repo, report, pdf=False)


@router.get("/api/v1/reports/daily-brief/{brief_date}/versions/{version}/export.pdf")
def export_daily_brief_version_pdf(
    brief_date: datetime.date,
    version: Annotated[int, Path(ge=1)],
    repo: LifecycleRepoDep,
    export_repo: ExportRepoDep,
) -> Response:
    report = _published_or_404(repo.daily_brief_version(brief_date, version))
    return _export_response(repo, export_repo, report, pdf=True)


@router.get("/api/v1/evidence/{claim_id}")
def get_evidence(claim_id: uuid.UUID, repo: RepositoryDep) -> dict[str, Any]:
    """Resolve one claim id (from ``report_sections.evidence_refs``) to its evidence for the drawer.

    A malformed id is rejected at routing (422 via the typed ``uuid.UUID`` path param); an unknown
    claim is 404. Evidence is a bounded, deterministic list carrying the real support_type and, for
    article evidence, a <= 200-char snippet plus canonical link/title/source attribution -- never
    article full text or raw payload.
    """
    claim = repo.get_claim(claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="claim not found")
    links = repo.claim_evidence_links(claim_id)
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


class GenerateDailyBriefRequest(BaseModel):
    """Body for ``POST /api/v1/internal/jobs/generate-daily-brief`` (ADR 0009 manual regeneration).

    ``brief_date`` is required and explicit -- a manual regeneration always names the calendar date
    it is for; an unparseable date is a 422 from the typed field. The ET-cutoff default is the
    scheduled beat's job, not this endpoint's.
    """

    brief_date: datetime.date


@router.post("/api/v1/internal/jobs/generate-daily-brief", status_code=202)
def generate_daily_brief_job(
    request: GenerateDailyBriefRequest, enqueue: BriefEnqueuerDep
) -> dict[str, Any]:
    """Queue the accepted ``generate_daily_brief`` task for one ``brief_date``; return 202 (ADR 0009).

    Asynchronous only: it enqueues on the pipeline queue and returns the task identity. It opens no
    database and no Redis, and never runs the coordinator inline -- the worker does that and mints a
    *new* version through the lifecycle's existing semantics, never reusing a prior report.
    """
    queued = enqueue(request.brief_date)
    return {
        "status": "queued",
        "task_id": queued.task_id,
        "task_name": queued.task_name,
        "queue": queued.queue,
        "brief_date": request.brief_date.isoformat(),
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
    count = repo.mark_published_reports_stale_for_event(event_id)
    session.commit()
    return {"event_id": str(event_id), "dependent_published_report_count": count}


__all__ = [
    "EvidenceDrawerLink",
    "IntelligenceRepository",
    "QueuedBrief",
    "enqueue_generate_daily_brief",
    "get_brief_enqueuer",
    "get_intelligence_repository",
    "get_report_export_repository",
    "get_report_lifecycle_repository",
    "router",
]
