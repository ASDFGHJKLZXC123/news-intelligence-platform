"""Evaluate one observation against the persisted alert for its dedupe key (ADR 0010).

This is the only writer of the alert lifecycle. It decides nothing itself: severity comes from
:mod:`services.alerts.hysteresis`, the transition from :mod:`services.alerts.lifecycle`, the
velocity streak from :mod:`services.alerts.velocity`, and the reduction verdict from
:mod:`services.alerts.reduction`. Its job is to hand those decisions the state they need, write
what they return to the row, and tell the caller -- in one typed outcome -- what happened and
whether anything must be sent to a human.

Three rules earn their keep here:

**Reduction versus hysteresis.** When every machine-checkable `what_could_reduce_risk` predicate
is met, the alert drops to the band the *current score* would enter from scratch -- no further.
Hysteresis holds a severity through its hold zone (a High alert stays High at 55); a met
reduction says that hold is no longer warranted, so the alert falls to Medium immediately
instead of waiting for the score to cross 50. It cannot fall below what the score supports,
because the very next run's hysteresis would raise it straight back -- that is flapping, which
is the one thing this ADR exists to prevent. It cannot resolve, either: resolution is the 7-day
score condition and nothing else, so a met predicate can never manufacture a false all-clear.
Free text is never machine-evaluated, so it can never move an alert at all.

**Cooldown.** A resolved key is silent for 24h unless the new severity beats the severity that
key *peaked* at -- not the Low it decayed to on the way out, which every new alert would beat.

**Idempotency.** ``now`` is the run's identity. Re-running an observation at the same ``now``
recomputes the same lifecycle (the decisions are pure functions of state and score) and refuses
to advance the velocity streak a second time; an observation older than the row's last
evaluation is rejected outright rather than allowed to rewind a resolve timer.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from db.models.core import Alert, AlertConditionState
from db.models.enums import RiskLevel, RiskType
from services.alerts.budget import (
    BudgetOutcome,
    LifecycleSnapshot,
    enforce_budget,
)
from services.alerts.contribution import news_driven_contribution
from services.alerts.dedupe import DedupeKey, build_dedupe_key
from services.alerts.experimental import ExperimentalGate, ScoreBasis
from services.alerts.hysteresis import (
    ALERTING_FLOOR,
    coerce_level,
    next_severity,
    raise_target,
    severity_rank,
)
from services.alerts.lifecycle import (
    AlertLifecycleState,
    AlertState,
    LifecycleDecision,
    LifecycleTrigger,
    decide_lifecycle,
)
from services.alerts.notifications import PendingAlertAction
from services.alerts.numeric import Numeric, to_score
from services.alerts.reduction import (
    ManualReviewCondition,
    ReductionAssessment,
    ReductionCondition,
    ReductionPredicate,
    evaluate_reduction_conditions,
    parse_reduction_conditions,
)
from services.alerts.repository import AlertRepository
from services.alerts.supersession import (
    SupersessionResult,
)
from services.alerts.supersession import (
    supersede_with_broader_alert as _supersede_with_broader_alert,
)
from services.alerts.velocity import VELOCITY_REQUIRED_RUNS, decide_velocity_persistence

#: ADR 0010: a resolved key cannot re-fire for this long unless the new severity is higher.
COOLDOWN = datetime.timedelta(hours=24)


class StaleObservationError(ValueError):
    """An observation older than the row's last evaluation.

    Replaying runs out of order would rewind the resolve timer and double-count velocity runs,
    so an out-of-order observation is refused rather than absorbed.
    """


class ConditionKind(StrEnum):
    """What makes this condition fire.

    A ``SCORE`` condition fires the moment the score enters an alerting band. A ``VELOCITY``
    condition (`z > 2.5`) fires only once it has held for two consecutive runs, so its first
    qualifying run is remembered on an `alert_condition_states` row that no alerts query can see.
    """

    SCORE = "score"
    VELOCITY = "velocity"


class AlertOutcomeKind(StrEnum):
    """What one evaluation did to the persisted alert."""

    CREATED = "created"
    UPDATED = "updated"
    ESCALATED = "escalated"
    DOWNGRADED = "downgraded"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"
    NOOP = "noop"


@dataclass(frozen=True)
class AlertScope:
    """Who owns an alert, what it is about, and how it is displayed.

    There is no default user: an alert nobody owns is an alert nobody is served, and inventing an
    owner would silently hand one user's risk to another.
    """

    user_id: uuid.UUID
    risk_type: RiskType | str
    scope_entity: str
    condition_class: str
    title: str
    message: str
    alert_type: str
    related_company_id: uuid.UUID | None = None
    related_industry_id: str | None = None
    related_event_id: uuid.UUID | None = None
    alert_rule_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, uuid.UUID):
            raise TypeError(f"user_id must be a UUID, got {type(self.user_id).__name__}")
        for name in ("title", "message", "alert_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-blank string")
        # A scope is one thing. A row claiming to be both a company alert and an industry alert
        # cannot be rendered, filtered, or reasoned about -- and its dedupe key names only one.
        if self.related_company_id is not None and self.related_industry_id is not None:
            raise ValueError("an alert is scoped to a company or an industry, not both")
        risk_type = self.dedupe_key.risk_type
        if risk_type is RiskType.COMPANY and self.related_company_id is None:
            raise ValueError("a company risk alert must carry related_company_id")

    @property
    def dedupe_key(self) -> DedupeKey:
        """The `(risk_type, scope_entity, condition_class)` identity this alert dedupes on."""
        return build_dedupe_key(self.risk_type, self.scope_entity, self.condition_class)


@dataclass(frozen=True)
class AlertObservation:
    """One pipeline run's reading of one condition.

    ``reduction_conditions=None`` means "leave the persisted conditions alone"; an empty sequence
    means "this condition has none", and clears them. ``news_contributions=None`` likewise
    preserves the stored `news_driven` badge rather than overwriting it with a zero the run never
    measured.

    ``score_basis=None`` means the caller did not state what produced the score. The null-model
    gate (ADR 0010) applies only to composite scores, so an unstated basis is resolved
    fail-closed to :attr:`ScoreBasis.COMPOSITE` for a score condition -- and to
    :attr:`ScoreBasis.SINGLE_SIGNAL` for a velocity condition, which is single-signal by
    definition and which the ADR never gates behind a composite backtest.
    """

    scope: AlertScope
    risk_score: Numeric
    kind: ConditionKind = ConditionKind.SCORE
    z_score: Numeric | None = None
    signals: Mapping[str, Any] = field(default_factory=dict)
    reduction_conditions: Any = None
    evidence_signal_ids: Sequence[uuid.UUID | str] = ()
    evidence_refs: Sequence[Any] = ()
    news_contributions: Sequence[Numeric] | None = None
    score_version: str | None = None
    score_basis: ScoreBasis | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ConditionKind(self.kind))
        to_score(self.risk_score)
        if self.kind is ConditionKind.SCORE and self.z_score is not None:
            raise ValueError("z_score belongs to a velocity condition, not a score condition")
        if self.score_basis is not None:
            object.__setattr__(self, "score_basis", ScoreBasis(self.score_basis))
        object.__setattr__(
            self, "reduction_conditions", _coerce_conditions(self.reduction_conditions)
        )
        object.__setattr__(
            self, "evidence_signal_ids", tuple(_coerce_uuid(v) for v in self.evidence_signal_ids)
        )
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    @property
    def score(self) -> Decimal:
        return to_score(self.risk_score)


@dataclass(frozen=True)
class AlertOutcome:
    """What one evaluation did, and what the caller must now send.

    ``notification_required``/``all_clear_required`` are *requests*, not receipts: the row's
    `notified_at`/`all_clear_notified_at` are written only when delivery is acknowledged, via
    :meth:`AlertLifecycleService.acknowledge_notification` and
    :meth:`AlertLifecycleService.acknowledge_all_clear`.

    ``budget`` is the verdict of the platform budget (ADR 0010) when this run entered a capped
    band, and ``actions`` carries the :class:`PendingAlertAction`\\ s that verdict produced for
    *other* rows -- the supersession notice a replaced incumbent owes its owner, or the
    audit-only budget-suppressed line for a candidate the cap refused. The caller delivers them
    through :func:`services.alerts.notifications.deliver_actions` alongside the alert's own
    notification.
    """

    kind: AlertOutcomeKind
    dedupe_key: str
    reason: str
    alert_id: uuid.UUID | None = None
    state: AlertState | None = None
    severity: RiskLevel | None = None
    velocity_streak: int = 0
    notification_required: bool = False
    all_clear_required: bool = False
    reduction: ReductionAssessment | None = None
    budget: BudgetOutcome | None = None
    actions: tuple[PendingAlertAction, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether the alert's own lifecycle moved on this run."""
        return self.kind not in (AlertOutcomeKind.NOOP, AlertOutcomeKind.SUPPRESSED)


