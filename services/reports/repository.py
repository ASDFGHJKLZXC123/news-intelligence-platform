"""Bounded, deterministic loads for the daily brief.

SQL, and only SQL. Every policy decision -- the hotness floor, the 0.6/0.4 ranking, the
tie-breaks, the linked-risk maximum, which version of yesterday's brief counts -- lives in
:mod:`services.reports.selection`, where it can be tested without a database. This module's
whole job is to hand that module the rows it reduces.

Two properties are enforced here rather than hoped for:

* **Bounded.** Every read has a ``LIMIT``. The event scan is ordered so that if it ever does
  hit its bound it keeps the *hottest* events -- the ones that can rank -- and the caller is
  told the bound was reached (`event_scan_limit`), because a silently truncated scan reads
  exactly like a complete one.
* **Deterministic.** Every read has a total ``ORDER BY``. Two runs over the same rows return
  them in the same order, so a regenerated brief is the same brief.

Reads are as-of the cutoff, never as-of *now*: a brief regenerated a week later must select
what it would have selected on the morning it was for.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Text, cast, func, literal, or_, select
from sqlalchemy.orm import Session

from db.models.core import (
    Alert,
    Article,
    Event,
    EventArticle,
    EventCompany,
    EventIndustry,
    Report,
    ReportSection,
    RiskScoreObservation,
    RiskWarning,
    Source,
)
from services.reports.contracts import (
    BRIEF_SEVERITIES,
    AlertStateChange,
    EventWindowRow,
    PriorBriefSection,
    PriorBriefVersion,
    RiskKey,
    RiskObservationRow,
)
from services.reports.window import BriefWindow

#: `risk_score_observations.target_type` for a score observed against an event itself. The
#: column is free text, so the value the brief reads is pinned here rather than spelled out
#: at each call site.
EVENT_RISK_TARGET_TYPE = "event"

#: The report type the daily brief is keyed by (ADR 0009: one brief per
#: `(report_type='daily_brief', brief_date)`, reruns adding versions).
DAILY_BRIEF_REPORT_TYPE = "daily_brief"

#: Far above any legitimate day: a window that clusters 500 events has a clustering problem,
#: not a reporting one. It is a backstop against a runaway scan, not a selection rule.
EVENT_SCAN_LIMIT = 500

#: ADR 0010 caps live alerts well below this; a window cannot legitimately change more.
ALERT_SCAN_LIMIT = 200

#: The radar reduces this in Python to two per-key snapshots, so the read must reach back far
#: enough to find each risk's last observation *before* the window opened -- a risk not
#: re-scored yesterday still has a standing score today. 14 days back from the window's start
#: bounds the read while keeping any risk scored within a fortnight on the board; a risk with
#: nothing newer than that is stale, and its absence from the radar is the honest answer.
RADAR_LOOKBACK = datetime.timedelta(days=14)
OBSERVATION_SCAN_LIMIT = 5_000

#: A brief with more than this many versions or sections is a bug upstream, not a long brief.
VERSION_SCAN_LIMIT = 50
SECTION_SCAN_LIMIT = 50


class SQLAlchemyBriefInputRepository:
    """Loads brief inputs from the operational schema. Read-only: it never writes or flushes."""

    def __init__(
        self,
        session: Session,
        *,
        prediction_backed_outputs_enabled: bool = False,
    ) -> None:
        self._session = session
        self._prediction_backed_outputs_enabled = prediction_backed_outputs_enabled

    @property
    def event_scan_limit(self) -> int:
        return EVENT_SCAN_LIMIT

    def events_in_window(self, window: BriefWindow) -> tuple[EventWindowRow, ...]:
        """The window's events, each with its linked-risk maxima and credibility sum.

        The four risk sources are correlated ``MAX`` subqueries rather than joins: a join
        would multiply an event's row by its company and industry links and inflate the
        credibility sum, and each subquery yields NULL exactly when the event has no row in
        that source -- which is the distinction selection needs to keep a real 80 from being
        averaged away by three absences.

        Rows are *not* filtered by the hotness floor here. Selection applies it, and the
        unscored events it drops become a data-quality note that a SQL-side floor would have
        silently swallowed.
        """
        credibility_sum = (
            select(func.coalesce(func.sum(Source.authority_score), 0.0))
            .select_from(EventArticle)
            .join(Article, Article.id == EventArticle.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(EventArticle.event_id == Event.id)
            .correlate(Event)
            .scalar_subquery()
        )
        observation_risk = (
            select(func.max(RiskScoreObservation.score))
            .where(
                RiskScoreObservation.target_type == EVENT_RISK_TARGET_TYPE,
                RiskScoreObservation.target_id == cast(Event.id, Text),
                RiskScoreObservation.as_of <= window.end,
            )
            .correlate(Event)
            .scalar_subquery()
        )
        if self._prediction_backed_outputs_enabled:
            warning_risk = (
                select(func.max(RiskWarning.risk_score))
                .where(RiskWarning.event_id == Event.id, RiskWarning.created_at <= window.end)
                .correlate(Event)
                .scalar_subquery()
            )
            company_risk = (
                select(func.max(EventCompany.risk_score))
                .where(EventCompany.event_id == Event.id, EventCompany.created_at <= window.end)
                .correlate(Event)
                .scalar_subquery()
            )
            industry_risk = (
                select(func.max(EventIndustry.risk_score))
                .where(EventIndustry.event_id == Event.id, EventIndustry.created_at <= window.end)
                .correlate(Event)
                .scalar_subquery()
            )
        else:
            # Direct event observations remain descriptive inputs. Warning and linked
            # company/industry scores are predictive/composite inputs and become SQL NULL
            # literals, so the closed-gate query does not reference those scored columns.
            warning_risk = literal(None)
            company_risk = literal(None)
            industry_risk = literal(None)

        stmt = (
            select(
                Event.id,
                Event.title,
                Event.hotness_score,
                Event.severity_score,
                Event.first_seen_at,
                Event.updated_at,
                credibility_sum.label("credibility_sum"),
                observation_risk.label("observation_risk"),
                warning_risk.label("warning_risk"),
                company_risk.label("company_risk"),
                industry_risk.label("industry_risk"),
            )
            # ADR 0009's qualifying predicate, exactly: `updated_at` in (previous cutoff,
            # cutoff]. `events.updated_at` is the repository's own last-updated stamp, so
            # there is no second timestamp to keep in step with it.
            .where(Event.updated_at > window.start, Event.updated_at <= window.end)
            # Hottest first, so the LIMIT (if it is ever reached) cuts from the tail that
            # could not have ranked anyway. `Event.id` makes the order total.
            .order_by(Event.hotness_score.desc().nulls_last(), Event.id)
            .limit(EVENT_SCAN_LIMIT)
        )
        return tuple(
            EventWindowRow(
                event_id=row.id,
                title=row.title,
                hotness_score=_as_float(row.hotness_score),
                severity_score=_as_float(row.severity_score),
                first_seen_at=row.first_seen_at,
                updated_at=row.updated_at,
                credibility_sum=_as_float(row.credibility_sum) or 0.0,
                observation_risk=_as_float(row.observation_risk),
                warning_risk=_as_float(row.warning_risk),
                company_risk=_as_float(row.company_risk),
                industry_risk=_as_float(row.industry_risk),
            )
            for row in self._session.execute(stmt).all()
        )

    def alert_state_changes(self, window: BriefWindow) -> tuple[AlertStateChange, ...]:
        """Alerts whose state changed in the window, at Critical/High now *or* at their peak.

        `peak_severity` is in the predicate, not just `severity`, because resolution decays an
        alert to Low: a `severity`-only filter would drop every all-clear the brief exists to
        report. `updated_at` is the change time -- every lifecycle write stamps it.
        """
        if not self._prediction_backed_outputs_enabled:
            return ()

        severities = tuple(sorted(BRIEF_SEVERITIES))
        stmt = (
            select(
                Alert.id,
                Alert.title,
                Alert.state,
                Alert.severity,
                Alert.peak_severity,
                Alert.updated_at,
                Alert.related_event_id,
            )
            .where(
                Alert.updated_at > window.start,
                Alert.updated_at <= window.end,
                or_(
                    Alert.severity.in_(severities),
                    Alert.peak_severity.in_(severities),
                ),
            )
            .order_by(Alert.updated_at.desc(), Alert.id)
            .limit(ALERT_SCAN_LIMIT)
        )
        return tuple(
            AlertStateChange(
                alert_id=row.id,
                title=row.title,
                state=row.state,
                severity=row.severity,
                peak_severity=row.peak_severity,
                changed_at=row.updated_at,
                related_event_id=row.related_event_id,
            )
            for row in self._session.execute(stmt).all()
        )

    def risk_observations(self, window: BriefWindow) -> tuple[RiskObservationRow, ...]:
        """Risk observations near the window, on both sides of its opening cutoff.

        The read spans `window.start - RADAR_LOOKBACK` to `window.end` because the radar needs
        two snapshots, not one: a risk's standing score at the cutoff that closed *yesterday's*
        brief is what today's movement is measured against, and that observation is older than
        this window by definition.
        """
        stmt = (
            select(
                RiskScoreObservation.target_type,
                RiskScoreObservation.target_id,
                RiskScoreObservation.risk_type,
                RiskScoreObservation.score,
                RiskScoreObservation.level,
                RiskScoreObservation.as_of,
            )
            .where(
                RiskScoreObservation.as_of > window.start - RADAR_LOOKBACK,
                RiskScoreObservation.as_of <= window.end,
            )
            .order_by(
                RiskScoreObservation.as_of.desc(),
                RiskScoreObservation.target_type,
                RiskScoreObservation.target_id,
                RiskScoreObservation.risk_type,
            )
            .limit(OBSERVATION_SCAN_LIMIT)
        )
        return tuple(
            RiskObservationRow(
                key=RiskKey(
                    target_type=row.target_type,
                    target_id=row.target_id,
                    risk_type=row.risk_type,
                ),
                score=_as_float(row.score) or 0.0,
                level=row.level,
                as_of=row.as_of,
            )
            for row in self._session.execute(stmt).all()
        )

    def prior_brief_versions(self, brief_date: datetime.date) -> tuple[PriorBriefVersion, ...]:
        """Every version of the brief for ``brief_date``, published or not.

        The unpublished ones are loaded on purpose: which version counts is a decision, and
        `services.reports.selection.pick_published_version` makes it in the open rather than
        having it silently made by a ``WHERE`` clause.
        """
        if not self._prediction_backed_outputs_enabled:
            # Legacy briefs have no durable marker proving every section was generated with
            # Gate G closed. Excluding them is the only fail-closed way to prevent a prior
            # executive summary from carrying probabilities or composite-alert language into
            # today's descriptive-only prompt.
            return ()

        stmt = (
            select(Report.id, Report.brief_date, Report.version, Report.status)
            .where(
                Report.report_type == DAILY_BRIEF_REPORT_TYPE,
                Report.brief_date == brief_date,
            )
            .order_by(Report.version.desc())
            .limit(VERSION_SCAN_LIMIT)
        )
        return tuple(
            PriorBriefVersion(
                report_id=row.id,
                brief_date=row.brief_date,
                version=row.version,
                status=row.status,
            )
            for row in self._session.execute(stmt).all()
        )

    def brief_sections(self, report_id: uuid.UUID) -> tuple[PriorBriefSection, ...]:
        """A past brief's sections, copied into frozen values and detached from the session.

        Published reports are immutable (report-generation spec), and the surest way to keep
        today's brief from editing yesterday's is to never hand it yesterday's ORM rows.
        """
        if not self._prediction_backed_outputs_enabled:
            return ()

        stmt = (
            select(
                ReportSection.section_order,
                ReportSection.title,
                ReportSection.body,
                ReportSection.evidence_refs,
            )
            .where(ReportSection.report_id == report_id)
            .order_by(ReportSection.section_order)
            .limit(SECTION_SCAN_LIMIT)
        )
        return tuple(
            PriorBriefSection(
                section_order=row.section_order,
                title=row.title,
                body=row.body,
                claim_ids=tuple(row.evidence_refs or ()),
            )
            for row in self._session.execute(stmt).all()
        )


def _as_float(value: object) -> float | None:
    """NUMERIC reaches Python as `Decimal` on some columns and `float` on others."""
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]
