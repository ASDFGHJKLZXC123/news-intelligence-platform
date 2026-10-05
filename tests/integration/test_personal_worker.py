"""Actual personal Celery task boundary with an explicit offline runtime fixture."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

import workers.personal_tasks as personal_tasks
from db.models import (
    Article,
    ArticleEmbedding,
    Claim,
    ClaimEvidence,
    EvidenceItem,
    Job,
    LLMRun,
    PersonalBriefSnapshot,
    PersonalProfileRevision,
    PersonalRun,
    Report,
    ReportSection,
    Source,
)
from packages.config.settings import Settings
from packages.providers.base import RSSItem
from packages.providers.fakes import FakeRSSProvider
from services.ingestion.normalize import url_hash
from services.personal.live_smoke import CONFIG_SCHEMA, LiveSmokeConfig, LiveSmokeLedger
from services.personal.offline_fixture import offline_fixture_route
from services.personal.runs import (
    PersonalRunConflict,
    create_daily_run,
    retry_run,
    retry_status,
)
from services.personal.workspace import configure_profile, ensure_workspace
from services.reports.grounding_prompts import (
    GROUNDING_PROMPT_TEMPLATE_VERSION,
    GROUNDING_SCHEMA,
    GROUNDING_SCHEMA_VERSION,
)
from services.reports.prompts import (
    COMPOSITION_PROMPT_TEMPLATE_VERSION,
    COMPOSITION_SCHEMA,
    COMPOSITION_SCHEMA_VERSION,
)
from tests.integration._stage6_db import disposable_database, url_for
from tests.integration._stage7_db import migrated_disposable_engine

pytestmark = pytest.mark.integration
FIXTURE_PATH = (
    Path(__file__).parents[2]
    / "personal-project-conversion"
    / "fixtures"
    / "phase-2-offline-workflow.json"
)
ROUTE = offline_fixture_route("phase-2-offline-workflow-v1")
LIVE_DB_PREFIX = "nip_phase2_live_"
UUID_PATTERN = re.compile(r'"claim_ids"\s*:\s*\[\s*"([0-9a-f-]{36})"')
CLAIM_ID_PATTERN = re.compile(r'"claim_id"\s*:\s*"([0-9a-f-]{36})"')

LIVE_EXECUTIVE_TEXT = (
    "The configured feed reports that the agency approved the coastal resilience project. "
    "This brief records the approval as a source-reported assertion from the captured article. "
    "The retained excerpt attributes the statement to the agency and does not add a reason, "
    "budget, schedule, or predicted outcome. Coverage in this snapshot comes from one configured "
    "source, so the report does not treat repetition as corroboration. The item appears because "
    "it matched the personal source and phrase settings. No independent verification, risk "
    "estimate, historical comparison, or forecast is supplied. The available evidence supports "
    "only the reported approval and its attribution within this captured source record."
)
LIVE_TOP_EVENT_TEXT = (
    "The leading item in this personal snapshot is the reported approval of a coastal resilience "
    "project. The configured feed says the agency confirmed that approval, and the brief preserves "
    "that attribution. The source record does not state a budget, schedule, implementation "
    "milestone, motive, or expected effect, so this section adds none of those details. It also "
    "does not turn the source statement into an independently verified fact. Coverage is limited "
    "to the retained article from one selected feed. No second source in this run corroborates the "
    "assertion. The supported reading is narrow: the captured publisher reports an agency "
    "confirmation that the project was approved, with no further conclusion about delivery or "
    "impact. The record offers no basis for additional interpretation."
)


@contextlib.contextmanager
def _migrated_live_engine() -> Iterator[Engine]:
    """Own a correctly named disposable live-smoke database and apply the real migrations."""

    with disposable_database(LIVE_DB_PREFIX) as name:
        database_url = url_for(name)
        environment = dict(os.environ)
        environment["DATABASE_URL"] = database_url
        project_root = Path(__file__).parents[2]
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr[-2000:])
        engine = create_engine(database_url, future=True)
        try:
            yield engine
        finally:
            engine.dispose()


def _live_config(source_id: uuid.UUID, article_url: str) -> LiveSmokeConfig:
    return LiveSmokeConfig.from_mapping(
        {
            "schema": CONFIG_SCHEMA,
            "verification_id": str(uuid.uuid4()),
            "authorization": {
                "live_execution_authorized": True,
                "authorized_allowance_usd": "0.05",
                "authorized_at": "2026-09-09T12:00:00-07:00",
            },
            "capture": {
                "articles": [
                    {
                        "capture_id": "fixed-rss-article-1",
                        "source_id": str(source_id),
                        "canonical_url": article_url,
                        "url_hash": url_hash(article_url),
                    }
                ]
            },
            "interest": {"include_phrases": ["agency"], "exclude_phrases": []},
            "limits": {
                "max_dispatches": 8,
                "reasoning_input_tokens": 8192,
                "reasoning_output_tokens": 4096,
                "embedding_input_tokens": 8192,
                "embedding_batch_articles": 1,
            },
            "plan": {
                "generation_route_ids": ["generation-primary"],
                "probe_route_ids": [],
                "embedding_route_id": "embedding-primary",
            },
            "routes": [
                {
                    "route_id": "generation-primary",
                    "role": "reasoning",
                    "provider": "openai",
                    "model": "bounded-generation-test-model",
                    "model_version": "fixture-2026-09-09",
                    "max_dispatches": 7,
                    "input_usd_per_million_tokens": "0.01",
                    "output_usd_per_million_tokens": "0.02",
                    "price_source": {
                        "url": "https://openai.com/api/pricing/",
                        "version": "synthetic-test-rate-not-current",
                        "retrieved_at": "2026-09-09T12:00:00-07:00",
                    },
                },
                {
                    "route_id": "embedding-primary",
                    "role": "embedding",
                    "provider": "openai",
                    "model": "bounded-embedding-test-model",
                    "model_version": "fixture-2026-09-09",
                    "max_dispatches": 1,
                    "input_usd_per_million_tokens": "0.01",
                    "output_usd_per_million_tokens": "0",
                    "price_source": {
                        "url": "https://openai.com/api/pricing/",
                        "version": "synthetic-test-rate-not-current",
                        "retrieved_at": "2026-09-09T12:00:00-07:00",
                    },
                },
            ],
        }
    )


class _ScriptedLiveHTTPClient:
    """Physical HTTP seam only: production adapters still serialize and parse every payload."""

    def __init__(self, **_kwargs) -> None:  # noqa: ANN003
        self.calls: list[dict] = []
        self.closed = False
        self.options = dict(_kwargs)

    def post(self, path: str, *, json: dict, **_kwargs) -> httpx.Response:  # noqa: A002, ANN003
        self.calls.append(json)
        request = httpx.Request("POST", f"https://api.openai.com{path}")
        if path == "/v1/embeddings":
            return httpx.Response(
                200,
                request=request,
                json={
                    "model": json["model"],
                    "data": [{"index": 0, "embedding": [0.01] * 1536}],
                    "usage": {"prompt_tokens": 24, "total_tokens": 24},
                },
            )

        schema = json["response_format"]["json_schema"]["name"]
        prompt = json["messages"][0]["content"]
        claim_ids = tuple(
            dict.fromkeys((*UUID_PATTERN.findall(prompt), *CLAIM_ID_PATTERN.findall(prompt)))
        )
        assert claim_ids, prompt
        if schema == COMPOSITION_SCHEMA:
            text = (
                LIVE_EXECUTIVE_TEXT
                if "executive summary" in prompt.casefold()
                else LIVE_TOP_EVENT_TEXT
            )
            payload = {
                "schema_name": COMPOSITION_SCHEMA,
                "schema_version": COMPOSITION_SCHEMA_VERSION,
                "prompt_template_version": COMPOSITION_PROMPT_TEMPLATE_VERSION,
                "blocks": [{"text": text, "claim_ids": [claim_ids[0]]}],
            }
        elif schema == GROUNDING_SCHEMA:
            payload = {
                "schema_name": GROUNDING_SCHEMA,
                "schema_version": GROUNDING_SCHEMA_VERSION,
                "prompt_template_version": GROUNDING_PROMPT_TEMPLATE_VERSION,
                "verdicts": [
                    {"claim_id": claim_id, "verdict": "supported"} for claim_id in claim_ids
                ],
            }
        else:  # pragma: no cover - a new production request must be explicitly reviewed
            raise AssertionError(f"unexpected live-smoke schema {schema}")
        return httpx.Response(
            200,
            request=request,
            json={
                "id": f"fixture-{len(self.calls)}",
                "choices": [{"message": {"content": json_module(payload)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 80},
            },
        )

    def close(self) -> None:
        self.closed = True


def json_module(value: object) -> str:
    """Avoid shadowing by the production adapter's ``json`` keyword argument."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _queued_run(engine, *, second_missing_feed: bool = False):  # noqa: ANN001, ANN202
    now = datetime.datetime.now(datetime.UTC)
    with Session(engine) as session:
        sources = [
            Source(
                name="Synthetic Phase 2 acceptance feed",
                feed_url="https://offline.personal.test/feed.xml",
            )
        ]
        if second_missing_feed:
            sources.append(
                Source(
                    name="Synthetic missing feed",
                    feed_url="https://offline.personal.test/missing.xml",
                )
            )
        session.add_all(sources)
        session.flush()
        workspace, _ = ensure_workspace(session)
        configure_profile(
            session,
            workspace,
            selected_source_ids=[source.id for source in sources],
            include_phrases=["agency"],
            settings={"model_route": ROUTE},
        )
        decision = create_daily_run(session, workspace, now=now)
        token = decision.run.ownership_token
        assert token is not None
        ids = decision.run.id, token, workspace.id
        session.commit()
        return ids


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, engine, *, fixture_path: str) -> None:  # noqa: ANN001
    monkeypatch.setattr(personal_tasks, "SessionLocal", lambda: Session(engine, autoflush=False))
    monkeypatch.setattr(
        personal_tasks,
        "get_settings",
        lambda: Settings(
            app_env="test",
            personal_offline_fixture_path=fixture_path,
            llm_budget_enforced=False,
        ),
    )


