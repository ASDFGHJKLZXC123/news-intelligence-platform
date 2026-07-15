"""Deterministic input selection for the daily brief (report-generation spec, ADR 0009).

Every policy decision the brief makes about *what goes in it* lives here, and every one of
them is a pure function of rows the repository handed over: the hotness floor, the 0.6/0.4
ranking, the tie-breaks, the linked-risk maximum, the risk radar's two cutoffs, and the
absences the brief has to declare. Nothing here touches a session, a clock, or the network.

This stage selects. It does not compose: no prose, no LLM, no persistence.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Iterable, Sequence

from services.alerts.hysteresis import severity_rank
from services.reports.contracts import (
    BRIEF_SEVERITIES,
    PROVENANCE_PRECEDENCE,
    AlertStateChange,
    BriefInputRepository,
    BriefInputs,
    DataQuality,
    DataQualityNote,
    EventWindowRow,
    ExecutiveSummaryInputs,
    LinkedRisk,
    PriorBriefContext,
    PriorBriefVersion,
    RiskKey,
    RiskMove,
    RiskObservationRow,
    RiskProvenance,
    RiskRadar,
    RiskRadarEntry,
    SelectedEvent,
)
from services.reports.window import ONE_DAY, BriefWindow

#: "Events below hotness 40 never qualify -- on quiet days the section shrinks rather than
#: padding" (report-generation spec). A hard floor, not a preference: it is not a sort order
#: that a thin day can push a dull event through.
HOTNESS_FLOOR = 40.0

#: "take top 5" and "top-2 events" (report-generation spec).
TOP_EVENT_COUNT = 5
EXEC_SUMMARY_EVENT_COUNT = 2

#: `0.6 x hotness_score + 0.4 x max_linked_risk_score` (report-generation spec).
HOTNESS_WEIGHT = 0.6
LINKED_RISK_WEIGHT = 0.4

#: Ranking scores are compared for equality to detect ties, so they are rounded first: the
#: two inputs carry two decimals, and 0.6 x 61.0 + 0.4 x 0.0 must not land on 36.599999999999994
#: and lose a tie-break that the spec says the credibility sum should settle.
_RANKING_PRECISION = 6

#: The report status that makes a version *the* version of a past brief.
PUBLISHED_STATUS = "published"


# --------------------------------------------------------------------------------------
# Top Events
# --------------------------------------------------------------------------------------


def resolve_linked_risk(row: EventWindowRow) -> LinkedRisk:
    """The true maximum over every risk source actually linked to the event.

    Conservative in the precise sense the spec asks for: a source the event has no row in
    contributes *nothing* rather than a zero, so an event with one linked risk of 80 scores
    80 and is not averaged down to 20 by three absences. An event with no linked risk at all
    scores the spec's default of 0 -- and records `NONE`, so that 0 reads as "no risk was
    ever linked to this" and never as "this was measured and found safe".
    """
    candidates: dict[RiskProvenance, float | None] = {
        RiskProvenance.EVENT_OBSERVATION: row.observation_risk,
        RiskProvenance.RISK_WARNING: row.warning_risk,
        RiskProvenance.EVENT_COMPANY: row.company_risk,
        RiskProvenance.EVENT_INDUSTRY: row.industry_risk,
    }
    linked = [score for score in candidates.values() if score is not None]
    if not linked:
        return LinkedRisk(score=0.0, provenance=RiskProvenance.NONE)
    highest = max(linked)
    provenance = next(source for source in PROVENANCE_PRECEDENCE if candidates[source] == highest)
    return LinkedRisk(score=highest, provenance=provenance)


def ranking_score(hotness_score: float, max_linked_risk_score: float) -> float:
    """The spec's Top Events ranking: ``0.6 x hotness + 0.4 x max_linked_risk``."""
    weighted = HOTNESS_WEIGHT * hotness_score + LINKED_RISK_WEIGHT * max_linked_risk_score
    return round(weighted, _RANKING_PRECISION)


def _rank_key(event: SelectedEvent) -> tuple[float, float, str]:
    """A *total* order, so one window always ranks one way.

    Ranking score descending; ties to the higher source-credibility sum (the spec's rule);
    ties there to the event id, which settles nothing meaningful and is chosen precisely
    because it cannot: it is stable across runs, which insertion order and float noise are not.
    """
    return (-event.ranking_score, -event.credibility_sum, str(event.event_id))


def select_top_events(
    rows: Iterable[EventWindowRow],
    window: BriefWindow,
    *,
    limit: int = TOP_EVENT_COUNT,
) -> tuple[SelectedEvent, ...]:
    """Rank the window's qualifying events and take the top ``limit``.

    Window first, floor second, rank third. A NULL hotness is excluded with the sub-40 ones:
    an event nobody scored is not an event that scored well, and the brief does not promote
    what it cannot rank (see migration 0016 on why historical events carry no hotness).
    """
    qualifying: list[SelectedEvent] = []
    for row in rows:
        if not window.contains(row.updated_at):
            continue
        if row.hotness_score is None or row.hotness_score < HOTNESS_FLOOR:
            continue
        linked_risk = resolve_linked_risk(row)
        qualifying.append(
            SelectedEvent(
                rank=0,
                event_id=row.event_id,
                title=row.title,
                hotness_score=row.hotness_score,
                max_linked_risk=linked_risk,
                ranking_score=ranking_score(row.hotness_score, linked_risk.score),
                credibility_sum=row.credibility_sum,
                developing=window.is_developing(row.first_seen_at),
            )
        )
    qualifying.sort(key=_rank_key)
    return tuple(
        dataclasses.replace(event, rank=rank)
        for rank, event in enumerate(qualifying[:limit], start=1)
    )


