"""Supersession: any -> superseded, refused rather than guessed at (ADR 0010)."""

from __future__ import annotations

import datetime
import uuid

import pytest

from db.models.core import Alert
from db.models.enums import RiskLevel
from services.alerts import (
    AlertActionKind,
    AlertState,
    InMemoryAlertRepository,
    SupersessionError,
    supersede,
    supersede_with_broader_alert,
    supersession_action,
)

NOW = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=datetime.UTC)
USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _alert(
    *,
    user_id: uuid.UUID = USER,
    state: AlertState = AlertState.OPEN,
    severity: RiskLevel = RiskLevel.HIGH,
    dedupe_key: str | None = None,
    alert_id: uuid.UUID | None = None,
    superseded_by: uuid.UUID | None = None,
) -> Alert:
    return Alert(
        id=alert_id or uuid.uuid4(),
        user_id=user_id,
        title="t",
        message="m",
        severity=severity.value,
        peak_severity=severity.value,
        risk_score=65,
        alert_type="risk_score",
        state=state.value,
        dedupe_key=dedupe_key or f"key-{uuid.uuid4()}",
        superseded_by=superseded_by,
        created_at=NOW,
        updated_at=NOW,
    )


class _SpyRepository:
    """Wraps :class:`InMemoryAlertRepository`, recording the order `get_alert` is called in."""

    def __init__(self, repository: InMemoryAlertRepository) -> None:
        self._repository = repository
        self.get_calls: list[uuid.UUID] = []

    def get_alert(self, alert_id: uuid.UUID) -> Alert | None:
        self.get_calls.append(alert_id)
        return self._repository.get_alert(alert_id)

    def flush(self) -> None:
        self._repository.flush()


# --------------------------------------------------------------------------------------
# `supersede` / `supersession_action`: the low-level primitive
# --------------------------------------------------------------------------------------


def test_supersede_marks_the_victim_and_preserves_terminal_history() -> None:
    victim = _alert(severity=RiskLevel.HIGH)
    victim.evidence_refs = [{"article": "a1"}]
    replacement = _alert()

    supersede(victim, replacement=replacement, now=NOW)

    assert victim.state == AlertState.SUPERSEDED.value
    assert victim.superseded_by == replacement.id
    assert victim.updated_at == NOW
    # Terminal history untouched: no resolution timestamp, no cooldown, evidence intact.
    assert victim.resolved_at is None
    assert victim.cooldown_until is None
    assert victim.severity == RiskLevel.HIGH.value
    assert victim.evidence_refs == [{"article": "a1"}]


def test_supersede_refuses_self_supersession() -> None:
    alert = _alert()

    with pytest.raises(SupersessionError, match="cannot supersede itself"):
        supersede(alert, replacement=alert, now=NOW)


def test_supersession_action_carries_the_successor_and_reason() -> None:
    victim = _alert(severity=RiskLevel.CRITICAL)
    replacement = _alert()
    supersede(victim, replacement=replacement, now=NOW)

    action = supersession_action(victim, reason="a broader alert covers this")

    assert action.kind is AlertActionKind.SUPERSESSION
    assert action.alert_id == victim.id
    assert action.user_id == victim.user_id
    assert action.severity is RiskLevel.CRITICAL
    assert action.superseded_by == replacement.id
    assert action.reason == "a broader alert covers this"


# --------------------------------------------------------------------------------------
# `supersede_with_broader_alert`: orchestration, validation, and refusals
# --------------------------------------------------------------------------------------


def test_a_broader_alert_replaces_multiple_narrower_alerts() -> None:
    repository = InMemoryAlertRepository()
    broader = _alert()
    narrow_a = _alert()
    narrow_b = _alert()
    repository.alerts.extend([broader, narrow_a, narrow_b])

    result = supersede_with_broader_alert(
        repository,
        broader_alert_id=broader.id,
        narrower_alert_ids=[narrow_a.id, narrow_b.id],
        now=NOW,
    )

    assert result.replacement_id == broader.id
    assert set(result.superseded_ids) == {narrow_a.id, narrow_b.id}
    assert narrow_a.state == AlertState.SUPERSEDED.value
    assert narrow_a.superseded_by == broader.id
    assert narrow_b.state == AlertState.SUPERSEDED.value
    assert narrow_b.superseded_by == broader.id
    assert broader.state == AlertState.OPEN.value  # the broader alert itself is untouched
    assert len(result.actions) == 2
    assert {action.alert_id for action in result.actions} == {narrow_a.id, narrow_b.id}
    assert all(action.kind is AlertActionKind.SUPERSESSION for action in result.actions)


@pytest.mark.parametrize("terminal_state", [AlertState.RESOLVED, AlertState.SUPERSEDED])
def test_a_terminal_broader_alert_is_refused(terminal_state: AlertState) -> None:
    repository = InMemoryAlertRepository()
    broader = _alert(state=terminal_state, superseded_by=uuid.uuid4())
    if terminal_state is AlertState.RESOLVED:
        broader.resolved_at = NOW
    narrow = _alert()
    repository.alerts.extend([broader, narrow])

    with pytest.raises(SupersessionError, match="broader alert"):
        supersede_with_broader_alert(
            repository, broader_alert_id=broader.id, narrower_alert_ids=[narrow.id], now=NOW
        )

    # The whole call is refused: nothing is half-replaced.
    assert narrow.state == AlertState.OPEN.value


