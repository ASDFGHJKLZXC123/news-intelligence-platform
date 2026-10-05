"""Embedding lifecycle for articles and events (ADR 0004).

Vectors are keyed by ``(subject, model, model_version)``, so one subject can hold one vector per
model space and a future model migration can dual-write both. Every call therefore pins exactly
one space up front and only ever fills the gaps *in that space*: an article already embedded by
the current model is skipped, and an article embedded only by some other model is not.

Nothing a provider returns is trusted into the database. Results are checked for identity and
width before a row is built, and a chunk that comes back a different length than the chunk sent
raises instead of being zipped against it -- a silently shortened response would otherwise pair
every text after the gap with the wrong vector.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from db.models import EMBEDDING_DIM, Article, ArticleEmbedding, Event, EventEmbedding
from packages.config.settings import Settings, get_settings
from packages.providers.base import EmbeddingProvider, EmbeddingResult
from packages.providers.openai_embeddings import (
    MAX_EMBEDDING_BATCH_SIZE,
    OpenAIEmbeddingProvider,
)
from services.nlp.embedding_text import build_article_embedding_text, build_event_embedding_text
from services.nlp.snapshot_registry import (
    DEFAULT_REGISTRY_PATH,
    SNAPSHOT_ID_PATTERN,
    EmbeddingSnapshotError,
    SnapshotRecord,
    active_snapshot,
    require_registered_snapshot,
)


def validate_production_embedding_identity(
    settings: Settings,
) -> SnapshotRecord | None:
    """Fail before a live write unless its vector space has source-controlled probe evidence."""

    if not settings.embedding_require_registered_snapshot:
        return None
    record = active_snapshot(
        model=settings.embedding_model,
        dimension=EMBEDDING_DIM,
        registry_path=DEFAULT_REGISTRY_PATH,
    )
    if record.snapshot_id != settings.embedding_model_version:
        raise EmbeddingSnapshotError(
            "configured embedding model version "
            f"{settings.embedding_model_version!r} is not the active snapshot "
            f"{record.snapshot_id!r}"
        )
    return record


def snapshot_manifest_sha256_for_identity(
    *,
    model: str,
    model_version: str,
    dimension: int = EMBEDDING_DIM,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> str | None:
    """Resolve provenance for a snapshot-shaped identity; legacy/test labels return ``None``.

    A name in the canonical ``nip-es1-*`` namespace is a claim that source-controlled probe
    evidence exists.  Such a claim must resolve successfully instead of being accepted as an
    arbitrary string.
    """

    if SNAPSHOT_ID_PATTERN.fullmatch(model_version) is None:
        return None
    return require_registered_snapshot(
        model=model,
        model_version=model_version,
        dimension=dimension,
        registry_path=registry_path,
    ).manifest_sha256


def build_embedding_provider(
    settings: Settings | None = None, *, client: object | None = None
) -> OpenAIEmbeddingProvider:
    """Build the production adapter in the configured model space at the column's dimension."""
    settings = settings or get_settings()
    validate_production_embedding_identity(settings)
    return OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model_name=settings.embedding_model,
        model_version=settings.embedding_model_version,
        dimension=EMBEDDING_DIM,
        timeout=settings.llm_request_timeout_seconds,
        client=client,  # type: ignore[arg-type]
    )


def resolve_embedding_identity(
    provider: EmbeddingProvider | None = None,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
) -> tuple[str, str]:
    """Pin the one vector space a call reads and writes: explicit, else provider, else settings."""
    settings = get_settings()
    model = embedding_model
    if model is None:
        model = provider.model_name if provider is not None else settings.embedding_model
    version = embedding_model_version
    if version is None:
        version = (
            provider.model_version if provider is not None else settings.embedding_model_version
        )
    return model, version


def validate_embedding_result(result: EmbeddingResult, *, model: str, model_version: str) -> None:
    """Reject any vector that is not what the pinned space says it must be."""
    if (result.model_name, result.model_version) != (model, model_version):
        msg = (
            "embedding result identity "
            f"{result.model_name}@{result.model_version} != configured identity "
            f"{model}@{model_version}"
        )
        raise ValueError(msg)
    if result.dimension != EMBEDDING_DIM or len(result.vector) != EMBEDDING_DIM:
        msg = f"embedding dimension {result.dimension} != EMBEDDING_DIM {EMBEDDING_DIM}"
        raise ValueError(msg)


