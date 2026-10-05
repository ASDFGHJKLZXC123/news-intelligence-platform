"""Immutable settings/readiness API proof on an owned disposable PostgreSQL database."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import PersonalProfileRevision, PersonalWorkspace, Source
from services.personal.runs import create_daily_run
from services.personal.settings import PersonalSettingsUpdate, update_settings
from services.personal.workspace import ensure_workspace
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_brief_api import _client

pytestmark = pytest.mark.integration


def test_settings_revisions_preserve_run_scope_and_reject_timezone_change():
    with migrated_disposable_engine() as engine:
        with Session(engine) as session:
            source = Source(
                name="Settings synthetic RSS", feed_url="https://settings.example.test/feed"
            )
            session.add(source)
            workspace, _ = ensure_workspace(session)
            session.flush()
            request = PersonalSettingsUpdate(
                selected_source_ids=[source.id],
                timezone="America/New_York",
                daily_article_limit=20,
                run_article_limit=20,
                enrichment_article_limit=20,
            )
            first = update_settings(session, workspace, request)
            session.commit()
            first_id = first.id
            first_values = dict(first.settings)
            run = create_daily_run(
                session, workspace, now=datetime.datetime(2026, 9, 20, 18, tzinfo=datetime.UTC)
            ).run
            session.commit()
            second = update_settings(
                session,
                workspace,
                request.model_copy(update={"daily_article_limit": 10, "run_article_limit": 40}),
            )
            session.commit()
            assert second.id != first_id and second.revision == first.revision + 1
            assert run.profile_revision_id == first_id
            assert session.get(PersonalProfileRevision, first_id).settings == first_values
            assert workspace.active_profile_revision_id == second.id
            with pytest.raises(ValueError, match="after the first run"):
                update_settings(
                    session,
                    workspace,
                    request.model_copy(update={"timezone": "America/Los_Angeles"}),
                )
            session.rollback()
            assert session.get(PersonalWorkspace, workspace.id).timezone == "America/New_York"


def test_settings_api_is_explicit_and_raw_readiness_survives_missing_ai_configuration():
    with migrated_disposable_engine() as engine:
        with Session(engine) as session:
            workspace, _ = ensure_workspace(session)
            source = Source(
                name="Synthetic explicit source", feed_url="https://settings-api.example.test/feed"
            )
            session.add(source)
            session.commit()
            source_id = str(source.id)
            initial_profile = workspace.active_profile_revision_id
        with _client(engine) as client:
            before = client.get("/api/v1/personal/settings")
            assert before.status_code == 200
            assert before.json()["settings"]["ai_enabled"] is False
            assert before.json()["settings"]["monthly_allowance_usd"] is None
            assert before.json()["saved_suggestion"]["monthly_allowance_authorized"] is False
            with Session(engine) as session:
                assert (
                    session.scalar(select(PersonalWorkspace)).active_profile_revision_id
                    == initial_profile
                )
            changed = client.put(
                "/api/v1/personal/settings",
                json={
                    "selected_source_ids": [source_id],
                    "execution_profile": "assisted",
                    "daily_article_limit": 20,
                    "run_article_limit": 20,
                    "enrichment_article_limit": 20,
                    "include_phrases": ["interest rate"],
                    "ai_enabled": False,
                },
            )
            assert changed.status_code == 200, changed.text
            body = changed.json()
            assert body["profile"]["schema_revision"] == "personal-profile.v2"
            assert body["effective_run_allowance_usd"] == "0"
            assert body["features"]["raw_collection"]["state"] == "ready"
            assert body["features"]["raw_reader"]["state"] == "ready"
            assert body["features"]["embeddings"]["reason"] == "ai_disabled"
            status = client.get("/api/v1/personal/workspace").json()
            assert status["actions"]["can_start"] is True
            rejected = client.put(
                "/api/v1/personal/settings", json={"monthly_allowance_usd": "NaN"}
            )
            assert rejected.status_code == 422
            assert (
                client.get("/api/v1/personal/settings").json()["profile"]["id"]
                == body["profile"]["id"]
            )


def test_draft_feed_is_inactive_until_explicit_settings_save_and_never_reactivates_legacy_source():
    with migrated_disposable_engine() as engine:
        with Session(engine) as session:
            workspace, _ = ensure_workspace(session)
            legacy = Source(
                name="Disabled legacy feed",
                feed_url="https://legacy.example.test/feed",
                active=False,
            )
            session.add(legacy)
            session.commit()
            legacy_id = str(legacy.id)
            initial_profile = workspace.active_profile_revision_id
        with _client(engine) as client:
            created = client.post(
                "/api/v1/personal/sources",
                json={"name": "Draft RSS", "feed_url": "https://draft.example.test/rss"},
            )
            assert created.status_code == 201, created.text
            item = created.json()["source"]
            assert item["draft"] is True and item["active"] is False
            assert (
                client.post(
                    "/api/v1/personal/sources",
                    json={"name": "Duplicate draft", "feed_url": "https://draft.example.test/rss"},
                ).json()["source"]["id"]
                == item["id"]
            )
            with Session(engine) as session:
                assert (
                    session.scalar(select(PersonalWorkspace)).active_profile_revision_id
                    == initial_profile
                )
            selected = client.put(
                "/api/v1/personal/settings", json={"selected_source_ids": [item["id"]]}
            )
            assert selected.status_code == 200, selected.text
            active = next(
                source
                for source in selected.json()["available_sources"]
                if source["id"] == item["id"]
            )
            assert active["active"] is True and active["draft"] is False
            reused = client.post(
                "/api/v1/personal/sources",
                json={"name": "Do not reactivate", "feed_url": "https://legacy.example.test/feed"},
            )
            assert reused.json()["source"]["draft"] is False
            rejected = client.put(
                "/api/v1/personal/settings", json={"selected_source_ids": [legacy_id]}
            )
            assert rejected.status_code == 422
            malformed = client.post(
                "/api/v1/personal/sources",
                json={"name": "Credential URL", "feed_url": "https://user:secret@example.test/rss"},
            )
            assert malformed.status_code == 422


@pytest.fixture(autouse=True)
def _phase4a_personal_test_context(monkeypatch):
    """Retain authored fixture time under explicit personal deployment selection."""
    import sys

    from packages.config.settings import get_settings
    from tests.integration._personal_processing_clock import install_authored_processing_clock

    monkeypatch.setenv("PERSONAL_PROCESSING_MODE", "personal")
    get_settings.cache_clear()
    install_authored_processing_clock(monkeypatch, sys.modules[__name__])
    yield
    get_settings.cache_clear()