# --------------------------------------------------------------------------------------
# Alert state changes
# --------------------------------------------------------------------------------------


def select_alert_state_changes(
    changes: Iterable[AlertStateChange], window: BriefWindow
) -> tuple[AlertStateChange, ...]:
    """Critical/High alert state changes in the window, all-clears included.

    Judged on `effective_severity`, which is why a resolved all-clear survives this filter at
    all: resolution decays an alert to Low, so a `severity`-only test would drop precisely the
    Critical alert the brief most needs to stand down. `peak_severity` is what remembers it.
    """
    selected = [
        change
        for change in changes
        if window.contains(change.changed_at) and change.effective_severity in BRIEF_SEVERITIES
    ]
    selected.sort(
        key=lambda change: (
            -severity_rank(change.effective_severity),
            change.changed_at,
            str(change.alert_id),
        )
    )
    return tuple(selected)


# --------------------------------------------------------------------------------------
# Risk radar and day-over-day movement
# --------------------------------------------------------------------------------------


def _latest_per_key(
    observations: Iterable[RiskObservationRow], *, cutoff: datetime.datetime
) -> dict[RiskKey, RiskRadarEntry]:
    """Each risk's standing score as of ``cutoff``: its last observation at or before it.

    Two observations of one key can share an `as_of` (they are unique per model_version, not
    per instant). The higher score wins -- deterministic, and the conservative direction for
    a risk table.
    """
    latest: dict[RiskKey, RiskRadarEntry] = {}
    for observation in observations:
        if observation.as_of > cutoff:
            continue
        standing = latest.get(observation.key)
        if standing is None or (observation.as_of, observation.score) > (
            standing.as_of,
            standing.score,
        ):
            latest[observation.key] = RiskRadarEntry(
                key=observation.key,
                score=observation.score,
                level=observation.level,
                as_of=observation.as_of,
            )
    return latest


def build_risk_radar(observations: Iterable[RiskObservationRow], window: BriefWindow) -> RiskRadar:
    """The risk table at both of the window's cutoffs, plus the moves between them.

    Cutoff semantics are the whole point: "current" is each risk's last observation at or
    before `window.end`, "previous" its last at or before `window.start`. Reading "previous"
    as yesterday's *radar row* instead would make a risk that was not re-scored today look
    like it moved to zero.
    """
    materialized = list(observations)
    current = _latest_per_key(materialized, cutoff=window.end)
    previous = _latest_per_key(materialized, cutoff=window.start)

    moves: list[RiskMove] = []
    for key, entry in current.items():
        standing = previous.get(key)
        if standing is None:
            # First observed inside this window: it has a score, but no move to measure.
            continue
        if standing.as_of == entry.as_of:
            # Not re-scored in the window: the same observation stands at both cutoffs.
            continue
        moves.append(RiskMove(key=key, current=entry, previous=standing))
    moves.sort(key=lambda move: (-move.magnitude, move.key))

    return RiskRadar(
        current=tuple(current[key] for key in sorted(current)),
        previous=tuple(previous[key] for key in sorted(previous)),
        moves=tuple(moves),
    )


def largest_risk_move(radar: RiskRadar) -> RiskMove | None:
    """The biggest move by absolute size, or ``None`` when nothing actually moved.

    A re-observation that landed on the same score is not a move, and reporting it as "the
    largest risk-score move" -- of zero -- would be worse than reporting no move at all.
    """
    for move in radar.moves:
        if move.magnitude > 0:
            return move
    return None


# --------------------------------------------------------------------------------------
# Prior brief
# --------------------------------------------------------------------------------------


def pick_published_version(
    versions: Iterable[PriorBriefVersion],
) -> PriorBriefVersion | None:
    """Yesterday's brief is its highest *published* version -- never merely its highest.

    A regeneration in flight (`generating`, `grounding_check`) or one that failed the
    grounding gate carries a higher version number than the brief that actually shipped, and
    neither is something today's brief may quote back to a reader as "yesterday we flagged X".
    A published version marked `stale` still counts: staleness means an upstream reprocess
    outdated it, not that we did not publish it, and the day-over-day rule is about what we
    said.
    """
    published = [version for version in versions if version.status == PUBLISHED_STATUS]
    if not published:
        return None
    return max(published, key=lambda version: version.version)


# --------------------------------------------------------------------------------------
# Facade
# --------------------------------------------------------------------------------------


