"""Daily-brief input selection: ranking, tie-breaks, alerts, radar, quiet days.

DB-free by construction. Every rule under test is a pure function over rows, so the rows are
hand-built here and the SQL that really loads them is exercised separately in
tests/integration/test_stage6_brief_selection.py.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from services.reports.contracts import (
    AlertStateChange,
    DataQuality,
    EventWindowRow,
    PriorBriefSection,
    PriorBriefVersion,
    RiskKey,
    RiskObservationRow,
)
from services.reports.selection import (
    EXEC_SUMMARY_EVENT_COUNT,
    HOTNESS_FLOOR,
    TOP_EVENT_COUNT,
    build_brief_inputs,
    build_risk_radar,
    largest_risk_move,
    pick_published_version,
    ranking_score,
    resolve_linked_risk,
    select_alert_state_changes,
    select_top_events,
)
from services.reports.window import window_for_date

UTC = datetime.UTC
BRIEF_DATE = datetime.date(2026, 7, 14)
WINDOW = window_for_date(BRIEF_DATE)
INSIDE = WINDOW.end - datetime.timedelta(hours=3)
BEFORE = WINDOW.start - datetime.timedelta(hours=3)


def _uuid(tag: int) -> uuid.UUID:
    return uuid.UUID(int=tag)


#: Distinguishes "the caller did not set a start time" from "the caller set it to None",
#: which is the difference between a default and the missing-data case under test.
_UNSET = object()


def _event(
    tag: int,
    *,
    hotness: float | None = 80.0,
    updated_at: datetime.datetime | None = None,
    first_seen_at: datetime.datetime | None | object = _UNSET,
    credibility: float = 0.0,
    observation_risk: float | None = None,
    warning_risk: float | None = None,
    company_risk: float | None = None,
    industry_risk: float | None = None,
) -> EventWindowRow:
    began_at = INSIDE if first_seen_at is _UNSET else first_seen_at
    return EventWindowRow(
        event_id=_uuid(tag),
        title=f"event {tag}",
        hotness_score=hotness,
        updated_at=updated_at or INSIDE,
        first_seen_at=began_at,  # type: ignore[arg-type]
        credibility_sum=credibility,
        observation_risk=observation_risk,
        warning_risk=warning_risk,
        company_risk=company_risk,
        industry_risk=industry_risk,
    )


def _alert(
    tag: int,
    *,
    state: str = "open",
    severity: str = "high",
    peak: str | None = None,
    changed_at: datetime.datetime | None = None,
) -> AlertStateChange:
    return AlertStateChange(
        alert_id=_uuid(tag),
        title=f"alert {tag}",
        state=state,
        severity=severity,
        peak_severity=peak,
        changed_at=changed_at or INSIDE,
    )


def _observation(
    target_id: str, score: float, level: str, as_of: datetime.datetime
) -> RiskObservationRow:
    return RiskObservationRow(
        key=RiskKey(target_type="country", target_id=target_id, risk_type="banking"),
        score=score,
        level=level,
        as_of=as_of,
    )


# --------------------------------------------------------------------------------------
# The hotness floor
# --------------------------------------------------------------------------------------


def test_events_below_the_hotness_floor_never_qualify() -> None:
    rows = [_event(1, hotness=HOTNESS_FLOOR - 0.01), _event(2, hotness=HOTNESS_FLOOR)]
    selected = select_top_events(rows, WINDOW)
    # The floor is inclusive at 40: "below hotness 40 never qualify".
    assert [event.event_id for event in selected] == [_uuid(2)]


def test_events_with_no_hotness_are_excluded_not_defaulted() -> None:
    # An unscored event is not a low-scoring event; it is unrankable, and the brief will not
    # promote what it cannot rank (these are the pre-migration-0016 rows).
    assert select_top_events([_event(1, hotness=None)], WINDOW) == ()


def test_a_thin_day_shrinks_rather_than_padding() -> None:
    rows = [_event(tag, hotness=12.0) for tag in range(1, 9)]
    assert select_top_events(rows, WINDOW) == ()


# --------------------------------------------------------------------------------------
# The window filter
# --------------------------------------------------------------------------------------


def test_selection_filters_to_the_window_at_both_boundaries() -> None:
    tick = datetime.timedelta(microseconds=1)
    rows = [
        _event(1, updated_at=WINDOW.start),  # previous brief's
        _event(2, updated_at=WINDOW.start + tick),  # first instant of this one
        _event(3, updated_at=WINDOW.end),  # the cutoff is inclusive
        _event(4, updated_at=WINDOW.end + tick),  # next brief's
    ]
    selected = select_top_events(rows, WINDOW)
    assert {event.event_id for event in selected} == {_uuid(2), _uuid(3)}


def test_developing_marks_events_that_began_before_the_window() -> None:
    rows = [
        _event(1, first_seen_at=BEFORE),
        _event(2, first_seen_at=INSIDE),
        _event(3, first_seen_at=None),
    ]
    developing = {event.event_id: event.developing for event in select_top_events(rows, WINDOW)}
    assert developing == {_uuid(1): True, _uuid(2): False, _uuid(3): False}


# --------------------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------------------


def test_ranking_score_is_exactly_the_specs_weighting() -> None:
    assert ranking_score(80.0, 60.0) == pytest.approx(0.6 * 80.0 + 0.4 * 60.0)
    assert ranking_score(80.0, 60.0) == pytest.approx(72.0)
    assert ranking_score(100.0, 0.0) == pytest.approx(60.0)
    assert ranking_score(0.0, 100.0) == pytest.approx(40.0)


def test_linked_risk_can_outrank_raw_hotness() -> None:
    # 0.6*45 + 0.4*100 = 67 beats 0.6*90 + 0.4*0 = 54. The 0.4 weight has to actually bite.
    rows = [
        _event(1, hotness=90.0),
        _event(2, hotness=45.0, observation_risk=100.0),
    ]
    selected = select_top_events(rows, WINDOW)
    assert [event.event_id for event in selected] == [_uuid(2), _uuid(1)]
    assert selected[0].ranking_score == pytest.approx(67.0)
    assert selected[1].ranking_score == pytest.approx(54.0)


def test_only_the_top_five_are_taken_and_ranks_are_dense_from_one() -> None:
    rows = [_event(tag, hotness=float(50 + tag)) for tag in range(1, 10)]
    selected = select_top_events(rows, WINDOW)
    assert len(selected) == TOP_EVENT_COUNT
    assert [event.rank for event in selected] == [1, 2, 3, 4, 5]
    # Highest hotness first, since no event carries linked risk.
    assert [event.event_id for event in selected] == [_uuid(t) for t in (9, 8, 7, 6, 5)]


# --------------------------------------------------------------------------------------
# Tie-breaks
# --------------------------------------------------------------------------------------


def test_ties_break_on_source_credibility_sum_not_on_source_count() -> None:
    rows = [
        _event(1, hotness=70.0, credibility=1.8),
        _event(2, hotness=70.0, credibility=4.2),
        _event(3, hotness=70.0, credibility=0.4),
    ]
    selected = select_top_events(rows, WINDOW)
    assert [event.event_id for event in selected] == [_uuid(2), _uuid(1), _uuid(3)]


def test_a_credibility_tie_breaks_on_a_stable_final_key() -> None:
    # Everything equal: the order must still be total, and the same on every run.
    rows = [_event(tag, hotness=70.0, credibility=2.0) for tag in (7, 3, 9, 1)]
    first = select_top_events(rows, WINDOW)
    second = select_top_events(list(reversed(rows)), WINDOW)
    assert [event.event_id for event in first] == [_uuid(t) for t in (1, 3, 7, 9)]
    assert first == second


def test_float_noise_does_not_silently_lose_a_tie_break() -> None:
    # 0.6*61.0 and 0.6*61.0 must compare equal so the credibility sum settles the tie.
    rows = [
        _event(1, hotness=61.0, credibility=0.5),
        _event(2, hotness=61.0, credibility=9.0),
    ]
    selected = select_top_events(rows, WINDOW)
    assert selected[0].ranking_score == selected[1].ranking_score
    assert selected[0].event_id == _uuid(2)


# --------------------------------------------------------------------------------------
# The linked-risk maximum
# --------------------------------------------------------------------------------------


def test_linked_risk_is_the_true_maximum_across_every_source() -> None:
    row = _event(1, observation_risk=30.0, warning_risk=88.0, company_risk=55.0, industry_risk=12.0)
    risk = resolve_linked_risk(row)
    assert risk.score == 88.0
    assert risk.provenance == "risk_warning"


def test_an_absent_risk_source_does_not_drag_the_maximum_down() -> None:
    # One linked risk of 80 and three absences is a risk of 80, not an average of 20.
    risk = resolve_linked_risk(_event(1, company_risk=80.0))
    assert risk.score == 80.0
    assert risk.provenance == "event_company"


def test_no_linked_risk_defaults_to_zero_and_records_that_it_is_an_absence() -> None:
    risk = resolve_linked_risk(_event(1))
    assert risk.score == 0.0
    # The 0 reads as "nothing was ever linked", never as "measured and found safe".
    assert risk.provenance == "none"


def test_a_genuine_linked_zero_is_not_confused_with_an_absence() -> None:
    risk = resolve_linked_risk(_event(1, industry_risk=0.0))
    assert risk.score == 0.0
    assert risk.provenance == "event_industry"


def test_equal_maxima_break_toward_the_most_direct_provenance() -> None:
    row = _event(1, observation_risk=70.0, warning_risk=70.0, industry_risk=70.0)
    assert resolve_linked_risk(row).provenance == "event_observation"


def test_the_selected_event_carries_its_risk_provenance() -> None:
    selected = select_top_events([_event(1, hotness=60.0, warning_risk=90.0)], WINDOW)
    assert selected[0].max_linked_risk_score == 90.0
    assert selected[0].max_linked_risk.provenance == "risk_warning"


# --------------------------------------------------------------------------------------
# Alert state changes
# --------------------------------------------------------------------------------------


def test_only_critical_and_high_alert_changes_reach_the_summary() -> None:
    changes = [
        _alert(1, severity="critical"),
        _alert(2, severity="high"),
        _alert(3, severity="medium"),
        _alert(4, severity="low"),
    ]
    selected = select_alert_state_changes(changes, WINDOW)
    assert [change.alert_id for change in selected] == [_uuid(1), _uuid(2)]


def test_a_resolved_all_clear_survives_via_peak_severity() -> None:
    # ADR 0010 only resolves an alert once it has decayed to Low, so the all-clear on a
    # Critical alert reads `severity='low'`. Filtering on `severity` alone would drop exactly
    # the stand-down the brief exists to report; `peak_severity` is what remembers it.
    all_clear = _alert(1, state="resolved", severity="low", peak="critical")
    selected = select_alert_state_changes([all_clear], WINDOW)
    assert [change.alert_id for change in selected] == [_uuid(1)]
    assert selected[0].is_all_clear
    assert selected[0].effective_severity == "critical"


def test_an_alert_that_never_reached_high_is_not_reported_on_resolution() -> None:
    quiet = _alert(1, state="resolved", severity="low", peak="medium")
    assert select_alert_state_changes([quiet], WINDOW) == ()


def test_a_null_peak_falls_back_to_the_rows_severity() -> None:
    # Alerts predating `peak_severity` recorded no peak; the alert service reads NULL as the
    # row's severity, and so does the brief.
    legacy = _alert(1, severity="critical", peak=None)
    assert select_alert_state_changes([legacy], WINDOW)[0].effective_severity == "critical"


def test_alert_changes_outside_the_window_are_excluded() -> None:
    outside = _alert(1, severity="critical", changed_at=BEFORE)
    inside = _alert(2, severity="critical", changed_at=WINDOW.end)
    selected = select_alert_state_changes([outside, inside], WINDOW)
    assert [change.alert_id for change in selected] == [_uuid(2)]


def test_alert_changes_are_ordered_most_severe_first_and_deterministically() -> None:
    changes = [
        _alert(1, severity="high", changed_at=INSIDE),
        _alert(2, state="resolved", severity="low", peak="critical", changed_at=INSIDE),
        _alert(3, severity="high", changed_at=INSIDE - datetime.timedelta(hours=1)),
    ]
    selected = select_alert_state_changes(changes, WINDOW)
    assert [change.alert_id for change in selected] == [_uuid(2), _uuid(3), _uuid(1)]
    assert select_alert_state_changes(list(reversed(changes)), WINDOW) == selected


# --------------------------------------------------------------------------------------
# Risk radar and the largest move
# --------------------------------------------------------------------------------------


def test_radar_reads_each_risk_at_both_cutoffs() -> None:
    observations = [
        _observation("TR", 40.0, "medium", WINDOW.start - datetime.timedelta(days=2)),
        _observation("TR", 75.0, "high", INSIDE),
    ]
    radar = build_risk_radar(observations, WINDOW)
    assert [entry.score for entry in radar.current] == [75.0]
    assert [entry.score for entry in radar.previous] == [40.0]
    assert len(radar.moves) == 1
    assert radar.moves[0].delta == 35.0
    assert radar.moves[0].is_reversal  # medium -> high


def test_previous_is_the_last_observation_before_the_cutoff_not_yesterdays_row() -> None:
    # A risk not re-scored during the window still has a standing score; it must not read as
    # having moved to zero.
    stale = _observation("AR", 62.0, "high", WINDOW.start - datetime.timedelta(days=9))
    radar = build_risk_radar([stale], WINDOW)
    assert [entry.score for entry in radar.current] == [62.0]
    assert [entry.score for entry in radar.previous] == [62.0]
    assert radar.moves == ()  # the same observation stands at both cutoffs: no move


def test_observations_after_the_cutoff_are_invisible_to_the_brief() -> None:
    # A brief regenerated a week later must select what it would have selected that morning.
    future = _observation("TR", 99.0, "critical", WINDOW.end + datetime.timedelta(hours=1))
    current = _observation("TR", 50.0, "medium", INSIDE)
    radar = build_risk_radar([future, current], WINDOW)
    assert [entry.score for entry in radar.current] == [50.0]


def test_a_risk_first_seen_inside_the_window_has_no_move_to_report() -> None:
    fresh = _observation("BR", 70.0, "high", INSIDE)
    radar = build_risk_radar([fresh], WINDOW)
    assert [entry.score for entry in radar.current] == [70.0]
    assert radar.previous == ()
    # Inventing a 0 -> 70 jump for it would be the largest fake move on the board.
    assert radar.moves == ()
    assert largest_risk_move(radar) is None


def test_largest_risk_move_is_the_biggest_by_absolute_size() -> None:
    earlier = WINDOW.start - datetime.timedelta(days=1)
    observations = [
        _observation("TR", 30.0, "medium", earlier),
        _observation("TR", 45.0, "medium", INSIDE),  # +15
        _observation("AR", 80.0, "critical", earlier),
        _observation("AR", 50.0, "medium", INSIDE),  # -30, the largest
        _observation("BR", 60.0, "high", earlier),
        _observation("BR", 65.0, "high", INSIDE),  # +5
    ]
    move = largest_risk_move(build_risk_radar(observations, WINDOW))
    assert move is not None
    assert move.key.target_id == "AR"
    assert move.delta == -30.0  # an easing risk is a move, and the sign survives
    assert move.magnitude == 30.0


def test_a_rescore_to_the_same_value_is_not_a_move() -> None:
    observations = [
        _observation("TR", 55.0, "medium", WINDOW.start - datetime.timedelta(days=1)),
        _observation("TR", 55.0, "medium", INSIDE),
    ]
    radar = build_risk_radar(observations, WINDOW)
    assert radar.moves[0].delta == 0.0
    # Reporting "the largest risk-score move" -- of zero -- is worse than reporting none.
    assert largest_risk_move(radar) is None


def test_radar_is_deterministic_regardless_of_row_order() -> None:
    observations = [
        _observation("TR", 30.0, "medium", WINDOW.start - datetime.timedelta(days=1)),
        _observation("AR", 80.0, "critical", WINDOW.start - datetime.timedelta(days=1)),
        _observation("TR", 45.0, "medium", INSIDE),
        _observation("AR", 50.0, "medium", INSIDE),
    ]
    assert build_risk_radar(observations, WINDOW) == build_risk_radar(
        list(reversed(observations)), WINDOW
    )


# --------------------------------------------------------------------------------------
# Yesterday's brief
# --------------------------------------------------------------------------------------


def _version(version: int, status: str) -> PriorBriefVersion:
    return PriorBriefVersion(
        report_id=_uuid(100 + version),
        brief_date=BRIEF_DATE - datetime.timedelta(days=1),
        version=version,
        status=status,
    )


def test_the_prior_brief_is_the_latest_published_version() -> None:
    chosen = pick_published_version([_version(1, "published"), _version(2, "published")])
    assert chosen is not None
    assert chosen.version == 2


def test_a_newer_unpublished_version_never_displaces_the_published_one() -> None:
    # A regeneration in flight, or one that failed the grounding gate, carries a higher
    # version than the brief that actually shipped -- and is not what we said yesterday.
    versions = [
        _version(1, "published"),
        _version(2, "published"),
        _version(3, "generating"),
        _version(4, "failed"),
        _version(5, "grounding_check"),
    ]
    chosen = pick_published_version(versions)
    assert chosen is not None
    assert chosen.version == 2


def test_no_published_version_yields_no_prior_context() -> None:
    assert pick_published_version([_version(1, "generating")]) is None
    assert pick_published_version([]) is None


# --------------------------------------------------------------------------------------
# The facade, and the absences it declares
# --------------------------------------------------------------------------------------


class _StubRepository:
    """A canned loader. It decides nothing -- which is the point of the split."""

    event_scan_limit = 500

    def __init__(
        self,
        *,
        events: list[EventWindowRow] | None = None,
        alerts: list[AlertStateChange] | None = None,
        observations: list[RiskObservationRow] | None = None,
        versions: list[PriorBriefVersion] | None = None,
        sections: list[PriorBriefSection] | None = None,
    ) -> None:
        self._events = events or []
        self._alerts = alerts or []
        self._observations = observations or []
        self._versions = versions or []
        self._sections = sections or []
        self.sections_loaded_for: list[uuid.UUID] = []

    def events_in_window(self, window):  # noqa: ANN001, ANN201
        return tuple(self._events)

    def alert_state_changes(self, window):  # noqa: ANN001, ANN201
        return tuple(self._alerts)

    def risk_observations(self, window):  # noqa: ANN001, ANN201
        return tuple(self._observations)

    def prior_brief_versions(self, brief_date):  # noqa: ANN001, ANN201
        return tuple(self._versions)

    def brief_sections(self, report_id):  # noqa: ANN001, ANN201
        self.sections_loaded_for.append(report_id)
        return tuple(self._sections)


def _codes(inputs) -> set[str]:  # noqa: ANN001
    return {note.code for note in inputs.data_quality_notes}


def test_a_quiet_day_is_a_valid_brief_with_an_explicit_note() -> None:
    inputs = build_brief_inputs(_StubRepository(), WINDOW)
    assert inputs.is_quiet_day
    assert inputs.top_events == ()
    assert DataQuality.NO_EVENTS_IN_WINDOW in _codes(inputs)
    assert DataQuality.NO_ALERT_STATE_CHANGES in _codes(inputs)
    assert DataQuality.NO_RISK_OBSERVATIONS in _codes(inputs)
    assert DataQuality.NO_PRIOR_BRIEF in _codes(inputs)


def test_events_that_all_miss_the_floor_are_declared_not_padded() -> None:
    repository = _StubRepository(events=[_event(1, hotness=20.0), _event(2, hotness=39.9)])
    inputs = build_brief_inputs(repository, WINDOW)
    assert inputs.top_events == ()
    assert DataQuality.NO_EVENTS_ABOVE_HOTNESS_FLOOR in _codes(inputs)
    assert DataQuality.NO_EVENTS_IN_WINDOW not in _codes(inputs)


def test_unscored_and_undated_events_are_declared() -> None:
    repository = _StubRepository(
        events=[_event(1, hotness=None), _event(2, hotness=90.0, first_seen_at=None)]
    )
    inputs = build_brief_inputs(repository, WINDOW)
    assert DataQuality.EVENTS_MISSING_HOTNESS in _codes(inputs)
    assert DataQuality.EVENTS_MISSING_START_TIME in _codes(inputs)


def test_a_truncated_event_scan_is_never_silent() -> None:
    repository = _StubRepository(events=[_event(tag, hotness=90.0) for tag in range(1, 6)])
    repository.event_scan_limit = 5  # the scan came back exactly full
    assert DataQuality.EVENT_SCAN_TRUNCATED in _codes(build_brief_inputs(repository, WINDOW))


def test_the_executive_summary_takes_the_top_two_events() -> None:
    repository = _StubRepository(
        events=[_event(tag, hotness=float(50 + tag)) for tag in range(1, 6)],
        alerts=[_alert(9, severity="critical")],
        observations=[
            _observation("TR", 20.0, "low", WINDOW.start - datetime.timedelta(days=1)),
            _observation("TR", 66.0, "high", INSIDE),
        ],
    )
    summary = build_brief_inputs(repository, WINDOW).executive_summary
    assert len(summary.top_events) == EXEC_SUMMARY_EVENT_COUNT
    assert [event.event_id for event in summary.top_events] == [_uuid(5), _uuid(4)]
    assert [change.alert_id for change in summary.alert_state_changes] == [_uuid(9)]
    assert summary.largest_risk_move is not None
    assert summary.largest_risk_move.delta == 46.0


def test_prior_brief_context_is_loaded_for_the_published_version_only() -> None:
    repository = _StubRepository(
        versions=[_version(1, "published"), _version(2, "published"), _version(3, "generating")],
        sections=[
            PriorBriefSection(1, "Risk radar", "body", (_uuid(201), _uuid(202))),
            PriorBriefSection(2, "Top events", "body", (_uuid(202),)),
        ],
    )
    inputs = build_brief_inputs(repository, WINDOW)
    assert inputs.prior_brief is not None
    assert inputs.prior_brief.version == 2
    # Sections were fetched for v2's report id, not v3's.
    assert repository.sections_loaded_for == [_uuid(102)]
    # Claims are de-duplicated across sections, in section order.
    assert inputs.prior_brief.key_claim_ids == (_uuid(201), _uuid(202))
    assert DataQuality.NO_PRIOR_BRIEF not in _codes(inputs)


def test_a_prior_brief_citing_no_claims_is_declared() -> None:
    repository = _StubRepository(
        versions=[_version(1, "published")],
        sections=[PriorBriefSection(1, "Risk radar", "body", ())],
    )
    inputs = build_brief_inputs(repository, WINDOW)
    assert inputs.prior_brief is not None
    assert inputs.prior_brief.key_claim_ids == ()
    assert DataQuality.NO_PRIOR_BRIEF_CLAIMS in _codes(inputs)


def test_brief_inputs_are_immutable() -> None:
    inputs = build_brief_inputs(_StubRepository(events=[_event(1)]), WINDOW)
    with pytest.raises(AttributeError):
        inputs.top_events = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        inputs.top_events[0].rank = 99  # type: ignore[misc]