def test_registered_worker_task_runs_actual_offline_coordinator_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with migrated_disposable_engine() as engine:
        run_id, token, _ = _queued_run(engine)
        _patch_runtime(monkeypatch, engine, fixture_path=str(FIXTURE_PATH))

        result = personal_tasks.run_personal_daily_task.run(str(run_id), str(token))

        assert result["status"] == "succeeded"
        assert result["published"] is True
        assert result["articles_captured"] == result["articles_admitted"] == 1
        assert result["events_observed"] == 1
        assert result["claims_supported"] >= 1
        assert result["fixture_label"].startswith("Synthetic Phase 2 acceptance")
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "succeeded"
            assert run.stage_results["grouping"]["events_created"] == 1
            assert session.scalar(select(func.count()).select_from(Article)) == 1
            assert session.scalar(select(func.count()).select_from(Report)) == 1
            embedding = session.scalar(select(ArticleEmbedding))
            assert embedding is not None
            assert (embedding.model, embedding.model_version) == (
                ROUTE["embedding"]["model"],
                ROUTE["embedding"]["model_version"],
            )


def test_worker_setup_failure_is_sanitized_and_stale_delivery_cannot_fail_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with migrated_disposable_engine() as engine:
        run_id, token, workspace_id = _queued_run(engine)
        _patch_runtime(monkeypatch, engine, fixture_path="")

        with pytest.raises(ValueError, match="PERSONAL_OFFLINE_FIXTURE_PATH"):
            personal_tasks.run_personal_daily_task.run(str(run_id), str(token))
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "failed"
            assert run.error == {
                "code": "worker_setup_failed",
                "message": "Personal update could not start; an explicit retry may be available.",
                "retryable": True,
            }
            assert session.scalar(select(func.count()).select_from(Article)) == 0
            workspace, _ = ensure_workspace(session)
            assert workspace.id == workspace_id
            retry = retry_run(
                session,
                workspace,
                run_id,
                now=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=5),
            )
            new_token = retry.run.ownership_token
            assert new_token is not None and new_token != token
            session.commit()

        with pytest.raises(ValueError, match="stale"):
            personal_tasks.run_personal_daily_task.run(str(run_id), str(token))
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "queued"
            assert run.ownership_token == new_token
            assert run.error is None


