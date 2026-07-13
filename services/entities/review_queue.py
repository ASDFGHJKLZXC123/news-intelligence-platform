"""Unresolved news-mention review queue (ADR 0005: "weekly manual pass for MVP").

Read-only aggregation over the ``entity_resolution_runs`` rows the news linker wrote: every
mention that did not deterministically attach an entity (NIL *and* adjudicate) is grouped by its
normalized surface, so the frequent unknowns — private and non-US companies the identity store
has never heard of — surface at the top of one weekly list instead of being lost one article at
a time.

Nothing here writes, and nothing here invents a column. The band is *derived* from the run's own
columns (``band_of_run``: an unmatched run is NIL or ADJUDICATE by its score), never parsed out of
prose; only the human-facing ``reason`` is read back from the concise summary the linker persists
to ``explanation``, and a run whose explanation is missing or was written by something else simply
has no reason to show. First/last seen are the runs' ``created_at``; a run whose ``created_at`` is
not set cannot be placed in a window, so it is excluded whenever one is given.
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select

from db.models import EntityResolutionRun
from services.entities.news_linking import (
    NEWS_MENTION_TARGET_TYPE,
    LinkBand,
    band_of_run,
    parse_link_explanation,
)
from services.provider_data.common import flush_pending, json_safe, normalize_alias

# The ADR's manual pass is weekly, so that is the window this queue is built for.
REVIEW_WINDOW: Final = datetime.timedelta(days=7)
# How many distinct surface forms are worth showing per group before they stop being evidence.
REPRESENTATIVE_SURFACE_LIMIT: Final = 3


@dataclass(frozen=True, slots=True)
class ReviewQueueEntry:
    """One unresolved surface, everything the reviewer needs, and nothing derived from nothing."""

    normalized_surface: str
    occurrence_count: int
    representative_surfaces: tuple[str, ...]
    article_keys: tuple[str, ...]
    band: LinkBand
    confidence_score: float | None
    reason: str | None
    first_seen_at: datetime.datetime | None
    last_seen_at: datetime.datetime | None

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


def unresolved_mention_queue(
    session: Any,
    *,
    since: datetime.datetime | None = None,
    until: datetime.datetime | None = None,
    min_occurrences: int = 1,
    limit: int | None = None,
) -> tuple[ReviewQueueEntry, ...]:
    """Group the news-mention runs that attached no entity, most frequent surface first.

    Ordering is ``(-occurrence_count, normalized_surface)``, so the list is stable across runs.
    The group's band, score, and reason are its best-scoring occurrence's: a surface that linked
    to nothing in one article and nearly linked in another is worth reviewing as the near miss.
    """
    if min_occurrences < 1:
        msg = f"min_occurrences must be at least 1, got {min_occurrences}"
        raise ValueError(msg)
    if limit is not None and limit < 1:
        msg = f"limit must keep at least one entry, got {limit}"
        raise ValueError(msg)
    for name, bound in (("since", since), ("until", until)):
        # created_at is timestamptz. A naive bound compares as a TypeError against the rows in a
        # dict-backed session, and as the wrong instant in SQL; neither is worth guessing at.
        if bound is not None and bound.tzinfo is None:
            msg = f"{name} must be timezone-aware, because entity_resolution_runs.created_at is"
            raise ValueError(msg)

    grouped: dict[str, list[EntityResolutionRun]] = {}
    for run in _unresolved_runs(session, since=since, until=until):
        surface = _surface_of(run)
        key = normalize_alias(surface) if surface else ""
        if key:
            grouped.setdefault(key, []).append(run)

    entries = [_entry(key, runs) for key, runs in grouped.items() if len(runs) >= min_occurrences]
    entries.sort(key=lambda entry: (-entry.occurrence_count, entry.normalized_surface))
    return tuple(entries[:limit] if limit is not None else entries)


def weekly_unresolved_mention_queue(
    session: Any,
    *,
    as_of: datetime.datetime,
    min_occurrences: int = 1,
    limit: int | None = None,
) -> tuple[ReviewQueueEntry, ...]:
    """The ADR's weekly manual pass: the unresolved surfaces of the seven days up to ``as_of``."""
    return unresolved_mention_queue(
        session,
        since=as_of - REVIEW_WINDOW,
        until=as_of,
        min_occurrences=min_occurrences,
        limit=limit,
    )


def _unresolved_runs(
    session: Any,
    *,
    since: datetime.datetime | None,
    until: datetime.datetime | None,
) -> tuple[EntityResolutionRun, ...]:
    """News-mention runs that attached no entity, with the window pushed into SQL when it can be."""
    listing = getattr(session, "all_of", None)
    if callable(listing):
        rows = [
            run
            for run in listing(EntityResolutionRun)
            if run.target_type == NEWS_MENTION_TARGET_TYPE and run.matched_entity_id is None
        ]
        return tuple(run for run in rows if _in_window(run.created_at, since, until))

    flush_pending(session)
    statement = select(EntityResolutionRun).where(
        EntityResolutionRun.target_type == NEWS_MENTION_TARGET_TYPE,
        EntityResolutionRun.matched_entity_id.is_(None),
    )
    if since is not None:
        statement = statement.where(EntityResolutionRun.created_at >= since)
    if until is not None:
        statement = statement.where(EntityResolutionRun.created_at <= until)
    return tuple(session.execute(statement).scalars().all())


def _in_window(
    created_at: datetime.datetime | None,
    since: datetime.datetime | None,
    until: datetime.datetime | None,
) -> bool:
    if since is None and until is None:
        return True
    if created_at is None:  # undatable: it cannot be shown to fall inside the window
        return False
    return not (
        (since is not None and created_at < since) or (until is not None and created_at > until)
    )


def _entry(normalized_surface: str, runs: Sequence[EntityResolutionRun]) -> ReviewQueueEntry:
    surfaces = Counter(surface for surface in (_surface_of(run) for run in runs) if surface)
    representative = sorted(surfaces.items(), key=lambda item: (-item[1], item[0]))
    timestamps = sorted(run.created_at for run in runs if run.created_at is not None)
    best = max(runs, key=_best_run_key)
    score = _score_of(best)
    return ReviewQueueEntry(
        normalized_surface=normalized_surface,
        occurrence_count=len(runs),
        representative_surfaces=tuple(
            surface for surface, _ in representative[:REPRESENTATIVE_SURFACE_LIMIT]
        ),
        article_keys=tuple(sorted({_article_key_of(run) for run in runs})),
        band=band_of_run(best.matched_entity_id, score),
        confidence_score=score,
        reason=parse_link_explanation(best.explanation).get("reason"),
        first_seen_at=timestamps[0] if timestamps else None,
        last_seen_at=timestamps[-1] if timestamps else None,
    )


def _best_run_key(run: EntityResolutionRun) -> tuple[float, str]:
    """The occurrence that represents a group: highest score, then the stable run key."""
    return (_score_of(run) or 0.0, run.run_key or "")


def _score_of(run: EntityResolutionRun) -> float | None:
    """``confidence_score`` is Numeric, so a real session hands back a Decimal."""
    return None if run.confidence_score is None else float(run.confidence_score)


def _surface_of(run: EntityResolutionRun) -> str:
    names = run.input_names
    if isinstance(names, list | tuple) and names:
        return str(names[0])
    return ""


def _article_key_of(run: EntityResolutionRun) -> str:
    """The linker's target id is ``<article key>#<start>-<end>``; the queue groups by article."""
    return str(run.target_id or "").rsplit("#", 1)[0]
