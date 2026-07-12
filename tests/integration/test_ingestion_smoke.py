"""Stage P3 ingestion smoke test against a live database (fake RSS provider)."""

from __future__ import annotations

import datetime

import pytest
from alembic import command
from alembic.config import Config

from db.base import SessionLocal
from packages.providers.base import RSSItem
from packages.providers.fakes import FakeRSSProvider
from services.ingestion import ensure_source, ingest_source

pytestmark = pytest.mark.integration


def _isolated_provider() -> FakeRSSProvider:
    published_at = datetime.datetime(2026, 6, 20, 12, 0, tzinfo=datetime.UTC)
    return FakeRSSProvider(
        items=[
            RSSItem(
                guid="ingestion-smoke-1",
                title="Integration smoke headline one",
                url="https://integration.example.test/news/ingestion-smoke-1",
                published_at=published_at,
                summary="A unique article for the ingestion integration smoke.",
                source="integration-smoke",
                provider_name="integration-smoke",
            ),
            RSSItem(
                guid="ingestion-smoke-2",
                title="Integration smoke headline two",
                url="https://integration.example.test/news/ingestion-smoke-2",
                published_at=published_at,
                summary="Another unique article for the ingestion integration smoke.",
                source="integration-smoke",
                provider_name="integration-smoke",
            ),
        ]
    )


def test_ingest_is_idempotent(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")
    session = SessionLocal()
    try:
        source = ensure_source(
            session,
            name="Integration Smoke Feed",
            feed_url="https://integration.example.test/feed.xml",
        )
        provider = _isolated_provider()

        first = ingest_source(session, source, provider)
        assert first.fetched >= 2
        assert first.inserted == first.fetched

        second = ingest_source(session, source, provider)
        assert second.inserted == 0
        assert second.skipped_duplicates == second.fetched
        session.commit()
    finally:
        session.rollback()
        session.close()
