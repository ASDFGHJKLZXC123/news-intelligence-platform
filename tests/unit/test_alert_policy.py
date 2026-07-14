"""Deterministic alert-policy primitives (ADR 0010)."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from db.models.core import ACTIVE_ALERT_STATES, ALERT_STATES
from db.models.enums import RiskLevel, RiskType, risk_level_for_score
from services.alerts import (
    ACTIVE_STATES,
    CLEAR_THRESHOLD,
    DEDUPE_KEY_MAX_LENGTH,
    ENTER_THRESHOLD,
    RESOLVE_AFTER,
    VELOCITY_REQUIRED_RUNS,
    VELOCITY_Z_THRESHOLD,
    AlertLifecycleState,
    AlertState,
    Comparator,
    LifecycleTrigger,
    ManualReviewCondition,
    ReductionPredicate,
    ReductionStatus,
    applicable_clear_threshold,
    build_dedupe_key,
    canonical_severity,
    decide_lifecycle,
    decide_velocity_persistence,
    evaluate_reduction_conditions,
    is_below_clear_band,
    news_driven_contribution,
    next_severity,
    parse_reduction_conditions,
    severity_rank,
)
from services.alerts.hysteresis import CANONICAL_BAND_FLOOR

NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)


def _state(
    state: AlertState,
    severity: RiskLevel,
    clear_band_since: datetime.datetime | None = None,
) -> AlertLifecycleState:
    return AlertLifecycleState(state=state, severity=severity, clear_band_since=clear_band_since)


# --------------------------------------------------------------------------------------
# Hysteresis bands
# --------------------------------------------------------------------------------------


def test_bands_match_the_adr_literals() -> None:
    assert ENTER_THRESHOLD == {RiskLevel.MEDIUM: 37, RiskLevel.HIGH: 62, RiskLevel.CRITICAL: 82}
    assert CLEAR_THRESHOLD == {RiskLevel.MEDIUM: 25, RiskLevel.HIGH: 50, RiskLevel.CRITICAL: 70}


def test_band_floors_agree_with_the_canonical_severity_mapping() -> None:
    # The bands are derived from these floors, so there is one severity mapping, not two.
    for level, floor in CANONICAL_BAND_FLOOR.items():
        assert risk_level_for_score(floor) is level
        below = risk_level_for_score(floor - 1)
        assert severity_rank(below) == severity_rank(level) - 1
    assert canonical_severity(30) is RiskLevel.LOW
    assert canonical_severity(Decimal("76")) is RiskLevel.CRITICAL


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, RiskLevel.LOW),
        (36, RiskLevel.LOW),
        (37, RiskLevel.MEDIUM),
        (61, RiskLevel.MEDIUM),
        (62, RiskLevel.HIGH),
        (81, RiskLevel.HIGH),
        (82, RiskLevel.CRITICAL),
        (100, RiskLevel.CRITICAL),
    ],
)
def test_new_alerts_enter_only_at_the_raise_threshold(score: int, expected: RiskLevel) -> None:
    assert next_severity(score) is expected


@pytest.mark.parametrize(
    ("current", "score"),
    [
        (RiskLevel.LOW, 31),  # bare Medium boundary
        (RiskLevel.MEDIUM, 56),  # bare High boundary
        (RiskLevel.HIGH, 76),  # bare Critical boundary
    ],
)
def test_bare_canonical_boundaries_never_raise_an_existing_alert(
    current: RiskLevel, score: int
) -> None:
    assert next_severity(score, current) is current


@pytest.mark.parametrize(
    ("current", "score"),
    [
        (RiskLevel.MEDIUM, 26),  # 26-36 holds Medium
        (RiskLevel.MEDIUM, 36),
        (RiskLevel.HIGH, 51),  # 51-61 holds High
        (RiskLevel.HIGH, 61),
        (RiskLevel.CRITICAL, 71),  # 71-81 holds Critical
        (RiskLevel.CRITICAL, 81),
    ],
)
def test_hold_zones_preserve_the_current_severity(current: RiskLevel, score: int) -> None:
    assert next_severity(score, current) is current


@pytest.mark.parametrize(
    ("current", "score", "expected"),
    [
        (RiskLevel.MEDIUM, 25, RiskLevel.LOW),  # clear exactly at the threshold
        (RiskLevel.HIGH, 50, RiskLevel.MEDIUM),
        (RiskLevel.CRITICAL, 70, RiskLevel.HIGH),
    ],
)
def test_clear_thresholds_are_inclusive(
    current: RiskLevel, score: int, expected: RiskLevel
) -> None:
    assert next_severity(score, current) is expected


def test_multi_band_jumps_resolve_in_a_single_step() -> None:
    assert next_severity(20, RiskLevel.CRITICAL) is RiskLevel.LOW
    assert next_severity(30, RiskLevel.CRITICAL) is RiskLevel.MEDIUM
    assert next_severity(95, RiskLevel.LOW) is RiskLevel.CRITICAL
    assert next_severity(65, RiskLevel.LOW) is RiskLevel.HIGH


def test_float_and_decimal_scores_decide_identically() -> None:
    assert next_severity(36.9, RiskLevel.LOW) is RiskLevel.LOW
    assert next_severity(Decimal("36.9"), RiskLevel.LOW) is RiskLevel.LOW
    assert next_severity(37.0, RiskLevel.LOW) is RiskLevel.MEDIUM
    assert next_severity(Decimal("37.0"), RiskLevel.LOW) is RiskLevel.MEDIUM
    # 25.000001 is above the clear band; 25.0 is on it.
    assert next_severity(Decimal("25.000001"), RiskLevel.MEDIUM) is RiskLevel.MEDIUM
    assert next_severity(25.0, RiskLevel.MEDIUM) is RiskLevel.LOW


@pytest.mark.parametrize("score", [-1, 101, float("nan"), float("inf"), Decimal("NaN")])
def test_invalid_scores_are_rejected(score: object) -> None:
    with pytest.raises(ValueError):
        next_severity(score)


@pytest.mark.parametrize("score", ["37", None, True])
def test_non_numeric_scores_are_rejected(score: object) -> None:
    with pytest.raises(TypeError):
        next_severity(score)


def test_only_an_alert_that_has_cleared_every_band_is_below_the_clear_band() -> None:
    # An alert still holding an alerting severity has somewhere to downgrade to, so it is
    # never a candidate for an all-clear -- whatever its score.
    assert applicable_clear_threshold(RiskLevel.LOW) == 25
    for severity in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
        assert applicable_clear_threshold(severity) is None
        assert is_below_clear_band(0, severity) is False

    assert is_below_clear_band(25, RiskLevel.LOW) is True
    # Low's hold zone: hysteresis still holds Low, but the score is back above the band.
    assert is_below_clear_band(26, RiskLevel.LOW) is False
    assert is_below_clear_band(36, RiskLevel.LOW) is False


def test_a_drop_is_below_the_clear_band_exactly_when_it_lands_on_low() -> None:
    for current in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
        for score in range(0, 101):
            severity = next_severity(score, current)
            assert is_below_clear_band(score, severity) is (severity is RiskLevel.LOW)


# --------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------


def test_state_vocabulary_matches_the_schema() -> None:
    assert tuple(state.value for state in AlertState) == ALERT_STATES
    assert {state.value for state in ACTIVE_STATES} == set(ACTIVE_ALERT_STATES)


def test_open_escalates_when_severity_rises() -> None:
    decision = decide_lifecycle(_state(AlertState.OPEN, RiskLevel.MEDIUM), 65, now=NOW)

    assert decision.state is AlertState.ESCALATED
    assert decision.severity is RiskLevel.HIGH
    assert decision.trigger is LifecycleTrigger.ESCALATED
    assert decision.clear_band_since is None
    assert decision.resolved_at is None
    assert decision.is_active is True


def test_open_holds_when_the_score_stays_in_band() -> None:
    decision = decide_lifecycle(_state(AlertState.OPEN, RiskLevel.MEDIUM), 45, now=NOW)

    assert decision.state is AlertState.OPEN
    assert decision.severity is RiskLevel.MEDIUM
    assert decision.trigger is LifecycleTrigger.UNCHANGED
    assert decision.clear_band_since is None


def test_escalated_downgrades_when_the_clear_band_is_crossed() -> None:
    decision = decide_lifecycle(_state(AlertState.ESCALATED, RiskLevel.HIGH), 50, now=NOW)

    assert decision.state is AlertState.DOWNGRADED
    assert decision.severity is RiskLevel.MEDIUM
    assert decision.trigger is LifecycleTrigger.DOWNGRADED
    # The risk eased, it did not end: no resolve timer while an alerting severity remains.
    assert decision.clear_band_since is None
    assert decision.resolved_at is None


def test_critical_holds_until_its_clear_band_then_downgrades() -> None:
    holding = decide_lifecycle(_state(AlertState.OPEN, RiskLevel.CRITICAL), 71, now=NOW)
    assert holding.state is AlertState.OPEN
    assert holding.severity is RiskLevel.CRITICAL

    dropping = decide_lifecycle(_state(AlertState.OPEN, RiskLevel.CRITICAL), 70, now=NOW)
    assert dropping.state is AlertState.DOWNGRADED
    assert dropping.severity is RiskLevel.HIGH
    assert dropping.clear_band_since is None


def test_a_live_medium_risk_never_resolves_however_long_it_parks_there() -> None:
    # The safety property: an all-clear on a score of 45 would be a lie.
    current = _state(AlertState.DOWNGRADED, RiskLevel.MEDIUM, None)
    for day in range(0, 40):
        decision = decide_lifecycle(current, 45, now=NOW + datetime.timedelta(days=day))
        assert decision.state is AlertState.DOWNGRADED
        assert decision.severity is RiskLevel.MEDIUM
        assert decision.clear_band_since is None
        assert decision.resolved_at is None
        current = _state(decision.state, decision.severity, decision.clear_band_since)


def test_the_resolve_timer_starts_when_the_alert_clears_the_last_band() -> None:
    decision = decide_lifecycle(_state(AlertState.DOWNGRADED, RiskLevel.MEDIUM, None), 25, now=NOW)

    assert decision.state is AlertState.DOWNGRADED
    assert decision.severity is RiskLevel.LOW
    assert decision.trigger is LifecycleTrigger.DOWNGRADED
    assert decision.clear_band_since == NOW


def test_downgraded_resolves_after_exactly_seven_full_days() -> None:
    since = NOW - RESOLVE_AFTER
    decision = decide_lifecycle(_state(AlertState.DOWNGRADED, RiskLevel.LOW, since), 20, now=NOW)

    assert decision.state is AlertState.RESOLVED
    assert decision.severity is RiskLevel.LOW
    assert decision.trigger is LifecycleTrigger.RESOLVED
    assert decision.resolved_at == NOW
    assert decision.emits_all_clear is True
    assert decision.is_active is False


def test_downgraded_does_not_resolve_one_second_early() -> None:
    since = NOW - RESOLVE_AFTER + datetime.timedelta(seconds=1)
    decision = decide_lifecycle(_state(AlertState.DOWNGRADED, RiskLevel.LOW, since), 20, now=NOW)

    assert decision.state is AlertState.DOWNGRADED
    assert decision.trigger is LifecycleTrigger.UNCHANGED
    assert decision.resolved_at is None
    assert decision.clear_band_since == since  # the running timer is preserved
    assert decision.emits_all_clear is False


def test_a_rebound_above_the_clear_band_resets_the_timer_without_killing_the_alert() -> None:
    since = NOW - datetime.timedelta(days=6)
    # 30 is in Low's hold zone: severity stays Low, but the score is back above 25.
    decision = decide_lifecycle(_state(AlertState.DOWNGRADED, RiskLevel.LOW, since), 30, now=NOW)

    assert decision.state is AlertState.DOWNGRADED
    assert decision.severity is RiskLevel.LOW
    assert decision.trigger is LifecycleTrigger.REBOUNDED
    assert decision.clear_band_since is None
    assert decision.is_active is True


def test_a_rebound_restarts_the_seven_day_clock_from_scratch() -> None:
    since = NOW - datetime.timedelta(days=6)
    rebounded = decide_lifecycle(_state(AlertState.DOWNGRADED, RiskLevel.LOW, since), 30, now=NOW)
    assert rebounded.clear_band_since is None

    later = NOW + datetime.timedelta(days=1)
    restarted = decide_lifecycle(
        _state(rebounded.state, rebounded.severity, rebounded.clear_band_since), 20, now=later
    )
    assert restarted.state is AlertState.DOWNGRADED
    assert restarted.clear_band_since == later  # not the original, pre-rebound timestamp

    # Six days after the *original* timer would have resolved it, it is still counting.
    almost = decide_lifecycle(
        _state(restarted.state, restarted.severity, restarted.clear_band_since),
        20,
        now=later + RESOLVE_AFTER - datetime.timedelta(seconds=1),
    )
    assert almost.state is AlertState.DOWNGRADED


def test_a_rebound_that_re_escalates_cancels_the_timer() -> None:
    since = NOW - datetime.timedelta(days=6)
    decision = decide_lifecycle(_state(AlertState.DOWNGRADED, RiskLevel.LOW, since), 65, now=NOW)

    assert decision.state is AlertState.ESCALATED
    assert decision.severity is RiskLevel.HIGH
    assert decision.trigger is LifecycleTrigger.ESCALATED
    assert decision.clear_band_since is None


def test_a_downgraded_alert_missing_its_timer_starts_counting_now() -> None:
    decision = decide_lifecycle(_state(AlertState.DOWNGRADED, RiskLevel.LOW, None), 20, now=NOW)

    assert decision.state is AlertState.DOWNGRADED
    assert decision.clear_band_since == NOW
    assert decision.resolved_at is None


def test_a_full_high_alert_life_cycle() -> None:
    # Raised High, decays to Medium, decays to Low, resolves 7 days later.
    current = _state(AlertState.OPEN, RiskLevel.HIGH)

    eased = decide_lifecycle(current, 45, now=NOW)
    assert (eased.state, eased.severity) == (AlertState.DOWNGRADED, RiskLevel.MEDIUM)
    assert eased.clear_band_since is None

    day_two = NOW + datetime.timedelta(days=2)
    cleared = decide_lifecycle(
        _state(eased.state, eased.severity, eased.clear_band_since), 22, now=day_two
    )
    assert (cleared.state, cleared.severity) == (AlertState.DOWNGRADED, RiskLevel.LOW)
    assert cleared.clear_band_since == day_two

    resolved = decide_lifecycle(
        _state(cleared.state, cleared.severity, cleared.clear_band_since),
        22,
        now=day_two + RESOLVE_AFTER,
    )
    assert resolved.state is AlertState.RESOLVED
    assert resolved.emits_all_clear is True


@pytest.mark.parametrize("state", [AlertState.RESOLVED, AlertState.SUPERSEDED])
def test_terminal_alerts_are_never_re_evaluated_in_place(state: AlertState) -> None:
    with pytest.raises(ValueError, match="cannot evaluate"):
        decide_lifecycle(_state(state, RiskLevel.MEDIUM), 45, now=NOW)


def test_naive_timestamps_are_rejected() -> None:
    naive = datetime.datetime(2026, 7, 13, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        decide_lifecycle(_state(AlertState.OPEN, RiskLevel.MEDIUM), 45, now=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        _state(AlertState.DOWNGRADED, RiskLevel.MEDIUM, naive)


def test_an_oscillating_score_cannot_flap() -> None:
    # 45 <-> 58 straddles the bare High boundary (56); neither crosses a raise/clear band.
    current = _state(AlertState.OPEN, RiskLevel.MEDIUM)
    for score in (58, 45, 58, 45, 58):
        decision = decide_lifecycle(current, score, now=NOW)
        assert decision.trigger is LifecycleTrigger.UNCHANGED
        assert decision.state is AlertState.OPEN
        assert decision.severity is RiskLevel.MEDIUM
        current = _state(decision.state, decision.severity, decision.clear_band_since)


# --------------------------------------------------------------------------------------
# Velocity persistence
# --------------------------------------------------------------------------------------


def test_velocity_requires_two_consecutive_qualifying_runs() -> None:
    first = decide_velocity_persistence(2.6, 0)
    assert (first.streak, first.qualified, first.persisted) == (1, True, False)

    second = decide_velocity_persistence(2.6, first.streak)
    assert (second.streak, second.qualified, second.persisted) == (2, True, True)

    third = decide_velocity_persistence(2.6, second.streak)
    assert (third.streak, third.persisted) == (3, True)


def test_velocity_threshold_is_strict() -> None:
    assert VELOCITY_Z_THRESHOLD == 2.5
    assert VELOCITY_REQUIRED_RUNS == 2

    on_threshold = decide_velocity_persistence(2.5, 1)
    assert (on_threshold.streak, on_threshold.qualified, on_threshold.persisted) == (0, False, False)

    just_above = decide_velocity_persistence(Decimal("2.5000001"), 1)
    assert (just_above.streak, just_above.persisted) == (2, True)


@pytest.mark.parametrize("z_score", [2.4, 0, -3.0, None])
def test_a_non_qualifying_run_resets_the_streak(z_score: float | None) -> None:
    decision = decide_velocity_persistence(z_score, 5)

    assert decision.streak == 0
    assert decision.qualified is False
    assert decision.persisted is False


@pytest.mark.parametrize("z_score", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_z_score_is_a_defect_not_missing_data(z_score: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        decide_velocity_persistence(z_score, 0)


def test_invalid_streaks_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        decide_velocity_persistence(2.6, -1)
    with pytest.raises(TypeError):
        decide_velocity_persistence(2.6, True)
    with pytest.raises(TypeError):
        decide_velocity_persistence(2.6, 1.5)


# --------------------------------------------------------------------------------------
# what_could_reduce_risk
# --------------------------------------------------------------------------------------


def test_parse_handles_structured_and_free_text_conditions() -> None:
    conditions = parse_reduction_conditions(
        [
            {"signal_ref": "ted_spread", "comparator": "lt", "threshold": 0.5},
            {"text": "the ECB opens a dollar swap line"},
            "deposit outflows stop",
        ]
    )

    predicate, mapping_text, bare_text = conditions
    assert isinstance(predicate, ReductionPredicate)
    assert predicate.comparator is Comparator.LT
    assert predicate.threshold == Decimal("0.5")
    assert isinstance(mapping_text, ManualReviewCondition)
    assert isinstance(bare_text, ManualReviewCondition)
    assert bare_text.text == "deposit outflows stop"
    assert parse_reduction_conditions(None) == ()


@pytest.mark.parametrize(
    ("comparator", "observed", "expected"),
    [
        ("lt", 0.5, ReductionStatus.NOT_MET),  # boundary: strict
        ("lt", 0.4999, ReductionStatus.MET),
        ("lte", 0.5, ReductionStatus.MET),  # boundary: inclusive
        ("lte", 0.5001, ReductionStatus.NOT_MET),
        ("gt", 0.5, ReductionStatus.NOT_MET),
        ("gt", 0.5001, ReductionStatus.MET),
        ("gte", 0.5, ReductionStatus.MET),
        ("gte", 0.4999, ReductionStatus.NOT_MET),
    ],
)
def test_comparator_boundaries(comparator: str, observed: float, expected: ReductionStatus) -> None:
    conditions = parse_reduction_conditions(
        [{"signal_ref": "ted_spread", "comparator": comparator, "threshold": 0.5}]
    )
    assessment = evaluate_reduction_conditions(conditions, {"ted_spread": observed})

    assert assessment.outcomes[0].status is expected
    assert assessment.predicates_met is (expected is ReductionStatus.MET)


@pytest.mark.parametrize("value", [None, "0.1", True, float("nan"), float("inf"), object()])
def test_missing_and_non_numeric_signals_never_claim_the_predicate_was_met(value: object) -> None:
    conditions = parse_reduction_conditions(
        [{"signal_ref": "ted_spread", "comparator": "lt", "threshold": 0.5}]
    )

    assessment = evaluate_reduction_conditions(conditions, {"ted_spread": value})
    assert assessment.outcomes[0].status is ReductionStatus.UNEVALUABLE
    assert assessment.predicates_met is False
    assert assessment.has_unevaluable is True

    absent = evaluate_reduction_conditions(conditions, {})
    assert absent.outcomes[0].status is ReductionStatus.UNEVALUABLE
    assert absent.predicates_met is False


def test_free_text_conditions_are_manual_review_only() -> None:
    conditions = parse_reduction_conditions(["the ECB opens a dollar swap line"])
    assessment = evaluate_reduction_conditions(conditions, {"the ECB opens a dollar swap line": 1})

    assert assessment.outcomes[0].status is ReductionStatus.MANUAL_REVIEW
    assert assessment.requires_manual_review is True
    # A free-text condition is never machine-evidence for a downgrade.
    assert assessment.predicates_met is False


def test_the_aggregate_requires_every_predicate_to_be_met() -> None:
    conditions = parse_reduction_conditions(
        [
            {"signal_ref": "ted_spread", "comparator": "lt", "threshold": 0.5},
            {"signal_ref": "deposit_outflow", "comparator": "lte", "threshold": 0},
            "regulator issues a backstop",
        ]
    )

    partial = evaluate_reduction_conditions(conditions, {"ted_spread": 0.2, "deposit_outflow": 3})
    assert partial.predicates_met is False

    full = evaluate_reduction_conditions(conditions, {"ted_spread": 0.2, "deposit_outflow": -1})
    assert full.predicates_met is True
    assert full.requires_manual_review is True  # the manual condition still needs a human


def test_empty_conditions_never_claim_a_reduction() -> None:
    assert evaluate_reduction_conditions((), {}).predicates_met is False


@pytest.mark.parametrize(
    "payload",
    [
        [{"signal_ref": "x", "comparator": "eq", "threshold": 1}],  # float equality is not exact
        [{"signal_ref": "x", "comparator": ">=", "threshold": 1}],
        [{"signal_ref": "  ", "comparator": "lt", "threshold": 1}],
        [{"signal_ref": "x", "comparator": "lt", "threshold": float("nan")}],
        [{"signal_ref": "x", "comparator": "lt", "threshold": "abc"}],
        [{"signal_ref": "x", "comparator": "lt"}],  # missing threshold
        [{"unrelated": "key"}],
        [""],
    ],
)
def test_malformed_condition_definitions_raise(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        parse_reduction_conditions(payload)


def test_conditions_round_trip_through_their_jsonb_payload() -> None:
    conditions = parse_reduction_conditions(
        [
            {"signal_ref": "ted_spread", "comparator": "GTE", "threshold": Decimal("0.5")},
            "manual check",
        ]
    )
    payload = [condition.as_payload() for condition in conditions]

    assert parse_reduction_conditions(payload) == conditions


# --------------------------------------------------------------------------------------
# news_driven contribution
# --------------------------------------------------------------------------------------


def test_news_driven_is_the_news_share_of_the_total_score() -> None:
    assert news_driven_contribution([12, 18], 100) == Decimal("0.3")
    assert news_driven_contribution([Decimal("15")], Decimal("60")) == Decimal("0.25")
    assert news_driven_contribution([12.5], 50.0) == Decimal("0.25")


@pytest.mark.parametrize("total", [0, -10, Decimal("0.0")])
def test_a_nonpositive_total_score_attributes_nothing(total: object) -> None:
    # No attributable risk, so no division and no infinite ratio.
    assert news_driven_contribution([20], total) == Decimal(0)


def test_news_share_is_clamped_into_the_zero_to_one_column_constraint() -> None:
    assert news_driven_contribution([-30], 100) == Decimal(0)  # news pulled the score down
    assert news_driven_contribution([20, -20], 100) == Decimal(0)
    assert news_driven_contribution([150], 100) == Decimal(1)  # other terms were negative
    assert news_driven_contribution([100], 100) == Decimal(1)
    assert news_driven_contribution([], 100) == Decimal(0)


def test_news_share_is_repeatable_and_bounded() -> None:
    share = news_driven_contribution([10], 30)
    assert share == Decimal("0.333333")
    assert Decimal(0) <= share <= Decimal(1)
    assert share == news_driven_contribution([10], 30)


@pytest.mark.parametrize(
    ("news", "total"),
    [([float("nan")], 100), ([float("inf")], 100), ([10], float("nan")), ([10], float("inf"))],
)
def test_non_finite_contributions_raise(news: list[float], total: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        news_driven_contribution(news, total)


# --------------------------------------------------------------------------------------
# Dedupe key
# --------------------------------------------------------------------------------------


def test_dedupe_key_components_cannot_bleed_across_the_delimiter() -> None:
    left = build_dedupe_key(RiskType.BANKING, "us:west", "composite_score")
    right = build_dedupe_key(RiskType.BANKING, "us", "west:composite_score")

    assert left.value != right.value
    assert left.value == "banking:us%3Awest:composite_score"


def test_dedupe_key_is_stable_and_normalised() -> None:
    key = build_dedupe_key("banking", "  NVDA ", "Composite_Score")

    assert key.value == "banking:nvda:composite_score"
    assert key.value == build_dedupe_key(RiskType.BANKING, "nvda", "composite_score").value
    assert str(key) == key.value


def test_dedupe_key_rejects_blank_and_unknown_components() -> None:
    with pytest.raises(ValueError, match="blank"):
        build_dedupe_key(RiskType.BANKING, "   ", "composite_score")
    with pytest.raises(ValueError, match="blank"):
        build_dedupe_key(RiskType.BANKING, "nvda", "")
    with pytest.raises(ValueError, match="unknown risk_type"):
        build_dedupe_key("not_a_risk_type", "nvda", "composite_score")
    with pytest.raises(ValueError, match="control characters"):
        build_dedupe_key(RiskType.BANKING, "nv\ndx", "composite_score")


def test_long_dedupe_keys_fit_the_column_and_stay_distinct() -> None:
    first = build_dedupe_key(RiskType.COMPANY, "e" * 400, "composite_score")
    second = build_dedupe_key(RiskType.COMPANY, "e" * 401, "composite_score")

    assert len(first.value) <= DEDUPE_KEY_MAX_LENGTH
    assert len(second.value) <= DEDUPE_KEY_MAX_LENGTH
    assert first.value != second.value
    assert "#" in first.value  # the digest marker, impossible inside a percent-encoded part
    assert first.value == build_dedupe_key(RiskType.COMPANY, "e" * 400, "composite_score").value


def test_a_shortened_key_cannot_collide_with_a_literal_one() -> None:
    # '#' is percent-encoded inside a component, so only the digest form can contain it.
    literal = build_dedupe_key(RiskType.COMPANY, "acme#deadbeef", "composite_score")

    assert "#" not in literal.value
    assert "%23" in literal.value
