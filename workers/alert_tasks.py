"""Celery tasks wiring the ADR 0010 alert lifecycle engine into a running worker.

Two tasks live here:

* :func:`run_alert_evaluation` -- the pipeline task. It takes a batch of already-scored,
  already-owned observations, evaluates each through :class:`AlertLifecycleService`, and
  delivers whatever notifications/all-clears/supersession notices the run produced through
  :func:`services.alerts.notifications.deliver_actions`.
* :func:`run_pending_alert_notification_sweep` -- a Beat-scheduled retry net. A notifier can
  fail, and ADR 0010's design is that a ``NULL`` `notified_at`/`all_clear_notified_at` is
  exactly how a retry finds the message still owed (see
  :mod:`services.alerts.notifications`). This sweep finds every active alert that has never
  been notified and every resolved alert whose all-clear was never acknowledged, and
  redelivers.

Nothing is loaded, connected, or configured at import, matching
:mod:`workers.entity_linking_tasks`: the notifier is built inside the task, and the session is
opened there too.

## Transaction ownership, and why a delivery is never inside the evaluation's transaction

Both tasks own their session, and both split it the same way, because a send cannot be un-sent.
The lifecycle -- every create, escalate, downgrade, resolve, and supersession one run decided --
is committed in one transaction *before* the first notifier call. Only then are the actions
delivered, and each delivered action's receipt (`notified_at` / `all_clear_notified_at`) is
committed on its own before the next action is sent.

The alternative -- deliver, then commit the run -- is what makes ADR 0010's all-clear a lie: a
commit that fails after the all-clear went out rolls the resolution back, the task retries, the
alert resolves a second time, and the one message the platform must never send twice is sent
twice. With the lifecycle already durable, a retry re-evaluates an alert that is *already*
resolved, decides nothing, and owes nothing; the un-acknowledged message stays owed on its NULL
timestamp and is redelivered by the sweep under the same idempotency key
(:attr:`~services.alerts.notifications.PendingAlertAction.idempotency_key`) rather than as a new
lifecycle event. Delivery is at-least-once by design and always was; what it now is not, is
capable of announcing an ending that never happened.

## Why this task takes observations, not "current risk scores" (the signal_fusion seam)

The task description asks this pipeline to "obtain current risk scores/signals via
signal_fusion if it exposes a usable entry point". It does not, yet, and not by omission --
:func:`services.risk.signal_fusion.build_rating_payload` is a pure function: given a
caller-supplied list of ``RiskSignalInput``, it returns a ``RatingPayload``. Nothing in this
repo persists that payload anywhere. There is no writer for `risk_score_observations`,
`company_risk_rollups`, or `industry_risk_rollups` today (grepped for every ORM
construction site; the only references are the read-only ``select`` queries in
``apps/api/intelligence.py``), and ``apps/api/risk_intelligence.py`` exposes
``build_rating_driver_payload`` only as a stateless POST endpoint -- request body in, JSON
out, no DB session. So there is no "current score for entity X" this task could read without
inventing one.

There is a second, sharper reason not to bridge a signal_fusion payload straight into an
alert even if one existed: :class:`~services.alerts.service.AlertScope` requires a
``user_id`` and refuses a default owner ("there is no default user: an alert nobody owns is
an alert nobody is served"). A risk score is scoped to a company/industry/country, not a
user; turning one into an alert requires resolving *which user's watchlist* that target
belongs to, and that join does not exist yet either. Inventing it here -- e.g. broadcasting
every risk score to every user -- would be exactly the kind of alert fatigue ADR 0010 exists
to prevent, decided unilaterally by a worker task instead of by a real ownership model.

So the seam this task exposes is the observation itself: whichever future stage assembles
owned, scored conditions (a risk-scoring pipeline that reads signal_fusion payloads, joined
against watchlist ownership -- Stage 5/6 territory, not Stage 4) calls
:func:`run_alert_evaluation` with a list of JSON-safe observation payloads.
:func:`observation_from_payload` documents the exact shape, which mirrors
:class:`~services.alerts.service.AlertObservation`/:class:`~services.alerts.service.AlertScope`
field for field. Composite-score alerts are not specially suppressed here: ADR 0010's
null-model gate is enforced service-side
(:class:`services.alerts.experimental.ExperimentalGate`, consulted by
:class:`~services.alerts.service.AlertLifecycleService` when an alert opens and defaulting,
here, to no backtest evidence -- every composite alert opens experimental), so this task only
has to pass ``score_basis`` through untouched, never police it.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.base import SessionLocal
from db.models import ACTIVE_ALERT_STATES, Alert
from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.config.settings import get_settings
from packages.jobs import Stage1Job
from services.alerts import (
    AlertLifecycleService,
    AlertObservation,
    AlertOutcome,
    AlertScope,
    AlertState,
    ConditionKind,
    SQLAlchemyAlertRepository,
)
from services.alerts.hysteresis import coerce_level
from services.alerts.notifications import (
    AlertActionKind,
    AlertNotifier,
    LoggingAlertNotifier,
    PendingAlertAction,
    deliver_actions,
)
from services.writer_mode import require_legacy_writer_mode
from workers.celery_app import QUEUE_PIPELINE, Stage1Task

logger = get_logger("workers.alert_tasks")

RUN_ALERT_EVALUATION = "workers.alert_tasks.run_alert_evaluation"
RUN_ALERT_NOTIFICATION_SWEEP = "workers.alert_tasks.run_pending_alert_notification_sweep"


def build_notifier() -> AlertNotifier:
    """The production notifier: a structured log line, no vendor (see notifications module).

    A separate, real hook point -- swapped for a fake in tests via ``monkeypatch``, exactly
    like :func:`workers.entity_linking_tasks.build_mention_adjudicator`.
    """
    return LoggingAlertNotifier()


def _legacy_alert_sessions() -> tuple[Session | None, Any]:
    """Return a continuous mode fence plus the alert work transaction."""

    candidate = SessionLocal()
    if not isinstance(candidate, Session):
        return None, candidate
    try:
        require_legacy_writer_mode(candidate, lock=True)
        return candidate, SessionLocal()
    except BaseException:
        candidate.rollback()
        candidate.close()
        raise


def _parse_now(now: str | datetime.datetime | None) -> datetime.datetime:
    """Coerce the task's ``now`` argument to an aware UTC datetime, defaulting to the wall clock."""
    if now is None:
        return datetime.datetime.now(datetime.UTC)
    if isinstance(now, datetime.datetime):
        parsed = now
    else:
        parsed = datetime.datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _coerce_uuid(value: Any, *, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value:
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise ValueError(f"{field} is not a UUID: {value!r}") from exc
    raise ValueError(f"{field} must be a non-empty UUID string, got {value!r}")


def _optional_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return _coerce_uuid(value, field="related id")


def observation_from_payload(payload: Mapping[str, Any]) -> AlertObservation:
    """Build an :class:`AlertObservation` from a JSON-serializable task payload.

    Expected shape (mirrors ``AlertScope``/``AlertObservation`` field for field)::

        {
            "scope": {
                "user_id": "<uuid>",
                "risk_type": "company" | "sovereign" | ...,
                "scope_entity": "<dedupe scope string>",
                "condition_class": "<dedupe condition string>",
                "title": "...",
                "message": "...",
                "alert_type": "...",
                "related_company_id": "<uuid>" | None,
                "related_industry_id": "<str>" | None,
                "related_event_id": "<uuid>" | None,
                "alert_rule_id": "<uuid>" | None,
            },
            "risk_score": 62.5,           # int/float -- NOT a string; see numeric.to_score
            "kind": "score" | "velocity",  # default "score"
            "z_score": 2.7 | None,
            "signals": {...},
            "reduction_conditions": [...] | None,
            "evidence_signal_ids": ["<uuid>", ...],
            "evidence_refs": [{...}, ...],
            "news_contributions": [numbers] | None,
            "score_version": "<str>" | None,
            "score_basis": "composite" | "single_signal" | None,
        }

    This is the seam documented in the module docstring: the task takes already-scored,
    already-owned observations rather than discovering them itself.
    """
    scope_payload = payload["scope"]
    scope = AlertScope(
        user_id=_coerce_uuid(scope_payload["user_id"], field="scope.user_id"),
        risk_type=scope_payload["risk_type"],
        scope_entity=scope_payload["scope_entity"],
        condition_class=scope_payload["condition_class"],
        title=scope_payload["title"],
        message=scope_payload["message"],
        alert_type=scope_payload["alert_type"],
        related_company_id=_optional_uuid(scope_payload.get("related_company_id")),
        related_industry_id=scope_payload.get("related_industry_id"),
        related_event_id=_optional_uuid(scope_payload.get("related_event_id")),
        alert_rule_id=_optional_uuid(scope_payload.get("alert_rule_id")),
    )
    return AlertObservation(
        scope=scope,
        risk_score=payload["risk_score"],
        kind=ConditionKind(payload.get("kind", ConditionKind.SCORE.value)),
        z_score=payload.get("z_score"),
        signals=payload.get("signals") or {},
        reduction_conditions=payload.get("reduction_conditions"),
        evidence_signal_ids=tuple(payload.get("evidence_signal_ids") or ()),
        evidence_refs=tuple(payload.get("evidence_refs") or ()),
        news_contributions=payload.get("news_contributions"),
        score_version=payload.get("score_version"),
        score_basis=payload.get("score_basis"),
    )


def _notification_action(outcome: AlertOutcome, scope: AlertScope) -> PendingAlertAction:
    return PendingAlertAction(
        kind=AlertActionKind.NOTIFICATION,
        alert_id=outcome.alert_id,
        user_id=scope.user_id,
        dedupe_key=outcome.dedupe_key,
        severity=outcome.severity,
        reason=outcome.reason,
    )


def _all_clear_action(outcome: AlertOutcome, scope: AlertScope) -> PendingAlertAction:
    # Resolution emits an explicit all-clear entry -- de-escalation is information, not
    # silence (ADR 0010).
    return PendingAlertAction(
        kind=AlertActionKind.ALL_CLEAR,
        alert_id=outcome.alert_id,
        user_id=scope.user_id,
        dedupe_key=outcome.dedupe_key,
        severity=outcome.severity,
        reason=f"resolved: {outcome.reason}",
    )


def _outcome_summary(outcome: AlertOutcome) -> dict[str, Any]:
    return {
        "dedupe_key": outcome.dedupe_key,
        "kind": outcome.kind.value,
        "alert_id": None if outcome.alert_id is None else str(outcome.alert_id),
        "state": None if outcome.state is None else outcome.state.value,
        "severity": None if outcome.severity is None else outcome.severity.value,
        "velocity_streak": outcome.velocity_streak,
        "notification_required": outcome.notification_required,
        "all_clear_required": outcome.all_clear_required,
        "reason": outcome.reason,
    }


@shared_task(name=RUN_ALERT_EVALUATION, base=Stage1Task, queue=QUEUE_PIPELINE)
def run_alert_evaluation(
    observations: Sequence[Mapping[str, Any]],
    now: str | None = None,
) -> dict[str, Any]:
    """Evaluate one pipeline run's score observations through the ADR 0010 alert engine.

    For each observation: build the engine's typed ``AlertObservation``, evaluate it through
    :class:`AlertLifecycleService` (which decides create/escalate/downgrade/resolve/suppress),
    and collect whatever :class:`~services.alerts.notifications.PendingAlertAction`\\ s the
    evaluation and the platform budget produced.

    Then, and only then, deliver. The whole batch of lifecycle decisions is committed in one
    transaction -- an evaluation is not partially true, so it is still one commit -- before a
    single notifier is called, and each delivered action's receipt is committed before the next
    action is sent (see :func:`~services.alerts.notifications.deliver_actions`). A delivery that
    fails, or whose receipt will not commit, leaves ``notified_at`` / ``all_clear_notified_at``
    NULL for :func:`run_pending_alert_notification_sweep` to redeliver, and takes nothing durable
    down with it.

    A failure *before* the lifecycle commit -- a malformed payload, a stale observation, a lost
    dedupe race -- rolls the whole evaluation back and re-raises for Stage1Task to retry, having
    delivered nothing at all.
    """
    if not get_settings().crisis_prediction_reads_enabled:
        # Gate G is also an output gate: evaluating these observations can persist a composite
        # alert and notify it. Return before constructing either the DB session or notifier.
        return {
            "status": "skipped",
            "state": "skipped",
            "reason": "crisis_prediction_reads_disabled",
            "evaluated": 0,
            "outcomes": [],
            "delivered": 0,
            "failed": 0,
        }

    run_at = _parse_now(now)
    job = Stage1Job.create(
        RUN_ALERT_EVALUATION, {"observation_count": len(observations)}
    ).mark_running()
    fence_session, session = _legacy_alert_sessions()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)

    try:
        repository = SQLAlchemyAlertRepository(session)
        service = AlertLifecycleService(repository)
        notifier = build_notifier()

        outcomes: list[AlertOutcome] = []
        actions: list[PendingAlertAction] = []
        for payload in observations:
            observation = observation_from_payload(payload)
            outcome = service.evaluate(observation, now=run_at)
            outcomes.append(outcome)
            if outcome.notification_required:
                actions.append(_notification_action(outcome, observation.scope))
            if outcome.all_clear_required:
                actions.append(_all_clear_action(outcome, observation.scope))
            actions.extend(outcome.actions)

        # Every lifecycle decision this run made becomes durable here, before the first notifier
        # call. Nothing delivered below can be rolled back, so nothing delivered below may sit in
        # a transaction that could still roll the alert that owed it back.
        session.commit()

        report = deliver_actions(
            actions, notifier=notifier, acknowledger=service, transaction=session, now=run_at
        )

        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        logger.info(
            "alert evaluation pipeline completed",
            extra={
                "observations": len(observations),
                "actions": len(actions),
                "delivered": len(report.delivered),
                "failed": len(report.failed),
            },
        )
        return {
            "status": "ok",
            "job_id": completed.job_id,
            "job_key": completed.job_key,
            "state": completed.state.value,
            "evaluated": len(outcomes),
            "outcomes": [_outcome_summary(outcome) for outcome in outcomes],
            "delivered": len(report.delivered),
            "failed": len(report.failed),
        }
    except Exception:
        # Before the commit above this discards the whole evaluation, which is the point: a
        # partially-evaluated batch is not a batch. After it, there is nothing durable left to
        # discard -- the lifecycle is committed and every receipt commits on its own -- so the
        # retry re-evaluates rows that already moved and re-owes only what was never acknowledged.
        session.rollback()
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("alert evaluation pipeline failed")
        raise
    finally:
        try:
            session.close()
        finally:
            if fence_session is not None:
                try:
                    fence_session.rollback()
                finally:
                    fence_session.close()
            set_job_id(None)


