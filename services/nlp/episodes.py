"""Curated historical-episode embedding lifecycle (ADR 0004 + historical-episode spec).

The legacy ``historical_episodes.onset_embedding`` remains NOT NULL during the expand phase, so a
new episode is written there once for compatibility. Its durable embedding history lives in
``historical_episode_embeddings``: one append-only row per curated episode version and exact model
snapshot. Re-embedding never overwrites or relabels an older vector.

Only onset text is ever embedded (see ``services.nlp.embedding_text``). Outcome fields are stored
on the row and joined in *after* matching, for base rates -- they never enter the vector.

Changing onset text requires a curated version bump. This makes the sidecar key an honest
description of its input; silently rewriting onset text under the same version is refused.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from db.models import EMBEDDING_DIM, HistoricalEpisode, HistoricalEpisodeEmbedding
from packages.providers.base import EmbeddingProvider, ensure_finite_vector
from packages.providers.openai_embeddings import MAX_EMBEDDING_BATCH_SIZE
from services.nlp.embedding_text import build_episode_onset_text
from services.nlp.embeddings import (
    embed_texts,
    resolve_embedding_identity,
    snapshot_manifest_sha256_for_identity,
)

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

#: Canonical input builder contract recorded beside every newly generated episode vector. This
#: exact identifier is also recorded in embedding snapshot manifests.
EPISODE_EMBEDDING_INPUT_CONTRACT_VERSION = "historical-episode-onset-text.v1"


@dataclass(frozen=True)
class EpisodeEmbedding:
    """An onset vector plus the model space it was produced in."""

    vector: tuple[float, ...]
    model: str
    model_version: str
    snapshot_manifest_sha256: str | None = None


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
            f"episode embedding dimension {len(embedding.vector)} != EMBEDDING_DIM {EMBEDDING_DIM}"
        )
        raise ValueError(msg)
    # A caller may hand us a vector it built itself (the seeder does). A non-finite component is
    # as unusable as a wrong width, and far quieter: the episode would seed, index, and then never
    # match anything.
    ensure_finite_vector(embedding.vector)
    expected_manifest = snapshot_manifest_sha256_for_identity(
        model=model,
        model_version=model_version,
    )
    if embedding.snapshot_manifest_sha256 != expected_manifest:
        raise ValueError(
            "episode embedding snapshot manifest does not match the registered model space"
        )


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
    text = build_episode_onset_text(onset_summary=onset_summary, onset_indicators=onset_indicators)
    result = embed_texts(provider, [text], model=model, model_version=model_version)[0]
    return EpisodeEmbedding(
        vector=tuple(result.vector),
        model=model,
        model_version=model_version,
        snapshot_manifest_sha256=snapshot_manifest_sha256_for_identity(
            model=model,
            model_version=model_version,
        ),
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
    """Create or update one curated episode and ensure its exact sidecar vector exists."""
    model, model_version = resolve_embedding_identity(
        provider, embedding_model, embedding_model_version
    )
    snapshot_manifest_sha256 = snapshot_manifest_sha256_for_identity(
        model=model,
        model_version=model_version,
    )
    if embedding is not None:
        validate_episode_embedding(embedding, model=model, model_version=model_version)

    existing = _find_episode(session, record)
    if existing is not None:
        _validate_episode_revision(existing, record)

    stored_embedding = (
        None
        if existing is None
        else session.get(
            HistoricalEpisodeEmbedding,
            (existing.id, model, model_version, record.version),
        )
    )
    vector: tuple[float, ...] | None = None
    needs_embedding = existing is None or episode_onset_is_stale(
        existing,
        record,
        model=model,
        model_version=model_version,
        embedding=stored_embedding,
    )
    if needs_embedding:
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
        for name in CURATED_FIELDS:
            setattr(episode, name, getattr(record, name))
        # Compatibility write for the old NOT NULL columns. They are deliberately never changed
        # on an existing episode; the sidecar below is the source of truth for all new reads.
        assert vector is not None
        episode.onset_embedding = list(vector)
        episode.model = model
        episode.model_version = model_version
        session.add(episode)
        session.flush()
    else:
        for name in CURATED_FIELDS:
            setattr(episode, name, getattr(record, name))

    if needs_embedding:
        assert vector is not None
        session.add(
            _embedding_row(
                episode,
                record,
                vector=vector,
                model=model,
                model_version=model_version,
                snapshot_manifest_sha256=snapshot_manifest_sha256,
            )
        )
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
    """Append the configured sidecar vector for every current episode version missing it."""
    model, model_version = resolve_embedding_identity(
        provider, embedding_model, embedding_model_version
    )
    snapshot_manifest_sha256 = snapshot_manifest_sha256_for_identity(
        model=model,
        model_version=model_version,
    )
    stmt = (
        select(HistoricalEpisode)
        .outerjoin(
            HistoricalEpisodeEmbedding,
            and_(
                HistoricalEpisodeEmbedding.historical_episode_id == HistoricalEpisode.id,
                HistoricalEpisodeEmbedding.model == model,
                HistoricalEpisodeEmbedding.model_version == model_version,
                HistoricalEpisodeEmbedding.episode_version == HistoricalEpisode.version,
            ),
        )
        .where(HistoricalEpisodeEmbedding.historical_episode_id.is_(None))
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
        session.add(
            HistoricalEpisodeEmbedding(
                historical_episode_id=episode.id,
                model=model,
                model_version=model_version,
                episode_version=episode.version,
                dimension=EMBEDDING_DIM,
                onset_embedding=list(result.vector),
                input_sha256=episode_embedding_input_sha256(episode),
                input_contract_version=EPISODE_EMBEDDING_INPUT_CONTRACT_VERSION,
                snapshot_manifest_sha256=snapshot_manifest_sha256,
            )
        )
    session.flush()
    return len(episodes)


def episode_onset_is_stale(
    existing: HistoricalEpisode,
    record: EpisodeRecord,
    *,
    model: str,
    model_version: str,
    embedding: HistoricalEpisodeEmbedding | None = None,
) -> bool:
    """Whether the exact sidecar vector for ``record`` and the requested space is absent.

    The seam a batch curation seeder plans against. It is the same predicate
    :func:`upsert_historical_episode` applies row by row -- exposed, not reimplemented, so a
    planner that batches 100 onset texts into one embeddings request cannot drift out of agreement
    with the writer about which rows need embedding.
    """
    _validate_episode_revision(existing, record)
    if embedding is None:
        return True
    if (
        embedding.historical_episode_id,
        embedding.model,
        embedding.model_version,
        embedding.episode_version,
    ) != (existing.id, model, model_version, record.version):
        return True
    expected_hash = episode_embedding_input_sha256(record)
    if embedding.input_sha256 is not None and embedding.input_sha256 != expected_hash:
        raise ValueError(
            "stored historical-episode embedding input hash does not match its curated "
            "episode version; refusing to overwrite an immutable sidecar row"
        )
    expected_manifest = snapshot_manifest_sha256_for_identity(
        model=model,
        model_version=model_version,
    )
    if embedding.snapshot_manifest_sha256 != expected_manifest:
        raise ValueError(
            "stored historical-episode embedding snapshot manifest does not match its "
            "registered model space; refusing to trust or overwrite an immutable sidecar row"
        )
    return False


def episode_fields_differ(existing: HistoricalEpisode, record: EpisodeRecord) -> bool:
    """Whether any curated field on the stored row differs from the curated record.

    Distinguishes a genuine update from an unchanged re-seed, so an idempotent run can report
    "unchanged" honestly instead of counting every row as written.
    """
    return any(getattr(existing, name) != getattr(record, name) for name in CURATED_FIELDS)


def _find_episode(session: Session, record: EpisodeRecord) -> HistoricalEpisode | None:
    """Locate the curated row: by explicit id when the curator pinned one, else by name."""
    if record.episode_id is not None:
        return session.get(HistoricalEpisode, record.episode_id)
    stmt = select(HistoricalEpisode).where(HistoricalEpisode.name == record.name)
    return session.scalars(stmt).first()


def _validate_episode_revision(existing: HistoricalEpisode, record: EpisodeRecord) -> None:
    """Keep the version in the embedding key honest about changes to its onset input."""
    if record.version < existing.version:
        raise ValueError(
            f"episode version cannot move backward ({record.version} < {existing.version})"
        )
    if _onset_text(existing) != _onset_text(record) and record.version <= existing.version:
        raise ValueError(
            "episode onset text changed without an episode version bump; increment version "
            "before generating a new embedding"
        )


def episode_embedding_input_sha256(source: HistoricalEpisode | EpisodeRecord) -> str:
    """SHA-256 of the exact UTF-8 onset text sent to the embedding provider."""
    return hashlib.sha256(_onset_text(source).encode("utf-8")).hexdigest()


def _embedding_row(
    episode: HistoricalEpisode,
    record: EpisodeRecord,
    *,
    vector: tuple[float, ...],
    model: str,
    model_version: str,
    snapshot_manifest_sha256: str | None,
) -> HistoricalEpisodeEmbedding:
    return HistoricalEpisodeEmbedding(
        historical_episode_id=episode.id,
        model=model,
        model_version=model_version,
        episode_version=record.version,
        dimension=EMBEDDING_DIM,
        onset_embedding=list(vector),
        input_sha256=episode_embedding_input_sha256(record),
        input_contract_version=EPISODE_EMBEDDING_INPUT_CONTRACT_VERSION,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
    )


def _onset_text(source: HistoricalEpisode | EpisodeRecord) -> str:
    return build_episode_onset_text(
        onset_summary=source.onset_summary, onset_indicators=source.onset_indicators
    )


__all__ = [
    "CURATED_FIELDS",
    "EPISODE_EMBEDDING_INPUT_CONTRACT_VERSION",
    "EpisodeEmbedding",
    "EpisodeRecord",
    "embed_episode_onset",
    "episode_embedding_input_sha256",
    "episode_fields_differ",
    "episode_onset_is_stale",
    "refresh_episode_embeddings",
    "upsert_historical_episode",
    "validate_episode_embedding",
]
