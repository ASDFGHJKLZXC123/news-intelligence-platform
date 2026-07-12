"""Cluster unclustered articles into events and emit event_risk_features (Stage P4).

Loads articles that have an embedding but no event yet, clusters them, and writes Event +
EventArticle rows plus one EventRiskFeature per event (the P4-P5 output contract). The
clustering/feature math is pure (see services.nlp); this module is the DB persistence and
is integration-tested.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Article, ArticleEmbedding, Event, EventArticle, EventRiskFeature, Source
from services.nlp.clustering import ClusterItem, cluster_by_similarity
from services.nlp.features import ArticleRecord, compute_event_features


@dataclass(frozen=True)
class ClusterResult:
    """Outcome counts for one clustering run."""

    events_created: int
    articles_clustered: int
    features_emitted: int


def cluster_unclustered_articles(session: Session, *, threshold: float = 0.8) -> ClusterResult:
    """Cluster embedded, not-yet-evented articles into events with risk features."""
    stmt = (
        select(Article, ArticleEmbedding.embedding, Source.authority_score)
        .join(ArticleEmbedding, ArticleEmbedding.article_id == Article.id)
        .join(Source, Source.id == Article.source_id)
        .outerjoin(EventArticle, EventArticle.article_id == Article.id)
        .where(EventArticle.article_id.is_(None))
        .order_by(Article.fetched_at)
    )
    rows = session.execute(stmt).all()
    if not rows:
        return ClusterResult(0, 0, 0)

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
    for group in cluster_by_similarity(items, threshold):
        group_keys = [items[index].key for index in group]
        features = compute_event_features([records_by_key[key] for key in group_keys])
        lead = articles_by_key[group_keys[0]]

        event = Event(
            title=lead.title,
            summary=lead.summary,
            severity_score=features["severity_score"],
            article_count=features["article_count"],
            source_count=features["source_count"],
            first_seen_at=features["first_seen_at"],
            last_seen_at=features["last_seen_at"],
        )
        session.add(event)
        session.flush()

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
    return ClusterResult(events_created, articles_clustered, features_emitted)
