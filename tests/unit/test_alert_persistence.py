"""Persisted alert lifecycle: dedupe, hysteresis, evidence, cooldown, all-clear (ADR 0010)."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from db.models.core import Alert, AlertConditionState
from db.models.enums import RiskLevel, RiskType
from services.alerts import (
    COOLDOWN,
    RESOLVE_AFTER,
    VELOCITY_REQUIRED_RUNS,
    AlertDedupeConflictError,
    AlertLifecycleService,
    AlertObservation,
    AlertOutcomeKind,
    AlertScope,
    AlertState,
    AlertStateCorruptionError,
    ConditionKind,
    InMemoryAlertRepository,
    ManualReviewCondition,
    SQLAlchemyAlertRepository,
    StaleObservationError,
)

NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)
USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
SIGNAL_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
SIGNAL_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")

#: A structured predicate and a free-text condition, as they arrive from the LLM contract.
CDS_UNDER_150 = {"signal_ref": "cds_spread", "comparator": "lt", "threshold": 150}
DEPOSITS_STABLE = {"signal_ref": "deposit_outflow", "comparator": "lte", "threshold": 0}
FREE_TEXT = "the ECB opens a swap line"


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


def _observation(score: float, **overrides: Any) -> AlertObservation:
    return AlertObservation(scope=overrides.pop("scope", _scope()), risk_score=score, **overrides)


def _service() -> tuple[AlertLifecycleService, InMemoryAlertRepository]:
    repository = InMemoryAlertRepository()
    return AlertLifecycleService(repository), repository


def _open_alert(
    service: AlertLifecycleService, score: float = 40, at: datetime.datetime = NOW, **kw: Any
) -> Alert:
    """Open a live alert and return the persisted row."""
    outcome = service.evaluate(_observation(score, **kw), now=at)
    assert outcome.kind is AlertOutcomeKind.CREATED
    return service._repository.get_alert(outcome.alert_id)  # noqa: SLF001


# --------------------------------------------------------------------------------------
# Create, update, dedupe, idempotency
# --------------------------------------------------------------------------------------


def test_a_qualifying_score_opens_one_alert_and_asks_for_a_notification() -> None:
    service, repository = _service()

    outcome = service.evaluate(_observation(40, score_version="v2"), now=NOW)

    assert outcome.kind is AlertOutcomeKind.CREATED
    assert outcome.notification_required is True
    assert outcome.all_clear_required is False
    assert outcome.severity is RiskLevel.MEDIUM
    assert len(repository.alerts) == 1
    alert = repository.alerts[0]
    assert alert.state == AlertState.OPEN.value
    assert alert.severity == "medium"
    assert alert.peak_severity == "medium"
    assert alert.risk_score == Decimal("40")
    assert alert.user_id == USER
    assert alert.dedupe_key == _scope().dedupe_key.value
    assert alert.last_evaluated_at == NOW
    assert alert.score_version == "v2"
    assert alert.velocity_streak == 0
    # The outcome *requests* a notification; delivery is acknowledged separately.
    assert alert.notified_at is None


def test_a_score_below_the_enter_threshold_opens_nothing() -> None:
    service, repository = _service()

    outcome = service.evaluate(_observation(36), now=NOW)

    assert outcome.kind is AlertOutcomeKind.NOOP
    assert repository.alerts == []


def test_the_same_key_updates_the_live_alert_instead_of_creating_a_second() -> None:
    service, repository = _service()
    alert = _open_alert(service, 40)
    later = NOW + datetime.timedelta(days=1)

    outcome = service.evaluate(_observation(45), now=later)

    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert outcome.alert_id == alert.id
    assert len(repository.alerts) == 1
    assert alert.risk_score == Decimal("45")
    assert alert.updated_at == later
    assert alert.last_evaluated_at == later
    assert alert.state == AlertState.OPEN.value


def test_re_running_the_same_observation_changes_nothing() -> None:
    service, repository = _service()
    observation = _observation(
        40, evidence_refs=[{"article": "a1"}], evidence_signal_ids=[SIGNAL_A]
    )
    service.evaluate(observation, now=NOW)

    outcome = service.evaluate(observation, now=NOW)

    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert len(repository.alerts) == 1
    alert = repository.alerts[0]
    assert alert.evidence_refs == [{"article": "a1"}]
    assert alert.evidence_signal_ids == [SIGNAL_A]
    assert alert.risk_score == Decimal("40")


def test_an_update_refreshes_the_display_but_never_the_identity() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40)
    event_id = uuid.uuid4()

    service.evaluate(
        _observation(
            65,
            scope=_scope(
                title="Eurozone banking stress worsening",
                message="Composite banking risk has escalated.",
                related_event_id=event_id,
            ),
        ),
        now=NOW + datetime.timedelta(days=1),
    )

    assert alert.title == "Eurozone banking stress worsening"
    assert alert.message == "Composite banking risk has escalated."
    assert alert.related_event_id == event_id
    assert alert.user_id == USER
    assert alert.dedupe_key == _scope().dedupe_key.value
    assert alert.alert_type == "risk_score"


def test_a_second_owner_cannot_rewrite_the_first_owners_alert() -> None:
    service, _ = _service()
    _open_alert(service, 40)

    with pytest.raises(ValueError, match="belongs to user"):
        service.evaluate(
            _observation(45, scope=_scope(user_id=uuid.uuid4())),
            now=NOW + datetime.timedelta(days=1),
        )


def test_an_observation_older_than_the_last_evaluation_is_refused() -> None:
    service, _ = _service()
    _open_alert(service, 40)

    with pytest.raises(StaleObservationError):
        service.evaluate(_observation(90), now=NOW - datetime.timedelta(hours=1))


def test_two_live_alerts_for_one_key_are_corruption_not_a_tie_to_break() -> None:
    service, repository = _service()
    _open_alert(service, 40)
    twin = Alert(
        id=uuid.uuid4(),
        user_id=USER,
        title="duplicate",
        message="duplicate",
        severity="medium",
        alert_type="risk_score",
        state=AlertState.OPEN.value,
        dedupe_key=_scope().dedupe_key.value,
    )
    repository.alerts.append(twin)  # bypass the repository's own guard, as a broken index would

    with pytest.raises(AlertStateCorruptionError):
        service.evaluate(_observation(45), now=NOW + datetime.timedelta(days=1))


def test_a_concurrent_insert_of_the_same_key_raises_a_dedupe_conflict() -> None:
    service, _ = _service()
    _open_alert(service, 40)
    # Two evaluators that both saw "no live alert" -- the unique index is the final protection.
    with pytest.raises(AlertDedupeConflictError):
        service._repository.add_alert(  # noqa: SLF001
            Alert(
                id=uuid.uuid4(),
                user_id=USER,
                title="t",
                message="m",
                severity="medium",
                alert_type="risk_score",
                state=AlertState.OPEN.value,
                dedupe_key=_scope().dedupe_key.value,
            )
        )


# --------------------------------------------------------------------------------------
# Lifecycle transitions and their timestamps
# --------------------------------------------------------------------------------------


def test_a_higher_severity_escalates_and_lifts_the_peak() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40)

    outcome = service.evaluate(_observation(65), now=NOW + datetime.timedelta(days=1))

    assert outcome.kind is AlertOutcomeKind.ESCALATED
    assert outcome.notification_required is True
    assert alert.state == AlertState.ESCALATED.value
    assert alert.severity == "high"
    assert alert.peak_severity == "high"


def test_a_hold_zone_score_leaves_the_severity_untouched() -> None:
    service, _ = _service()
    alert = _open_alert(service, 65)  # High

    outcome = service.evaluate(_observation(55), now=NOW + datetime.timedelta(days=1))

    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert alert.severity == "high"
    assert alert.state == AlertState.OPEN.value


def test_crossing_a_clear_band_downgrades_but_keeps_the_alert_live() -> None:
    service, _ = _service()
    alert = _open_alert(service, 65)  # High

    outcome = service.evaluate(_observation(45), now=NOW + datetime.timedelta(days=1))

    assert outcome.kind is AlertOutcomeKind.DOWNGRADED
    assert alert.state == AlertState.DOWNGRADED.value
    assert alert.severity == "medium"
    # A live Medium risk has not ended: no resolve timer, no all-clear.
    assert alert.clear_band_since is None
    assert alert.resolved_at is None
    assert alert.peak_severity == "high"


def test_falling_below_the_clear_band_starts_the_resolve_timer() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40)
    day_one = NOW + datetime.timedelta(days=1)

    outcome = service.evaluate(_observation(20), now=day_one)

    assert outcome.kind is AlertOutcomeKind.DOWNGRADED
    assert alert.severity == "low"
    assert alert.clear_band_since == day_one
    assert alert.resolved_at is None


def test_a_rebound_above_the_clear_band_resets_the_resolve_timer() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40)
    service.evaluate(_observation(20), now=NOW + datetime.timedelta(days=1))

    outcome = service.evaluate(_observation(30), now=NOW + datetime.timedelta(days=3))

    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert alert.state == AlertState.DOWNGRADED.value
    assert alert.clear_band_since is None  # the seven days must start over
    assert alert.resolved_at is None


def test_seven_full_days_below_the_clear_band_resolve_the_alert_with_an_all_clear() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40)
    cleared_at = NOW + datetime.timedelta(days=1)
    service.evaluate(_observation(20), now=cleared_at)

    outcome = service.evaluate(_observation(20), now=cleared_at + RESOLVE_AFTER)

    assert outcome.kind is AlertOutcomeKind.RESOLVED
    assert outcome.all_clear_required is True
    assert outcome.notification_required is False
    assert alert.state == AlertState.RESOLVED.value
    assert alert.resolved_at == cleared_at + RESOLVE_AFTER
    assert alert.cooldown_until == cleared_at + RESOLVE_AFTER + COOLDOWN
    assert alert.peak_severity == "medium"  # the row still remembers what it was
    # The all-clear is requested, not delivered.
    assert alert.all_clear_notified_at is None


def test_an_alert_one_day_short_of_seven_does_not_resolve() -> None:
    service, _ = _service()
    _open_alert(service, 40)
    cleared_at = NOW + datetime.timedelta(days=1)
    service.evaluate(_observation(20), now=cleared_at)

    outcome = service.evaluate(
        _observation(20), now=cleared_at + RESOLVE_AFTER - datetime.timedelta(seconds=1)
    )

    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert outcome.all_clear_required is False


# --------------------------------------------------------------------------------------
# Evidence: appended, de-duplicated, typed, never overwritten
# --------------------------------------------------------------------------------------


def test_evidence_is_appended_and_de_duplicated_not_replaced() -> None:
    service, _ = _service()
    alert = _open_alert(
        service, 40, evidence_refs=[{"article": "a1"}], evidence_signal_ids=[SIGNAL_A]
    )

    service.evaluate(
        _observation(
            45,
            evidence_refs=[{"article": "a2"}, {"article": "a1"}],
            evidence_signal_ids=[SIGNAL_B, SIGNAL_A],
        ),
        now=NOW + datetime.timedelta(days=1),
    )

    assert alert.evidence_refs == [{"article": "a1"}, {"article": "a2"}]
    assert alert.evidence_signal_ids == [SIGNAL_A, SIGNAL_B]
    assert all(isinstance(value, uuid.UUID) for value in alert.evidence_signal_ids)


def test_a_run_without_evidence_preserves_the_history() -> None:
    service, _ = _service()
    alert = _open_alert(
        service, 40, evidence_refs=[{"article": "a1"}], evidence_signal_ids=[SIGNAL_A]
    )

    service.evaluate(_observation(45), now=NOW + datetime.timedelta(days=1))

    assert alert.evidence_refs == [{"article": "a1"}]
    assert alert.evidence_signal_ids == [SIGNAL_A]


def test_signal_ids_given_as_strings_are_stored_as_uuids() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40, evidence_signal_ids=[str(SIGNAL_A)])

    assert alert.evidence_signal_ids == [SIGNAL_A]


# --------------------------------------------------------------------------------------
# Velocity: two consecutive runs, persisted off the alert until it fires
# --------------------------------------------------------------------------------------


def _velocity(score: float, z: float | None, **kw: Any) -> AlertObservation:
    return _observation(
        score,
        scope=_scope(condition_class="news_velocity"),
        kind=ConditionKind.VELOCITY,
        z_score=z,
        **kw,
    )


def test_the_first_qualifying_velocity_run_fires_nothing_but_persists_the_streak() -> None:
    service, repository = _service()

    outcome = service.evaluate(_velocity(10, 3.0), now=NOW)

    assert outcome.kind is AlertOutcomeKind.NOOP
    assert outcome.velocity_streak == 1
    assert repository.alerts == []  # nothing visible, nothing active
    assert len(repository.condition_states) == 1
    state = repository.condition_states[0]
    assert state.velocity_streak == 1
    assert state.last_evaluated_at == NOW
    assert state.dedupe_key == _scope(condition_class="news_velocity").dedupe_key.value


def test_the_second_consecutive_qualifying_run_opens_the_alert() -> None:
    service, repository = _service()
    service.evaluate(_velocity(10, 3.0), now=NOW)

    outcome = service.evaluate(_velocity(10, 2.6), now=NOW + datetime.timedelta(days=1))

    assert outcome.kind is AlertOutcomeKind.CREATED
    assert outcome.velocity_streak == VELOCITY_REQUIRED_RUNS
    assert len(repository.alerts) == 1
    alert = repository.alerts[0]
    assert alert.velocity_streak == 2  # the streak now lives on the alert row
    # A score of 10 would never open a score alert; the streak fired this one, and an alert
    # below the alerting floor is a contradiction, so it opens at Medium.
    assert alert.severity == "medium"
    assert repository.condition_states == []  # one home for the streak, not two


def test_a_velocity_alert_takes_a_higher_severity_from_its_score() -> None:
    service, repository = _service()
    service.evaluate(_velocity(90, 3.0), now=NOW)

    service.evaluate(_velocity(90, 3.0), now=NOW + datetime.timedelta(days=1))

    assert repository.alerts[0].severity == "critical"


@pytest.mark.parametrize("z", [2.5, 1.0, None])
def test_a_non_qualifying_run_resets_the_streak(z: float | None) -> None:
    service, repository = _service()
    service.evaluate(_velocity(10, 3.0), now=NOW)

    outcome = service.evaluate(_velocity(10, z), now=NOW + datetime.timedelta(days=1))

    assert outcome.kind is AlertOutcomeKind.NOOP
    assert outcome.velocity_streak == 0
    assert repository.condition_states[0].velocity_streak == 0
    assert repository.alerts == []


def test_a_broken_streak_must_restart_from_one() -> None:
    service, repository = _service()
    service.evaluate(_velocity(10, 3.0), now=NOW)
    service.evaluate(_velocity(10, 0.5), now=NOW + datetime.timedelta(days=1))

    outcome = service.evaluate(_velocity(10, 3.0), now=NOW + datetime.timedelta(days=2))

    assert outcome.kind is AlertOutcomeKind.NOOP
    assert outcome.velocity_streak == 1
    assert repository.alerts == []


def test_replaying_a_velocity_run_does_not_count_it_twice() -> None:
    service, repository = _service()
    observation = _velocity(10, 3.0)
    service.evaluate(observation, now=NOW)

    outcome = service.evaluate(observation, now=NOW)  # the same run, retried

    assert outcome.kind is AlertOutcomeKind.NOOP
    assert outcome.velocity_streak == 1
    assert repository.condition_states[0].velocity_streak == 1
    assert repository.alerts == []


def test_replaying_a_run_against_a_live_velocity_alert_does_not_count_it_twice() -> None:
    service, repository = _service()
    service.evaluate(_velocity(40, 3.0), now=NOW)
    observation = _velocity(40, 3.0)
    day_one = NOW + datetime.timedelta(days=1)
    service.evaluate(observation, now=day_one)  # opens the alert at streak 2

    service.evaluate(observation, now=day_one)  # the same run, retried

    assert repository.alerts[0].velocity_streak == 2


def test_a_live_velocity_alert_keeps_its_streak_on_the_row() -> None:
    service, repository = _service()
    service.evaluate(_velocity(40, 3.0), now=NOW)
    service.evaluate(_velocity(40, 3.0), now=NOW + datetime.timedelta(days=1))

    service.evaluate(_velocity(40, 0.1), now=NOW + datetime.timedelta(days=2))

    assert repository.alerts[0].velocity_streak == 0


# --------------------------------------------------------------------------------------
# what_could_reduce_risk: machine predicates downgrade, free text never does
# --------------------------------------------------------------------------------------


def test_met_predicates_downgrade_out_of_the_hysteresis_hold_zone() -> None:
    service, _ = _service()
    alert = _open_alert(service, 65, reduction_conditions=[CDS_UNDER_150, DEPOSITS_STABLE])

    # 55 sits in High's hold zone: hysteresis alone would keep this High.
    outcome = service.evaluate(
        _observation(55, signals={"cds_spread": 120, "deposit_outflow": -3}),
        now=NOW + datetime.timedelta(days=1),
    )

    assert outcome.kind is AlertOutcomeKind.DOWNGRADED
    assert outcome.reduction.predicates_met is True
    assert alert.state == AlertState.DOWNGRADED.value
    assert alert.severity == "medium"  # the band a score of 55 enters from scratch
    # A reduction eases a risk; it never ends one.
    assert alert.clear_band_since is None
    assert alert.resolved_at is None
    assert alert.state != AlertState.RESOLVED.value


def test_a_reduction_downgrade_does_not_flap_on_the_next_run() -> None:
    service, _ = _service()
    alert = _open_alert(service, 65, reduction_conditions=[CDS_UNDER_150])
    signals = {"cds_spread": 120}
    service.evaluate(_observation(55, signals=signals), now=NOW + datetime.timedelta(days=1))

    outcome = service.evaluate(
        _observation(55, signals=signals), now=NOW + datetime.timedelta(days=2)
    )

    # It fell to the band the score supports, so the score cannot raise it straight back.
    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert alert.severity == "medium"


def test_free_text_conditions_never_downgrade_an_alert() -> None:
    service, _ = _service()
    alert = _open_alert(service, 65, reduction_conditions=[FREE_TEXT])

    outcome = service.evaluate(_observation(55), now=NOW + datetime.timedelta(days=1))

    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert outcome.reduction.predicates_met is False
    assert outcome.reduction.requires_manual_review is True
    assert alert.severity == "high"


def test_an_unevaluable_predicate_is_never_counted_as_met() -> None:
    service, _ = _service()
    alert = _open_alert(service, 65, reduction_conditions=[CDS_UNDER_150, DEPOSITS_STABLE])

    # One signal met, the other missing entirely: absent data must not buy a downgrade.
    outcome = service.evaluate(
        _observation(55, signals={"cds_spread": 120}), now=NOW + datetime.timedelta(days=1)
    )

    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert outcome.reduction.has_unevaluable is True
    assert alert.severity == "high"


def test_met_predicates_cannot_push_below_the_band_the_score_supports() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40, reduction_conditions=[CDS_UNDER_150])

    outcome = service.evaluate(
        _observation(40, signals={"cds_spread": 10}), now=NOW + datetime.timedelta(days=1)
    )

    # A score of 40 still enters Medium: the reduction has nothing left to give.
    assert outcome.kind is AlertOutcomeKind.UPDATED
    assert alert.severity == "medium"
    assert alert.state == AlertState.OPEN.value


def test_met_predicates_never_mask_an_escalation() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40, reduction_conditions=[CDS_UNDER_150])

    outcome = service.evaluate(
        _observation(90, signals={"cds_spread": 10}), now=NOW + datetime.timedelta(days=1)
    )

    assert outcome.kind is AlertOutcomeKind.ESCALATED
    assert alert.severity == "critical"


def test_conditions_are_stored_as_stable_json_and_re_evaluated_from_the_row() -> None:
    service, _ = _service()
    alert = _open_alert(service, 65, reduction_conditions=[CDS_UNDER_150, FREE_TEXT])

    assert alert.what_could_reduce_risk == [
        {"signal_ref": "cds_spread", "comparator": "lt", "threshold": "150"},
        {"text": FREE_TEXT},
    ]

    # The next run carries signals but no conditions: the persisted ones still drive the run.
    outcome = service.evaluate(
        _observation(55, signals={"cds_spread": 120}), now=NOW + datetime.timedelta(days=1)
    )

    assert outcome.kind is AlertOutcomeKind.DOWNGRADED
    assert alert.what_could_reduce_risk[0]["signal_ref"] == "cds_spread"


def test_a_manual_condition_alone_is_reported_for_review_and_persisted() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40, reduction_conditions=[ManualReviewCondition(FREE_TEXT)])

    assert alert.what_could_reduce_risk == [{"text": FREE_TEXT}]


# --------------------------------------------------------------------------------------
# Cooldown: 24h of silence unless the risk comes back worse
# --------------------------------------------------------------------------------------


def _resolve(service: AlertLifecycleService, peak: float = 40) -> Alert:
    """Open an alert at ``peak``, then walk it through to resolution."""
    alert = _open_alert(service, peak)
    cleared_at = NOW + datetime.timedelta(days=1)
    service.evaluate(_observation(20), now=cleared_at)
    outcome = service.evaluate(_observation(20), now=cleared_at + RESOLVE_AFTER)
    assert outcome.kind is AlertOutcomeKind.RESOLVED
    return alert


def test_a_resolved_key_cannot_re_fire_within_24h_at_the_same_severity() -> None:
    service, repository = _service()
    resolved = _resolve(service, peak=40)

    outcome = service.evaluate(
        _observation(40), now=resolved.resolved_at + datetime.timedelta(hours=1)
    )

    assert outcome.kind is AlertOutcomeKind.SUPPRESSED
    assert len(repository.alerts) == 1  # no second row
    assert repository.alerts[0].state == AlertState.RESOLVED.value


def test_the_cooldown_compares_the_peak_not_the_low_it_resolved_at() -> None:
    service, repository = _service()
    resolved = _resolve(service, peak=65)  # peaked High, resolved at Low

    outcome = service.evaluate(
        _observation(40), now=resolved.resolved_at + datetime.timedelta(hours=1)
    )

    # Medium is higher than the Low on the resolved row -- and lower than the High it reached.
    assert outcome.kind is AlertOutcomeKind.SUPPRESSED
    assert len(repository.alerts) == 1


def test_a_higher_severity_breaks_the_cooldown() -> None:
    service, repository = _service()
    resolved = _resolve(service, peak=40)  # peaked Medium

    outcome = service.evaluate(
        _observation(65), now=resolved.resolved_at + datetime.timedelta(hours=1)
    )

    assert outcome.kind is AlertOutcomeKind.CREATED
    assert outcome.severity is RiskLevel.HIGH
    assert len(repository.alerts) == 2
    assert repository.alerts[0].state == AlertState.RESOLVED.value  # history is untouched


def test_the_exact_24h_boundary_is_eligible_to_re_fire() -> None:
    service, _ = _service()
    resolved = _resolve(service, peak=40)

    outcome = service.evaluate(_observation(40), now=resolved.cooldown_until)

    assert outcome.kind is AlertOutcomeKind.CREATED


def test_one_second_before_the_boundary_is_still_suppressed() -> None:
    service, _ = _service()
    resolved = _resolve(service, peak=40)

    outcome = service.evaluate(
        _observation(40), now=resolved.cooldown_until - datetime.timedelta(seconds=1)
    )

    assert outcome.kind is AlertOutcomeKind.SUPPRESSED


def test_a_row_that_predates_peak_severity_falls_back_to_its_severity() -> None:
    service, repository = _service()
    legacy = Alert(
        id=uuid.uuid4(),
        user_id=USER,
        title="t",
        message="m",
        severity="high",
        peak_severity=None,  # written before the column existed; never backfilled with a guess
        alert_type="risk_score",
        state=AlertState.RESOLVED.value,
        dedupe_key=_scope().dedupe_key.value,
        resolved_at=NOW,
        cooldown_until=NOW + COOLDOWN,
    )
    repository.alerts.append(legacy)

    outcome = service.evaluate(_observation(40), now=NOW + datetime.timedelta(hours=1))

    assert outcome.kind is AlertOutcomeKind.SUPPRESSED  # compared against the row's own severity
    assert len(repository.alerts) == 1


def test_a_suppressed_velocity_condition_keeps_its_streak() -> None:
    service, repository = _service()
    scope = _scope(condition_class="news_velocity")
    resolved = Alert(
        id=uuid.uuid4(),
        user_id=USER,
        title="t",
        message="m",
        severity="low",
        peak_severity="high",
        alert_type="risk_score",
        state=AlertState.RESOLVED.value,
        dedupe_key=scope.dedupe_key.value,
        resolved_at=NOW,
        cooldown_until=NOW + COOLDOWN,
    )
    repository.alerts.append(resolved)

    service.evaluate(_velocity(10, 3.0), now=NOW + datetime.timedelta(hours=1))
    outcome = service.evaluate(_velocity(10, 3.0), now=NOW + datetime.timedelta(hours=2))

    assert outcome.kind is AlertOutcomeKind.SUPPRESSED
    assert repository.condition_states[0].velocity_streak == 2  # ready the moment it may fire
    assert len(repository.alerts) == 1


# --------------------------------------------------------------------------------------
# Terminal history: resolved and superseded rows
# --------------------------------------------------------------------------------------


def test_a_superseded_row_is_never_revived_and_never_blocks_a_new_alert() -> None:
    service, repository = _service()
    successor = uuid.uuid4()
    superseded = Alert(
        id=uuid.uuid4(),
        user_id=USER,
        title="t",
        message="m",
        severity="high",
        peak_severity="high",
        alert_type="risk_score",
        state=AlertState.SUPERSEDED.value,
        dedupe_key=_scope().dedupe_key.value,
        superseded_by=successor,
    )
    repository.alerts.append(superseded)

    outcome = service.evaluate(_observation(40), now=NOW)

    assert outcome.kind is AlertOutcomeKind.CREATED
    assert outcome.alert_id != superseded.id
    # Supersession is not a resolution: it arms no cooldown and it stays terminal.
    assert superseded.state == AlertState.SUPERSEDED.value
    assert superseded.superseded_by == successor
    assert len(repository.alerts) == 2


def test_a_resolved_key_re_fires_into_a_new_row_once_the_cooldown_has_run() -> None:
    service, repository = _service()
    resolved = _resolve(service, peak=40)

    outcome = service.evaluate(
        _observation(40), now=resolved.cooldown_until + datetime.timedelta(hours=1)
    )

    assert outcome.kind is AlertOutcomeKind.CREATED
    assert len(repository.alerts) == 2
    assert resolved.state == AlertState.RESOLVED.value
    assert resolved.resolved_at is not None


# --------------------------------------------------------------------------------------
# Notification acknowledgement
# --------------------------------------------------------------------------------------


def test_notification_timestamps_record_delivery_not_intent() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40)
    assert alert.notified_at is None

    delivered_at = NOW + datetime.timedelta(minutes=5)
    assert service.acknowledge_notification(alert.id, at=delivered_at) is True

    assert alert.notified_at == delivered_at


def test_an_all_clear_is_acknowledged_exactly_once() -> None:
    service, _ = _service()
    resolved = _resolve(service)
    delivered_at = resolved.resolved_at + datetime.timedelta(minutes=5)

    assert service.acknowledge_all_clear(resolved.id, at=delivered_at) is True
    assert resolved.all_clear_notified_at == delivered_at

    assert service.acknowledge_all_clear(resolved.id, at=delivered_at) is False
    assert resolved.all_clear_notified_at == delivered_at


def test_a_live_alert_cannot_be_all_cleared() -> None:
    service, _ = _service()
    alert = _open_alert(service, 40)

    with pytest.raises(ValueError, match="cannot all-clear"):
        service.acknowledge_all_clear(alert.id, at=NOW)


def test_acknowledging_an_unknown_alert_raises() -> None:
    service, _ = _service()

    with pytest.raises(LookupError):
        service.acknowledge_notification(uuid.uuid4(), at=NOW)


# --------------------------------------------------------------------------------------
# Invalid input
# --------------------------------------------------------------------------------------


def test_a_scope_needs_a_real_owner() -> None:
    with pytest.raises(TypeError, match="user_id"):
        _scope(user_id="not-a-uuid")


def test_a_company_alert_must_name_its_company() -> None:
    with pytest.raises(ValueError, match="related_company_id"):
        _scope(risk_type=RiskType.COMPANY, scope_entity="nvda")


def test_an_alert_cannot_be_scoped_to_a_company_and_an_industry() -> None:
    with pytest.raises(ValueError, match="not both"):
        _scope(related_company_id=uuid.uuid4(), related_industry_id="banking")


@pytest.mark.parametrize("blank", ["", "   "])
def test_display_fields_must_not_be_blank(blank: str) -> None:
    with pytest.raises(ValueError, match="title"):
        _scope(title=blank)


def test_a_score_condition_may_not_carry_a_z_score() -> None:
    with pytest.raises(ValueError, match="velocity condition"):
        _observation(40, z_score=3.0)


def test_an_out_of_range_score_is_refused() -> None:
    with pytest.raises(ValueError, match="0-100"):
        _observation(140)


def test_a_naive_now_is_refused() -> None:
    service, _ = _service()

    with pytest.raises(ValueError, match="timezone-aware"):
        service.evaluate(_observation(40), now=datetime.datetime(2026, 7, 13, 12, 0))


def test_a_malformed_evidence_signal_id_is_refused() -> None:
    with pytest.raises(ValueError, match="not a UUID"):
        _observation(40, evidence_signal_ids=["nope"])


def test_a_malformed_reduction_predicate_is_refused() -> None:
    with pytest.raises(ValueError, match="comparator"):
        _observation(40, reduction_conditions=[{"signal_ref": "cds", "comparator": "eq"}])


def test_evidence_refs_that_are_not_a_json_list_are_refused() -> None:
    service, repository = _service()
    alert = _open_alert(service, 40)
    alert.evidence_refs = {"article": "a1"}  # a corrupt row, not a list

    with pytest.raises(ValueError, match="JSON list"):
        service.evaluate(_observation(45), now=NOW + datetime.timedelta(days=1))

    assert len(repository.alerts) == 1


# --------------------------------------------------------------------------------------
# The SQLAlchemy repository: bounded, locking, and not the transaction's owner
# --------------------------------------------------------------------------------------


def _session_returning(rows: list[Any]) -> MagicMock:
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = rows
    session.execute.return_value.scalars.return_value.first.return_value = rows[0] if rows else None
    return session


def _sql(session: MagicMock) -> str:
    statement = session.execute.call_args[0][0]
    return str(statement.compile(dialect=postgresql.dialect()))


def test_the_live_alert_lookup_is_bounded_and_locks_the_row() -> None:
    session = _session_returning([])
    repository = SQLAlchemyAlertRepository(session)

    assert repository.active_for_key("k") is None

    sql = _sql(session)
    assert "FOR UPDATE" in sql
    assert "LIMIT" in sql
    assert "state IN" in sql.replace("\n", " ")


def test_the_live_alert_lookup_fetches_two_rows_so_a_duplicate_is_seen() -> None:
    session = _session_returning([MagicMock(spec=Alert), MagicMock(spec=Alert)])
    repository = SQLAlchemyAlertRepository(session)

    with pytest.raises(AlertStateCorruptionError, match="uq_alerts_active_dedupe_key"):
        repository.active_for_key("k")


def test_the_resolved_lookup_takes_the_newest_row_only() -> None:
    session = _session_returning([MagicMock(spec=Alert)])
    repository = SQLAlchemyAlertRepository(session)

    repository.latest_resolved_for_key("k")

    sql = _sql(session).replace("\n", " ")
    assert "LIMIT" in sql
    assert "resolved_at DESC" in sql
    assert "'resolved'" in sql or "state = " in sql


def test_the_condition_state_lookup_is_bounded_and_locks_the_row() -> None:
    session = _session_returning([])
    repository = SQLAlchemyAlertRepository(session)

    assert repository.condition_state_for_key("k") is None

    sql = _sql(session)
    assert "FOR UPDATE" in sql
    assert "LIMIT" in sql


def test_the_repository_flushes_and_never_owns_the_transaction() -> None:
    session = _session_returning([])
    repository = SQLAlchemyAlertRepository(session)
    alert = MagicMock(spec=Alert)
    state = MagicMock(spec=AlertConditionState)

    repository.add_alert(alert)
    repository.add_condition_state(state)
    repository.delete_condition_state(state)
    repository.flush()

    assert session.flush.call_count == 4
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def test_a_lost_dedupe_race_becomes_a_typed_conflict_and_leaves_the_session_alone() -> None:
    session = _session_returning([])
    session.flush.side_effect = IntegrityError("insert", {}, Exception("duplicate key"))
    repository = SQLAlchemyAlertRepository(session)

    with pytest.raises(AlertDedupeConflictError):
        repository.add_alert(MagicMock(spec=Alert))

    # The caller owns the transaction: rolling it back here would discard the caller's work.
    session.rollback.assert_not_called()
    session.commit.assert_not_called()