@shared_task(name=RUN_ALERT_NOTIFICATION_SWEEP, base=Stage1Task, queue=QUEUE_PIPELINE)
def run_pending_alert_notification_sweep(now: str | None = None) -> dict[str, Any]:
    """Retry-deliver every notification and all-clear the platform still owes (ADR 0010).

    A notifier can fail without raising (:class:`~services.alerts.notifications.AlertNotifier`
    may return ``False``) or the process can die between delivery and acknowledgement; either
    way the row is left with a NULL ``notified_at``/``all_clear_notified_at``, which
    :mod:`services.alerts.notifications` documents as exactly how a retry finds the message
    still owed. This sweep is the retry: every active alert never notified, and every resolved
    alert whose all-clear was never acknowledged, redelivered on a fixed cadence
    (``celery_app.BEAT_SCHEDULE``) independently of whatever pipeline stage evaluates scores.

    This never re-runs the lifecycle decision -- an alert's severity or state is not
    recomputed here -- it only resends what an earlier run already decided was owed. So it writes
    no lifecycle state and has none to commit up front: it reads the owed rows in a deterministic
    order (oldest first, id-tiebroken), delivers them in that order, and commits each receipt
    before the next send, exactly as :func:`run_alert_evaluation` does. A receipt that will not
    commit costs its own action and nothing else; that action keeps its NULL timestamp, and the
    next sweep finds it owed again.
    """
    if not get_settings().crisis_prediction_reads_enabled:
        # Existing persisted alerts may have been produced by the prediction-backed path.
        # Closed Gate G must not read or deliver them, including scheduled retries.
        return {
            "status": "skipped",
            "state": "skipped",
            "reason": "crisis_prediction_reads_disabled",
            "pending_notifications": 0,
            "pending_all_clears": 0,
            "delivered": 0,
            "failed": 0,
        }

    run_at = _parse_now(now)
    job = Stage1Job.create(RUN_ALERT_NOTIFICATION_SWEEP, {}).mark_running()
    fence_session, session = _legacy_alert_sessions()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)

    try:
        repository = SQLAlchemyAlertRepository(session)
        service = AlertLifecycleService(repository)
        notifier = build_notifier()

        # Ordered, so a redelivery run is reproducible and two sweeps take the same rows in the
        # same order rather than racing each other into a different one.
        pending_notifications = list(
            session.execute(
                select(Alert)
                .where(
                    Alert.state.in_(ACTIVE_ALERT_STATES),
                    Alert.notified_at.is_(None),
                )
                .order_by(Alert.created_at.asc(), Alert.id.asc())
            )
            .scalars()
            .all()
        )
        pending_all_clears = list(
            session.execute(
                select(Alert)
                .where(
                    Alert.state == AlertState.RESOLVED.value,
                    Alert.all_clear_notified_at.is_(None),
                )
                .order_by(Alert.resolved_at.asc(), Alert.id.asc())
            )
            .scalars()
            .all()
        )

        actions = [
            PendingAlertAction(
                kind=AlertActionKind.NOTIFICATION,
                alert_id=alert.id,
                user_id=alert.user_id,
                dedupe_key=alert.dedupe_key,
                severity=coerce_level(alert.severity),
                reason="retry: notification was not yet acknowledged",
            )
            for alert in pending_notifications
        ] + [
            PendingAlertAction(
                kind=AlertActionKind.ALL_CLEAR,
                alert_id=alert.id,
                user_id=alert.user_id,
                dedupe_key=alert.dedupe_key,
                severity=coerce_level(alert.severity),
                reason="retry: all-clear was not yet acknowledged",
            )
            for alert in pending_all_clears
        ]

        # Built from the rows before anything is delivered: an action carries plain values, so no
        # step below has to touch an ORM row a per-receipt commit has since expired.
        report = deliver_actions(
            actions, notifier=notifier, acknowledger=service, transaction=session, now=run_at
        )

        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        logger.info(
            "alert notification sweep completed",
            extra={
                "pending_notifications": len(pending_notifications),
                "pending_all_clears": len(pending_all_clears),
                "delivered": len(report.delivered),
                "failed": len(report.failed),
            },
        )
        return {
            "status": "ok",
            "job_id": completed.job_id,
            "job_key": completed.job_key,
            "state": completed.state.value,
            "pending_notifications": len(pending_notifications),
            "pending_all_clears": len(pending_all_clears),
            "delivered": len(report.delivered),
            "failed": len(report.failed),
        }
    except Exception:
        session.rollback()
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("alert notification sweep failed")
        raise
    finally:
        try:
            session.close()
        finally:
            if fence_session is not None:
                try:
                    fence_session.rollback()
                finally:
                    fence_session.close()
            set_job_id(None)