class AlertLifecycleService:
    """Evaluate observations against persisted alerts. Flushes; never commits.

    ``gate`` is the null-model gate (ADR 0010) consulted once, when an alert is created, to set
    `alerts.experimental`. It defaults to an :class:`ExperimentalGate` with no evidence -- the
    state the platform is actually in until the gold-datasets backtest lands (stage 9) -- which
    fails closed: every composite-score alert opens experimental, and only single-signal
    conditions open released.
    """

    def __init__(
        self, repository: AlertRepository, *, gate: ExperimentalGate | None = None
    ) -> None:
        self._repository = repository
        self._gate = gate if gate is not None else ExperimentalGate()

    def evaluate(self, observation: AlertObservation, *, now: datetime.datetime) -> AlertOutcome:
        """Evaluate one observation against the live alert for its key, if any."""
        _require_aware(now, field="now")
        key = observation.scope.dedupe_key
        alert = self._repository.active_for_key(key.value)
        if alert is not None:
            return self._evaluate_live(alert, observation, key, now)
        return self._evaluate_absent(observation, key, now)

    def acknowledge_notification(self, alert_id: uuid.UUID, *, at: datetime.datetime) -> bool:
        """Record that a notification for this alert was actually delivered.

        Escalations re-notify by design, so this records the *latest* delivery.
        """
        _require_aware(at, field="at")
        alert = self._require_alert(alert_id)
        alert.notified_at = at
        alert.updated_at = at
        self._repository.flush()
        return True

    def acknowledge_all_clear(self, alert_id: uuid.UUID, *, at: datetime.datetime) -> bool:
        """Record that the all-clear was delivered. Returns False if it already had been.

        An all-clear is sent once and only for a resolved alert: announcing that a live risk has
        ended is the one message this system must never send twice, or wrongly.
        """
        _require_aware(at, field="at")
        alert = self._require_alert(alert_id)
        if alert.state != AlertState.RESOLVED.value:
            raise ValueError(f"cannot all-clear an alert in state {alert.state!r}")
        if alert.all_clear_notified_at is not None:
            return False
        alert.all_clear_notified_at = at
        alert.updated_at = at
        self._repository.flush()
        return True

    # -- live alert ---------------------------------------------------------------------

    def _evaluate_live(
        self,
        alert: Alert,
        observation: AlertObservation,
        key: DedupeKey,
        now: datetime.datetime,
    ) -> AlertOutcome:
        replay = self._check_run_order(alert.last_evaluated_at, now)
        self._require_owner(alert, observation.scope)
        # Snapshotted before this run's mutation: the budget's "previous" is the band the row
        # held coming in, so a refusal below has something to restore.
        previous = LifecycleSnapshot.of(alert)
        score = observation.score

        streak = alert.velocity_streak or 0
        if observation.kind is ConditionKind.VELOCITY and not replay:
            streak = decide_velocity_persistence(observation.z_score, streak).streak

        conditions = self._conditions_for(alert, observation)
        assessment = evaluate_reduction_conditions(conditions, observation.signals)
        decision = decide_lifecycle(
            AlertLifecycleState(
                state=AlertState(alert.state),
                severity=coerce_level(alert.severity),
                clear_band_since=alert.clear_band_since,
            ),
            score,
            now=now,
        )
        state, severity, reason, reduced = _apply_reduction(decision, score, assessment)
        resolved = decision.trigger is LifecycleTrigger.RESOLVED

        alert.state = state.value
        alert.severity = severity.value
        alert.peak_severity = _peak(alert.peak_severity, severity).value
        alert.risk_score = score
        # A reduction downgrade never touches a running resolve timer: it only fires while the
        # severity is still Medium or above, and `clear_band_since` is only ever set at Low.
        alert.clear_band_since = decision.clear_band_since
        alert.resolved_at = decision.resolved_at
        alert.velocity_streak = streak
        alert.last_evaluated_at = now
        alert.updated_at = now

        # ADR 0010: the platform budget is enforced on every entry into a capped band -- create,
        # escalate into it, or downgrade into it. It is a no-op for an alert that already held its
        # slot (an ordinary update) and for a transition into an uncapped or terminal state, so it
        # is safe -- and cheap -- to consult on every live run rather than only on some of them.
        budget_outcome = enforce_budget(self._repository, alert, previous=previous, now=now)

        self._write_display(alert, observation)
        if resolved:
            alert.cooldown_until = now + COOLDOWN
        self._write_condition_payload(alert, observation, conditions, score)
        self._repository.flush()

        final_state = AlertState(alert.state)
        final_severity = coerce_level(alert.severity)
        kind = _outcome_kind(decision.trigger, reduced)
        final_reason = reason
        if not budget_outcome.admitted:
            # The budget rolled the row back to the band it held before this run: nothing about
            # its visible lifecycle actually moved, so the outcome is an update, not the
            # escalation or downgrade that was attempted and refused.
            kind = AlertOutcomeKind.UPDATED
            final_reason = budget_outcome.reason

        return AlertOutcome(
            kind=kind,
            dedupe_key=key.value,
            reason=final_reason,
            alert_id=alert.id,
            state=final_state,
            severity=final_severity,
            velocity_streak=streak,
            notification_required=kind is AlertOutcomeKind.ESCALATED,
            all_clear_required=resolved,
            reduction=assessment,
            budget=budget_outcome,
            actions=_budget_actions(budget_outcome),
        )

    # -- no live alert ------------------------------------------------------------------

    def _evaluate_absent(
        self, observation: AlertObservation, key: DedupeKey, now: datetime.datetime
    ) -> AlertOutcome:
        score = observation.score
        # `current=None` is item 1's "no alert yet": nothing below the Medium enter threshold
        # (37) raises one, so this is also the severity a new alert opens at.
        entering = next_severity(score, None)
        condition_state: AlertConditionState | None = None
        streak = 0

        if observation.kind is ConditionKind.VELOCITY:
            condition_state, streak, persisted = self._advance_velocity(key, observation, now)
            if not persisted:
                return AlertOutcome(
                    kind=AlertOutcomeKind.NOOP,
                    dedupe_key=key.value,
                    velocity_streak=streak,
                    reason=(
                        f"velocity streak {streak}/{VELOCITY_REQUIRED_RUNS}: a velocity alert "
                        "opens only on the second consecutive qualifying run"
                    ),
                )
            # The streak, not the score, fired this alert -- but an alert below the alerting
            # floor is a contradiction in terms (item 1: below Medium there is nothing to alert
            # on), so a velocity alert opens at Medium unless its score already justifies more.
            severity = max(entering, ALERTING_FLOOR, key=severity_rank)
        else:
            if severity_rank(entering) < severity_rank(ALERTING_FLOOR):
                return AlertOutcome(
                    kind=AlertOutcomeKind.NOOP,
                    dedupe_key=key.value,
                    reason=f"score {score} is below the {ALERTING_FLOOR.value} enter threshold",
                )
            severity = entering

        blocking = self._cooldown_block(key, severity, now)
        if blocking is not None:
            return AlertOutcome(
                kind=AlertOutcomeKind.SUPPRESSED,
                dedupe_key=key.value,
                velocity_streak=streak,
                severity=severity,
                reason=(
                    f"key resolved at peak severity "
                    f"{(blocking.peak_severity or blocking.severity)}; a {severity.value} "
                    f"condition cannot re-fire before {blocking.cooldown_until}"
                ),
            )

        alert = self._create(observation, key, score, severity, streak, now)

        # ADR 0010: a new Critical (or High) beyond budget must supersede an existing one, or --
        # for a candidate that does not outrank the weakest incumbent -- lose its slot outright.
        # `previous=None` because this row did not exist a moment ago.
        budget_outcome = enforce_budget(self._repository, alert, previous=None, now=now)
        if not budget_outcome.admitted:
            # The row is deleted and was never visible, so its pre-fire velocity streak (if any)
            # must not be retired either -- the condition's persistence is not lost with it.
            return AlertOutcome(
                kind=AlertOutcomeKind.SUPPRESSED,
                dedupe_key=key.value,
                reason=budget_outcome.reason,
                severity=severity,
                velocity_streak=streak,
                notification_required=False,
                all_clear_required=False,
                budget=budget_outcome,
                actions=_budget_actions(budget_outcome),
            )

        if condition_state is not None:
            # The alert row owns the streak from here (ADR 0010); one home, one truth -- and
            # only now that the budget has actually admitted the alert into a slot.
            self._repository.delete_condition_state(condition_state)
        assessment = evaluate_reduction_conditions(
            observation.reduction_conditions or (), observation.signals
        )
        return AlertOutcome(
            kind=AlertOutcomeKind.CREATED,
            dedupe_key=key.value,
            reason=f"score {score} opened a {severity.value} alert",
            alert_id=alert.id,
            state=AlertState.OPEN,
            severity=severity,
            velocity_streak=streak,
            notification_required=True,
            all_clear_required=False,
            reduction=assessment,
            budget=budget_outcome,
            actions=_budget_actions(budget_outcome),
        )

    def _advance_velocity(
        self, key: DedupeKey, observation: AlertObservation, now: datetime.datetime
    ) -> tuple[AlertConditionState | None, int, bool]:
        """Advance the pre-fire velocity streak on its own row, and report whether it has held."""
        row = self._repository.condition_state_for_key(key.value)
        stored = row.velocity_streak if row is not None else 0
        if row is not None and self._check_run_order(row.last_evaluated_at, now):
            # A retried run must not count as a second consecutive one.
            return row, stored, stored >= VELOCITY_REQUIRED_RUNS

        decision = decide_velocity_persistence(observation.z_score, stored)
        if row is None:
            if decision.streak == 0:
                return None, 0, False  # a non-qualifying run with no history persists nothing
            row = AlertConditionState(
                dedupe_key=key.value,
                velocity_streak=decision.streak,
                last_evaluated_at=now,
                created_at=now,
                updated_at=now,
            )
            self._repository.add_condition_state(row)
        else:
            row.velocity_streak = decision.streak
            row.last_evaluated_at = now
            row.updated_at = now
            self._repository.flush()
        return row, decision.streak, decision.persisted

    def _cooldown_block(
        self, key: DedupeKey, severity: RiskLevel, now: datetime.datetime
    ) -> Alert | None:
        """Return the resolved alert whose cooldown blocks this key, if one does."""
        resolved = self._repository.latest_resolved_for_key(key.value)
        if resolved is None or resolved.cooldown_until is None:
            return None
        if now >= resolved.cooldown_until:  # the exact 24h boundary is eligible to re-fire
            return None
        # Not the Low it decayed to on the way out -- the severity this key actually reached.
        peak = coerce_level(resolved.peak_severity or resolved.severity)
        if severity_rank(severity) > severity_rank(peak):
            return None  # genuinely worse than the risk that just ended: the cooldown yields
        return resolved

    def _create(
        self,
        observation: AlertObservation,
        key: DedupeKey,
        score: Decimal,
        severity: RiskLevel,
        streak: int,
        now: datetime.datetime,
    ) -> Alert:
        scope = observation.scope
        # ADR 0010's null-model gate is consulted once, here, at creation: a composite alert
        # opens experimental until the gold-datasets backtest beats the baseline, and a
        # single-signal condition (velocity, or an explicitly-stated basis) is carved out and
        # opens released.
        gate_decision = self._gate.decide(_score_basis_for(observation))
        alert = Alert(
            id=uuid.uuid4(),
            user_id=scope.user_id,
            alert_rule_id=scope.alert_rule_id,
            title=scope.title,
            message=scope.message,
            severity=severity.value,
            peak_severity=severity.value,
            risk_score=score,
            alert_type=scope.alert_type,
            state=AlertState.OPEN.value,
            related_event_id=scope.related_event_id,
            related_company_id=scope.related_company_id,
            related_industry_id=scope.related_industry_id,
            dedupe_key=key.value,
            velocity_streak=streak,
            experimental=gate_decision.experimental,
            last_evaluated_at=now,
            created_at=now,
            updated_at=now,
        )
        self._write_condition_payload(alert, observation, observation.reduction_conditions, score)
        self._repository.add_alert(alert)
        return alert

    def supersede_with_broader_alert(
        self,
        *,
        broader_alert_id: uuid.UUID,
        narrower_alert_ids: Sequence[uuid.UUID],
        now: datetime.datetime,
    ) -> SupersessionResult:
        """Replace one or more active narrower alerts with an explicitly-named broader one.

        ADR 0010's ``any -> superseded`` transition: a caller (an operator action, or a rule
        that recognises one alert as a broader restatement of others) names the successor and
        the alerts it replaces. This is a thin pass-through to
        :func:`services.alerts.supersession.supersede_with_broader_alert` over this service's
        own repository, so callers only need this service's public surface.
        """
        _require_aware(now, field="now")
        return _supersede_with_broader_alert(
            self._repository,
            broader_alert_id=broader_alert_id,
            narrower_alert_ids=narrower_alert_ids,
            now=now,
        )

    # -- shared writes ------------------------------------------------------------------

    def _conditions_for(
        self, alert: Alert, observation: AlertObservation
    ) -> tuple[ReductionCondition, ...]:
        if observation.reduction_conditions is not None:
            return observation.reduction_conditions
        return parse_reduction_conditions(alert.what_could_reduce_risk)

    def _write_condition_payload(
        self,
        alert: Alert,
        observation: AlertObservation,
        conditions: tuple[ReductionCondition, ...] | None,
        score: Decimal,
    ) -> None:
        """Write the run's evidence, reduction conditions, and badges onto the row.

        Evidence is history: it is appended and de-duplicated, never replaced. An alert that
        forgot why it fired cannot be reviewed, and the newest run is not the whole story.
        """
        alert.evidence_signal_ids = _merge_signal_ids(
            alert.evidence_signal_ids, observation.evidence_signal_ids
        )
        alert.evidence_refs = _merge_refs(alert.evidence_refs, observation.evidence_refs)
        if observation.reduction_conditions is not None and conditions is not None:
            alert.what_could_reduce_risk = [condition.as_payload() for condition in conditions]
        if observation.news_contributions is not None:
            alert.news_driven = news_driven_contribution(observation.news_contributions, score)
        if observation.score_version is not None:
            alert.score_version = observation.score_version

    def _require_owner(self, alert: Alert, scope: AlertScope) -> None:
        """Refuse to rewrite an alert that belongs to someone else.

        `uq_alerts_active_dedupe_key` is global, not per-user, so two owners can contend for one
        key. Whoever opened the alert keeps it: quietly stamping a second user's title and message
        onto the first user's row would show one user another user's words.
        """
        if alert.user_id != scope.user_id:
            raise ValueError(
                f"alert {alert.id} belongs to user {alert.user_id}, "
                f"not {scope.user_id}; the dedupe key is platform-wide"
            )

    def _write_display(self, alert: Alert, observation: AlertObservation) -> None:
        """Refresh the display and scope fields from the newest run.

        The title and message describe the condition *now* -- an escalated alert still captioned
        with the sentence it opened with is a stale alert. Identity is not refreshed: `user_id`,
        `alert_type`, and `dedupe_key` are what make this row the same alert. A related id is only
        written when the run supplies one, so a run that omits it does not sever the link.
        """
        scope = observation.scope
        alert.title = scope.title
        alert.message = scope.message
        if scope.related_event_id is not None:
            alert.related_event_id = scope.related_event_id
        if scope.related_company_id is not None:
            alert.related_company_id = scope.related_company_id
        if scope.related_industry_id is not None:
            alert.related_industry_id = scope.related_industry_id

    def _check_run_order(self, last: datetime.datetime | None, now: datetime.datetime) -> bool:
        """Return whether ``now`` replays the row's last run; raise if it predates it."""
        if last is None:
            return False
        _require_aware(last, field="last_evaluated_at")
        if now < last:
            raise StaleObservationError(
                f"observation at {now} predates the last evaluation at {last}"
            )
        return now == last

    def _require_alert(self, alert_id: uuid.UUID) -> Alert:
        alert = self._repository.get_alert(alert_id)
        if alert is None:
            raise LookupError(f"unknown alert {alert_id}")
        return alert


