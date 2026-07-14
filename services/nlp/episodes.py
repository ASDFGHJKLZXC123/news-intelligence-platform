"""Curated historical-episode embedding lifecycle (ADR 0004 + historical-episode spec).

``historical_episodes.onset_embedding`` is NOT NULL, and that is a deliberate constraint, not an
inconvenience: an episode without a real onset vector would still be *retrievable* if a placeholder
or zero vector were inserted to satisfy the column, and it would match arbitrary events at
meaningless similarities. So there is no code path here that writes a row without embedding it
first. :func:`embed_episode_onset` is the seam a curation script calls to obtain the vector, and
:func:`upsert_historical_episode` embeds on its own if the caller has not.

Only onset text is ever embedded (see ``services.nlp.embedding_text``). Outcome fields are stored
on the row and joined in *after* matching, for base rates -- they never enter the vector.

Re-embedding is driven by change, not by a clock. Curation is the only writer of onset text, so
:func:`upsert_historical_episode` re-embeds exactly when the onset text, the curated ``version``,
or the model space moved, and leaves the stored vector alone otherwise.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from db.models import EMBEDDING_DIM, HistoricalEpisode
from packages.providers.base import EmbeddingProvider, ensure_finite_vector
from packages.providers.openai_embeddings import MAX_EMBEDDING_BATCH_SIZE
from services.nlp.embedding_text import build_episode_onset_text
from services.nlp.embeddings import embed_texts, resolve_embedding_identity

#: Everything curation owns. `onset_embedding`/`model`/`model_version` are derived from the onset
#: fields, never supplied as curated data, so they are deliberately absent.
#: Public because the corpus seeder (item 4) plans a whole batch before it writes: it has to ask
#: which rows changed, and which of those need a new vector, *before* it calls the embeddings API.
CURATED_FIELDS = (
    "name",
    "episode_type",
    "onset_date",
    "peak_date",
    "end_date",
    "onset_summary",
    "onset_indicators",
    "outcome_summary",
    "outcomes",
    "resolution_mechanism",
    "geography",
    "affected_industries",
    "regime_tags",
    "parent_episode_id",
    "is_counterexample",
    "source_refs",
    "license_note",
    "version",
    "review_due_at",
)


@dataclass(frozen=True)
class EpisodeEmbedding:
    """An onset vector plus the model space it was produced in."""

    vector: tuple[float, ...]
    model: str
    model_version: str


@dataclass(frozen=True)
class EpisodeRecord:
    """One curated episode.

    Onset and outcome fields sit side by side here because the *row* holds both; only the onset
    ones are read when the embedding text is built (historical-episode spec).
    """

    name: str
    episode_type: str
    onset_date: datetime.date
    onset_summary: str
    onset_indicators: Any = None
    peak_date: datetime.date | None = None
    end_date: datetime.date | None = None
    outcome_summary: str | None = None
    outcomes: list[str] | None = None
    resolution_mechanism: str | None = None
    geography: str | None = None
    affected_industries: list[str] | None = None
    regime_tags: list[str] | None = None
    parent_episode_id: uuid.UUID | None = None
    is_counterexample: bool = False
    source_refs: Any = None
    license_note: str | None = None
    version: int = 1
    review_due_at: datetime.date | None = None
    episode_id: uuid.UUID | None = field(default=None)


def validate_episode_embedding(
    embedding: EpisodeEmbedding, *, model: str, model_version: str
) -> None:
    """Reject a supplied vector that is not in the pinned space, the column's width, or finite."""
    if (embedding.model, embedding.model_version) != (model, model_version):
        msg = (
            f"episode embedding identity {embedding.model}@{embedding.model_version} != "
            f"configured identity {model}@{model_version}"
        )
        raise ValueError(msg)
    if len(embedding.vector) != EMBEDDING_DIM:
        msg = (
            f"episode embedding dimension {len(embedding.vector)} != "
            f"EMBEDDING_DIM {EMBEDDING_DIM}"
        )
        raise ValueError(msg)
    # A caller may hand us a vector it built itself (the seeder does). A non-finite component is
    # as unusable as a wrong width, and far quieter: the episode would seed, index, and then never
    # match anything.
    ensure_finite_vector(embedding.vector)


def embed_episode_onset(
    provider: EmbeddingProvider,
    onset_summary: str,
    onset_indicators: Any = None,
    *,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
) -> EpisodeEmbedding:
    """Embed one episode's onset text. The vector a curation script inserts with."""
    model, model_version = resolve_embedding_identity(
        provider, embedding_model, embedding_model_version
    )
    text = build_episode_onset_text(
        onset_summary=onset_summary, onset_indicators=onset_indicators
    )
    result = embed_texts(provider, [text], model=model, model_version=model_version)[0]
    return EpisodeEmbedding(
        vector=tuple(result.vector), model=model, model_version=model_version
    )


