"""Brief section material: stable order, quiet-day skeleton, tables, exact disclaimer.

DB-free: pure over hand-built :class:`BriefInputs` and :class:`BriefContext`.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping

import pytest

from services.reports.context import BriefContext, EventForecast, ForecastScenarioRow
from services.reports.contracts import (
    AlertStateChange,
    BriefInputs,
    DataQuality,
    DataQualityNote,
    ExecutiveSummaryInputs,
    LinkedRisk,
    RiskKey,
    RiskMove,
    RiskProvenance,
    RiskRadar,
    RiskRadarEntry,
    SelectedEvent,
)
from services.reports.material import (
    FINAL_DISCLAIMER,
    SECTION_ORDER,
    SectionKind,
    build_brief_material,
)
from services.reports.window import window_for_date

UTC = datetime.UTC
BRIEF_DATE = datetime.date(2026, 7, 14)
WINDOW = window_for_date(BRIEF_DATE)
NOW = WINDOW.end - datetime.timedelta(hours=1)


def _selected(
    title: str = "Event", rank: int = 1, event_id: uuid.UUID | None = None
) -> SelectedEvent:
    return SelectedEvent(
        rank=rank,
        event_id=event_id or uuid.uuid4(),
        title=title,
        hotness_score=70.0,
        max_linked_risk=LinkedRisk(score=50.0, provenance=RiskProvenance.EVENT_OBSERVATION),
        ranking_score=62.0,
        credibility_sum=1.0,
        developing=False,
    )


def _entry(key: RiskKey, score: float, level: str, when: datetime.datetime) -> RiskRadarEntry:
    return RiskRadarEntry(key=key, score=score, level=level, as_of=when)


def _radar(*, current=(), previous=(), moves=()) -> RiskRadar:
    return RiskRadar(current=tuple(current), previous=tuple(previous), moves=tuple(moves))


def _inputs(
    *,
    top_events=(),
    alert_changes=(),
    radar: RiskRadar | None = None,
    prior_brief=None,
    notes=(),
    move: RiskMove | None = None,
) -> BriefInputs:
    radar = radar or _radar()
    return BriefInputs(
        window=WINDOW,
        top_events=tuple(top_events),
        executive_summary=ExecutiveSummaryInputs(
            alert_state_changes=tuple(alert_changes),
            top_events=tuple(top_events[:2]),
            largest_risk_move=move,
        ),
        risk_radar=radar,
        prior_brief=prior_brief,
        data_quality_notes=tuple(notes),
    )


# --------------------------------------------------------------------------------------
# Section order and disclaimer
# --------------------------------------------------------------------------------------


def test_section_order_is_stable_and_disclaimer_is_last() -> None:
    assert SECTION_ORDER[0] is SectionKind.EXECUTIVE_SUMMARY
    assert SECTION_ORDER[-1] is SectionKind.DISCLAIMER


def test_disclaimer_is_exact_immutable_final_material() -> None:
    material = build_brief_material(_inputs(), BriefContext())
    disclaimer = material.disclaimer
    assert disclaimer.kind is SectionKind.DISCLAIMER
    assert material.sections[-1] is disclaimer
    assert disclaimer.prose == (
        "This system provides probabilistic analysis based on public information, data "
        "signals, and model-assisted reasoning. It is for informational purposes only and "
        "does not constitute investment, legal, or financial advice."
    )
    assert disclaimer.prose == FINAL_DISCLAIMER
    assert disclaimer.is_placeholder is False


def test_no_watchlist_section_exists() -> None:
    assert not any(k.value == "watchlist" for k in SectionKind)


def test_produced_sections_follow_the_canonical_relative_order() -> None:
    event = _selected()
    material = build_brief_material(
        _inputs(
            top_events=[event],
            alert_changes=[_alert(is_all_clear=False)],
            radar=_radar(current=[_entry(RiskKey("country", "US", "banking"), 60.0, "high", NOW)]),
        ),
        BriefContext(),
    )
    positions = {section.kind: i for i, section in enumerate(material.sections)}
    ordered_present = [k for k in SECTION_ORDER if k in positions]
    assert [material.sections[positions[k]].kind for k in ordered_present] == ordered_present


def test_section_order_field_is_sequential() -> None:
    material = build_brief_material(_inputs(), BriefContext())
    assert [s.order for s in material.sections] == list(range(1, len(material.sections) + 1))


# --------------------------------------------------------------------------------------
# Quiet day
# --------------------------------------------------------------------------------------


def test_quiet_day_has_no_top_events_but_keeps_radar_what_changed_quality_disclaimer() -> None:
    radar = _radar(current=[_entry(RiskKey("country", "US", "banking"), 45.0, "medium", NOW)])
    material = build_brief_material(
        _inputs(
            top_events=[],
            radar=radar,
            notes=[DataQualityNote(DataQuality.NO_EVENTS_ABOVE_HOTNESS_FLOOR, "quiet")],
        ),
        BriefContext(),
    )
    kinds = material.kinds
    assert SectionKind.TOP_EVENT not in kinds
    assert SectionKind.RISK_RADAR in kinds
    assert SectionKind.WHAT_CHANGED in kinds
    assert SectionKind.DATA_QUALITY in kinds
    assert kinds[-1] is SectionKind.DISCLAIMER


def test_top_events_are_never_padded_beyond_selection() -> None:
    events = [_selected(title=f"E{i}", rank=i) for i in range(1, 4)]
    material = build_brief_material(_inputs(top_events=events), BriefContext())
    assert len(material.of_kind(SectionKind.TOP_EVENT)) == 3


# --------------------------------------------------------------------------------------
# Risk radar rows
# --------------------------------------------------------------------------------------


def test_risk_rows_include_standing_risks_that_did_not_move() -> None:
    key = RiskKey("country", "US", "banking")
    radar = _radar(
        current=[_entry(key, 60.0, "high", NOW)],
        previous=[_entry(key, 60.0, "high", NOW - datetime.timedelta(days=1))],
    )
    material = build_brief_material(_inputs(radar=radar), BriefContext())
    (section,) = material.of_kind(SectionKind.RISK_RADAR)
    (row,) = section.risk_rows
    assert row.score == 60.0
    assert row.delta == 0.0
    assert row.comparison_available is True


def test_risk_row_delta_is_none_for_a_risk_first_seen_in_window() -> None:
    key = RiskKey("country", "US", "banking")
    radar = _radar(current=[_entry(key, 60.0, "high", NOW)], previous=[])
    material = build_brief_material(_inputs(radar=radar), BriefContext())
    (section,) = material.of_kind(SectionKind.RISK_RADAR)
    (row,) = section.risk_rows
    assert row.delta is None
    assert row.comparison_available is False


# --------------------------------------------------------------------------------------
# Alerts list, all-clears preserved
# --------------------------------------------------------------------------------------


def _alert(*, is_all_clear: bool, severity: str = "critical") -> AlertStateChange:
    return AlertStateChange(
        alert_id=uuid.uuid4(),
        title="Bank run risk",
        state="resolved" if is_all_clear else "open",
        severity="low" if is_all_clear else severity,
        peak_severity=severity if is_all_clear else None,
        changed_at=NOW,
        related_event_id=None,
    )


def test_alerts_list_preserves_resolved_all_clears() -> None:
    material = build_brief_material(
        _inputs(alert_changes=[_alert(is_all_clear=True), _alert(is_all_clear=False)]),
        BriefContext(),
    )
    (section,) = material.of_kind(SectionKind.ALERTS)
    all_clears = [row for row in section.alert_rows if row.is_all_clear]
    assert len(all_clears) == 1
    # Judged at peak severity, not the decayed Low.
    assert all_clears[0].severity == "critical"


def test_alerts_section_omitted_when_no_changes() -> None:
    material = build_brief_material(_inputs(alert_changes=[]), BriefContext())
    assert material.of_kind(SectionKind.ALERTS) == ()


# --------------------------------------------------------------------------------------
# What changed since yesterday
# --------------------------------------------------------------------------------------


def _move(*, prev_level: str, cur_level: str, prev: float, cur: float) -> RiskMove:
    key = RiskKey("country", "US", "banking")
    return RiskMove(
        key=key,
        current=_entry(key, cur, cur_level, NOW),
        previous=_entry(key, prev, prev_level, NOW - datetime.timedelta(days=1)),
    )


def test_what_changed_is_always_present() -> None:
    material = build_brief_material(_inputs(), BriefContext())
    assert SectionKind.WHAT_CHANGED in material.kinds


def test_what_changed_flags_reversals_and_signed_deltas() -> None:
    move = _move(prev_level="medium", cur_level="high", prev=50.0, cur=62.0)
    material = build_brief_material(_inputs(radar=_radar(moves=[move])), BriefContext())
    (section,) = material.of_kind(SectionKind.WHAT_CHANGED)
    (row,) = section.change_rows
    assert row.is_reversal is True
    assert row.delta == 12.0
    assert row.direction == "rose"


def test_what_changed_orders_reversals_first() -> None:
    reversal = _move(prev_level="medium", cur_level="high", prev=50.0, cur=57.0)
    bigger_non_reversal = _move(prev_level="high", cur_level="high", prev=60.0, cur=90.0)
    material = build_brief_material(
        _inputs(radar=_radar(moves=[bigger_non_reversal, reversal])), BriefContext()
    )
    (section,) = material.of_kind(SectionKind.WHAT_CHANGED)
    assert section.change_rows[0].is_reversal is True


def test_what_changed_states_when_no_prior_brief() -> None:
    material = build_brief_material(_inputs(prior_brief=None), BriefContext())
    (section,) = material.of_kind(SectionKind.WHAT_CHANGED)
    assert any("previous day" in note for note in section.notes)


def test_what_changed_states_when_nothing_could_be_measured() -> None:
    material = build_brief_material(_inputs(radar=_radar(moves=[])), BriefContext())
    (section,) = material.of_kind(SectionKind.WHAT_CHANGED)
    assert any("day-over-day movement" in note for note in section.notes)


def test_no_prior_comparison_becomes_a_quality_note() -> None:
    material = build_brief_material(
        _inputs(prior_brief=None, radar=_radar(moves=[])), BriefContext()
    )
    (quality,) = material.of_kind(SectionKind.DATA_QUALITY)
    assert any(n.code is DataQuality.NO_PRIOR_COMPARISON for n in quality.quality_notes)


# --------------------------------------------------------------------------------------
# Forecast table only
# --------------------------------------------------------------------------------------


def test_forecasts_section_is_table_rows_only() -> None:
    event = _selected()
    forecast = EventForecast(
        event_id=event.event_id,
        scenario_set_id=uuid.uuid4(),
        scenarios=(
            ForecastScenarioRow(
                event_id=event.event_id,
                scenario_set_id=uuid.uuid4(),
                scenario_name="base_case",
                probability=1.0,
                risk_score=60.0,
                severity="high",
                horizon="0_6m",
                confidence=0.7,
                evidence_refs=None,
                created_at=NOW,
            ),
        ),
        created_at=NOW,
    )
    material = build_brief_material(
        _inputs(top_events=[event]),
        BriefContext(forecasts=(forecast,)),
    )
    (section,) = material.of_kind(SectionKind.FORECASTS)
    assert section.forecast_rows
    assert section.prose == ""
    assert not hasattr(section.forecast_rows[0], "narrative")


def test_forecast_row_evidence_refs_are_deep_frozen() -> None:
    event = _selected()
    forecast = EventForecast(
        event_id=event.event_id,
        scenario_set_id=uuid.uuid4(),
        scenarios=(
            ForecastScenarioRow(
                event_id=event.event_id,
                scenario_set_id=uuid.uuid4(),
                scenario_name="base_case",
                probability=1.0,
                risk_score=60.0,
                severity="high",
                horizon="0_6m",
                confidence=0.7,
                evidence_refs={"claims": [str(uuid.uuid4())]},
                created_at=NOW,
            ),
        ),
        created_at=NOW,
    )
    material = build_brief_material(_inputs(top_events=[event]), BriefContext(forecasts=(forecast,)))
    (section,) = material.of_kind(SectionKind.FORECASTS)
    refs = section.forecast_rows[0].evidence_refs
    assert isinstance(refs, Mapping)
    assert isinstance(refs["claims"], tuple)
    with pytest.raises(TypeError):
        refs["claims"] = ()  # type: ignore[index]
