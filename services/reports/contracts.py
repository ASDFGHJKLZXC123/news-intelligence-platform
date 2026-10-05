"""Typed, immutable values for daily-brief input selection.

Pure data: nothing here imports SQLAlchemy. :mod:`services.reports.repository` loads rows
into these shapes, :mod:`services.reports.selection` reduces them, and the composition stage
that follows consumes the result without ever seeing a session. Everything is frozen, so a
selected brief cannot be edited into disagreeing with the window it was selected for.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from db.models.enums import RiskLevel
from services.alerts.lifecycle import AlertState
from services.reports.window import BriefWindow

#: The severities the executive summary reports on (report-generation spec: "any Critical/High
#: alert state changes (incl. all-clears)").
BRIEF_SEVERITIES: frozenset[str] = frozenset({RiskLevel.CRITICAL.value, RiskLevel.HIGH.value})


class RiskProvenance(StrEnum):
    """Which linked risk source produced an event's ``max_linked_risk_score``.

    Recorded rather than inferred: "this event's risk is 72" is not a usable input to a brief
    unless the composer can say *whose* 72 it is -- the event's own risk observation, a
    warning raised from it, or the risk it transmits to a company or an industry.
    """

    EVENT_OBSERVATION = "event_observation"
    RISK_WARNING = "risk_warning"
    EVENT_COMPANY = "event_company"
    EVENT_INDUSTRY = "event_industry"
    NONE = "none"


#: Tie order when two linked risk sources report the same maximum. Most direct evidence of
#: risk *to this event* first: a score observed against the event itself, then a warning
#: raised from it, then the risk it carries into a company, then into an industry.
PROVENANCE_PRECEDENCE: tuple[RiskProvenance, ...] = (
    RiskProvenance.EVENT_OBSERVATION,
    RiskProvenance.RISK_WARNING,
    RiskProvenance.EVENT_COMPANY,
    RiskProvenance.EVENT_INDUSTRY,
)


class DataQuality(StrEnum):
    """An absence the brief must declare rather than paper over (ADR 0009).

    A note is not necessarily a fault: a day with no Critical alert is a good day, not a
    broken pipeline. It is a statement about what the brief could *not* draw on, so that a
    quiet brief is legible as quiet instead of reading as a complete one.
    """

    NO_EVENTS_IN_WINDOW = "no_events_in_window"
    NO_EVENTS_ABOVE_HOTNESS_FLOOR = "no_events_above_hotness_floor"
    EVENTS_MISSING_HOTNESS = "events_missing_hotness"
    EVENTS_MISSING_START_TIME = "events_missing_start_time"
    EVENT_SCAN_TRUNCATED = "event_scan_truncated"
    NO_ALERT_STATE_CHANGES = "no_alert_state_changes"
    NO_RISK_OBSERVATIONS = "no_risk_observations"
    NO_RISK_MOVE = "no_risk_move"
    NO_PRIOR_BRIEF = "no_prior_brief"
    NO_PRIOR_BRIEF_CLAIMS = "no_prior_brief_claims"
    #: Composition context (`services.reports.context`).
    NO_EVIDENCE_FOR_EVENT = "no_evidence_for_event"
    EVIDENCE_TRUNCATED = "evidence_truncated"
    #: A global context scan reached its immutable per-brief bound (`BoundedRead`); the reducer
    #: saw at most the bound and may not have seen every row. Distinct from the per-event
    #: `EVIDENCE_TRUNCATED`: this is the backstop scan, not the per-event/per-claim bound.
    EVIDENCE_SCAN_TRUNCATED = "evidence_scan_truncated"
    ANALOGY_SCAN_TRUNCATED = "analogy_scan_truncated"
    FORECAST_SCAN_TRUNCATED = "forecast_scan_truncated"
    NO_RELIABLE_ANALOGY = "no_reliable_analogy"
    NO_FORECAST = "no_forecast"
    FORECAST_SET_INVALID = "forecast_set_invalid"
    NO_PRIOR_COMPARISON = "no_prior_comparison"
    #: Personal workflow capture accounting. Always rendered so a quiet result can be
    #: distinguished from an all-feed failure and partial coverage remains visible.
    CAPTURE_COVERAGE = "capture_coverage"


@dataclass(frozen=True)
class DataQualityNote:
    """One declared absence, with enough detail for the composer to render it."""

    code: DataQuality
    detail: str


@dataclass(frozen=True)
class LinkedRisk:
    """The maximum risk linked to an event, and where it came from."""

    score: float
    provenance: RiskProvenance


@dataclass(frozen=True)
class EventWindowRow:
    """One event in the window, with its linked-risk and credibility inputs gathered.

    The four ``*_risk`` fields are the maxima of the four linked risk sources, each ``None``
    when the event has no row in that source at all -- which is not the same as a risk of
    zero, and :func:`services.reports.selection.resolve_linked_risk` keeps the difference.
    """

    event_id: uuid.UUID
    title: str
    hotness_score: float | None
    updated_at: datetime.datetime
    first_seen_at: datetime.datetime | None = None
    severity_score: float | None = None
    #: Sum of `Source.authority_score` over the event's linked articles (the spec's
    #: source-credibility tie-break). A sum over articles, not over distinct sources: an event
    #: ten credible outlets each covered twice is better sourced than one they covered once.
    credibility_sum: float = 0.0
    observation_risk: float | None = None
    warning_risk: float | None = None
    company_risk: float | None = None
    industry_risk: float | None = None


@dataclass(frozen=True)
class SelectedEvent:
    """An event that qualified for the brief's Top Events, and why."""

    rank: int
    event_id: uuid.UUID
    title: str
    hotness_score: float | None
    max_linked_risk: LinkedRisk
    ranking_score: float
    credibility_sum: float
    #: ADR 0009's straddling marker: the event began before this window opened.
    developing: bool

    @property
    def max_linked_risk_score(self) -> float:
        return self.max_linked_risk.score


