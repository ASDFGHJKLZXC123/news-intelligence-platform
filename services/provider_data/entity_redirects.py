"""Entity redirect history for renames and mergers (ADR 0006).

ADR 0006 makes renames and mergers data operations: an ``entity_redirects`` row records that
mentions of the old entity resolve to the new one from ``effective_date``, and the linker follows
those rows at resolve time. The ADR sources redirects from operational merge/rename decisions and
names no provider statement for them — in particular its Wikidata pull (item 2B) is scoped to
labels, aliases, identifiers, parents/subsidiaries, and industry — so this module exposes the
upsert/resolve API those callers use instead of inventing a provider path for it.

History is never collapsed: a new ``effective_date`` for a pair is a new row, so an entity that
was redirected twice keeps both hops, and resolution walks the chain rather than overwriting it.

Traversal fails closed. A chain that cycles, that is still going after ``REDIRECT_MAX_DEPTH`` hops,
that ends on a profile which is no longer there, or that forks into competing redirects sharing one
effective date has no endpoint, and every one of those raises a :class:`RedirectChainError` instead
of returning the last node walked. Returning that node would hand a caller the *middle* of a broken
chain dressed up as the entity a mention resolves to — the one failure mode a redirect store must
not have. ``upsert_entity_redirect`` still rejects a cycle at write time, so a chain that only ever
grew through this module stays resolvable; the traversal guards are what keep a bulk load, a
restored dump, or a deleted profile from silently repointing mentions.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from db.models import EntityProfile, EntityRedirect
from services.provider_data.common import add, find_all, find_one, parse_optional_date

# A chain longer than this is a data pathology, not identity history; walking it is refused there.
REDIRECT_MAX_DEPTH = 16

REDIRECT_CYCLE = "redirect chain is a cycle"
REDIRECT_TOO_DEEP = "redirect chain exceeds the maximum depth"
REDIRECT_MISSING_TARGET = "redirect target is not a live entity"
REDIRECT_AMBIGUOUS = "competing redirects share the latest effective date"


class RedirectChainError(ValueError):
    """A redirect chain with no endpoint: it resolves to nothing, and never to a node it passed.

    Carries the ``entity_id`` the walk started from and the ``chain`` it got through before the
    chain proved unusable, so a caller can log or queue the pathology it hit. ``ValueError`` stays
    the base class because every rejection this module already raised was one, so callers guarding
    these reads and writes keep catching them.
    """

    def __init__(self, message: str, *, entity_id: uuid.UUID, chain: Sequence[uuid.UUID]) -> None:
        super().__init__(message)
        self.entity_id = entity_id
        self.chain = tuple(chain)


class RedirectCycleError(RedirectChainError):
    """The chain revisits an entity, so following it would never terminate."""


class RedirectTooDeepError(RedirectChainError):
    """The chain still has a hop left after ``REDIRECT_MAX_DEPTH`` of them."""


class RedirectTargetMissingError(RedirectChainError):
    """The chain ends on an entity profile that is not there."""


class RedirectAmbiguousError(RedirectChainError):
    """Two redirects out of one entity compete on the same effective date."""


@dataclass(frozen=True, slots=True)
class _Walk:
    """Where a chain ends, or why it has none. ``error`` is ``None`` only for a usable chain."""

    chain: tuple[uuid.UUID, ...]
    error: RedirectChainError | None = None


def upsert_entity_redirect(
    session: Any,
    *,
    old_entity_id: uuid.UUID,
    new_entity_id: uuid.UUID,
    effective_date: datetime.date | str,
    reason: str | None = None,
) -> tuple[EntityRedirect, bool]:
    """Record that ``old_entity_id`` resolves to ``new_entity_id``; returns ``(row, created)``.

    Rejects a self-redirect, any redirect that would close a cycle, and any redirect onto an
    endpoint whose own chain is already unusable; requires both endpoints to be existing profiles;
    and is idempotent on ``(old, new, effective_date)`` — the unique key.
    """

    if old_entity_id == new_entity_id:
        msg = "a redirect must not point an entity at itself"
        raise ValueError(msg)

    date = parse_optional_date(effective_date)
    if date is None:
        msg = "effective_date is required"
        raise ValueError(msg)
    for entity_id in (old_entity_id, new_entity_id):
        if find_one(session, EntityProfile, id=entity_id) is None:
            msg = f"redirect endpoint is not an existing entity profile: {entity_id}"
            raise ValueError(msg)

    walk = _walk(session, new_entity_id, as_of=None)
    if old_entity_id in walk.chain:
        # The new endpoint already redirects back to the old one, directly or through a chain.
        msg = "redirect would create a cycle"
        raise ValueError(msg)
    if walk.error is not None:
        # Nothing this hop is pointed at resolves, so the hop itself would not resolve either.
        msg = f"redirect endpoint does not resolve: {new_entity_id}: {walk.error}"
        raise ValueError(msg) from walk.error

    existing = find_one(
        session,
        EntityRedirect,
        old_entity_id=old_entity_id,
        new_entity_id=new_entity_id,
        effective_date=date,
    )
    if existing is not None:
        if reason is not None:
            existing.reason = reason
        return existing, False

    redirect = EntityRedirect(
        id=uuid.uuid4(),
        old_entity_id=old_entity_id,
        new_entity_id=new_entity_id,
        reason=reason,
        effective_date=date,
    )
    add(session, redirect)
    return redirect, True


def resolve_entity_redirect(
    session: Any, entity_id: uuid.UUID, *, as_of: datetime.date | None = None
) -> uuid.UUID:
    """Return the entity a mention of ``entity_id`` resolves to after following redirects.

    Raises a :class:`RedirectChainError` when the chain has no endpoint; an entity nothing
    redirects away from resolves to itself.
    """

    return redirect_chain(session, entity_id, as_of=as_of)[-1]


def redirect_chain(
    session: Any, entity_id: uuid.UUID, *, as_of: datetime.date | None = None
) -> list[uuid.UUID]:
    """Follow redirects from ``entity_id``, starting with it and ending at its live target.

    A chain of exactly ``REDIRECT_MAX_DEPTH`` hops that terminates is returned in full. Anything
    the walk cannot take to a live endpoint raises instead: :class:`RedirectCycleError`,
    :class:`RedirectTooDeepError`, :class:`RedirectAmbiguousError`, or
    :class:`RedirectTargetMissingError`.
    """

    walk = _walk(session, entity_id, as_of=as_of)
    if walk.error is not None:
        raise walk.error
    return list(walk.chain)


def entity_redirect_history(session: Any, entity_id: uuid.UUID) -> list[EntityRedirect]:
    """Every redirect recorded for ``entity_id`` as the old endpoint, oldest effective date first."""

    return _sorted_redirects(find_all(session, EntityRedirect, old_entity_id=entity_id))


def _walk(session: Any, entity_id: uuid.UUID, *, as_of: datetime.date | None) -> _Walk:
    """Follow the effective redirects out of ``entity_id`` to its endpoint, or to why it has none.

    A chain ends where an entity has no redirect out of it, so the *absence* of a next hop is the
    only thing that terminates a walk successfully. Running out of allowed hops while a hop is
    still there is the over-deep case, and the two are never conflated: the last node of a chain
    that is still going is not an endpoint, and is never returned as one.

    The endpoint is looked up only when a redirect was actually followed. An entity nothing
    redirects away from is returned as it came in, which is both the ordinary case and the one
    where the caller already holds the row.
    """

    chain = [entity_id]
    seen = {entity_id}
    while True:
        targets = _effective_targets(session, chain[-1], as_of=as_of)
        if not targets:
            break
        if len(targets) > 1:
            return _failed(RedirectAmbiguousError, REDIRECT_AMBIGUOUS, entity_id, chain)
        target = targets[0]
        if target in seen:
            # Also where a self-redirect that got past the table's CHECK constraint lands.
            return _failed(RedirectCycleError, REDIRECT_CYCLE, entity_id, (*chain, target))
        if len(chain) > REDIRECT_MAX_DEPTH:
            return _failed(RedirectTooDeepError, REDIRECT_TOO_DEEP, entity_id, chain)
        chain.append(target)
        seen.add(target)

    if len(chain) > 1 and find_one(session, EntityProfile, id=chain[-1]) is None:
        return _failed(RedirectTargetMissingError, REDIRECT_MISSING_TARGET, entity_id, chain)
    return _Walk(chain=tuple(chain))


def _failed(
    error: type[RedirectChainError],
    message: str,
    entity_id: uuid.UUID,
    chain: Sequence[uuid.UUID],
) -> _Walk:
    return _Walk(chain=tuple(chain), error=error(message, entity_id=entity_id, chain=tuple(chain)))


def _effective_targets(
    session: Any, entity_id: uuid.UUID, *, as_of: datetime.date | None
) -> list[uuid.UUID]:
    """The distinct entities ``entity_id`` redirects to on the latest effective date in force.

    The most recent redirect in effect wins; an entity redirected twice follows the latest hop.
    Two distinct targets on that date are not a tie to break but an ambiguity to report: the unique
    key is ``(old, new, effective_date)``, so the schema lets one entity be pointed at two others
    on one day, and nothing in the data says which merge happened. Choosing between them by id
    would resolve mentions onto a coin flip. A later-dated redirect settles it, because only the
    latest date group is read.
    """

    redirects = find_all(session, EntityRedirect, old_entity_id=entity_id)
    if as_of is not None:
        redirects = [row for row in redirects if row.effective_date <= as_of]
    if not redirects:
        return []
    latest = max(row.effective_date for row in redirects)
    targets = {row.new_entity_id for row in redirects if row.effective_date == latest}
    return sorted(targets, key=str)


def _sorted_redirects(redirects: list[EntityRedirect]) -> list[EntityRedirect]:
    return sorted(redirects, key=lambda row: (row.effective_date, str(row.new_entity_id)))