def _apply_reduction(
    decision: LifecycleDecision, score: Decimal, assessment: ReductionAssessment
) -> tuple[AlertState, RiskLevel, str, bool]:
    """Fold a met `what_could_reduce_risk` verdict into the score's lifecycle decision.

    The reduction may only pull the alert down to the band the score itself would enter from
    scratch: below that, hysteresis would raise it straight back on the next run. It may not
    touch a resolution, and -- because it fires only while the severity is still Medium or above
    -- it can neither start nor cancel a resolve timer. So a met predicate accelerates a
    downgrade the score already supports; it can never invent an all-clear.
    """
    if decision.trigger is LifecycleTrigger.RESOLVED or not assessment.predicates_met:
        return decision.state, decision.severity, decision.reason, False
    floor = raise_target(score)
    if severity_rank(floor) >= severity_rank(decision.severity):
        return decision.state, decision.severity, decision.reason, False
    return (
        AlertState.DOWNGRADED,
        floor,
        (
            f"every machine-checkable reduction condition is met; severity "
            f"{decision.severity.value} -> {floor.value} (the band score {score} supports)"
        ),
        True,
    )


def _score_basis_for(observation: AlertObservation) -> ScoreBasis:
    """Resolve what produced this observation's score, defaulting fail-closed (ADR 0010).

    A caller-stated basis always wins. Absent one, a score condition defaults to
    :attr:`ScoreBasis.COMPOSITE` -- the null-model gate applies until told otherwise -- and a
    velocity condition defaults to :attr:`ScoreBasis.SINGLE_SIGNAL`, which is single-signal by
    definition and which the ADR never gates behind a composite backtest.
    """
    if observation.score_basis is not None:
        return ScoreBasis(observation.score_basis)
    if observation.kind is ConditionKind.VELOCITY:
        return ScoreBasis.SINGLE_SIGNAL
    return ScoreBasis.COMPOSITE


