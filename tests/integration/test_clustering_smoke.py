"""Stage P4 smoke test: ingest -> embed -> cluster -> event_risk_features (live DB)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from db.base import SessionLocal
from db.models import EMBEDDING_DIM, EventRiskFeature
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

        embedded = embed_unembedded_articles(
            session, FakeEmbeddingProvider(dimension=EMBEDDING_DIM)
        )
        assert embedded >= 2

        result = cluster_unclustered_articles(session)
        assert result.events_created >= 1
        assert result.features_emitted == result.events_created
        assert result.articles_clustered == embedded

        feature_rows = session.scalar(select(func.count()).select_from(EventRiskFeature))
        assert feature_rows == result.features_emitted

        # Re-running clusters nothing new (idempotent on already-evented articles).
        assert cluster_unclustered_articles(session).events_created == 0
        session.commit()
    finally:
        session.rollback()
        session.close()
