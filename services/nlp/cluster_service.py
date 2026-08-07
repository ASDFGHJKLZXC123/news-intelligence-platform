"""Cluster unclustered articles into events and emit event_risk_features (Stage P4).

Loads articles that have an embedding but no event yet, clusters them, and writes Event +
EventArticle rows plus one EventRiskFeature per event (the P4-P5 output contract). The
clustering/feature math is pure (see services.nlp); this module is the DB persistence and
is integration-tested.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from db.models import Article, ArticleEmbedding, Event, EventArticle, EventRiskFeature, Source
from packages.config.settings import get_settings
from services.nlp.clustering import (
    DEFAULT_CLUSTERING_THRESHOLD,
    ClusterItem,
    cluster_by_similarity,
)
from services.nlp.features import ArticleRecord, compute_event_features


@dataclass(frozen=True)
class ClusterResult:
    """Outcome counts for one clustering run."""

    events_created: int
    articles_clustered: int
    features_emitted: int
    event_ids: tuple[uuid.UUID, ...] = ()


class IncompleteEmbeddingCoverageError(RuntimeError):
    """At least one unclustered article is absent from the requested vector space."""


def cluster_unclustered_articles(
    session: Session,
    *,
    threshold: float = DEFAULT_CLUSTERING_THRESHOLD,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
) -> ClusterResult:
    """Cluster articles in exactly one configured embedding vector space."""
    settings = get_settings()
    model = settings.embedding_model if embedding_model is None else embedding_model
    model_version = (
        settings.embedding_model_version
        if embedding_model_version is None
        else embedding_model_version
    )
    stmt = (
        select(Article, ArticleEmbedding.embedding, Source.authority_score)
        .outerjoin(
            ArticleEmbedding,
            and_(
                ArticleEmbedding.article_id == Article.id,
                ArticleEmbedding.model == model,
                ArticleEmbedding.model_version == model_version,
            ),
        )
        .join(Source, Source.id == Article.source_id)
        .outerjoin(EventArticle, EventArticle.article_id == Article.id)
        .where(EventArticle.article_id.is_(None))
        .order_by(Article.fetched_at)
    )
    rows = session.execute(stmt).all()
    if not rows:
        return ClusterResult(0, 0, 0)

    missing = [str(article.id) for article, embedding, _authority in rows if embedding is None]
    if missing:
        preview = ", ".join(missing[:5])
        suffix = "" if len(missing) <= 5 else f", ... (+{len(missing) - 5} more)"
        raise IncompleteEmbeddingCoverageError(
            f"{len(missing)} unclustered article(s) have no embedding in "
            f"{model}@{model_version}: {preview}{suffix}; refusing to report an empty or "
            "partial clustering success"
        )

    articles_by_key: dict[str, Article] = {}
    items: list[ClusterItem] = []
    records_by_key: dict[str, ArticleRecord] = {}
    for article, embedding, authority in rows:
        key = str(article.id)
        articles_by_key[key] = article
        items.append(
            ClusterItem(key=key, vector=tuple(embedding), content_hash=article.content_hash)
        )
        records_by_key[key] = ArticleRecord(
            key=key,
            source_id=str(article.source_id),
            published_at=article.published_at or article.fetched_at,
            title=article.title,
            source_authority=float(authority) if authority is not None else None,
        )

    events_created = 0
    articles_clustered = 0
    features_emitted = 0
    event_ids: list[uuid.UUID] = []
    for group in cluster_by_similarity(items, threshold):
        group_keys = [items[index].key for index in group]
        features = compute_event_features([records_by_key[key] for key in group_keys])
        lead = articles_by_key[group_keys[0]]

        event = Event(
            title=lead.title,
            summary=lead.summary,
            severity_score=features["severity_score"],
            hotness_score=features["hotness_score"],
            article_count=features["article_count"],
            source_count=features["source_count"],
            first_seen_at=features["first_seen_at"],
            last_seen_at=features["last_seen_at"],
        )
        session.add(event)
        session.flush()
        event_ids.append(event.id)

        for key in group_keys:
            session.add(EventArticle(event_id=event.id, article_id=articles_by_key[key].id))
            articles_clustered += 1

        session.add(
            EventRiskFeature(
                event_id=event.id,
                severity_score=features["severity_score"],
                novelty_score=features["novelty_score"],
                source_diversity_score=features["source_diversity_score"],
                source_authority_score=features["source_authority_score"],
                confidence_score=features["confidence_score"],
                observed_at=features["last_seen_at"],
                evidence_article_ids=[articles_by_key[key].id for key in group_keys],
            )
        )
        events_created += 1
        features_emitted += 1

    session.flush()
    return ClusterResult(
        events_created,
        articles_clustered,
        features_emitted,
        event_ids=tuple(event_ids),
    )
