"""Persistence for the alert lifecycle (ADR 0010).

A narrow repository over the `alerts` and `alert_condition_states` rows one dedupe key owns.
It holds no policy -- :mod:`services.alerts.service` decides, this stores -- and it never owns
the transaction: writes are *flushed* so the caller sees its own rows, but commit, rollback,
and close belong to whoever opened the session.

Two invariants are enforced here rather than assumed:

* **At most one live alert per key.** `uq_alerts_active_dedupe_key` is the final protection, and
  the lookups take `SELECT ... FOR UPDATE` so two concurrent evaluators of the same key serialise
  instead of racing. A second active row is corruption -- a broken invariant, and the loudest
  thing this module can do about it is refuse to guess which row is the real one.
* **Queries are bounded.** Every read has a `LIMIT`; the ones that must be unique fetch two rows
  precisely so a duplicate is *detected* rather than silently ordered away.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models.core import ACTIVE_ALERT_STATES, Alert, AlertConditionState
from services.alerts.lifecycle import AlertState

#: How many active alerts one severity band is read at a time. The budget caps that band at 3 or
#: 10, so this is far above any legitimate population: reaching it means the caps are already
#: breached, and the budget's pick stays deterministic (and only ever evicts a strictly weaker
#: alert) either way.
ACTIVE_BAND_SCAN_LIMIT = 64


class AlertPersistenceError(RuntimeError):
    """Base class for persistence faults the caller must handle rather than retry blindly."""


class AlertStateCorruptionError(AlertPersistenceError):
    """More than one live alert exists for a dedupe key.

    The partial unique index makes this impossible while it is intact, so reaching it means the
    index is gone or was never applied. Picking a row (by UUID, by age) would hide that and let a
    duplicate alert keep flapping, so the evaluation fails instead.
    """


class AlertDedupeConflictError(AlertPersistenceError):
    """A concurrent worker opened the alert for this key first.

    Raised from the `uq_alerts_active_dedupe_key` violation. The session is left exactly as the
    failure found it: the caller owns the transaction, so the caller rolls back and re-evaluates.
    """


@runtime_checkable
class AlertRepository(Protocol):
    """The persistence surface the lifecycle service needs, and nothing more."""

    def active_for_key(self, dedupe_key: str) -> Alert | None: ...

    def latest_resolved_for_key(self, dedupe_key: str) -> Alert | None: ...

    def active_alerts_at_severity(self, severity: str) -> list[Alert]: ...

    def condition_state_for_key(self, dedupe_key: str) -> AlertConditionState | None: ...

    def get_alert(self, alert_id: uuid.UUID) -> Alert | None: ...

    def add_alert(self, alert: Alert) -> None: ...

    def delete_alert(self, alert: Alert) -> None: ...

    def add_condition_state(self, state: AlertConditionState) -> None: ...

    def delete_condition_state(self, state: AlertConditionState) -> None: ...

    def flush(self) -> None: ...


class SQLAlchemyAlertRepository(AlertRepository):
    """Durable repository over a caller-owned :class:`~sqlalchemy.orm.Session`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def active_for_key(self, dedupe_key: str) -> Alert | None:
        """Return the live alert for ``dedupe_key``, locking it for this transaction.

        Two rows are fetched so that a duplicate raises instead of being ordered away. The lock
        serialises concurrent evaluators of the same key; it cannot stop two *inserts* of a key
        that has no row yet, which is what the partial unique index is for.
        """
        statement = (
            select(Alert)
            .where(Alert.dedupe_key == dedupe_key, Alert.state.in_(ACTIVE_ALERT_STATES))
            .order_by(Alert.created_at.asc(), Alert.id.asc())
            .limit(2)
            .with_for_update()
        )
        rows = list(self._session.execute(statement).scalars().all())
        return _exactly_one_active(rows, dedupe_key)

    def latest_resolved_for_key(self, dedupe_key: str) -> Alert | None:
        """Return the most recently resolved alert for ``dedupe_key``: the one whose cooldown runs.

        Resolved rows accumulate -- a key may live and end many times -- so this is a deterministic
        pick of the newest, not a uniqueness check. Superseded rows are excluded: supersession
        replaced an alert, it did not end a risk, so it neither arms a cooldown nor all-clears.
        """
        statement = (
            select(Alert)
            .where(Alert.dedupe_key == dedupe_key, Alert.state == AlertState.RESOLVED.value)
            .order_by(Alert.resolved_at.desc(), Alert.id.desc())
            .limit(1)
        )
        return self._session.execute(statement).scalars().first()

    def active_alerts_at_severity(self, severity: str) -> list[Alert]:
        """Lock every active alert in one severity band, so the platform budget can rank them.

        The rows are taken in ascending id order -- the same order every other multi-row lock in
        this package takes -- so two workers admitting into the same band serialise instead of
        deadlocking, and neither sees a stale count.
        """
        statement = (
            select(Alert)
            .where(Alert.severity == severity, Alert.state.in_(ACTIVE_ALERT_STATES))
            .order_by(Alert.id.asc())
            .limit(ACTIVE_BAND_SCAN_LIMIT)
            .with_for_update()
        )
        return list(self._session.execute(statement).scalars().all())

    def condition_state_for_key(self, dedupe_key: str) -> AlertConditionState | None:
        """Return the pre-fire condition state for ``dedupe_key``, locked for this transaction."""
        statement = (
            select(AlertConditionState)
            .where(AlertConditionState.dedupe_key == dedupe_key)
            .order_by(AlertConditionState.id.asc())
            .limit(2)
            .with_for_update()
        )
        rows = list(self._session.execute(statement).scalars().all())
        if len(rows) > 1:
            raise AlertStateCorruptionError(
                f"{len(rows)} condition-state rows for dedupe_key {dedupe_key!r}; "
                "the unique constraint on alert_condition_states.dedupe_key is not in force"
            )
        return rows[0] if rows else None

    def get_alert(self, alert_id: uuid.UUID) -> Alert | None:
        return self._session.get(Alert, alert_id, with_for_update=True)

    def add_alert(self, alert: Alert) -> None:
        """Insert an alert, translating a lost dedupe race into :class:`AlertDedupeConflictError`.

        The session is not rolled back here: it belongs to the caller, and rolling it back would
        silently discard work the caller had already done in the same transaction.
        """
        self._session.add(alert)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise AlertDedupeConflictError(
                f"an active alert already exists for dedupe_key {alert.dedupe_key!r}"
            ) from exc

    def delete_alert(self, alert: Alert) -> None:
        """Remove an alert this transaction created and the budget then refused a slot.

        Only ever called on a row inserted moments earlier in the same uncommitted transaction, so
        nothing that any user or query has ever seen is deleted here.
        """
        self._session.delete(alert)
        self._session.flush()

    def add_condition_state(self, state: AlertConditionState) -> None:
        self._session.add(state)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise AlertDedupeConflictError(
                f"condition state already exists for dedupe_key {state.dedupe_key!r}"
            ) from exc

    def delete_condition_state(self, state: AlertConditionState) -> None:
        self._session.delete(state)
        self._session.flush()

    def flush(self) -> None:
        self._session.flush()