def _data_quality_notes(
    *,
    event_rows: Sequence[EventWindowRow],
    top_events: Sequence[SelectedEvent],
    scan_truncated: bool,
    alert_changes: Sequence[AlertStateChange],
    observations: Sequence[RiskObservationRow],
    move: RiskMove | None,
    prior_brief: PriorBriefContext | None,
) -> tuple[DataQualityNote, ...]:
    """Declare what the brief could not draw on. ADR 0009: never present partial data as complete."""
    notes: list[DataQualityNote] = []

    if not event_rows:
        notes.append(
            DataQualityNote(
                DataQuality.NO_EVENTS_IN_WINDOW,
                "No events were updated in the window; the brief is a quiet day.",
            )
        )
    elif not top_events:
        notes.append(
            DataQualityNote(
                DataQuality.NO_EVENTS_ABOVE_HOTNESS_FLOOR,
                f"{len(event_rows)} event(s) in the window, none at or above hotness "
                f"{HOTNESS_FLOOR:g}; Top Events is empty rather than padded.",
            )
        )

    unscored = [row for row in event_rows if row.hotness_score is None]
    if unscored:
        notes.append(
            DataQualityNote(
                DataQuality.EVENTS_MISSING_HOTNESS,
                f"{len(unscored)} event(s) in the window carry no hotness score and were "
                "excluded from ranking; they predate the score (migration 0016).",
            )
        )

    undated = [row for row in event_rows if row.first_seen_at is None]
    if undated:
        notes.append(
            DataQualityNote(
                DataQuality.EVENTS_MISSING_START_TIME,
                f"{len(undated)} event(s) have no first_seen_at; they cannot be marked "
                "developing and are reported as new.",
            )
        )

    if scan_truncated:
        notes.append(
            DataQualityNote(
                DataQuality.EVENT_SCAN_TRUNCATED,
                f"The window's event scan hit its {len(event_rows)}-row bound; ranking saw "
                "the hottest events but may not have seen every event.",
            )
        )

    if not alert_changes:
        notes.append(
            DataQualityNote(
                DataQuality.NO_ALERT_STATE_CHANGES,
                "No Critical or High alert changed state in the window.",
            )
        )

    if not observations:
        notes.append(
            DataQualityNote(
                DataQuality.NO_RISK_OBSERVATIONS,
                "No risk observations are available; the risk radar is empty.",
            )
        )
    elif move is None:
        notes.append(
            DataQualityNote(
                DataQuality.NO_RISK_MOVE,
                "No risk was re-scored to a different value across the window.",
            )
        )

    if prior_brief is None:
        notes.append(
            DataQualityNote(
                DataQuality.NO_PRIOR_BRIEF,
                "No published brief exists for the previous day; day-over-day context is "
                "unavailable.",
            )
        )
    elif not prior_brief.key_claim_ids:
        notes.append(
            DataQualityNote(
                DataQuality.NO_PRIOR_BRIEF_CLAIMS,
                f"The previous brief (version {prior_brief.version}) cites no claims; its "
                "key-claim context is empty.",
            )
        )

    return tuple(notes)


def build_brief_inputs(repository: BriefInputRepository, window: BriefWindow) -> BriefInputs:
    """Select every deterministic input for one daily brief.

    The composition stage's entry point, and the only function it needs: it hands over a
    window, and gets back the ranked events, the executive summary's inputs, the risk radar
    with yesterday's comparison, yesterday's published brief, and an explicit list of what was
    missing. Historical analogies are not selected here -- Stage 5 is consumed downstream, at
    composition.
    """
    event_rows = repository.events_in_window(window)
    alert_rows = repository.alert_state_changes(window)
    observations = repository.risk_observations(window)

    top_events = select_top_events(event_rows, window)
    alert_changes = select_alert_state_changes(alert_rows, window)
    radar = build_risk_radar(observations, window)
    move = largest_risk_move(radar)

    prior_brief = _load_prior_brief(repository, window.brief_date - ONE_DAY)

    return BriefInputs(
        window=window,
        top_events=top_events,
        executive_summary=ExecutiveSummaryInputs(
            alert_state_changes=alert_changes,
            top_events=top_events[:EXEC_SUMMARY_EVENT_COUNT],
            largest_risk_move=move,
        ),
        risk_radar=radar,
        prior_brief=prior_brief,
        data_quality_notes=_data_quality_notes(
            event_rows=event_rows,
            top_events=top_events,
            # A bound that was reached is a bound that may have cut something off, and a
            # truncated scan that says nothing reads exactly like a complete one.
            scan_truncated=len(event_rows) >= repository.event_scan_limit,
            alert_changes=alert_changes,
            observations=observations,
            move=move,
            prior_brief=prior_brief,
        ),
    )


def _load_prior_brief(
    repository: BriefInputRepository, brief_date: datetime.date
) -> PriorBriefContext | None:
    """Yesterday's published brief, read-only: sections are copied out and frozen."""
    chosen = pick_published_version(repository.prior_brief_versions(brief_date))
    if chosen is None:
        return None
    return PriorBriefContext(
        report_id=chosen.report_id,
        brief_date=chosen.brief_date,
        version=chosen.version,
        sections=repository.brief_sections(chosen.report_id),
    )