@dataclass(frozen=True)
class AlertStateChange:
    """An alert whose lifecycle state changed inside the window (ADR 0010)."""

    alert_id: uuid.UUID
    title: str
    state: str
    severity: str
    peak_severity: str | None
    changed_at: datetime.datetime
    related_event_id: uuid.UUID | None = None

    @property
    def effective_severity(self) -> str:
        """The severity this change should be *judged* at.

        A resolving alert has already decayed to Low -- ADR 0010 only resolves at Low -- so
        its live `severity` says nothing about the alert the brief is standing down. Its
        `peak_severity` does. Alerts predating that column recorded no peak, and the alert
        service reads a NULL peak as the row's severity; so does this.
        """
        return self.peak_severity or self.severity

    @property
    def is_all_clear(self) -> bool:
        """A resolution: de-escalation is information, and the brief reports it."""
        return self.state == AlertState.RESOLVED


@dataclass(frozen=True, order=True)
class RiskKey:
    """What a risk score is *about*: `risk_score_observations`' natural key, minus time."""

    target_type: str
    target_id: str
    risk_type: str


@dataclass(frozen=True)
class RiskObservationRow:
    """One point of a risk time series."""

    key: RiskKey
    score: float
    level: str
    as_of: datetime.datetime


@dataclass(frozen=True)
class RiskRadarEntry:
    """A risk's standing score as of one cutoff."""

    key: RiskKey
    score: float
    level: str
    as_of: datetime.datetime


@dataclass(frozen=True)
class RiskMove:
    """One risk's movement across the window: where it stood at each cutoff."""

    key: RiskKey
    current: RiskRadarEntry
    previous: RiskRadarEntry

    @property
    def delta(self) -> float:
        """Signed: positive is a risk that rose, negative is a risk that eased."""
        return round(self.current.score - self.previous.score, 2)

    @property
    def magnitude(self) -> float:
        return abs(self.delta)

    @property
    def is_reversal(self) -> bool:
        """The level changed, not just the score -- what the day-over-day rule must catch."""
        return self.current.level != self.previous.level