def _budget_actions(outcome: BudgetOutcome) -> tuple[PendingAlertAction, ...]:
    """The action(s) one budget verdict owes delivery, if any."""
    return () if outcome.action is None else (outcome.action,)


def _outcome_kind(trigger: LifecycleTrigger, reduced: bool) -> AlertOutcomeKind:
    if trigger is LifecycleTrigger.RESOLVED:
        return AlertOutcomeKind.RESOLVED
    if trigger is LifecycleTrigger.ESCALATED:
        return AlertOutcomeKind.ESCALATED
    if reduced or trigger is LifecycleTrigger.DOWNGRADED:
        return AlertOutcomeKind.DOWNGRADED
    return AlertOutcomeKind.UPDATED


def _peak(current: str | None, severity: RiskLevel) -> RiskLevel:
    if current is None:
        return severity
    return max(coerce_level(current), severity, key=severity_rank)


def _merge_signal_ids(
    existing: Sequence[uuid.UUID] | None, incoming: Sequence[uuid.UUID | str]
) -> list[uuid.UUID]:
    merged = [_coerce_uuid(value) for value in (existing or ())]
    seen = set(merged)
    for value in incoming:
        signal_id = _coerce_uuid(value)
        if signal_id not in seen:
            seen.add(signal_id)
            merged.append(signal_id)
    return merged