def test_worker_returns_partial_state_for_partial_offline_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with migrated_disposable_engine() as engine:
        run_id, token, _ = _queued_run(engine, second_missing_feed=True)
        _patch_runtime(monkeypatch, engine, fixture_path=str(FIXTURE_PATH))

        result = personal_tasks.run_personal_daily_task.run(str(run_id), str(token))

        assert result["status"] == "partially_failed"
        assert result["published"] is True
        assert result["feeds_succeeded"] == result["feeds_failed"] == 1
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "partially_failed"
            assert run.error["code"] == "partial_capture"


def test_raw_worker_needs_no_model_route_credentials_or_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.datetime.now(datetime.UTC)
    with migrated_disposable_engine() as engine:
        with Session(engine) as session:
            source = Source(name="Raw feed", feed_url="https://raw.test/feed.xml")
            session.add(source)
            session.flush()
            workspace, _ = ensure_workspace(session)
            configure_profile(
                session,
                workspace,
                selected_source_ids=[source.id],
                include_phrases=["agency"],
                execution_profile="raw",
                # A retained assisted setting must not override the frozen raw execution mode.
                settings={"model_route": ROUTE},
            )
            decision = create_daily_run(session, workspace, now=now)
            run_id = decision.run.id
            token = decision.run.ownership_token
            assert token is not None
            session.commit()

        monkeypatch.setattr(
            personal_tasks, "SessionLocal", lambda: Session(engine, autoflush=False)
        )
        monkeypatch.setattr(
            personal_tasks,
            "get_settings",
            lambda: Settings(
                app_env="test",
                anthropic_api_key="",
                openai_api_key="",
                gemini_api_key="",
                deepseek_api_key="",
                personal_offline_fixture_path="",
            ),
        )
        monkeypatch.setattr(
            personal_tasks,
            "HttpRSSProvider",
            lambda: FakeRSSProvider(
                [
                    RSSItem(
                        guid="raw-1",
                        title="Agency releases raw update",
                        url="https://raw.test/items/1",
                        published_at=now,
                        summary="The agency released a raw update.",
                        source="https://raw.test/feed.xml",
                    )
                ]
            ),
        )

        result = personal_tasks.run_personal_daily_task.run(str(run_id), str(token))

        assert result["status"] == "succeeded"
        assert result["published"] is False
        assert result["articles_captured"] == result["articles_admitted"] == 1
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run is not None and run.state == "succeeded"
            assert run.stage_results["embedding"]["status"] == "disabled"
            assert session.scalar(select(func.count()).select_from(LLMRun)) == 0


