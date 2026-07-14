"""Seed the curated historical-episode corpus (historical-episode spec, ADR 0004).

Planning is separated from writing, and the reason is the embeddings API. Item 1 embeds one episode
at a time, which is right for a single curation edit and wrong for a 100-row corpus: it would send
100 requests where ADR 0004 allows 96 texts in one. So this module first asks, for every row, what
the database already holds -- new, changed, or identical -- then embeds *only* the rows that need a
vector, in chunks of at most 96, and only then writes.

The staleness predicate is not reimplemented here. :func:`services.nlp.episodes.episode_onset_is_stale`
is the same one :func:`~services.nlp.episodes.upsert_historical_episode` applies when it writes, so
the planner and the writer cannot disagree about which rows need embedding -- a disagreement would
either waste an API call or, far worse, leave a row whose onset text moved pointing at its old
vector.

Nothing here commits, and nothing here inserts a placeholder vector: ``onset_embedding`` is NOT
NULL, and a row that could not be embedded raises rather than being stored with zeros, which would
be retrievable and meaningless. Seed order is corpus order, which puts every parent arc before its
children, so the self-referencing foreign key holds on a fresh database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from db.models.core import HistoricalEpisode
from packages.providers.base import EmbeddingProvider
from packages.providers.openai_embeddings import MAX_EMBEDDING_BATCH_SIZE
from services.analogies.corpus import CuratedEpisode, EpisodeCorpus, unreviewed_episodes
from services.nlp.embedding_text import build_episode_onset_text
from services.nlp.embeddings import embed_texts, resolve_embedding_identity
from services.nlp.episodes import (
    EpisodeEmbedding,
    EpisodeRecord,
    episode_fields_differ,
    episode_onset_is_stale,
    upsert_historical_episode,
)

INSERT = "insert"
UPDATE = "update"
UNCHANGED = "unchanged"

#: How many unreviewed slugs the gate's message names before it stops listing them.
_LISTED_SLUGS = 5


class EpisodeReviewGateError(RuntimeError):
    """An unreviewed draft was about to be published as verified production corpus.

    The spec's curation workflow is three steps -- "LLM drafts from a fixed template (onset written
    as-if-live) -> human verifies onset/outcome separation + dates + sources -> committed via seed
    script" -- and this is the arrow before the last one. Skipping it does not produce a corpus that
    is merely unpolished; it produces one whose onset paragraphs, dates and citations no human has
    checked, being served to users as what history actually did. The offline validator catches a
    malformed row, a leaked hindsight phrase, a source outside the allowlist. It cannot catch a
    fluent, well-formed, confidently wrong one, and it must not be mistaken for the human who can.
    """


@dataclass(frozen=True)
class EpisodeSeedItem:
    """One curated row, and what the database says needs doing with it."""

    episode: CuratedEpisode
    record: EpisodeRecord
    action: str
    #: True when the row has no vector in the pinned space, or its onset text/version moved.
    needs_embedding: bool

    @property
    def episode_id(self) -> uuid.UUID:
        return self.episode.episode_id

    def onset_text(self) -> str:
        """The exact text that will be embedded. Onset fields only, by construction."""
        return build_episode_onset_text(
            onset_summary=self.record.onset_summary,
            onset_indicators=self.record.onset_indicators,
        )


@dataclass(frozen=True)
class EpisodeSeedPlan:
    """What a seed run will do, decided before a single text is sent to the provider."""

    items: tuple[EpisodeSeedItem, ...]
    model: str
    model_version: str
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE

    @property
    def to_embed(self) -> tuple[EpisodeSeedItem, ...]:
        return tuple(item for item in self.items if item.needs_embedding)

    @property
    def request_sizes(self) -> tuple[int, ...]:
        """The chunk sizes this plan will send. A fresh 100-row corpus is (96, 4), not 100 calls."""
        pending = len(self.to_embed)
        return tuple(
            min(self.batch_size, pending - start)
            for start in range(0, pending, self.batch_size)
        )

    def counts(self) -> dict[str, int]:
        return {
            "inserted": sum(1 for i in self.items if i.action == INSERT),
            "updated": sum(1 for i in self.items if i.action == UPDATE),
            "unchanged": sum(1 for i in self.items if i.action == UNCHANGED),
            "re_embedded": len(self.to_embed),
        }


@dataclass(frozen=True)
class EpisodeSeedSummary:
    """What the run did. Reported per run, so an operator can see a no-op as a no-op."""

    inserted: int
    updated: int
    unchanged: int
    re_embedded: int
    embedding_requests: tuple[int, ...]
    quotas: dict[str, Any]
    #: Rows written that no human has verified. Non-zero only on an explicitly unreviewed seed.
    unreviewed: int = 0
    #: True when the review gate was deliberately bypassed. Carried into the summary so a draft
    #: seed can never be mistaken, later or by anyone else, for a verified production corpus.
    unreviewed_allowed: bool = False

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "re_embedded": self.re_embedded,
            "embedding_requests": list(self.embedding_requests),
            "quotas": self.quotas,
            "unreviewed": self.unreviewed,
            "review_gate": "bypassed" if self.unreviewed_allowed else "enforced",
        }


def enforce_review_gate(corpus: EpisodeCorpus, *, allow_unreviewed: bool) -> int:
    """Enforce the spec's human-verification step. Returns how many unreviewed rows are seeded.

    Public and pure, so the CLI can run it *before* it goes looking for an API key: "these rows are
    not reviewed" is a curation blocker and "OPENAI_API_KEY is unset" is a configuration one, and an
    operator hitting both should be told about the one that actually stops the seed.
    """
    pending = unreviewed_episodes(corpus)
    if not pending:
        return 0
    if allow_unreviewed:
        return len(pending)

    slugs = ", ".join(episode.slug for episode in pending[:_LISTED_SLUGS])
    if len(pending) > _LISTED_SLUGS:
        slugs += f", ... (+{len(pending) - _LISTED_SLUGS} more)"
    raise EpisodeReviewGateError(
        f"{len(pending)} of {len(corpus.episodes)} curated episodes have not been human-reviewed "
        f"and will not be published as verified corpus: {slugs}. The spec's workflow is 'LLM drafts "
        "-> human verifies onset/outcome separation + dates + sources -> committed via seed "
        "script', and offline validation is not that human. To seed them, either record each real "
        "sign-off in the episode's `review` block (status 'human_reviewed', with a real reviewer, a "
        "real date, and a completed checklist), or pass allow_unreviewed=True / --allow-unreviewed "
        "to load them into a development or staging database as the unverified drafts they are."
    )


def plan_episode_seed(
    session: Session,
    corpus: EpisodeCorpus,
    *,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE,
) -> EpisodeSeedPlan:
    """Compare the corpus against the database. Reads only; embeds nothing; writes nothing.

    Rows are looked up by their fixed curated id, never by name, so renaming an episode updates it
    instead of inserting a second copy of the same history.
    """
    model, model_version = resolve_embedding_identity(
        None, embedding_model, embedding_model_version
    )
    items: list[EpisodeSeedItem] = []
    for episode in corpus.episodes:
        record = episode.to_record()
        existing = session.get(HistoricalEpisode, episode.episode_id)
        if existing is None:
            items.append(
                EpisodeSeedItem(
                    episode=episode, record=record, action=INSERT, needs_embedding=True
                )
            )
            continue
        stale = episode_onset_is_stale(
            existing, record, model=model, model_version=model_version
        )
        changed = episode_fields_differ(existing, record)
        items.append(
            EpisodeSeedItem(
                episode=episode,
                record=record,
                action=UPDATE if changed else UNCHANGED,
                needs_embedding=stale,
            )
        )
    return EpisodeSeedPlan(
        items=tuple(items),
        model=model,
        model_version=model_version,
        batch_size=batch_size,
    )


def embed_plan(plan: EpisodeSeedPlan, provider: EmbeddingProvider) -> dict[uuid.UUID, EpisodeEmbedding]:
    """Embed exactly the rows the plan says need a vector, in chunks of at most 96 (ADR 0004)."""
    pending = plan.to_embed
    if not pending:
        return {}
    results = embed_texts(
        provider,
        [item.onset_text() for item in pending],
        model=plan.model,
        model_version=plan.model_version,
        batch_size=plan.batch_size,
    )
    return {
        item.episode_id: EpisodeEmbedding(
            vector=tuple(result.vector), model=plan.model, model_version=plan.model_version
        )
        for item, result in zip(pending, results, strict=True)
    }


def seed_episode_corpus(
    session: Session,
    corpus: EpisodeCorpus,
    provider: EmbeddingProvider | None = None,
    *,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE,
    allow_unreviewed: bool = False,
) -> EpisodeSeedSummary:
    """Make the database hold exactly this corpus. Flushes; never commits (the CLI owns that).

    ``provider`` may be omitted only when nothing needs embedding -- an already-seeded, unchanged
    corpus. Anything else raises, because the alternative would be a row with no usable vector.

    ``allow_unreviewed`` bypasses the spec's human-verification gate. It defaults to False, and the
    default is the point: seeding is the step the spec puts *after* a human has checked the drafts,
    so publishing them without one has to be a thing somebody explicitly asked for and can be seen
    to have asked for (it lands in the summary), never something that happens by omission.
    """
    # Before the provider is touched: an unverified corpus must not reach an embeddings API, let
    # alone the table, and the operator should find out for free rather than after 100 embeddings.
    unreviewed = enforce_review_gate(corpus, allow_unreviewed=allow_unreviewed)

    # The provider's own space wins over the configured default, so pointing the seeder at a new
    # model is all a re-embedding migration takes (ADR 0004).
    model, model_version = resolve_embedding_identity(
        provider, embedding_model, embedding_model_version
    )
    plan = plan_episode_seed(
        session,
        corpus,
        embedding_model=model,
        embedding_model_version=model_version,
        batch_size=batch_size,
    )
    if plan.to_embed and provider is None:
        raise ValueError(
            f"{len(plan.to_embed)} episode(s) need an onset embedding in "
            f"{plan.model}@{plan.model_version} but no embedding provider was supplied; "
            "onset_embedding is NOT NULL and must never be a placeholder"
        )

    embeddings = embed_plan(plan, provider) if provider is not None else {}
    for item in plan.items:
        if item.action == UNCHANGED and not item.needs_embedding:
            continue
        # Parents come first in corpus order, so a child's FK target already exists.
        upsert_historical_episode(
            session,
            item.record,
            embedding=embeddings.get(item.episode_id),
            embedding_model=plan.model,
            embedding_model_version=plan.model_version,
        )
    session.flush()

    counts = plan.counts()
    return EpisodeSeedSummary(
        inserted=counts["inserted"],
        updated=counts["updated"],
        unchanged=counts["unchanged"],
        re_embedded=counts["re_embedded"],
        embedding_requests=plan.request_sizes,
        quotas=corpus.quotas.as_dict(),
        unreviewed=unreviewed,
        unreviewed_allowed=allow_unreviewed,
    )


__all__ = [
    "INSERT",
    "UNCHANGED",
    "UPDATE",
    "EpisodeReviewGateError",
    "EpisodeSeedItem",
    "EpisodeSeedPlan",
    "EpisodeSeedSummary",
    "embed_plan",
    "enforce_review_gate",
    "plan_episode_seed",
    "seed_episode_corpus",
]
