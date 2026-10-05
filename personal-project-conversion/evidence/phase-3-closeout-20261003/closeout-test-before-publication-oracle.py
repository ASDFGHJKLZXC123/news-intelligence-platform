"""Literal Phase 3 acceptance gaps through actual API, coordinator and snapshots.

Broker delivery and RSS/model responses are synthetic. The default triad uses the
existing offline fixture providers with actual grouping, publication and reading.
All cases require the identity-checked disposable PostgreSQL fixture; no paid
provider is constructed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import apps.api.personal as personal_api
from db.models import (
    Article,
    Job,
    PersonalArticleRevision,
    PersonalBriefSnapshot,
    PersonalCapture,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    Report,
    Source,
)
from db.models.personal_spending import PersonalPaidRequest
from packages.providers.base import RSSItem
from packages.providers.fakes import FakeRSSProvider
from services.personal.coordinator import PersonalCoordinatorError, run_personal_daily
from services.personal.offline_fixture import OfflinePersonalFixture
from services.personal.runs import PersonalOwnershipLost, create_daily_run, lock_owned_run
from services.personal.settings import PersonalSettingsUpdate, update_settings
from services.personal.workspace import ensure_workspace
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_brief_api import _client
from tests.integration.test_personal_coordinator import StepClock
from tests.integration.test_personal_daily_bounds import START, Desk, items
from workers.celery_app import QUEUE_PIPELINE, celery_app

pytestmark = pytest.mark.integration


class _NoPaidProvider:
    model_name = "disabled-closeout-test"
    model_version = "raw"

    def embed(self, _texts):
        pytest.fail("raw closeout acceptance must not dispatch an embedding request")


def _no_report(*_args):
    pytest.fail("raw closeout acceptance must not generate or probe a report")


def _capture_enqueues(monkeypatch, engine):
    deliveries = []

    def send_task(task_name, *, args, queue, task_id):
        assert task_name == "workers.personal_tasks.run_personal_daily"
        assert queue == QUEUE_PIPELINE
        assert len(args) == 2
        uuid.UUID(task_id)
        run_id, token = (uuid.UUID(value) for value in args)
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "queued"
            assert run.ownership_token == token and run.celery_task_id == task_id
        deliveries.append((run_id, token, task_id))

    monkeypatch.setattr(celery_app, "send_task", send_task)
    return deliveries


def _capture_state(session):
    return sorted(
        (
            str(row.id),
            str(row.article_id) if row.article_id else None,
            str(row.admitted_run_id) if row.admitted_run_id else None,
            row.admitted_at,
            row.admission_charged,
        )
        for row in session.scalars(select(PersonalCapture))
    )


def test_two_concurrent_http_retries_rotate_one_attempt_and_preserve_frozen_admissions(
    monkeypatch,
):
    with migrated_disposable_engine() as engine:
        desk = Desk(engine)
        profile_id = desk.configure(daily=2, admission=2, enrichment=2)
        first = desk.start()
        desk.capture(first, {desk.source_ids[0]: items("closeout-concurrent-retry", 3)})
        admitted = desk.admit(first)
        assert len(admitted) == 2
        assert desk.freeze(first, admitted) == ()  # Raw processing freezes zero enrichment.
        desk.finish(first, failed=True)
        with desk.session() as session:
            run = session.get(PersonalRun, first.run_id)
            original_date = run.local_date
            frozen_at = run.scopes_frozen_at
            initial_records = _capture_state(session)
        server_now = START + dt.timedelta(minutes=1)
        monkeypatch.setattr(personal_api, "_utc_now", lambda: server_now)
        deliveries = _capture_enqueues(monkeypatch, engine)
        barrier = Barrier(2)
        with _client(engine) as client:

            def retry():
                barrier.wait(timeout=10)
                return client.post(f"/api/v1/personal/runs/{first.run_id}/retry")

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(retry) for _ in range(2)]
                responses = [future.result(timeout=30) for future in futures]
        assert sorted(response.status_code for response in responses) == [202, 409]
        accepted = next(
            response.json()["run"] for response in responses if response.status_code == 202
        )
        assert accepted["id"] == str(first.run_id) and accepted["attempt"] == 2
        assert len(deliveries) == 1
        run_id, current_token, _ = deliveries[0]
        assert run_id == first.run_id and current_token != first.token
        with desk.session() as session:
            run = session.get(PersonalRun, run_id)
            assert run.attempt == 2 and run.state == "queued"
            assert run.profile_revision_id == profile_id and run.local_date == original_date
            assert run.scopes_frozen_at == frozen_at
            assert run.admitted_article_ids == list(admitted) and run.enrichment_article_ids == []
            assert _capture_state(session) == initial_records
            assert session.scalar(select(func.count()).select_from(Job)) == 1
            with pytest.raises(PersonalOwnershipLost):
                lock_owned_run(session, run_id, first.token, allow_queued=True)
            session.rollback()

        def no_recapture(_source):
            pytest.fail("a frozen retry must not recapture or append admission scope")

        result = run_personal_daily(
            run_id,
            current_token,
            session_factory=desk.session,
            rss_provider_factory=no_recapture,
            embedding_provider=_NoPaidProvider(),
            orchestrator_factory=_no_report,
            now=server_now,
            clock=lambda: server_now,
        )
        assert result.report is None and result.articles_admitted == 2
        with desk.session() as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "succeeded" and run.attempt == 2
            assert run.local_date == original_date and run.profile_revision_id == profile_id
            assert run.scopes_frozen_at == frozen_at
            assert run.admitted_article_ids == list(admitted) and run.enrichment_article_ids == []
            assert _capture_state(session) == initial_records
            assert session.scalar(select(func.count()).select_from(Article)) == 2
            captures = list(session.scalars(select(PersonalCapture)))
            assert (
                sum(row.admitted_at is not None and row.admission_charged for row in captures) == 2
            )
            assert sum(row.admitted_at is None for row in captures) == 1
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0


def test_http_old_caller_dates_cannot_backdate_run_or_reopen_same_day_admission(monkeypatch):
    with migrated_disposable_engine() as engine:
        desk = Desk(engine)
        profile_id = desk.configure(daily=2, admission=2, enrichment=2)
        monkeypatch.setattr(personal_api, "_utc_now", lambda: START)
        deliveries = _capture_enqueues(monkeypatch, engine)
        with _client(engine) as client:
            started = client.post("/api/v1/personal/runs")
            assert started.status_code == 202, started.text
            assert len(deliveries) == 1
            run_id, token, _ = deliveries[0]
            result = run_personal_daily(
                run_id,
                token,
                session_factory=desk.session,
                rss_provider_factory=lambda _source: FakeRSSProvider(items("closeout-old-date", 3)),
                embedding_provider=_NoPaidProvider(),
                orchestrator_factory=_no_report,
                now=START,
                clock=lambda: START + dt.timedelta(seconds=10),
            )
            assert result.articles_captured == 3 and result.articles_admitted == 2
            assert result.report is None
            with desk.session() as session:
                initial_records = _capture_state(session)
                run = session.get(PersonalRun, run_id)
                admitted = list(run.admitted_article_ids)
                frozen_at = run.scopes_frozen_at
                actual_date = run.local_date.isoformat()
                assert (
                    actual_date
                    == START.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
                )
            supplied_old_date = (START - dt.timedelta(days=100)).date().isoformat()
            repeated = client.post(
                "/api/v1/personal/runs",
                json={
                    "report_date": supplied_old_date,
                    "local_date": supplied_old_date,
                    "process_date": supplied_old_date,
                    "brief_date": supplied_old_date,
                },
            )
            assert repeated.status_code == 200, repeated.text
            body = repeated.json()
            assert body["already_processed"] is True
            assert body["run"]["id"] == str(run_id)
            assert body["run"]["local_date"] == actual_date != supplied_old_date
            assert body["run"]["attempt"] == 1 and body["run"]["state"] == "succeeded"
            assert len(deliveries) == 1  # Caller dates cannot create a second collection/delivery.
            assert client.get("/api/v1/personal/articles").json()["total"] == 2
            assert client.get("/api/v1/personal/backlog").json()["total"] == 1
        with desk.session() as session:
            run = session.get(PersonalRun, run_id)
            assert run.profile_revision_id == profile_id and run.ownership_token == token
            assert run.scopes_frozen_at == frozen_at and run.admitted_article_ids == admitted
            assert run.enrichment_article_ids == []
            assert _capture_state(session) == initial_records
            assert session.scalar(select(func.count()).select_from(PersonalRun)) == 1
            assert session.scalar(select(func.count()).select_from(Job)) == 1
            assert session.scalar(select(func.count()).select_from(Article)) == 2
            captures = list(session.scalars(select(PersonalCapture)))
            assert (
                sum(row.admitted_at is not None and row.admission_charged for row in captures) == 2
            )
            assert sum(row.admitted_at is None for row in captures) == 1
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0


_TRIAD_FEED_URL = "https://offline.example/p312/feed.xml"
_TRIAD_FIXTURE_ID = "phase3-closeout-p312-same-input-v1"
_TRIAD_AUTHORED_RSS = [
    {
        "guid": "closeout-p312-agency",
        "title": "Agency approves coastal verification project",
        "summary": "The agency confirmed the coastal verification project was approved.",
        "url": "https://offline.example/p312/agency",
        "published_at": "2026-09-19T16:00:00+00:00",
        "source": "https://offline.example/p312/feed.xml",
        "provider_name": "offline-personal-fixture",
        "source_refs": ["fixture:phase3-closeout-p312:agency"],
        "evidence_refs": ["synthetic:p312:agency"],
    },
    {
        "guid": "closeout-p312-league",
        "title": "League reports local match result",
        "summary": "The league reported the local match was completed.",
        "url": "https://offline.example/p312/league",
        "published_at": None,
        "source": "https://offline.example/p312/feed.xml",
        "provider_name": "offline-personal-fixture",
        "source_refs": ["fixture:phase3-closeout-p312:league"],
        "evidence_refs": ["synthetic:p312:league"],
    },
]
_TRIAD_INPUT_SHA256 = "f1fddce95550b567f275aa2986bd3a06440fe015089b82093db8a2edd3a78f07"


def _triad_fixture():
    rows = tuple(
        RSSItem(
            guid=row["guid"],
            title=row["title"],
            summary=row["summary"],
            url=row["url"],
            published_at=dt.datetime.fromisoformat(row["published_at"])
            if row["published_at"]
            else None,
            source=row["source"],
            provider_name=row["provider_name"],
            source_refs=tuple(row["source_refs"]),
            evidence_refs=tuple(row["evidence_refs"]),
        )
        for row in _TRIAD_AUTHORED_RSS
    )
    reconstructed = [
        {
            "guid": row.guid,
            "title": row.title,
            "summary": row.summary,
            "url": row.url,
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "source": row.source,
            "provider_name": row.provider_name,
            "source_refs": list(row.source_refs),
            "evidence_refs": list(row.evidence_refs),
        }
        for row in rows
    ]
    assert reconstructed == _TRIAD_AUTHORED_RSS
    encoded = json.dumps(reconstructed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256(encoded.encode()).hexdigest() == _TRIAD_INPUT_SHA256
    return OfflinePersonalFixture(
        _TRIAD_FIXTURE_ID,
        "Synthetic P3-12 same authored input acceptance fixture",
        {_TRIAD_FEED_URL: rows},
        "Synthetic offline brief: the agency confirmed the coastal verification project was approved.",
        "The agency confirmed the coastal verification project was approved.",
    )


@pytest.mark.parametrize("mode", ["default", "raw", "stage_failure"])
def test_same_authored_rss_fixture_preserves_v2_scope_across_processing_modes(mode):
    # Identical authored RSS fields/hash in every mode; DB identities differ.
    fixture = _triad_fixture()
    with migrated_disposable_engine() as engine:

        def factory():
            return Session(engine, autoflush=False)

        with factory() as session:
            source = Source(name="Synthetic P3-12 shared fixture feed", feed_url=_TRIAD_FEED_URL)
            session.add(source)
            session.flush()
            workspace, _ = ensure_workspace(session)
            profile = update_settings(
                session,
                workspace,
                PersonalSettingsUpdate(
                    selected_source_ids=[source.id],
                    include_phrases=["agency"],
                    execution_profile="raw" if mode == "raw" else "assisted",
                    daily_article_limit=2,
                    run_article_limit=2,
                    enrichment_article_limit=2,
                    ai_enabled=False,
                    model_route=fixture.route,
                ),
            )
            decision = create_daily_run(session, workspace, now=START)
            assert decision.created
            run_id, token = decision.run.id, decision.run.ownership_token
            profile_id, workspace_id, source_id = profile.id, workspace.id, source.id
            session.commit()
        embedding_calls, report_calls = [], []
        delegate_embedding = fixture.embedding_provider()

        class ScopedEmbedding:
            model_name = delegate_embedding.model_name
            model_version = delegate_embedding.model_version

            def embed(self, texts):
                embedding_calls.append(tuple(texts))
                with factory() as probe:
                    frozen = probe.get(PersonalRun, run_id)
                    assert frozen.scopes_frozen_at is not None
                    assert frozen.profile_revision_id == profile_id
                    assert (
                        len(frozen.admitted_article_ids) == len(frozen.enrichment_article_ids) == 2
                    )
                    assert (
                        probe.scalar(select(func.count()).select_from(PersonalArticleRevision)) == 2
                    )
                if mode == "stage_failure":
                    raise RuntimeError("synthetic required embedding failure after scope freeze")
                # After freeze, edit only the active profile. The run must use its old agency
                # selection and old enrichment ceiling, not the new league preference/zero cap.
                with factory() as edit:
                    live_workspace = edit.get(PersonalWorkspace, workspace_id)
                    revised = update_settings(
                        edit,
                        live_workspace,
                        PersonalSettingsUpdate(
                            selected_source_ids=[source_id],
                            include_phrases=["league"],
                            execution_profile="assisted",
                            daily_article_limit=2,
                            run_article_limit=2,
                            enrichment_article_limit=0,
                            ai_enabled=False,
                            model_route=fixture.route,
                        ),
                    )
                    assert revised.id != profile_id
                    edit.commit()
                return delegate_embedding.embed(texts)

        def offline_report(session, route):
            report_calls.append(route)
            assert route == fixture.route
            return fixture.orchestrator(session, route)

        arguments = dict(
            session_factory=factory,
            rss_provider_factory=fixture.rss_provider,
            embedding_provider=_NoPaidProvider() if mode == "raw" else ScopedEmbedding(),
            orchestrator_factory=_no_report if mode == "raw" else offline_report,
            now=START,
            clock=StepClock(START + dt.timedelta(seconds=10)),
        )
        if mode == "stage_failure":
            with pytest.raises(PersonalCoordinatorError, match="workflow failed"):
                run_personal_daily(run_id, token, **arguments)
        else:
            result = run_personal_daily(run_id, token, **arguments)
            assert result.articles_captured == result.articles_admitted == 2
            assert bool(result.report and result.report.published) is (mode == "default")
        with factory() as session:
            run = session.get(PersonalRun, run_id)
            profile = session.get(PersonalProfileRevision, profile_id)
            assert profile.schema_revision == "personal-profile.v2"
            assert (
                profile.include_phrases == ["agency"]
                and profile.settings["model_route"] == fixture.route
            )
            assert run.profile_revision_id == profile_id and run.ownership_token == token
            assert run.scopes_frozen_at is not None and len(run.admitted_article_ids) == 2
            assert run.capture_ended_at <= run.scopes_frozen_at
            assert run.coverage["articles_captured"] == run.coverage["articles_admitted"] == 2
            assert run.stage_results["scope"]["articles_admitted"] == 2
            captures = list(session.scalars(select(PersonalCapture)))
            assert len(captures) == 2 and all(row.admission_charged for row in captures)
            assert {row.article_id for row in captures} == set(run.admitted_article_ids)
            authored_by_url = {row["url"]: row for row in _TRIAD_AUTHORED_RSS}
            for capture in captures:
                authored = authored_by_url[capture.original_url]
                assert (
                    capture.title == authored["title"]
                    and capture.rss_summary == authored["summary"]
                )
                assert capture.published_at == (
                    dt.datetime.fromisoformat(authored["published_at"])
                    if authored["published_at"]
                    else None
                )
            for stage in (
                "entity_linking",
                "event_embeddings",
                "historical_analogies",
                "forecasting",
            ):
                assert run.stage_results[stage] == {
                    "status": "disabled",
                    "reason": "disabled_by_profile",
                }
            assert session.scalar(select(func.count()).select_from(PersonalPaidRequest)) == 0
            if mode == "raw":
                assert run.state == "succeeded" and run.result["reason"] == "disabled_by_profile"
                assert run.enrichment_article_ids == [] and embedding_calls == report_calls == []
                for stage in ("embedding", "grouping", "claim_preparation", "snapshot", "report"):
                    assert run.stage_results[stage] == {
                        "status": "disabled",
                        "reason": "disabled_by_profile",
                    }
                assert all(row.enrichment_state == "disabled_by_profile" for row in captures)
                assert run.snapshot_id is None and run.report_id is None
            elif mode == "stage_failure":
                assert run.state == "failed" and run.error["code"] == "personal_workflow_failed"
                assert run.stage_results["workflow"] == {
                    "status": "failed",
                    "code": "personal_workflow_failed",
                }
                assert run.enrichment_article_ids == run.admitted_article_ids
                assert len(embedding_calls) == 1 and report_calls == []
                assert all(row.enrichment_state == "provider_failed" for row in captures)
                assert run.snapshot_id is None and run.report_id is None
                assert (
                    session.scalar(select(func.count()).select_from(PersonalArticleRevision)) == 2
                )
            else:
                assert (
                    run.state == "succeeded"
                    and run.enrichment_article_ids == run.admitted_article_ids
                )
                assert len(embedding_calls) == 1 and len(report_calls) == 1
                assert (
                    session.get(PersonalWorkspace, workspace_id).active_profile_revision_id
                    != profile_id
                )
                assert all(
                    run.stage_results[stage]["status"] == "succeeded"
                    for stage in (
                        "embedding",
                        "grouping",
                        "claim_preparation",
                        "snapshot",
                        "report",
                    )
                )
                report = session.get(Report, run.report_id)
                snapshot = session.get(PersonalBriefSnapshot, run.snapshot_id)
                assert report.status == "published" and snapshot.profile_revision_id == profile_id
                payload = snapshot.input_payload
                encoded = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                assert snapshot.input_hash == hashlib.sha256(encoded.encode()).hexdigest()
                assert (
                    payload["profile_revision_id"] == str(profile_id)
                    and payload["model_route"] == fixture.route
                )
                assert payload["admitted_article_ids"] == [str(i) for i in run.admitted_article_ids]
                assert payload["enrichment_article_ids"] == [
                    str(i) for i in run.enrichment_article_ids
                ]
                agency_id = next(
                    row.article_id for row in captures if row.original_url.endswith("/agency")
                )
                source_inputs = [
                    row
                    for candidate in payload["candidates"]
                    for row in candidate["source_inputs"]["articles"]
                ]
                assert len(source_inputs) == 1 and source_inputs[0]["article_id"] == str(agency_id)
                assert source_inputs[0]["title"] == _TRIAD_AUTHORED_RSS[0]["title"]
                assert source_inputs[0]["rss_summary"] == _TRIAD_AUTHORED_RSS[0]["summary"]
                assert source_inputs[0]["url"] == _TRIAD_AUTHORED_RSS[0]["url"]
                assert {row["article_id"] for row in payload["claims"] if row["citable"]} == {
                    str(agency_id)
                }
                report_id, snapshot_id, input_hash = report.id, snapshot.id, snapshot.input_hash
        with _client(engine) as client:
            status = client.get(f"/api/v1/personal/runs/{run_id}").json()["run"]
            assert status["profile_revision_id"] == str(profile_id)
            assert status["counts"]["admitted"] == 2
            assert status["counts"]["selected_for_enrichment"] == (0 if mode == "raw" else 2)
            if mode != "default":
                raw = client.get("/api/v1/personal/articles").json()
                assert raw["total"] == 2 and {row["url"] for row in raw["items"]} == set(
                    authored_by_url
                )
                if mode == "stage_failure":
                    assert all(
                        row["enrichment_state"] == "provider_failed" and row["error"]
                        for row in raw["items"]
                    )
                assert client.get("/api/v1/personal/briefs").json()["total"] == 0
            else:
                detail_path = f"/api/v1/personal/briefs/{report_id}"
                before_detail = client.get(detail_path)
                assert before_detail.status_code == 200
                detail = before_detail.json()
                assert (
                    detail["snapshot"]["id"] == str(snapshot_id)
                    and detail["snapshot"]["input_hash"] == input_hash
                )
                assert detail["snapshot"]["profile_revision_id"] == str(profile_id)
                assert len(detail["citations"]) >= 1
                claim_id = detail["citations"][0]["claim_id"]
                evidence_path = f"{detail_path}/claims/{claim_id}/evidence"
                before_evidence = client.get(evidence_path)
                evidence = before_evidence.json()["evidence"]
                assert evidence and all(row["article_id"] == str(agency_id) for row in evidence)
                assert all(
                    row["title"] == _TRIAD_AUTHORED_RSS[0]["title"]
                    and row["url"] == _TRIAD_AUTHORED_RSS[0]["url"]
                    for row in evidence
                )
                with factory() as edit:
                    article = edit.get(Article, agency_id)
                    article.title, article.summary = (
                        "MUTATED current source",
                        "MUTATED current RSS summary",
                    )
                    article.url = "https://offline.example/current-mutated"
                    edit.commit()
                assert client.get(detail_path).content == before_detail.content
                assert client.get(evidence_path).content == before_evidence.content
