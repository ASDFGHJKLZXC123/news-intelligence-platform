"""Durable ``event_analogies`` rows, reconciled as a set (historical-episode spec, Stage 5).

An event's analogies are a *set*, not a log: rerunning the rerank after the corpus grew must leave
the event holding exactly what the latest run selected. So this module reconciles rather than
appends -- kept pairs are updated in place, new pairs inserted, and pairs the new run did not
select are deleted. A legitimate "no reliable analogy" is therefore the same operation with an
empty selection, and it *removes* the previous run's rows: continuing to serve analogies the
current model no longer stands behind is the one failure mode worse than serving none.

Nothing here commits. The worker owns the transaction (its LLM audit rows and these rows are one
unit of work), so a failure anywhere in the run rolls back to the previously durable set rather
than to an event with half its analogies replaced.

The two scales stay apart, which is why they are on different columns: the 0-100 structural score
the model produced is ``similarity_score``, and item 2's 0.0-1.0 vector cosine rides in
``evidence_refs`` under its own name. Nothing rescales one into the other.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models.core import EventAnalogy
from services.analogies.compatibility import normalize_tags
from services.analogies.rerank import (
    RERANK_PROMPT_TEMPLATE_VERSION,
    RERANK_SCHEMA,
    RERANK_SCHEMA_VERSION,
    RerankedCandidate,
)

#: The database's own bound (``ck_event_analogies_similarity_score``), checked before the flush so
#: a bad score is a typed Python failure rather than an IntegrityError from three layers down.
SCORE_MIN = 0.0
SCORE_MAX = 100.0


class AnalogyPersistenceError(ValueError):
    """A selection that must never reach the table."""


@dataclass(frozen=True)
class EventAnalogyRow:
    """One durable row, as a pure value. Built without a Session, so mapping is testable alone."""

    episode_id: uuid.UUID
    similarity_score: float
    rationale: str
    regime_caveats: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    shared_causes: tuple[str, ...] = ()
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    llm_run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AnalogyReconciliation:
    """What the reconciliation did. Reruns are visible, not silent."""

    inserted: int = 0
    updated: int = 0
    removed: int = 0

    @property
    def persisted(self) -> int:
        return self.inserted + self.updated


def _validated_score(value: float) -> float:
    score = float(value)
    if not SCORE_MIN <= score <= SCORE_MAX:
        msg = f"similarity_score must be on the 0-100 contract scale, got {value!r}"
        raise AnalogyPersistenceError(msg)
    return score


def _limitations(selection: RerankedCandidate) -> tuple[str, ...]:
    """Deterministic limitations: item 2's regime reasons, and the counterexample flag.

    These are facts about the corpus, computed here, never prose the model was asked to supply --
    the model's own caveats are a separate column.
    """

    candidate = selection.candidate
    limitations = list(candidate.regime_caveat_reasons)
    if candidate.is_counterexample:
        limitations.append(
            "episode is a curated counterexample: a near-miss that resolved benignly"
        )
    return tuple(limitations)


def _shared_causes(
    selection: RerankedCandidate, current_regime_tags: Iterable[str]
) -> tuple[str, ...]:
    """Deterministic structural overlap: the family both sit in, and the regime tags they share.

    Retrieval hard-filters by episode family, so the type is always genuinely shared. Everything
    here is derived from the two rows; none of it is a cause the model asserted.
    """

    candidate = selection.candidate
    shared_tags = normalize_tags(current_regime_tags) & normalize_tags(candidate.regime_tags)
    return (
        f"episode_type:{candidate.episode_type}",
        *(f"regime:{tag}" for tag in sorted(shared_tags)),
    )


def build_analogy_rows(
    selections: Sequence[RerankedCandidate],
    *,
    llm_run_id: uuid.UUID | None,
    trace_id: str | None,
    model: str,
    model_version: str,
    current_regime_tags: Iterable[str] = (),
) -> tuple[EventAnalogyRow, ...]:
    """Map the rerank's selections to durable rows. Pure: no Session, no network."""

    return tuple(
        EventAnalogyRow(
            episode_id=selection.episode_id,
            similarity_score=_validated_score(selection.similarity_score),
            rationale=selection.explanation,
            regime_caveats=selection.regime_caveats,
            limitations=_limitations(selection),
            shared_causes=_shared_causes(selection, current_regime_tags),
            evidence_refs={
                # The 0.0-1.0 vector prior, kept under its own name and off the 0-100 column.
                "vector_similarity": selection.vector_similarity,
                "embedding_model": model,
                "embedding_model_version": model_version,
                "llm_confidence": selection.confidence,
                "trace_id": trace_id,
                "schema_name": RERANK_SCHEMA,
                "schema_version": RERANK_SCHEMA_VERSION,
                "prompt_template_version": RERANK_PROMPT_TEMPLATE_VERSION,
                "is_counterexample": selection.candidate.is_counterexample,
                "parent_episode_id": (
                    str(selection.candidate.parent_episode_id)
                    if selection.candidate.parent_episode_id
                    else None
                ),
                "episode_source_refs": selection.candidate.source_refs,
            },
            llm_run_id=llm_run_id,
        )
        for selection in selections
    )


def _apply(row: EventAnalogy, values: EventAnalogyRow) -> None:
    row.llm_run_id = values.llm_run_id
    row.similarity_score = values.similarity_score
    row.rationale = values.rationale
    row.regime_caveats = list(values.regime_caveats)
    row.limitations = list(values.limitations)
    row.shared_causes = list(values.shared_causes)
    row.evidence_refs = values.evidence_refs


def reconcile_event_analogies(
    session: Session,
    event_id: uuid.UUID,
    rows: Sequence[EventAnalogyRow],
) -> AnalogyReconciliation:
    """Make the event's durable set exactly ``rows``. Flushes; never commits.

    An empty ``rows`` is a valid, meaningful instruction -- the current run found no reliable
    analogy -- and clears the event, so nothing stale is served.
    """

    desired: dict[uuid.UUID, EventAnalogyRow] = {}
    for row in rows:
        if row.episode_id in desired:
            msg = f"episode {row.episode_id} appears twice in one event's analogy set"
            raise AnalogyPersistenceError(msg)
        desired[row.episode_id] = row

    existing = {
        row.historical_episode_id: row
        for row in session.execute(
            select(EventAnalogy).where(EventAnalogy.event_id == event_id)
        ).scalars()
    }

    inserted = updated = removed = 0
    for episode_id, values in desired.items():
        current = existing.get(episode_id)
        if current is None:
            row = EventAnalogy(event_id=event_id, historical_episode_id=episode_id)
            _apply(row, values)
            session.add(row)
            inserted += 1
        else:
            _apply(current, values)
            updated += 1

    for episode_id, current in existing.items():
        if episode_id not in desired:
            session.delete(current)
            removed += 1

    # Flush, so the unique (event, episode) constraint and the score bound are enforced here --
    # inside the caller's transaction, where a violation can still be rolled back cleanly.
    session.flush()
    return AnalogyReconciliation(inserted=inserted, updated=updated, removed=removed)


__all__ = [
    "SCORE_MAX",
    "SCORE_MIN",
    "AnalogyPersistenceError",
    "AnalogyReconciliation",
    "EventAnalogyRow",
    "build_analogy_rows",
    "reconcile_event_analogies",
]