def test_ordinary_live_profile_cannot_start_outside_dedicated_smoke() -> None:
    now = datetime.datetime.now(datetime.UTC)
    with migrated_disposable_engine() as engine, Session(engine) as session:
        source = Source(name="Live route", feed_url="https://feed.example.test/rss")
        session.add(source)
        session.flush()
        workspace, _ = ensure_workspace(session)
        configure_profile(
            session,
            workspace,
            selected_source_ids=[source.id],
            include_phrases=["agency"],
            execution_profile="assisted",
            settings={
                "model_route": {
                    "mode": "live",
                    "generation": {"provider": "openai", "model": "configured-model"},
                    "embedding": {
                        "provider": "openai",
                        "model": "configured-embedding",
                        "model_version": "configured-version",
                    },
                },
                "authorized_spend_usd": "0.05",
            },
        )

        with pytest.raises(PersonalRunConflict, match="dedicated bounded smoke"):
            create_daily_run(session, workspace, now=now)
        assert session.scalar(select(func.count()).select_from(PersonalRun)) == 0


def test_dedicated_live_run_cannot_consume_an_ordinary_retry_attempt() -> None:
    now = datetime.datetime.now(datetime.UTC)
    with migrated_disposable_engine() as engine, Session(engine) as session:
        source = Source(name="Live route", feed_url="https://feed.example.test/rss")
        session.add(source)
        session.flush()
        workspace, _ = ensure_workspace(session)
        configure_profile(
            session,
            workspace,
            selected_source_ids=[source.id],
            include_phrases=["agency"],
            execution_profile="assisted",
            settings={
                "model_route": {
                    "mode": "live",
                    "generation": {"provider": "openai", "model": "configured-model"},
                    "embedding": {
                        "provider": "openai",
                        "model": "configured-embedding",
                        "model_version": "configured-version",
                    },
                },
                "authorized_spend_usd": "0.05",
            },
        )
        decision = create_daily_run(session, workspace, now=now, allow_bounded_live=True)
        run_id = decision.run.id
        job_id = decision.run.job_id
        decision.run.state = "failed"
        decision.run.error = {"code": "bounded_verification_failed"}
        job = session.get(Job, job_id)
        assert job is not None
        job.state = "failed"
        session.commit()

        run = session.get(PersonalRun, run_id)
        profile = session.get(PersonalProfileRevision, run.profile_revision_id)
        assert retry_status(run, now=now, profile=profile) == (
            False,
            "dedicated live smoke runs require a separately authorized verification",
        )
        with pytest.raises(PersonalRunConflict, match="cannot be retried through the ordinary API"):
            retry_run(session, workspace, run_id, now=now + datetime.timedelta(minutes=5))
        session.rollback()
        run = session.get(PersonalRun, run_id)
        job = session.get(Job, job_id)
        assert run.attempt == 1
        assert run.state == "failed"
        assert run.error == {"code": "bounded_verification_failed"}
        assert job is not None and job.state == "failed"


