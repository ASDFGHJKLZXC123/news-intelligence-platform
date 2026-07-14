"""The platform alert budget: top-K ranking, refusals, and end-to-end wiring (ADR 0010)."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

import pytest

from db.models.core import Alert
from db.models.enums import RiskLevel, RiskType
from services.alerts import (
    AlertActionKind,
    AlertLifecycleService,
    AlertObservation,
    AlertOutcomeKind,
    AlertScope,
    AlertState,
    BudgetOutcomeKind,
    ConditionKind,
    InMemoryAlertRepository,
    LifecycleSnapshot,
    alert_rank,
    enforce_budget,
    weakest,
)

NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)
USER = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _alert(
    *,
    severity: RiskLevel,
    score: float | None,
    state: AlertState = AlertState.OPEN,
    peak_severity: RiskLevel | None = None,
    created_at: datetime.datetime = NOW,
    dedupe_key: str | None = None,
    alert_id: uuid.UUID | None = None,
) -> Alert:
    return Alert(
        id=alert_id or uuid.uuid4(),
        user_id=USER,
        title="t",
        message="m",
        severity=severity.value,
        peak_severity=(peak_severity or severity).value,
        risk_score=score,
        alert_type="risk_score",
        state=state.value,
        dedupe_key=dedupe_key or f"key-{uuid.uuid4()}",
        created_at=created_at,
        updated_at=created_at,
    )


# --------------------------------------------------------------------------------------
# Top-K ranking and tie-breaks (pure `enforce_budget` / `weakest` / `alert_rank`)
# --------------------------------------------------------------------------------------


def test_top_k_keeps_the_strongest_three_criticals() -> None:
    repository = InMemoryAlertRepository()
    incumbents = [_alert(severity=RiskLevel.CRITICAL, score=s) for s in (80, 85, 90)]
    repository.alerts.extend(incumbents)
    candidate = _alert(severity=RiskLevel.CRITICAL, score=95)
    repository.alerts.append(candidate)

    outcome = enforce_budget(repository, candidate, previous=None, now=NOW)

    assert outcome.kind is BudgetOutcomeKind.REPLACED
    assert outcome.admitted is True
    victim = min(incumbents, key=lambda a: a.risk_score)
    assert outcome.victim_id == victim.id
    assert victim.state == AlertState.SUPERSEDED.value
    assert victim.superseded_by == candidate.id
    assert candidate.state == AlertState.OPEN.value
    assert outcome.action.kind is AlertActionKind.SUPERSESSION
    assert outcome.action.alert_id == victim.id


def test_a_tied_score_does_not_outrank_the_incumbent_and_the_create_is_suppressed() -> None:
    repository = InMemoryAlertRepository()
    incumbents = [_alert(severity=RiskLevel.CRITICAL, score=s) for s in (80, 85, 90)]
    repository.alerts.extend(incumbents)
    candidate = _alert(severity=RiskLevel.CRITICAL, score=80)  # ties the weakest incumbent
    repository.alerts.append(candidate)

    outcome = enforce_budget(repository, candidate, previous=None, now=NOW)

    assert outcome.kind is BudgetOutcomeKind.SUPPRESSED
    assert outcome.admitted is False
    assert candidate not in repository.alerts
    assert all(alert.state == AlertState.OPEN.value for alert in incumbents)  # nobody evicted
    assert outcome.action.kind is AlertActionKind.BUDGET_SUPPRESSED
    assert outcome.action.alert_id is None  # the row never existed as far as anyone can see


def test_the_weakest_tie_break_evicts_the_newest_of_the_tied_incumbents() -> None:
    older = _alert(
        severity=RiskLevel.CRITICAL, score=80, created_at=NOW - datetime.timedelta(days=2)
    )
    newer = _alert(
        severity=RiskLevel.CRITICAL, score=80, created_at=NOW - datetime.timedelta(days=1)
    )
    stronger = _alert(severity=RiskLevel.CRITICAL, score=90, created_at=NOW)

    victim = weakest([older, newer, stronger])

    assert victim.id == newer.id


def test_a_null_score_ranks_below_every_real_score_and_is_evicted_first() -> None:
    unscored = _alert(severity=RiskLevel.HIGH, score=None)
    scored = _alert(severity=RiskLevel.HIGH, score=1)

    assert alert_rank(unscored) < alert_rank(scored)
    assert weakest([unscored, scored]) is unscored


# --------------------------------------------------------------------------------------
# Refusal paths: suppressed create, withheld escalation/downgrade, no-op for a held slot
# --------------------------------------------------------------------------------------


def test_escalation_into_a_full_critical_band_is_withheld_and_rolled_back() -> None:
    repository = InMemoryAlertRepository()
    repository.alerts.extend(_alert(severity=RiskLevel.CRITICAL, score=s) for s in (90, 91, 92))
    candidate = _alert(
        severity=RiskLevel.HIGH, score=65, state=AlertState.OPEN, peak_severity=RiskLevel.HIGH
    )
    repository.alerts.append(candidate)
    previous = LifecycleSnapshot(
        state=AlertState.OPEN, severity=RiskLevel.HIGH, peak_severity=RiskLevel.HIGH
    )
    # Simulate the run having already written an attempted escalation to Critical.
    candidate.severity = RiskLevel.CRITICAL.value
    candidate.state = AlertState.ESCALATED.value
    candidate.peak_severity = RiskLevel.CRITICAL.value
    candidate.risk_score = 83  # weaker than every incumbent

    outcome = enforce_budget(repository, candidate, previous=previous, now=NOW)

    assert outcome.kind is BudgetOutcomeKind.WITHHELD
    assert outcome.admitted is False
    # Band membership rolled back...
    assert candidate.state == AlertState.OPEN.value
    assert candidate.severity == RiskLevel.HIGH.value
    assert candidate.peak_severity == RiskLevel.HIGH.value
    # ...but the newest reading stays, for the next run to rank on.
    assert candidate.risk_score == 83
    assert outcome.action.kind is AlertActionKind.BUDGET_SUPPRESSED
    assert outcome.action.alert_id == candidate.id


def test_downgrade_into_a_full_high_band_is_withheld() -> None:
    repository = InMemoryAlertRepository()
    repository.alerts.extend(_alert(severity=RiskLevel.HIGH, score=60 + i) for i in range(10))
    candidate = _alert(
        severity=RiskLevel.CRITICAL,
        score=90,
        state=AlertState.ESCALATED,
        peak_severity=RiskLevel.CRITICAL,
    )
    repository.alerts.append(candidate)
    previous = LifecycleSnapshot(
        state=AlertState.ESCALATED, severity=RiskLevel.CRITICAL, peak_severity=RiskLevel.CRITICAL
    )
    # Simulate the run having decided this alert eased from Critical down into High.
    candidate.severity = RiskLevel.HIGH.value
    candidate.state = AlertState.DOWNGRADED.value
    candidate.risk_score = 55  # weaker than every High incumbent

    outcome = enforce_budget(repository, candidate, previous=previous, now=NOW)

    assert outcome.kind is BudgetOutcomeKind.WITHHELD
    # Held one band high: dropping it to satisfy a cap it did not breach would be the one
    # unrecoverable outcome, and the next run retries the demotion.
    assert candidate.state == AlertState.ESCALATED.value
    assert candidate.severity == RiskLevel.CRITICAL.value
    assert candidate.peak_severity == RiskLevel.CRITICAL.value


def test_an_alert_already_holding_its_slot_is_never_evicted_by_an_ordinary_update() -> None:
    class _ExplodingRepository:
        def active_alerts_at_severity(self, severity: str) -> list[Alert]:
            raise AssertionError("must not query the band for an ordinary update")

        def delete_alert(self, alert: Alert) -> None:
            raise AssertionError("must not delete on an ordinary update")

        def flush(self) -> None:
            pass

    candidate = _alert(severity=RiskLevel.CRITICAL, score=80)
    previous = LifecycleSnapshot(
        state=AlertState.OPEN, severity=RiskLevel.CRITICAL, peak_severity=RiskLevel.CRITICAL
    )

    outcome = enforce_budget(_ExplodingRepository(), candidate, previous=previous, now=NOW)

    assert outcome.kind is BudgetOutcomeKind.WITHIN_BUDGET


def test_a_terminal_alert_holds_no_slot_regardless_of_how_full_the_band_is() -> None:
    repository = InMemoryAlertRepository()
    repository.alerts.extend(_alert(severity=RiskLevel.CRITICAL, score=s) for s in (80, 85, 90))
    candidate = _alert(severity=RiskLevel.CRITICAL, score=99, state=AlertState.RESOLVED)

    outcome = enforce_budget(repository, candidate, previous=None, now=NOW)

    assert outcome.kind is BudgetOutcomeKind.WITHIN_BUDGET
    assert outcome.admitted is True


@pytest.mark.parametrize("severity", [RiskLevel.LOW, RiskLevel.MEDIUM])
def test_medium_and_low_are_uncapped(severity: RiskLevel) -> None:
    repository = InMemoryAlertRepository()
    candidate = _alert(severity=severity, score=50)

    outcome = enforce_budget(repository, candidate, previous=None, now=NOW)

    assert outcome.kind is BudgetOutcomeKind.WITHIN_BUDGET
    assert outcome.cap is None


# --------------------------------------------------------------------------------------
# End-to-end through AlertLifecycleService: create vs escalation, actions, and streaks
# --------------------------------------------------------------------------------------


def _scope(condition_class: str, **overrides: Any) -> AlertScope:
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


def _observation(condition_class: str, score: float, **overrides: Any) -> AlertObservation:
    return AlertObservation(scope=_scope(condition_class), risk_score=score, **overrides)


def _service() -> tuple[AlertLifecycleService, InMemoryAlertRepository]:
    repository = InMemoryAlertRepository()
    return AlertLifecycleService(repository), repository


def test_a_critical_beyond_budget_supersedes_the_weakest_incumbent_through_the_service() -> None:
    service, repository = _service()
    incumbents: list[Alert] = []
    for i, score in enumerate((85, 90, 95)):
        outcome = service.evaluate(_observation(f"cond-{i}", score), now=NOW)
        assert outcome.kind is AlertOutcomeKind.CREATED
        incumbents.append(repository.get_alert(outcome.alert_id))

    outcome = service.evaluate(
        _observation("cond-new", 99), now=NOW + datetime.timedelta(minutes=1)
    )

    assert outcome.kind is AlertOutcomeKind.CREATED
    assert outcome.budget is not None
    assert outcome.budget.kind is BudgetOutcomeKind.REPLACED
    victim = min(incumbents, key=lambda a: a.risk_score)
    assert outcome.budget.victim_id == victim.id
    assert victim.state == AlertState.SUPERSEDED.value
    assert victim.superseded_by == outcome.alert_id
    assert len(outcome.actions) == 1
    action = outcome.actions[0]
    assert action.kind is AlertActionKind.SUPERSESSION
    assert action.alert_id == victim.id
    # The candidate itself is admitted and untouched by the supersession of its victim.
    assert repository.get_alert(outcome.alert_id).state == AlertState.OPEN.value


def test_an_escalation_beyond_budget_is_withheld_through_the_service() -> None:
    service, repository = _service()
    for i, score in enumerate((90, 91, 92)):
        outcome = service.evaluate(_observation(f"cond-{i}", score), now=NOW)
        assert outcome.kind is AlertOutcomeKind.CREATED

    create_outcome = service.evaluate(_observation("cond-new", 40), now=NOW)
    assert create_outcome.kind is AlertOutcomeKind.CREATED
    alert_id = create_outcome.alert_id

    outcome = service.evaluate(
        _observation("cond-new", 83), now=NOW + datetime.timedelta(days=1)
    )

    # The escalation into Critical was withheld: nothing about the visible lifecycle moved.
    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert outcome.notification_required is False
    assert outcome.budget is not None
    assert outcome.budget.kind is BudgetOutcomeKind.WITHHELD
    alert = repository.get_alert(alert_id)
    assert alert.state == AlertState.OPEN.value
    assert alert.severity == RiskLevel.MEDIUM.value
    assert alert.risk_score == Decimal("83")  # the new reading is still persisted


def test_a_budget_suppressed_velocity_create_keeps_its_pre_fire_streak() -> None:
    service, repository = _service()
    for i, score in enumerate((90, 91, 92)):
        outcome = service.evaluate(_observation(f"cond-{i}", score), now=NOW)
        assert outcome.kind is AlertOutcomeKind.CREATED

    scope = _scope("velocity-new")

    def _velocity_obs() -> AlertObservation:
        return AlertObservation(
            scope=scope, risk_score=85, kind=ConditionKind.VELOCITY, z_score=3.0
        )

    service.evaluate(_velocity_obs(), now=NOW)  # first qualifying run: persists the streak
    outcome = service.evaluate(_velocity_obs(), now=NOW + datetime.timedelta(days=1))

    assert outcome.kind is AlertOutcomeKind.SUPPRESSED
    assert outcome.budget is not None
    assert outcome.budget.kind is BudgetOutcomeKind.SUPPRESSED
    assert len(repository.alerts) == 3  # the incumbents only; the candidate was never kept
    assert len(repository.condition_states) == 1
    assert repository.condition_states[0].velocity_streak == 2
    assert repository.condition_states[0].dedupe_key == scope.dedupe_key.value