def upsert_historical_episode(
    session: Session,
    record: EpisodeRecord,
    *,
    provider: EmbeddingProvider | None = None,
    embedding: EpisodeEmbedding | None = None,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
) -> HistoricalEpisode:
    """Create or update one curated episode, re-embedding its onset only when that changed."""
    model, model_version = resolve_embedding_identity(
        provider, embedding_model, embedding_model_version
    )
    if embedding is not None:
        validate_episode_embedding(embedding, model=model, model_version=model_version)

    existing = _find_episode(session, record)
    vector: tuple[float, ...] | None = None
    if existing is None or _onset_is_stale(
        existing, record=record, model=model, model_version=model_version
    ):
        if embedding is not None:
            vector = embedding.vector
        elif provider is not None:
            vector = embed_episode_onset(
                provider,
                record.onset_summary,
                record.onset_indicators,
                embedding_model=model,
                embedding_model_version=model_version,
            ).vector
        else:
            msg = (
                "a new or re-embedded episode needs a provider or a supplied embedding: "
                "onset_embedding is NOT NULL and must never be a placeholder"
            )
            raise ValueError(msg)

    episode = existing
    if episode is None:
        episode = HistoricalEpisode()
        if record.episode_id is not None:
            episode.id = record.episode_id
        session.add(episode)
    for name in CURATED_FIELDS:
        setattr(episode, name, getattr(record, name))
    if vector is not None:
        episode.onset_embedding = list(vector)
        episode.model = model
        episode.model_version = model_version
    session.flush()
    return episode


def refresh_episode_embeddings(
    session: Session,
    provider: EmbeddingProvider,
    *,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE,
    limit: int | None = None,
) -> int:
    """Re-embed curated episodes whose stored vector sits outside the configured model space.

    This is the migration path ADR 0004's stored ``model``/``model_version`` exists to make
    possible: switch the configured space, run this, and every episode is carried across.
    """
    model, model_version = resolve_embedding_identity(
        provider, embedding_model, embedding_model_version
    )
    stmt = (
        select(HistoricalEpisode)
        .where(
            or_(
                HistoricalEpisode.model != model,
                HistoricalEpisode.model_version != model_version,
            )
        )
        .order_by(HistoricalEpisode.onset_date)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    episodes = list(session.scalars(stmt).all())
    if not episodes:
        return 0

    texts = [
        build_episode_onset_text(
            onset_summary=episode.onset_summary, onset_indicators=episode.onset_indicators
        )
        for episode in episodes
    ]
    results = embed_texts(
        provider, texts, model=model, model_version=model_version, batch_size=batch_size
    )
    for episode, result in zip(episodes, results, strict=True):
        episode.onset_embedding = list(result.vector)
        episode.model = model
        episode.model_version = model_version
    session.flush()
    return len(episodes)


def episode_onset_is_stale(
    existing: HistoricalEpisode, record: EpisodeRecord, *, model: str, model_version: str
) -> bool:
    """Whether ``existing`` needs a new vector for ``record``: onset text, version or space moved.

    The seam a batch curation seeder plans against. It is the same predicate
    :func:`upsert_historical_episode` applies row by row -- exposed, not reimplemented, so a
    planner that batches 100 onset texts into one embeddings request cannot drift out of agreement
    with the writer about which rows need embedding.
    """
    return _onset_is_stale(existing, record=record, model=model, model_version=model_version)


def episode_fields_differ(existing: HistoricalEpisode, record: EpisodeRecord) -> bool:
    """Whether any curated field on the stored row differs from the curated record.

    Distinguishes a genuine update from an unchanged re-seed, so an idempotent run can report
    "unchanged" honestly instead of counting every row as written.
    """
    return any(
        getattr(existing, name) != getattr(record, name) for name in CURATED_FIELDS
    )


def _find_episode(session: Session, record: EpisodeRecord) -> HistoricalEpisode | None:
    """Locate the curated row: by explicit id when the curator pinned one, else by name."""
    if record.episode_id is not None:
        return session.get(HistoricalEpisode, record.episode_id)
    stmt = select(HistoricalEpisode).where(HistoricalEpisode.name == record.name)
    return session.scalars(stmt).first()


def _onset_is_stale(
    existing: HistoricalEpisode, *, record: EpisodeRecord, model: str, model_version: str
) -> bool:
    """True when the stored vector no longer describes the curated onset in the pinned space."""
    if (existing.model, existing.model_version) != (model, model_version):
        return True
    if existing.version != record.version:
        return True
    return _onset_text(existing) != _onset_text(record)


def _onset_text(source: HistoricalEpisode | EpisodeRecord) -> str:
    return build_episode_onset_text(
        onset_summary=source.onset_summary, onset_indicators=source.onset_indicators
    )


__all__ = [
    "CURATED_FIELDS",
    "EpisodeEmbedding",
    "EpisodeRecord",
    "embed_episode_onset",
    "episode_fields_differ",
    "episode_onset_is_stale",
    "refresh_episode_embeddings",
    "upsert_historical_episode",
    "validate_episode_embedding",
]
