"""Seed only an identity-checked Phase 2 disposable harness with Phase 3 raw fixtures."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from db.models import Source
from packages.providers.base import RSSItem
from packages.providers.fakes import FakeRSSProvider
from scripts.personal_phase2_offline_db import MARKER_TABLE, _load_manifest
from services.personal.coordinator import run_personal_daily
from services.personal.runs import create_daily_run
from services.personal.settings import PersonalSettingsUpdate, update_settings
from services.personal.workspace import get_workspace


def main() -> None:
    manifest_path = Path(sys.argv[1]).resolve()
    manifest = _load_manifest(manifest_path)
    engine = create_engine(manifest["database"]["url"])
    with engine.connect() as connection:
        assert (
            connection.scalar(text(f"SELECT owner_token FROM {MARKER_TABLE}"))
            == manifest["owner_token"]
        )
    now = dt.datetime.now(dt.UTC)
    with Session(engine) as session:
        workspace, _ = get_workspace(session)
        assert workspace is not None
        source = session.scalar(select(Source))
        update_settings(
            session,
            workspace,
            PersonalSettingsUpdate(
                selected_source_ids=[source.id],
                execution_profile="raw",
                run_article_limit=2,
                daily_article_limit=2,
                enrichment_article_limit=2,
            ),
        )
        decision = create_daily_run(session, workspace, now=now)
        assert decision.created
        run_id, token = decision.run.id, decision.run.ownership_token
        session.commit()
    items = [
        RSSItem(
            guid=f"phase3-browser-{index}",
            title=title,
            summary=summary,
            url=f"https://offline.personal.test/phase3/{index}",
            published_at=None if index == 0 else now - dt.timedelta(hours=index),
            provider_name="offline-personal-fixture",
        )
        for index, (title, summary) in enumerate(
            [
                (
                    "Synthetic: AI lab releases a research note",
                    "A retained RSS description. Publication time was not supplied.",
                ),
                (
                    "Synthetic: Agency publishes a policy update",
                    "A retained RSS summary with an authentic fixture URL; AI is disabled.",
                ),
                (
                    "Synthetic: Technology reading remains in backlog",
                    "Durably retained pending candidate, not admitted or enriched.",
                ),
                (
                    "Synthetic: Business reading remains in backlog",
                    "This second pending candidate proves exact deferred counts.",
                ),
            ]
        )
    ]

    class NoPaidProvider:
        model_name = "disabled"
        model_version = "raw"

        def embed(self, _texts):
            raise AssertionError("synthetic raw browser seed cannot dispatch paid work")

    def no_report(*_args):
        raise AssertionError("synthetic raw browser seed cannot generate a report")

    result = run_personal_daily(
        run_id,
        token,
        session_factory=lambda: Session(engine, autoflush=False),
        rss_provider_factory=lambda _source: FakeRSSProvider(items),
        embedding_provider=NoPaidProvider(),
        orchestrator_factory=no_report,
        now=now,
        clock=lambda: now,
    )
    print(
        json.dumps(
            {
                "dataset": "synthetic",
                "run_id": str(run_id),
                "captured": result.articles_captured,
                "admitted": result.articles_admitted,
                "paid_dispatches": 0,
            }
        )
    )
    engine.dispose()


if __name__ == "__main__":
    main()
