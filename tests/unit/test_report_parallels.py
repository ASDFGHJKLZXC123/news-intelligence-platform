"""Historical-parallel and forecast context: onset/outcome separation, latest complete set.

DB-free by construction: pure functions over hand-built context rows.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping

import pytest

from db.models.core import SCENARIO_NAMES
from services.reports.context import (
    MAX_ANALOGIES_PER_EVENT,
    PROBABILITY_TOLERANCE,
    AnalogyContext,
    ForecastScenarioRow,
    HistoricalOnset,
    HistoricalOutcomeContext,
    ParentEpisode,
    build_analogy_context,
    select_event_forecasts,
)
from services.reports.contracts import DataQuality, LinkedRisk, RiskProvenance, SelectedEvent

UTC = datetime.UTC


def _selected(event_id: uuid.UUID, title: str = "Event", rank: int = 1) -> SelectedEvent:
    return SelectedEvent(
        rank=rank,
        event_id=event_id,
        title=title,
        hotness_score=70.0,
        max_linked_risk=LinkedRisk(score=50.0, provenance=RiskProvenance.EVENT_OBSERVATION),
        ranking_score=62.0,
        credibility_sum=1.0,
        developing=False,
    )


def _analogy(
    *,
    event_id: uuid.UUID,
    similarity: float = 80.0,
    episode_id: uuid.UUID | None = None,
    is_counterexample: bool = False,
    parent: ParentEpisode | None = None,
    outcomes: tuple[str, ...] = ("systemic_crisis",),
) -> AnalogyContext:
    return AnalogyContext(
        event_id=event_id,
        similarity=similarity,
        rationale="rationale",
        limitations=("different regime",),
        shared_causes=("leverage",),
        regime_caveats=("floating FX now",),
        evidence_refs=None,
        onset=HistoricalOnset(
            episode_id=episode_id or uuid.uuid4(),
            name="1998 LTCM",
            episode_type="banking_crisis",
            onset_date=datetime.date(1998, 8, 1),
            onset_summary="Leverage unwind begins.",
            geography="US",
            regime_tags=("pre_gfc",),
            is_counterexample=is_counterexample,
            source_refs=None,
            parent=parent,
        ),
        outcome=HistoricalOutcomeContext(
            outcome_summary="Contained by coordinated recapitalization.",
            outcomes=outcomes,
            resolution_mechanism="bailout",
            peak_date=datetime.date(1998, 9, 1),
            end_date=datetime.date(1998, 12, 1),
        ),
    )


def _scenario(
    *,
    event_id: uuid.UUID,
    set_id: uuid.UUID,
    name: str,
    probability: float,
    created_at: datetime.datetime,
) -> ForecastScenarioRow:
    return ForecastScenarioRow(
        event_id=event_id,
        scenario_set_id=set_id,
        scenario_name=name,
        probability=probability,
        risk_score=60.0,
        severity="high",
        horizon="0_6m",
        confidence=0.7,
        evidence_refs=None,
        created_at=created_at,
    )


def _complete_set(
    event_id: uuid.UUID, set_id: uuid.UUID, created_at: datetime.datetime
) -> list[ForecastScenarioRow]:
    probs = {"base_case": 0.4, "upside_case": 0.3, "downside_case": 0.2, "tail_risk_case": 0.1}
    return [
        _scenario(
            event_id=event_id,
            set_id=set_id,
            name=name,
            probability=probs[name],
            created_at=created_at,
        )
        for name in SCENARIO_NAMES
    ]


# --------------------------------------------------------------------------------------
# Onset / outcome separation
# --------------------------------------------------------------------------------------


def test_onset_has_no_outcome_field_and_outcome_has_no_forecast_field() -> None:
    onset_fields = {f.name for f in dataclasses.fields(HistoricalOnset)}
    outcome_fields = {f.name for f in dataclasses.fields(HistoricalOutcomeContext)}
    assert not onset_fields & {"outcome_summary", "outcomes", "resolution_mechanism"}
    assert not outcome_fields & {"probability", "horizon", "scenario_name"}


def test_analogy_carries_episode_id_never_a_claim_id() -> None:
    event_id = uuid.uuid4()
    analogy = _analogy(event_id=event_id)
    assert analogy.episode_id == analogy.onset.episode_id
    assert not hasattr(analogy, "claim_id")


def test_counterexample_marker_and_parent_context_are_preserved() -> None:
    event_id = uuid.uuid4()
    parent = ParentEpisode(
        episode_id=uuid.uuid4(), name="Great Depression", onset_summary="1929 crash"
    )
    (context,), _ = build_analogy_context(
        [_analogy(event_id=event_id, is_counterexample=True, parent=parent)], [_selected(event_id)]
    )
    (analogy,) = context.analogies
    assert analogy.onset.is_counterexample is True
    assert analogy.onset.parent is not None
    assert analogy.onset.parent.name == "Great Depression"
    assert analogy.regime_caveats == ("floating FX now",)


# --------------------------------------------------------------------------------------
# Order, bounds, no-match
# --------------------------------------------------------------------------------------


def test_analogies_ordered_by_similarity_descending() -> None:
    event_id = uuid.uuid4()
    rows = [_analogy(event_id=event_id, similarity=s) for s in (55.0, 90.0, 72.0)]
    (context,), _ = build_analogy_context(rows, [_selected(event_id)])
    assert [a.similarity for a in context.analogies] == [90.0, 72.0, 55.0]


def test_analogies_are_bounded_per_event() -> None:
    event_id = uuid.uuid4()
    rows = [
        _analogy(event_id=event_id, similarity=float(90 - i))
        for i in range(MAX_ANALOGIES_PER_EVENT + 2)
    ]
    (context,), _ = build_analogy_context(rows, [_selected(event_id)])
    assert len(context.analogies) == MAX_ANALOGIES_PER_EVENT


def test_no_analogy_is_a_valid_answer_with_an_omission_note() -> None:
    event_id = uuid.uuid4()
    contexts, notes = build_analogy_context([], [_selected(event_id, title="Uncharted")])
    assert contexts == ()
    assert len(notes) == 1
    assert notes[0].code is DataQuality.NO_RELIABLE_ANALOGY
    assert "Uncharted" in notes[0].detail


def test_events_with_and_without_analogies_are_separated() -> None:
    have, lack = uuid.uuid4(), uuid.uuid4()
    contexts, notes = build_analogy_context(
        [_analogy(event_id=have)], [_selected(have), _selected(lack, title="Lack")]
    )
    assert len(contexts) == 1 and contexts[0].event_id == have
    assert any("Lack" in note.detail for note in notes)


# --------------------------------------------------------------------------------------
# Forecast: latest complete set, no set mixing
# --------------------------------------------------------------------------------------


def test_latest_complete_set_is_chosen() -> None:
    event_id = uuid.uuid4()
    old_set, new_set = uuid.uuid4(), uuid.uuid4()
    rows = [
        *_complete_set(event_id, old_set, datetime.datetime(2026, 7, 10, tzinfo=UTC)),
        *_complete_set(event_id, new_set, datetime.datetime(2026, 7, 14, tzinfo=UTC)),
    ]
    (forecasts, notes) = select_event_forecasts(rows, [_selected(event_id)])
    assert len(forecasts) == 1
    assert forecasts[0].scenario_set_id == new_set
    assert notes == ()


def test_scenarios_come_out_in_canonical_order() -> None:
    event_id, set_id = uuid.uuid4(), uuid.uuid4()
    rows = list(
        reversed(_complete_set(event_id, set_id, datetime.datetime(2026, 7, 14, tzinfo=UTC)))
    )
    (forecasts, _) = select_event_forecasts(rows, [_selected(event_id)])
    assert tuple(s.scenario_name for s in forecasts[0].scenarios) == SCENARIO_NAMES


def test_newer_invalid_set_is_skipped_for_the_older_valid_set() -> None:
    # "Latest complete set": a half-written newer set is rejected *with a note*, but it must not
    # hide the older complete set -- the brief falls back to the last good forecast.
    event_id = uuid.uuid4()
    good_set, bad_set = uuid.uuid4(), uuid.uuid4()
    rows = [
        *_complete_set(event_id, good_set, datetime.datetime(2026, 7, 10, tzinfo=UTC)),
        # Newest, but only two scenarios: half-written, probabilities sum to 0.7.
        _scenario(
            event_id=event_id,
            set_id=bad_set,
            name="base_case",
            probability=0.4,
            created_at=datetime.datetime(2026, 7, 14, tzinfo=UTC),
        ),
        _scenario(
            event_id=event_id,
            set_id=bad_set,
            name="downside_case",
            probability=0.3,
            created_at=datetime.datetime(2026, 7, 14, tzinfo=UTC),
        ),
    ]
    (forecasts, notes) = select_event_forecasts(rows, [_selected(event_id)])
    assert len(forecasts) == 1
    assert forecasts[0].scenario_set_id == good_set
    invalid = [note for note in notes if note.code is DataQuality.FORECAST_SET_INVALID]
    assert len(invalid) == 1
    assert str(bad_set) in invalid[0].detail


def test_all_invalid_sets_yield_no_forecast_but_each_is_noted() -> None:
    # No valid set anywhere: no forecast, and every invalid set is on record as to why.
    event_id = uuid.uuid4()
    older, newer = uuid.uuid4(), uuid.uuid4()
    rows = [
        _scenario(
            event_id=event_id,
            set_id=older,
            name="base_case",
            probability=0.4,
            created_at=datetime.datetime(2026, 7, 10, tzinfo=UTC),
        ),
        _scenario(
            event_id=event_id,
            set_id=newer,
            name="base_case",
            probability=0.4,
            created_at=datetime.datetime(2026, 7, 14, tzinfo=UTC),
        ),
    ]
    (forecasts, notes) = select_event_forecasts(rows, [_selected(event_id)])
    assert forecasts == ()
    invalid = {str(older), str(newer)}
    noted = {
        set_id
        for note in notes
        if note.code is DataQuality.FORECAST_SET_INVALID
        for set_id in invalid
        if set_id in note.detail
    }
    assert noted == invalid


def test_same_created_at_tie_is_broken_deterministically() -> None:
    # Two complete valid sets at the identical instant: the choice is tie-broken by
    # scenario_set_id and is the same on every run, never insertion-order dependent.
    event_id = uuid.uuid4()
    when = datetime.datetime(2026, 7, 14, tzinfo=UTC)
    low_set, high_set = uuid.UUID(int=1), uuid.UUID(int=2)
    rows = [
        *_complete_set(event_id, low_set, when),
        *_complete_set(event_id, high_set, when),
    ]
    (forecasts, _) = select_event_forecasts(rows, [_selected(event_id)])
    assert forecasts[0].scenario_set_id == high_set
    # Reversing the input order must not change the winner.
    (again, _) = select_event_forecasts(list(reversed(rows)), [_selected(event_id)])
    assert again[0].scenario_set_id == high_set


def test_chosen_forecast_never_mixes_scenarios_across_sets() -> None:
    # The chosen forecast's scenarios all belong to the one chosen set -- never a base case from
    # today grafted onto a tail risk from last week.
    event_id = uuid.uuid4()
    old_set, new_set = uuid.uuid4(), uuid.uuid4()
    rows = [
        *_complete_set(event_id, old_set, datetime.datetime(2026, 7, 10, tzinfo=UTC)),
        *_complete_set(event_id, new_set, datetime.datetime(2026, 7, 14, tzinfo=UTC)),
    ]
    (forecasts, _) = select_event_forecasts(rows, [_selected(event_id)])
    (forecast,) = forecasts
    assert forecast.scenario_set_id == new_set
    assert {s.scenario_set_id for s in forecast.scenarios} == {new_set}


def test_set_whose_probabilities_do_not_sum_to_one_is_rejected() -> None:
    event_id, set_id = uuid.uuid4(), uuid.uuid4()
    rows = _complete_set(event_id, set_id, datetime.datetime(2026, 7, 14, tzinfo=UTC))
    # Bump one probability well past the tolerance.
    rows[0] = dataclasses.replace(rows[0], probability=0.9)
    (forecasts, notes) = select_event_forecasts(rows, [_selected(event_id)])
    assert forecasts == ()
    assert any(note.code is DataQuality.FORECAST_SET_INVALID for note in notes)


def test_probability_sum_tolerance_is_respected() -> None:
    event_id, set_id = uuid.uuid4(), uuid.uuid4()
    rows = _complete_set(event_id, set_id, datetime.datetime(2026, 7, 14, tzinfo=UTC))
    # 0.4/0.3/0.2/0.1 -> exactly 1.0; nudge within tolerance (<= 0.01).
    rows[0] = dataclasses.replace(rows[0], probability=0.405)
    (forecasts, notes) = select_event_forecasts(rows, [_selected(event_id)])
    assert len(forecasts) == 1
    assert float(PROBABILITY_TOLERANCE) == 0.01


def test_no_forecast_produces_a_note() -> None:
    event_id = uuid.uuid4()
    (forecasts, notes) = select_event_forecasts([], [_selected(event_id, title="Silent")])
    assert forecasts == ()
    assert any(note.code is DataQuality.NO_FORECAST and "Silent" in note.detail for note in notes)


def test_two_events_do_not_borrow_each_others_sets() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    rows = [
        *_complete_set(a, uuid.uuid4(), datetime.datetime(2026, 7, 14, tzinfo=UTC)),
        *_complete_set(b, uuid.uuid4(), datetime.datetime(2026, 7, 14, tzinfo=UTC)),
    ]
    (forecasts, _) = select_event_forecasts(rows, [_selected(a), _selected(b)])
    by_event = {f.event_id: f for f in forecasts}
    assert by_event[a].scenario_set_id != by_event[b].scenario_set_id


# --------------------------------------------------------------------------------------
# Deep immutability of JSON-shaped provenance refs
# --------------------------------------------------------------------------------------


def _onset_with_refs(source_refs: object) -> HistoricalOnset:
    return HistoricalOnset(
        episode_id=uuid.uuid4(),
        name="1998 LTCM",
        episode_type="banking_crisis",
        onset_date=datetime.date(1998, 8, 1),
        onset_summary="Leverage unwind begins.",
        geography="US",
        regime_tags=("pre_gfc",),
        is_counterexample=False,
        source_refs=source_refs,
        parent=None,
    )


def test_analogy_source_and_evidence_refs_are_deep_frozen_and_order_deterministic() -> None:
    onset = _onset_with_refs({"b": 2, "a": [{"k": "v"}]})
    analogy = AnalogyContext(
        event_id=uuid.uuid4(),
        similarity=80.0,
        rationale="r",
        limitations=(),
        shared_causes=(),
        regime_caveats=(),
        evidence_refs=[{"z": 1}, {"a": 2}],
        onset=onset,
        outcome=HistoricalOutcomeContext(
            outcome_summary=None,
            outcomes=(),
            resolution_mechanism=None,
            peak_date=None,
            end_date=None,
        ),
    )

    source_refs = analogy.onset.source_refs
    assert isinstance(source_refs, Mapping)
    # Deterministic key order regardless of the source dict's insertion order.
    assert list(source_refs.keys()) == ["a", "b"]
    assert isinstance(source_refs["a"], tuple)  # a JSON array becomes a tuple
    with pytest.raises(TypeError):
        source_refs["a"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        source_refs["a"][0]["k"] = "x"  # nested mutation is impossible, all the way down

    # A top-level JSON array becomes a tuple of frozen mappings.
    assert isinstance(analogy.evidence_refs, tuple)
    with pytest.raises(TypeError):
        analogy.evidence_refs[0]["z"] = 9


def test_analogy_refs_with_same_content_normalize_identically() -> None:
    one = _onset_with_refs({"a": 1, "b": {"c": 2, "d": 3}})
    two = _onset_with_refs({"b": {"d": 3, "c": 2}, "a": 1})
    # Same content, different insertion order -> identical frozen structure.
    assert one.source_refs == two.source_refs
    assert list(one.source_refs.keys()) == list(two.source_refs.keys())


def test_forecast_scenario_evidence_refs_are_deep_frozen() -> None:
    row = ForecastScenarioRow(
        event_id=uuid.uuid4(),
        scenario_set_id=uuid.uuid4(),
        scenario_name="base_case",
        probability=0.4,
        risk_score=60.0,
        severity="high",
        horizon="0_6m",
        confidence=0.7,
        evidence_refs={"claims": [str(uuid.uuid4())]},
        created_at=datetime.datetime(2026, 7, 14, tzinfo=UTC),
    )
    assert isinstance(row.evidence_refs, Mapping)
    assert isinstance(row.evidence_refs["claims"], tuple)
    with pytest.raises(TypeError):
        row.evidence_refs["claims"] = ()  # type: ignore[index]
