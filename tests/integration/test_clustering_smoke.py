"""Stage P4 smoke test: ingest -> embed -> cluster -> event_risk_features (live DB)."""

from __future__ import annotations

import datetime
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from db.base import SessionLocal
from db.models import (
    EMBEDDING_DIM,
    Article,
    ArticleEmbedding,
    EventArticle,
    EventRiskFeature,
    Source,
)
from packages.providers.fakes import FakeEmbeddingProvider, FakeRSSProvider
from services.ingestion import ensure_source, ingest_source
from services.nlp.cluster_service import cluster_unclustered_articles
from services.nlp.embeddings import embed_unembedded_articles

pytestmark = pytest.mark.integration


def test_ingest_embed_cluster_emits_features(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")
    session = SessionLocal()
    try:
        source = ensure_source(session, name="Fake Feed", feed_url="https://fake.example/feed.xml")
        ingest_source(session, source, FakeRSSProvider())

        provider = FakeEmbeddingProvider(dimension=EMBEDDING_DIM)
        embedded = embed_unembedded_articles(session, provider)
        assert embedded >= 2

        result = cluster_unclustered_articles(
            session,
            embedding_model=provider.model_name,
            embedding_model_version=provider.model_version,
        )
        assert result.events_created >= 1
        assert result.features_emitted == result.events_created
        assert result.articles_clustered == embedded

        feature_rows = session.scalar(select(func.count()).select_from(EventRiskFeature))
        assert feature_rows == result.features_emitted

        # Re-running clusters nothing new (idempotent on already-evented articles).
        assert (
            cluster_unclustered_articles(
                session,
                embedding_model=provider.model_name,
                embedding_model_version=provider.model_version,
            ).events_created
            == 0
        )
        session.commit()
    finally:
        session.rollback()
        session.close()


def test_clustering_uses_only_configured_embedding_version(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")
    session = SessionLocal()
    try:
        source = Source(
            name="Embedding space regression",
            feed_url=f"https://embedding-space-{uuid.uuid4()}.example/feed.xml",
        )
        session.add(source)
        session.flush()

        now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        articles = [
            Article(
                source_id=source.id,
                url=f"https://example.test/{uuid.uuid4()}",
                url_hash=uuid.uuid4().hex,
                title=f"Pinned-space article {index}",
                fetched_at=now + datetime.timedelta(minutes=index),
            )
            for index in range(2)
        ]
        session.add_all(articles)
        session.flush()

        pinned_vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
        incompatible_vectors = (
            [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2),
            [0.0, 0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 3),
        )
        for article, incompatible_vector in zip(articles, incompatible_vectors, strict=True):
            session.add_all(
                [
                    ArticleEmbedding(
                        article_id=article.id,
                        model="text-embedding-3-small",
                        model_version="current",
                        dimension=EMBEDDING_DIM,
                        embedding=pinned_vector,
                        created_at=now,
                    ),
                    ArticleEmbedding(
                        article_id=article.id,
                        model="text-embedding-3-small",
                        model_version="next",
                        dimension=EMBEDDING_DIM,
                        embedding=incompatible_vector,
                        created_at=now + datetime.timedelta(days=1),
                    ),
                ]
            )
        session.flush()

        cluster_unclustered_articles(session)

        event_ids = session.scalars(
            select(EventArticle.event_id).where(
                EventArticle.article_id.in_([article.id for article in articles])
            )
        ).all()
        assert len(event_ids) == 2
        assert len(set(event_ids)) == 1
    finally:
        session.rollback()
        session.close()