def test_a_terminal_narrower_alert_is_refused_and_nothing_is_half_replaced() -> None:
    repository = InMemoryAlertRepository()
    broader = _alert()
    live_narrow = _alert()
    terminal_narrow = _alert(state=AlertState.RESOLVED)
    terminal_narrow.resolved_at = NOW
    repository.alerts.extend([broader, live_narrow, terminal_narrow])

    with pytest.raises(SupersessionError, match="narrower alert"):
        supersede_with_broader_alert(
            repository,
            broader_alert_id=broader.id,
            narrower_alert_ids=[live_narrow.id, terminal_narrow.id],
            now=NOW,
        )

    assert live_narrow.state == AlertState.OPEN.value  # untouched: the call refused wholesale
    assert broader.state == AlertState.OPEN.value


def test_a_narrower_alert_cannot_be_superseded_on_another_owners_behalf() -> None:
    repository = InMemoryAlertRepository()
    broader = _alert(user_id=USER)
    narrow = _alert(user_id=OTHER_USER)
    repository.alerts.extend([broader, narrow])

    with pytest.raises(SupersessionError, match="another owner's behalf"):
        supersede_with_broader_alert(
            repository, broader_alert_id=broader.id, narrower_alert_ids=[narrow.id], now=NOW
        )

    assert narrow.state == AlertState.OPEN.value


def test_a_narrower_alert_sharing_the_broader_s_dedupe_key_is_refused() -> None:
    repository = InMemoryAlertRepository()
    shared_key = "shared-key"
    broader = _alert(dedupe_key=shared_key)
    narrow = _alert(dedupe_key=shared_key)
    repository.alerts.extend([broader, narrow])

    with pytest.raises(SupersessionError, match="cannot replace itself"):
        supersede_with_broader_alert(
            repository, broader_alert_id=broader.id, narrower_alert_ids=[narrow.id], now=NOW
        )


def test_the_broader_alert_cannot_be_named_as_its_own_narrower() -> None:
    repository = InMemoryAlertRepository()
    broader = _alert()
    repository.alerts.append(broader)

    with pytest.raises(SupersessionError, match="cannot supersede itself"):
        supersede_with_broader_alert(
            repository, broader_alert_id=broader.id, narrower_alert_ids=[broader.id], now=NOW
        )


def test_at_least_one_narrower_alert_is_required() -> None:
    repository = InMemoryAlertRepository()
    broader = _alert()
    repository.alerts.append(broader)

    with pytest.raises(SupersessionError, match="at least one narrower alert"):
        supersede_with_broader_alert(
            repository, broader_alert_id=broader.id, narrower_alert_ids=[], now=NOW
        )


def test_an_unknown_alert_id_is_refused() -> None:
    repository = InMemoryAlertRepository()
    broader = _alert()
    repository.alerts.append(broader)

    with pytest.raises(SupersessionError, match="unknown alert"):
        supersede_with_broader_alert(
            repository, broader_alert_id=broader.id, narrower_alert_ids=[uuid.uuid4()], now=NOW
        )


def test_string_ids_are_coerced_to_uuid() -> None:
    repository = InMemoryAlertRepository()
    broader = _alert()
    narrow = _alert()
    repository.alerts.extend([broader, narrow])

    result = supersede_with_broader_alert(
        repository,
        broader_alert_id=str(broader.id),
        narrower_alert_ids=[str(narrow.id)],
        now=NOW,
    )

    assert result.replacement_id == broader.id


def test_a_malformed_string_id_is_refused() -> None:
    repository = InMemoryAlertRepository()
    broader = _alert()
    repository.alerts.append(broader)

    with pytest.raises(SupersessionError, match="not a UUID"):
        supersede_with_broader_alert(
            repository, broader_alert_id=broader.id, narrower_alert_ids=["not-a-uuid"], now=NOW
        )


def test_a_naive_now_is_refused() -> None:
    repository = InMemoryAlertRepository()
    broader = _alert()
    narrow = _alert()
    repository.alerts.extend([broader, narrow])

    with pytest.raises(ValueError, match="timezone-aware"):
        supersede_with_broader_alert(
            repository,
            broader_alert_id=broader.id,
            narrower_alert_ids=[narrow.id],
            now=datetime.datetime(2026, 7, 13, 12, 0),
        )


def test_rows_are_locked_in_ascending_id_order() -> None:
    repository = InMemoryAlertRepository()
    # Deliberately construct so the broader alert does not sort first, to prove the lock
    # order is id-based rather than "broader alert first, then narrower alerts".
    ids = sorted(uuid.uuid4() for _ in range(3))
    broader = _alert(alert_id=ids[1])
    narrow_a = _alert(alert_id=ids[0])
    narrow_b = _alert(alert_id=ids[2])
    repository.alerts.extend([broader, narrow_a, narrow_b])
    spy = _SpyRepository(repository)

    supersede_with_broader_alert(
        spy, broader_alert_id=broader.id, narrower_alert_ids=[narrow_b.id, narrow_a.id], now=NOW
    )

    assert spy.get_calls == sorted(spy.get_calls)
    assert spy.get_calls == ids