@dataclass(frozen=True)
class RiskRadar:
    """The risk table as of this cutoff, as of the previous one, and the moves between.

    Both snapshots are kept rather than only their diff: the day-over-day check the
    composition stage runs needs to see what yesterday's level *was*, not just that it moved.
    """

    current: tuple[RiskRadarEntry, ...]
    previous: tuple[RiskRadarEntry, ...]
    #: Sorted by magnitude descending, then key. Only risks observed on both sides of the
    #: window's opening cutoff appear: a risk first seen inside the window has no move to
    #: measure, and inventing a zero-to-current jump for it would be the largest fake move
    #: on the board.
    moves: tuple[RiskMove, ...]


@dataclass(frozen=True)
class PriorBriefVersion:
    """A candidate version of a previous brief, before the published one is chosen."""

    report_id: uuid.UUID
    brief_date: datetime.date
    version: int
    status: str


@dataclass(frozen=True)
class PriorBriefSection:
    """One section of the previous brief, copied out of the ORM and frozen.

    Frozen and detached on purpose: the spec makes a published report immutable, and the
    cheapest way to guarantee today's brief cannot edit yesterday's is to never hand it
    yesterday's rows.
    """

    section_order: int
    title: str
    body: str
    claim_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class PriorBriefContext:
    """Yesterday's published brief, as context for the day-over-day consistency rule."""

    report_id: uuid.UUID
    brief_date: datetime.date
    version: int
    sections: tuple[PriorBriefSection, ...]

    @property
    def key_claim_ids(self) -> tuple[uuid.UUID, ...]:
        """Every claim yesterday's brief cited, de-duplicated, in section order."""
        seen: dict[uuid.UUID, None] = {}
        for section in self.sections:
            for claim_id in section.claim_ids:
                seen.setdefault(claim_id, None)
        return tuple(seen)


@dataclass(frozen=True)
class ExecutiveSummaryInputs:
    """What the executive summary is composed from, in the spec's order.

    Inputs only: this stage selects, it does not write prose. The 120-word cap is a composer
    constraint and lives with the composer.
    """

    alert_state_changes: tuple[AlertStateChange, ...]
    top_events: tuple[SelectedEvent, ...]
    largest_risk_move: RiskMove | None


class CompositionPolicy(StrEnum):
    """Versioned prose rules, retained with inputs through grounding regeneration."""

    LEGACY = "legacy_daily_brief.v1"
    PERSONAL_DESCRIPTIVE = "personal_descriptive.v1"


@dataclass(frozen=True)
class BriefInputs:
    """Everything the composition stage needs to write one daily brief, and nothing it does not."""

    window: BriefWindow
    top_events: tuple[SelectedEvent, ...]
    executive_summary: ExecutiveSummaryInputs
    risk_radar: RiskRadar
    prior_brief: PriorBriefContext | None
    data_quality_notes: tuple[DataQualityNote, ...]
    composition_policy: CompositionPolicy = CompositionPolicy.LEGACY

    @property
    def is_quiet_day(self) -> bool:
        """ADR 0009: zero qualifying events is a valid brief -- risk radar only, never padding."""
        return not self.top_events


@runtime_checkable
class BriefInputRepository(Protocol):
    """The persistence surface brief selection needs, and nothing more.

    Declared here, among the pure values, so that `services.reports.selection` can depend on
    the *shape* of its loader without importing SQLAlchemy -- which is what keeps the
    selection rules testable with hand-built rows and no database.
    """

    @property
    def event_scan_limit(self) -> int:
        """The bound on `events_in_window`, so a scan that reached it can say so."""

    def events_in_window(self, window: BriefWindow) -> tuple[EventWindowRow, ...]: ...

    def alert_state_changes(self, window: BriefWindow) -> tuple[AlertStateChange, ...]: ...

    def risk_observations(self, window: BriefWindow) -> tuple[RiskObservationRow, ...]: ...

    def prior_brief_versions(self, brief_date: datetime.date) -> tuple[PriorBriefVersion, ...]: ...

    def brief_sections(self, report_id: uuid.UUID) -> tuple[PriorBriefSection, ...]: ...