@pytest.mark.parametrize("accounting_blocked", [False, True])
def test_dedicated_live_smoke_uses_real_http_adapters_and_guarded_full_workflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, accounting_blocked: bool
) -> None:
    article_url = "https://news.example.test/coastal-project"
    source_id = uuid.uuid4()
    with _migrated_live_engine() as engine:
        with Session(engine) as session:
            session.add(
                Source(
                    id=source_id,
                    name="Fixed live-smoke source",
                    feed_url="https://news.example.test/feed.xml",
                )
            )
            session.commit()

        config = _live_config(source_id, article_url)
        ledger = LiveSmokeLedger.open(
            LiveSmokeLedger.canonical_path(tmp_path / "private", config.verification_id),
            config,
        )
        clients: list[_ScriptedLiveHTTPClient] = []

        def client_factory(**kwargs):  # noqa: ANN003, ANN202
            client = _ScriptedLiveHTTPClient(**kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr(
            personal_tasks, "SessionLocal", lambda: Session(engine, autoflush=False)
        )
        monkeypatch.setattr(personal_tasks.httpx, "Client", client_factory)
        monkeypatch.setattr(
            personal_tasks,
            "HttpRSSProvider",
            lambda: FakeRSSProvider(
                [
                    RSSItem(
                        guid="live-smoke-1",
                        title="Agency approves coastal resilience project",
                        url=article_url,
                        published_at=datetime.datetime.now(datetime.UTC)
                        - datetime.timedelta(minutes=10),
                        summary=(
                            "The agency confirmed the coastal resilience project was approved."
                        ),
                        source="https://news.example.test/feed.xml",
                    )
                ]
            ),
        )
        monkeypatch.setattr(
            personal_tasks,
            "get_settings",
            lambda: Settings(
                app_env="test",
                database_url=engine.url.render_as_string(hide_password=False),
                openai_api_key="explicit-test-only-key",
                anthropic_api_key="",
                gemini_api_key="",
                deepseek_api_key="",
                llm_budget_enforced=True,
            ),
        )

        if accounting_blocked:
            original_record_outcome = ledger.record_outcome

            def reject_successful_accounting(**kwargs):  # noqa: ANN003, ANN202
                if kwargs["outcome"] == "succeeded":
                    raise personal_tasks.LiveSmokeError(
                        "a successful outcome requires reconciled usage for every dispatch"
                    )
                return original_record_outcome(**kwargs)

            monkeypatch.setattr(ledger, "record_outcome", reject_successful_accounting)
            with pytest.raises(personal_tasks.LiveSmokeError, match="reconciled usage"):
                personal_tasks.execute_personal_live_smoke(config=config, ledger=ledger)
            result = None
        else:
            result = personal_tasks.execute_personal_live_smoke(config=config, ledger=ledger)

        ledger_state = ledger.snapshot()
        physical_payloads = [payload for client in clients for payload in client.calls]
        assert physical_payloads
        assert all(client.options["follow_redirects"] is False for client in clients)
        assert all(client.options["trust_env"] is False for client in clients)
        assert all(
            str(client.options["base_url"]).startswith("https://api.openai.com")
            for client in clients
        )
        assert all(
            payload["model"] in {"bounded-generation-test-model", "bounded-embedding-test-model"}
            for payload in physical_payloads
        )

        if accounting_blocked:
            outcome = ledger_state["outcome"]
            assert outcome["status"] == "blocked"
            assert outcome["reason"] == "accounting_incomplete"
            assert outcome["capture_ids"] == ["fixed-rss-article-1"]
            assert outcome["snapshot_id"] is not None
            assert outcome["report_id"] is not None
            with Session(engine) as session:
                run = session.get(PersonalRun, uuid.UUID(outcome["personal_run_id"]))
                report = session.get(Report, uuid.UUID(outcome["report_id"]))
                assert run is not None and report is not None
                assert run.state == "succeeded"
                assert report.status == "published"
                assert run.stage_results["live_smoke"] == {
                    "status": "verification_blocked",
                    "code": "accounting_incomplete",
                    "verification_id": config.verification_id,
                    "config_hash": config.config_hash,
                    "capture_ids": ["fixed-rss-article-1"],
                    "snapshot_id": outcome["snapshot_id"],
                    "report_id": outcome["report_id"],
                    "report_published": True,
                }
            return

        assert result is not None

        assert result["status"] == "succeeded"
        assert result["capture_ids"] == ["fixed-rss-article-1"]
        assert Decimal(result["ledger"]["reserved_usd"]) <= Decimal("0.05")
        assert ledger_state["outcome"]["status"] == "succeeded"
        assert 1 < ledger_state["totals"]["dispatches"] <= 8
        assert {item["status"] for item in ledger_state["dispatches"]} == {"reconciled"}

        with Session(engine) as session:
            run = session.get(PersonalRun, uuid.UUID(result["run_id"]))
            snapshot = session.get(PersonalBriefSnapshot, uuid.UUID(result["snapshot_id"]))
            assert run is not None and snapshot is not None
            profile = session.get(PersonalProfileRevision, run.profile_revision_id)
            report = session.get(Report, uuid.UUID(result["report_id"]))
            assert profile is not None and report is not None
            sections = list(
                session.execute(
                    select(ReportSection)
                    .where(ReportSection.report_id == report.id)
                    .order_by(ReportSection.section_order)
                ).scalars()
            )
            assert run.state == "succeeded"
            assert report.status == "published"
            assert snapshot.model_route == profile.settings["model_route"]
            assert snapshot.model_route["verification_id"] == config.verification_id
            assert snapshot.model_route["config_hash"] == config.config_hash
            assert snapshot.model_route["ledger_path"] == str(ledger.path)
            assert run.stage_results["live_smoke"]["status"] == "verified"
            assert run.stage_results["live_smoke"]["code"] == "ledger_reconciled"
            assert any("coastal resilience project" in section.body for section in sections)
            assert session.scalar(select(func.count()).select_from(Article)) == 1
            assert session.scalar(select(func.count()).select_from(Claim)) >= 1
            assert session.scalar(select(func.count()).select_from(EvidenceItem)) == 1
            assert session.scalar(select(func.count()).select_from(ClaimEvidence)) >= 1


def test_live_smoke_preflight_rejects_unsupported_or_ambiguous_paid_routes() -> None:
    config = _live_config(uuid.uuid4(), "https://news.example.test/fixed")
    settings = Settings(
        app_env="test",
        database_url="postgresql+psycopg2://news:news@127.0.0.1:55432/nip_phase2_live_unused",
        openai_api_key="explicit-test-only-key",
    )
    generation = config.route_by_id[config.generation_route_ids[0]]
    embedding = config.route_by_id[config.embedding_route_id]
    probe = replace(generation, route_id="identity-probe", role="probe")
    with pytest.raises(personal_tasks.LiveSmokeError, match="probe routes must be empty"):
        personal_tasks._preflight_live_smoke_runtime(
            settings,
            replace(
                config,
                probe_route_ids=(probe.route_id,),
                routes=(generation, probe, embedding),
            ),
        )

    fallback = replace(
        generation,
        route_id="generation-fallback",
        input_usd_per_million_tokens=Decimal("0.02"),
    )
    with pytest.raises(personal_tasks.LiveSmokeError, match="cannot carry two prices"):
        personal_tasks._preflight_live_smoke_runtime(
            settings,
            replace(
                config,
                generation_route_ids=(generation.route_id, fallback.route_id),
                routes=(generation, fallback, embedding),
            ),
        )

    custom_endpoint = settings.model_copy(
        update={"openai_base_url": "https://proxy.example.test/v1"}
    )
    with pytest.raises(personal_tasks.LiveSmokeError, match="official openai API endpoint"):
        personal_tasks._preflight_live_smoke_runtime(custom_endpoint, config)


def test_live_smoke_restart_with_bound_ledger_fails_before_database_or_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _live_config(uuid.uuid4(), "https://news.example.test/fixed")
    ledger = LiveSmokeLedger.open(
        LiveSmokeLedger.canonical_path(tmp_path / "private", config.verification_id), config
    )
    run_id = str(uuid.uuid4())
    ledger.bind_run(run_id)
    reopened = LiveSmokeLedger.open(ledger.path, config, require_existing=True)
    touched: list[str] = []
    monkeypatch.setattr(
        personal_tasks,
        "get_settings",
        lambda: Settings(
            app_env="test",
            database_url=(
                "postgresql+psycopg2://news:news@127.0.0.1:55432/nip_phase2_live_never_opened"
            ),
            openai_api_key="explicit-test-only-key",
        ),
    )
    monkeypatch.setattr(
        personal_tasks,
        "SessionLocal",
        lambda: touched.append("database") or (_ for _ in ()).throw(AssertionError()),
    )

    different_config = _live_config(uuid.uuid4(), "https://news.example.test/other")
    with pytest.raises(personal_tasks.LiveSmokeError, match="does not match the supplied ledger"):
        personal_tasks.execute_personal_live_smoke(config=different_config, ledger=reopened)
    with pytest.raises(personal_tasks.LiveSmokeError, match="fresh unbound ledger"):
        personal_tasks.execute_personal_live_smoke(config=config, ledger=reopened)

    assert touched == []
    assert reopened.snapshot()["personal_run_id"] == run_id
    assert reopened.snapshot()["dispatches"] == []


def test_live_smoke_fresh_database_guard_includes_claim_and_evidence_tables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    article_url = "https://news.example.test/fixed"
    source_id = uuid.uuid4()
    with _migrated_live_engine() as engine:
        with Session(engine) as session:
            claim = Claim(claim_text="Preexisting claim", claim_type="legacy")
            evidence = EvidenceItem(
                source_type="legacy",
                source_id="preexisting",
                title="Preexisting evidence",
            )
            session.add_all(
                [
                    Source(
                        id=source_id,
                        name="Fixed live-smoke source",
                        feed_url="https://news.example.test/feed.xml",
                    ),
                    claim,
                    evidence,
                ]
            )
            session.flush()
            session.add(
                ClaimEvidence(
                    claim_id=claim.id,
                    evidence_item_id=evidence.id,
                    support_type="supports",
                )
            )
            session.commit()

        config = _live_config(source_id, article_url)
        ledger = LiveSmokeLedger.open(
            LiveSmokeLedger.canonical_path(tmp_path / "private", config.verification_id), config
        )
        clients: list[str] = []
        monkeypatch.setattr(
            personal_tasks, "SessionLocal", lambda: Session(engine, autoflush=False)
        )
        monkeypatch.setattr(
            personal_tasks,
            "get_settings",
            lambda: Settings(
                app_env="test",
                database_url=engine.url.render_as_string(hide_password=False),
                openai_api_key="explicit-test-only-key",
            ),
        )
        monkeypatch.setattr(
            personal_tasks.httpx,
            "Client",
            lambda **_kwargs: clients.append("http") or (_ for _ in ()).throw(AssertionError()),
        )
        monkeypatch.setattr(
            personal_tasks,
            "HttpRSSProvider",
            lambda: clients.append("rss") or (_ for _ in ()).throw(AssertionError()),
        )

        with pytest.raises(personal_tasks.LiveSmokeError, match="claims contains rows"):
            personal_tasks.execute_personal_live_smoke(config=config, ledger=ledger)

        assert clients == []
        assert ledger.snapshot()["personal_run_id"] is None
        assert ledger.snapshot()["dispatches"] == []


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
