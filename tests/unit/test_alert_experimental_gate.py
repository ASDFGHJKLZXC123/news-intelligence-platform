"""The null-model gate: fail-closed, single-signal carve-out, malformed evidence (ADR 0010)."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from db.models.enums import RiskType
from services.alerts import (
    REQUIRED_WINDOWS,
    AlertLifecycleService,
    AlertObservation,
    AlertScope,
    ConditionKind,
    ExperimentalGate,
    GateEvidenceError,
    InMemoryAlertRepository,
    ScoreBasis,
    WindowEvidence,
)

NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)
USER = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _evidence(
    window: str,
    *,
    episodes: int = 5,
    cand_p: float = 0.9,
    base_p: float = 0.5,
    cand_lt: float = 10,
    base_lt: float = 2,
) -> WindowEvidence:
    return WindowEvidence(
        window=window,
        labeled_episodes=episodes,
        candidate_precision=cand_p,
        baseline_precision=base_p,
        candidate_lead_time_days=cand_lt,
        baseline_lead_time_days=base_lt,
    )


def _winning_evidence() -> tuple[WindowEvidence, ...]:
    return tuple(_evidence(window) for window in REQUIRED_WINDOWS)


# --------------------------------------------------------------------------------------
# Fail-closed and single-signal carve-out (pure `ExperimentalGate.decide`)
# --------------------------------------------------------------------------------------


def test_single_signal_is_always_released_even_with_no_evidence() -> None:
    decision = ExperimentalGate().decide(ScoreBasis.SINGLE_SIGNAL)

    assert decision.released is True
    assert decision.experimental is False
    assert decision.blocking_windows == ()


def test_composite_with_no_evidence_stays_experimental_fail_closed() -> None:
    decision = ExperimentalGate().decide(ScoreBasis.COMPOSITE)

    assert decision.released is False
    assert decision.experimental is True
    assert set(decision.blocking_windows) == set(REQUIRED_WINDOWS)


def test_decide_defaults_to_the_composite_basis() -> None:
    assert ExperimentalGate().decide().basis is ScoreBasis.COMPOSITE


def test_composite_beating_the_baseline_in_every_window_is_released() -> None:
    decision = ExperimentalGate(evidence=_winning_evidence()).decide(ScoreBasis.COMPOSITE)

    assert decision.released is True
    assert decision.experimental is False
    assert decision.blocking_windows == ()


def test_a_single_missing_window_blocks_release() -> None:
    decision = ExperimentalGate(
        evidence=tuple(_evidence(window) for window in REQUIRED_WINDOWS[:-1])
    ).decide(ScoreBasis.COMPOSITE)

    assert decision.released is False
    assert decision.blocking_windows == (REQUIRED_WINDOWS[-1],)


def test_a_window_with_no_labeled_episodes_blocks_release() -> None:
    blocked = REQUIRED_WINDOWS[0]
    evidence = [_evidence(window) for window in REQUIRED_WINDOWS]
    evidence[0] = _evidence(blocked, episodes=0)

    decision = ExperimentalGate(evidence=tuple(evidence)).decide(ScoreBasis.COMPOSITE)

    assert decision.released is False
    assert blocked in decision.blocking_windows
    assert "no labeled episodes" in decision.reason


def test_a_precision_tie_is_not_a_win() -> None:
    tied = REQUIRED_WINDOWS[0]
    evidence = [_evidence(window) for window in REQUIRED_WINDOWS]
    evidence[0] = _evidence(tied, cand_p=0.6, base_p=0.6)

    decision = ExperimentalGate(evidence=tuple(evidence)).decide(ScoreBasis.COMPOSITE)

    assert decision.released is False
    assert tied in decision.blocking_windows


def test_a_lead_time_tie_is_not_a_win() -> None:
    tied = REQUIRED_WINDOWS[0]
    evidence = [_evidence(window) for window in REQUIRED_WINDOWS]
    evidence[0] = _evidence(tied, cand_lt=5, base_lt=5)

    decision = ExperimentalGate(evidence=tuple(evidence)).decide(ScoreBasis.COMPOSITE)

    assert decision.released is False
    assert tied in decision.blocking_windows


def test_beating_baseline_on_only_one_of_two_metrics_still_blocks() -> None:
    tied = REQUIRED_WINDOWS[0]
    evidence = [_evidence(window) for window in REQUIRED_WINDOWS]
    # Better precision but *worse* lead time: must beat both.
    evidence[0] = _evidence(tied, cand_p=0.9, base_p=0.1, cand_lt=1, base_lt=10)

    decision = ExperimentalGate(evidence=tuple(evidence)).decide(ScoreBasis.COMPOSITE)

    assert decision.released is False
    assert tied in decision.blocking_windows


# --------------------------------------------------------------------------------------
# Malformed evidence: never scored, never promoted -- raised, not failed
# --------------------------------------------------------------------------------------


def test_an_unknown_window_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="unknown backtest window"):
        _evidence("2011")


def test_a_duplicate_window_in_one_gate_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="duplicate backtest evidence"):
        ExperimentalGate(
            evidence=(_evidence(REQUIRED_WINDOWS[0]), _evidence(REQUIRED_WINDOWS[0]))
        )


def test_evidence_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="must be WindowEvidence"):
        ExperimentalGate(evidence=({"window": REQUIRED_WINDOWS[0]},))  # type: ignore[arg-type]


def test_a_precision_outside_0_1_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="precision"):
        _evidence(REQUIRED_WINDOWS[0], cand_p=1.5)


def test_a_negative_precision_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="precision"):
        _evidence(REQUIRED_WINDOWS[0], base_p=-0.1)


def test_a_negative_lead_time_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="non-negative"):
        _evidence(REQUIRED_WINDOWS[0], cand_lt=-1)


def test_a_nan_precision_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="finite"):
        _evidence(REQUIRED_WINDOWS[0], cand_p=float("nan"))


def test_an_infinite_lead_time_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="finite"):
        _evidence(REQUIRED_WINDOWS[0], cand_lt=float("inf"))


def test_a_negative_labeled_episode_count_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="non-negative"):
        _evidence(REQUIRED_WINDOWS[0], episodes=-1)


def test_a_non_int_labeled_episode_count_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="int"):
        _evidence(REQUIRED_WINDOWS[0], episodes=5.5)  # type: ignore[arg-type]


def test_a_boolean_labeled_episode_count_is_refused() -> None:
    with pytest.raises(GateEvidenceError, match="int"):
        _evidence(REQUIRED_WINDOWS[0], episodes=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Wired into AlertLifecycleService._create(): composite fails closed, single-signal ships
# --------------------------------------------------------------------------------------


def _scope(condition_class: str = "composite_score", **overrides: Any) -> AlertScope:
    defaults: dict[str, Any] = {
        "user_id": USER,
        "risk_type": RiskType.BANKING,
        "scope_entity": "eurozone",
        "condition_class": condition_class,
        "title": "Eurozone banking stress",
        "message": "Composite banking risk is elevated.",
        "alert_type": "risk_score",
    }
    return AlertScope(**(defaults | overrides))


def test_a_composite_alert_opens_experimental_with_no_backtest_evidence() -> None:
    repository = InMemoryAlertRepository()
    service = AlertLifecycleService(repository)

    outcome = service.evaluate(AlertObservation(scope=_scope(), risk_score=90), now=NOW)

    alert = repository.get_alert(outcome.alert_id)
    assert alert.experimental is True


def test_a_velocity_alert_is_carved_out_and_opens_released() -> None:
    repository = InMemoryAlertRepository()
    service = AlertLifecycleService(repository)
    scope = _scope(condition_class="velocity")

    def _obs() -> AlertObservation:
        return AlertObservation(
            scope=scope, risk_score=10, kind=ConditionKind.VELOCITY, z_score=3.0
        )

    service.evaluate(_obs(), now=NOW)
    outcome = service.evaluate(_obs(), now=NOW + datetime.timedelta(days=1))

    alert = repository.get_alert(outcome.alert_id)
    assert alert.experimental is False


def test_an_explicit_single_signal_basis_is_carved_out_even_for_a_score_condition() -> None:
    repository = InMemoryAlertRepository()
    service = AlertLifecycleService(repository)

    outcome = service.evaluate(
        AlertObservation(scope=_scope(), risk_score=90, score_basis=ScoreBasis.SINGLE_SIGNAL),
        now=NOW,
    )

    alert = repository.get_alert(outcome.alert_id)
    assert alert.experimental is False


def test_a_composite_alert_ships_once_the_gate_has_winning_evidence() -> None:
    repository = InMemoryAlertRepository()
    service = AlertLifecycleService(repository, gate=ExperimentalGate(evidence=_winning_evidence()))

    outcome = service.evaluate(AlertObservation(scope=_scope(), risk_score=90), now=NOW)

    alert = repository.get_alert(outcome.alert_id)
    assert alert.experimental is False


def test_a_composite_alert_stays_experimental_if_only_some_windows_win() -> None:
    partial = tuple(_evidence(window) for window in REQUIRED_WINDOWS[:-1])
    repository = InMemoryAlertRepository()
    service = AlertLifecycleService(repository, gate=ExperimentalGate(evidence=partial))

    outcome = service.evaluate(AlertObservation(scope=_scope(), risk_score=90), now=NOW)

    alert = repository.get_alert(outcome.alert_id)
    assert alert.experimental is True


def test_the_gate_is_consulted_once_at_creation_and_never_revisited_on_update() -> None:
    repository = InMemoryAlertRepository()
    service = AlertLifecycleService(repository)

    outcome = service.evaluate(AlertObservation(scope=_scope(), risk_score=40), now=NOW)
    alert = repository.get_alert(outcome.alert_id)
    assert alert.experimental is True

    service.evaluate(
        AlertObservation(scope=_scope(), risk_score=45), now=NOW + datetime.timedelta(days=1)
    )

    assert alert.experimental is True  # untouched by the update; not re-decided