def embed_texts(
    provider: EmbeddingProvider,
    texts: Sequence[str],
    *,
    model: str,
    model_version: str,
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE,
) -> list[EmbeddingResult]:
    """Embed texts in deterministic chunks, validating each chunk's cardinality and identity."""
    if not 1 <= batch_size <= MAX_EMBEDDING_BATCH_SIZE:
        msg = f"embedding batch size must be 1..{MAX_EMBEDDING_BATCH_SIZE}"
        raise ValueError(msg)

    results: list[EmbeddingResult] = []
    for start in range(0, len(texts), batch_size):
        chunk = list(texts[start : start + batch_size])
        chunk_results = list(provider.embed(chunk))
        if len(chunk_results) != len(chunk):
            msg = f"provider returned {len(chunk_results)} vectors for {len(chunk)} texts"
            raise ValueError(msg)
        results.extend(chunk_results)

    for result in results:
        validate_embedding_result(result, model=model, model_version=model_version)
    return results


def embed_unembedded_articles(
    session: Session,
    provider: EmbeddingProvider,
    *,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE,
    limit: int | None = None,
    article_ids: Sequence[uuid.UUID] | None = None,
) -> int:
    """Embed articles missing a vector in the selected model space (ADR 0004)."""
    model, model_version = resolve_embedding_identity(
        provider, embedding_model, embedding_model_version
    )
    stmt = (
        select(Article)
        .outerjoin(
            ArticleEmbedding,
            and_(
                ArticleEmbedding.article_id == Article.id,
                ArticleEmbedding.model == model,
                ArticleEmbedding.model_version == model_version,
            ),
        )
        .where(ArticleEmbedding.article_id.is_(None))
        .order_by(Article.fetched_at)
    )
    if article_ids is not None:
        stmt = stmt.where(Article.id.in_(tuple(article_ids)))
    if limit is not None:
        stmt = stmt.limit(limit)
    articles = list(session.scalars(stmt).all())
    if not articles:
        return 0

    texts = [
        build_article_embedding_text(
            title=article.title, summary=article.summary, body=article.body
        )
        for article in articles
    ]
    results = embed_texts(
        provider, texts, model=model, model_version=model_version, batch_size=batch_size
    )
    for article, result in zip(articles, results, strict=True):
        session.add(
            ArticleEmbedding(
                article_id=article.id,
                model=model,
                model_version=model_version,
                dimension=result.dimension,
                embedding=list(result.vector),
            )
        )
    session.flush()
    return len(articles)


def embed_unembedded_events(
    session: Session,
    provider: EmbeddingProvider,
    *,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE,
    limit: int | None = None,
) -> int:
    """Embed events missing a vector in the selected model space (ADR 0004).

    Same keying and same dual-write safety as articles: an event embedding is one row per
    ``(event_id, model, model_version)``, so re-embedding into a new space adds a row instead of
    overwriting the space the current retrieval reads.
    """
    model, model_version = resolve_embedding_identity(
        provider, embedding_model, embedding_model_version
    )
    stmt = (
        select(Event)
        .outerjoin(
            EventEmbedding,
            and_(
                EventEmbedding.event_id == Event.id,
                EventEmbedding.model == model,
                EventEmbedding.model_version == model_version,
            ),
        )
        .where(EventEmbedding.event_id.is_(None))
        .order_by(Event.created_at)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    events = list(session.scalars(stmt).all())
    if not events:
        return 0

    texts = [
        build_event_embedding_text(title=event.title, summary=event.summary) for event in events
    ]
    results = embed_texts(
        provider, texts, model=model, model_version=model_version, batch_size=batch_size
    )
    for event, result in zip(events, results, strict=True):
        session.add(
            EventEmbedding(
                event_id=event.id,
                model=model,
                model_version=model_version,
                dimension=result.dimension,
                embedding=list(result.vector),
            )
        )
    session.flush()
    return len(events)


__all__ = [
    "build_embedding_provider",
    "embed_texts",
    "embed_unembedded_articles",
    "embed_unembedded_events",
    "resolve_embedding_identity",
    "snapshot_manifest_sha256_for_identity",
    "validate_production_embedding_identity",
    "validate_embedding_result",
]