def _merge_refs(existing: Any, incoming: Sequence[Any]) -> list[Any]:
    if existing is None:
        merged: list[Any] = []
    elif isinstance(existing, list):
        merged = list(existing)
    else:
        raise ValueError(f"evidence_refs must be a JSON list, found {type(existing).__name__}")
    seen = {_canonical(ref) for ref in merged}
    for ref in incoming:
        fingerprint = _canonical(ref)
        if fingerprint not in seen:
            seen.add(fingerprint)
            merged.append(ref)
    return merged


def _canonical(ref: Any) -> str:
    """A stable fingerprint for a JSON evidence ref, so re-running a run appends nothing."""
    return json.dumps(ref, sort_keys=True, default=str, separators=(",", ":"))


def _coerce_conditions(payload: Any) -> tuple[ReductionCondition, ...] | None:
    if payload is None:
        return None
    items = list(payload) if isinstance(payload, list | tuple) else [payload]
    return parse_reduction_conditions(
        [
            item.as_payload()
            if isinstance(item, ReductionPredicate | ManualReviewCondition)
            else item
            for item in items
        ]
    )


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise ValueError(f"evidence signal id is not a UUID: {value!r}") from exc
    raise TypeError(f"evidence signal id must be a UUID, got {type(value).__name__}")


def _require_aware(value: datetime.datetime, *, field: str) -> None:
    if not isinstance(value, datetime.datetime):
        raise TypeError(f"{field} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must be timezone-aware")