class InMemoryAlertRepository(AlertRepository):
    """Deterministic in-memory repository for unit tests and local runs.

    It enforces the same invariants the database does -- one live alert and one condition state
    per key -- so a test that passes here is testing the rules, not the absence of them.
    """

    def __init__(self) -> None:
        self.alerts: list[Alert] = []
        self.condition_states: list[AlertConditionState] = []
        self.flushes = 0

    def active_for_key(self, dedupe_key: str) -> Alert | None:
        rows = [
            alert
            for alert in self.alerts
            if alert.dedupe_key == dedupe_key and alert.state in ACTIVE_ALERT_STATES
        ]
        return _exactly_one_active(rows, dedupe_key)

    def latest_resolved_for_key(self, dedupe_key: str) -> Alert | None:
        resolved = [
            alert
            for alert in self.alerts
            if alert.dedupe_key == dedupe_key
            and alert.state == AlertState.RESOLVED.value
            and alert.resolved_at is not None
        ]
        if not resolved:
            return None
        return max(resolved, key=lambda alert: (alert.resolved_at, alert.id))

    def active_alerts_at_severity(self, severity: str) -> list[Alert]:
        rows = [
            alert
            for alert in self.alerts
            if alert.severity == severity and alert.state in ACTIVE_ALERT_STATES
        ]
        return sorted(rows, key=lambda alert: alert.id)[:ACTIVE_BAND_SCAN_LIMIT]

    def condition_state_for_key(self, dedupe_key: str) -> AlertConditionState | None:
        rows = [state for state in self.condition_states if state.dedupe_key == dedupe_key]
        if len(rows) > 1:
            raise AlertStateCorruptionError(
                f"{len(rows)} condition-state rows for dedupe_key {dedupe_key!r}"
            )
        return rows[0] if rows else None

    def get_alert(self, alert_id: uuid.UUID) -> Alert | None:
        for alert in self.alerts:
            if alert.id == alert_id:
                return alert
        return None

    def add_alert(self, alert: Alert) -> None:
        if alert.dedupe_key is not None and alert.state in ACTIVE_ALERT_STATES:
            if any(
                other.dedupe_key == alert.dedupe_key and other.state in ACTIVE_ALERT_STATES
                for other in self.alerts
            ):
                raise AlertDedupeConflictError(
                    f"an active alert already exists for dedupe_key {alert.dedupe_key!r}"
                )
        if alert.id is None:
            alert.id = uuid.uuid4()
        self.alerts.append(alert)
        self.flush()

    def delete_alert(self, alert: Alert) -> None:
        self.alerts = [other for other in self.alerts if other.id != alert.id]
        self.flush()

    def add_condition_state(self, state: AlertConditionState) -> None:
        if any(other.dedupe_key == state.dedupe_key for other in self.condition_states):
            raise AlertDedupeConflictError(
                f"condition state already exists for dedupe_key {state.dedupe_key!r}"
            )
        if state.id is None:
            state.id = uuid.uuid4()
        self.condition_states.append(state)
        self.flush()

    def delete_condition_state(self, state: AlertConditionState) -> None:
        self.condition_states = [other for other in self.condition_states if other.id != state.id]
        self.flush()

    def flush(self) -> None:
        self.flushes += 1


def _exactly_one_active(rows: list[Alert], dedupe_key: str) -> Alert | None:
    if len(rows) > 1:
        raise AlertStateCorruptionError(
            f"{len(rows)} active alerts for dedupe_key {dedupe_key!r}; "
            "uq_alerts_active_dedupe_key is not in force. Refusing to pick one"
        )
    return rows[0] if rows else None
