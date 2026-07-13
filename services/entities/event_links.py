"""Accepted event/entity links and their assertion semantics (ADR 0005).

An accepted mention becomes one ``event_entities`` row: the canonical entity the mention
resolved to, its confidence, and *how the article asserted it*. The committed schema has no
raw-mention table and no assertion column, so the assertion status rides on the existing
``role`` field under an explicit, stable vocabulary (``direct_mention_asserted`` and friends).
Per-mention detail is not lost: it stays in the ``entity_resolution_runs`` / ``llm_runs`` audit
rows the linker and the adjudicator write. No migration, no new table.

That vocabulary is what makes the ADR's exclusion rule queryable: "denied/speculative links are
excluded from risk-score inputs". They are still persisted, still auditable, still readable --
they simply do not appear in :func:`risk_eligible_event_links`, which is the only door the risk
inputs come through.

One event and one entity have exactly one row, so several mentions of the same company in the
same event *merge* rather than fight:

* confidence keeps the **maximum** across mentions -- a rerun can never lower a link's score;
* the role keeps the **strongest claim** -- a single asserted mention keeps the link
  risk-eligible even if the article also speculated about the same company elsewhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from db.models import EventEntity
from services.nlp.assertions import AssertionStatus
from services.provider_data.common import add, find_all, find_one, flush_pending, json_safe

ROLE_ASSERTED: Final = "direct_mention_asserted"
ROLE_DENIED: Final = "direct_mention_denied"
ROLE_SPECULATIVE: Final = "direct_mention_speculative"

#: In precedence order, strongest claim first. An asserted mention outranks every other, which
#: is the property risk eligibility depends on. Between the two non-asserted roles, an explicit
#: denial outranks a speculation: it is the more definite statement the article made about the
#: claim, and neither is risk-eligible, so the choice only decides which one the audit shows.
DIRECT_MENTION_ROLES: Final[tuple[str, ...]] = (ROLE_ASSERTED, ROLE_DENIED, ROLE_SPECULATIVE)

ROLE_FOR_ASSERTION: Final[Mapping[AssertionStatus, str]] = {
    AssertionStatus.ASSERTED: ROLE_ASSERTED,
    AssertionStatus.DENIED: ROLE_DENIED,
    AssertionStatus.SPECULATIVE: ROLE_SPECULATIVE,
}

#: ADR 0005: only asserted direct links feed risk scoring.
RISK_ELIGIBLE_ROLES: Final[frozenset[str]] = frozenset({ROLE_ASSERTED})

_ROLE_RANK: Final[Mapping[str, int]] = {
    role: rank for rank, role in enumerate(DIRECT_MENTION_ROLES)
}


@dataclass(frozen=True, slots=True)
class EventEntityLink:
    """One persisted event/entity link, read back as an immutable value."""

    event_id: uuid.UUID
    entity_profile_id: uuid.UUID
    role: str | None
    confidence_score: float | None

    @property
    def is_risk_eligible(self) -> bool:
        """True only for an asserted direct mention (ADR 0005)."""
        return self.role in RISK_ELIGIBLE_ROLES

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


def role_for_assertion(status: AssertionStatus) -> str:
    """The ``event_entities.role`` value that records how the article asserted the mention."""

    return ROLE_FOR_ASSERTION[status]


def merge_roles(existing: str | None, incoming: str) -> str:
    """Keep the strongest claim made about one entity in one event.

    A role this module does not own -- ``None``, or something another writer put there -- carries
    no assertion semantics, so it is replaced rather than compared: the incoming direct-mention
    role is strictly more informative than an unknown one.
    """

    if existing not in _ROLE_RANK:
        return incoming
    return min(existing, incoming, key=lambda role: _ROLE_RANK[role])


def persist_event_entity_link(
    session: Any,
    *,
    event_id: uuid.UUID,
    entity_profile_id: uuid.UUID,
    confidence_score: float,
    assertion_status: AssertionStatus,
) -> EventEntity:
    """Upsert one accepted direct link, idempotently, on ``(event_id, entity_profile_id)``.

    Only deterministic ACCEPTs and whitelist-valid adjudicated selections get here: NIL, rejected,
    and failed mentions never call this. Rerunning the pipeline re-derives the same score for the
    same mention, so the ``max`` below is a no-op on a rerun and a genuine merge across mentions.
    """

    score = _bounded_confidence(confidence_score)
    role = role_for_assertion(assertion_status)

    existing = find_one(
        session, EventEntity, event_id=event_id, entity_profile_id=entity_profile_id
    )
    if existing is not None:
        existing.role = merge_roles(existing.role, role)
        current = existing.confidence_score
        existing.confidence_score = max(score, float(current)) if current is not None else score
        return existing

    link = add(
        session,
        EventEntity(
            event_id=event_id,
            entity_profile_id=entity_profile_id,
            role=role,
            confidence_score=score,
        ),
    )
    # Flushed on insert so the *next* mention of the same entity in this run reads it back and
    # merges onto it, instead of adding a second row that only fails at commit on the PK.
    flush_pending(session)
    return link


def event_entity_links(
    session: Any, event_id: uuid.UUID, *, roles: Sequence[str] | None = None
) -> tuple[EventEntityLink, ...]:
    """Every persisted link for an event, strongest confidence first, optionally filtered by role."""

    wanted = None if roles is None else frozenset(roles)
    links = [
        EventEntityLink(
            event_id=row.event_id,
            entity_profile_id=row.entity_profile_id,
            role=row.role,
            confidence_score=None if row.confidence_score is None else float(row.confidence_score),
        )
        for row in find_all(session, EventEntity, event_id=event_id)
        if wanted is None or row.role in wanted
    ]
    return tuple(sorted(links, key=_link_sort_key))


def risk_eligible_event_links(session: Any, event_id: uuid.UUID) -> tuple[EventEntityLink, ...]:
    """The only links that may feed a risk score: asserted direct mentions (ADR 0005).

    Denied and speculative links stay in the table and stay queryable through
    :func:`event_entity_links`; they are simply not risk inputs, and this is the one function the
    risk side calls, so they cannot become one by accident.
    """

    return event_entity_links(session, event_id, roles=sorted(RISK_ELIGIBLE_ROLES))


def _bounded_confidence(value: float) -> float:
    """``ck_event_entities_confidence_score`` allows 0..1; a score outside it is a bug, not a row."""

    score = float(value)
    if not 0.0 <= score <= 1.0:
        msg = f"confidence_score must be within [0, 1], got {value}"
        raise ValueError(msg)
    return score


def _link_sort_key(link: EventEntityLink) -> tuple[float, str]:
    return (-(link.confidence_score or 0.0), str(link.entity_profile_id))
