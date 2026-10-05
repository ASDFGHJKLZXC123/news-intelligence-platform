"""Fresh-source integration proof for personal composition, grounding and publication."""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    Article,
    Claim,
    ClaimEvidence,
    Event,
    EventArticle,
    EvidenceItem,
    Job,
    LLMRun,
    PersonalBriefSnapshot,
    PersonalReportLink,
    PersonalRun,
    PersonalWorkspace,
    PersonalWriterMode,
    Report,
    ReportSection,
    Source,
)
from packages.config.settings import Settings
from services.llm.cache import InMemoryLLMPromptCache
from services.llm.fake_providers import CallableLLMProvider
from services.llm.orchestrator import LLMOrchestrator
from services.llm.repository import SQLAlchemyLLMRuntimeRepository
from services.personal.briefs import (
    PersonalBriefGenerationError,
    build_personal_brief_inputs,
    generate_personal_brief,
)
from services.personal.claims import prepare_article_claims
from services.personal.processing import lock_processing_control
from services.personal.runs import acquire_run, create_daily_run, lock_owned_run, retry_run
from services.personal.snapshots import (
    freeze_article_scopes,
    freeze_brief_snapshot,
    record_event_observations,
)
from services.personal.workspace import configure_profile, ensure_workspace
from services.reports.contracts import CompositionPolicy
from services.reports.grounding_prompts import (
    GROUNDING_PROMPT_TEMPLATE_VERSION,
    GROUNDING_SCHEMA,
    GROUNDING_SCHEMA_VERSION,
)
from services.reports.material import FINAL_DISCLAIMER
from services.reports.prompts import (
    COMPOSITION_PROMPT_TEMPLATE_VERSION,
    COMPOSITION_SCHEMA,
    COMPOSITION_SCHEMA_VERSION,
)
from tests.integration._personal_processing_clock import install_authored_processing_clock
from tests.integration._stage7_db import migrated_disposable_engine


@pytest.fixture(autouse=True)
def authored_processing_clock(monkeypatch):
    from packages.config.settings import get_settings

    monkeypatch.setenv("PERSONAL_PROCESSING_MODE", "personal")
    get_settings.cache_clear()
    install_authored_processing_clock(monkeypatch, sys.modules[__name__])
    yield
    get_settings.cache_clear()


pytestmark = pytest.mark.integration
UTC = datetime.UTC

EXECUTIVE_TEXT = (
    "The configured feed reports that the agency approved the coastal resilience project. "
    "This brief records the approval as a source-reported assertion from the captured article. "
    "The retained excerpt attributes the statement to the agency and does not add a reason, "
    "budget, schedule, or predicted outcome. Coverage in this snapshot comes from one configured "
    "source, so the report does not treat repetition as corroboration. The item appears because "
    "it matched the personal source and phrase settings. No independent verification, risk "
    "estimate, historical comparison, or forecast is supplied. The available evidence supports "
    "only the reported approval and its attribution within this captured source record."
)
TOP_EVENT_TEXT = (
    "The leading item in this personal snapshot is the reported approval of a coastal resilience "
    "project. The configured feed says the agency confirmed that approval, and the brief preserves "
    "that attribution. The source record does not state a budget, schedule, implementation "
    "milestone, motive, or expected effect, so this section adds none of those details. It also "
    "does not turn the source statement into an independently verified fact. Coverage is limited "
    "to the retained article from one selected feed. No second source in this run corroborates the "
    "assertion. The supported reading is therefore narrow: the captured publisher reports an "
    "agency confirmation that the project was approved, with no further conclusion about delivery "
    "or impact. The record offers no basis for additional interpretation."
)


def _offline_provider_response(request):  # noqa: ANN001, ANN202
    allowed = list(request.context.get("allowed_claim_ids", []))
    if request.requested_schema == COMPOSITION_SCHEMA:
        assert allowed
        text = (
            EXECUTIVE_TEXT
            if request.context["section_kind"] == "executive_summary"
            else TOP_EVENT_TEXT
        )
        return {
            "schema_name": COMPOSITION_SCHEMA,
            "schema_version": COMPOSITION_SCHEMA_VERSION,
            "prompt_template_version": COMPOSITION_PROMPT_TEMPLATE_VERSION,
            "blocks": [{"text": text, "claim_ids": [allowed[0]]}],
        }
    if request.requested_schema == GROUNDING_SCHEMA:
        return {
            "schema_name": GROUNDING_SCHEMA,
            "schema_version": GROUNDING_SCHEMA_VERSION,
            "prompt_template_version": GROUNDING_PROMPT_TEMPLATE_VERSION,
            "verdicts": [{"claim_id": claim_id, "verdict": "supported"} for claim_id in allowed],
        }
    raise AssertionError(f"unexpected offline schema: {request.requested_schema}")


def _orchestrator(session: Session, model_route: dict) -> LLMOrchestrator:  # noqa: ANN401
    assert model_route == {
        "mode": "offline_fixture",
        "provider": "callable",
        "model": "offline-fixture-v1",
    }
    provider = CallableLLMProvider(
        _offline_provider_response,
        provider_name="offline-fixture",
        model_name="offline-fixture-v1",
    )
    return LLMOrchestrator(
        settings=Settings(app_env="test", llm_budget_enforced=False),
        repository=SQLAlchemyLLMRuntimeRepository(session, commit_on_write=False),
        providers_by_tier={"T1": (provider,), "T2": (provider,), "T3": (provider,)},
        cache=InMemoryLLMPromptCache(),
    )


def _provider_orchestrator(session: Session, model_route: dict, response) -> LLMOrchestrator:  # noqa: ANN001
    assert model_route["mode"] == "offline_fixture"
    provider = CallableLLMProvider(
        response,
        provider_name="offline-fixture",
        model_name="offline-fixture-v1",
    )
    return LLMOrchestrator(
        settings=Settings(app_env="test", llm_budget_enforced=False),
        repository=SQLAlchemyLLMRuntimeRepository(session, commit_on_write=False),
        providers_by_tier={"T1": (provider,), "T2": (provider,), "T3": (provider,)},
        cache=InMemoryLLMPromptCache(),
    )


def _prepared_snapshot(engine, *, now: datetime.datetime, coverage: dict):  # noqa: ANN001, ANN202
    with Session(engine) as session:
        source = Source(
            name="Offline captured feed",
            feed_url=f"https://offline.example/{uuid.uuid4()}.xml",
        )
        session.add(source)
        session.flush()
        workspace, _ = ensure_workspace(session)
        workspace_id = workspace.id
        configure_profile(
            session,
            workspace,
            selected_source_ids=[source.id],
            include_phrases=["agency"],
            settings={
                "model_route": {
                    "mode": "offline_fixture",
                    "provider": "callable",
                    "model": "offline-fixture-v1",
                }
            },
        )
        article = Article(
            source_id=source.id,
            url=f"https://offline.example/story/{uuid.uuid4()}",
            url_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            title="Agency approves coastal resilience project",
            summary="The agency confirmed the coastal resilience project was approved.",
            published_at=now - datetime.timedelta(minutes=20),
        )
        session.add(article)
        session.flush()
        event = Event(title="Coastal resilience project approved", hotness_score=74)
        session.add(event)
        session.flush()
        session.add(EventArticle(event_id=event.id, article_id=article.id))
        session.commit()
        decision = create_daily_run(session, workspace, now=now)
        token = decision.run.ownership_token
        assert token is not None
        run_id = decision.run.id
        workspace_id = workspace.id
        session.commit()
        run = acquire_run(session, run_id, token, now=now)
        run.capture_started_at = now - datetime.timedelta(minutes=30)
        run.capture_ended_at = now
        run.coverage = coverage
        freeze_article_scopes(
            session,
            run_id,
            token,
            admitted_article_ids=[article.id],
            enrichment_article_ids=[article.id],
            now=now,
        )
        record_event_observations(session, run_id, token)
        prepare_article_claims(session, run_id, token)
        snapshot = freeze_brief_snapshot(
            session,
            run_id,
            token,
            model_route={
                "mode": "offline_fixture",
                "provider": "callable",
                "model": "offline-fixture-v1",
            },
            prepared_at=now,
        )
        session.commit()
        return run_id, token, workspace_id, snapshot.id


def _prepared_quiet_snapshot(engine, *, now: datetime.datetime, coverage: dict):  # noqa: ANN001, ANN202
    with Session(engine) as session:
        source = Source(
            name="Offline empty feed",
            feed_url=f"https://offline.example/{uuid.uuid4()}.xml",
        )
        session.add(source)
        session.flush()
        workspace, _ = ensure_workspace(session)
        configure_profile(
            session,
            workspace,
            selected_source_ids=[source.id],
            include_phrases=["agency"],
            settings={"model_route": {"mode": "offline_fixture"}},
        )
        decision = create_daily_run(session, workspace, now=now)
        token = decision.run.ownership_token
        assert token is not None
        run_id = decision.run.id
        session.commit()
        run = acquire_run(session, run_id, token, now=now)
        run.capture_started_at = now - datetime.timedelta(minutes=20)
        run.capture_ended_at = now
        run.coverage = coverage
        freeze_article_scopes(
            session,
            run_id,
            token,
            admitted_article_ids=[],
            enrichment_article_ids=[],
            now=now,
        )
        record_event_observations(session, run_id, token)
        snapshot = freeze_brief_snapshot(
            session,
            run_id,
            token,
            model_route={"mode": "offline_fixture"},
            prepared_at=now,
        )
        session.commit()
        return run_id, token, snapshot.id


def test_fresh_captured_source_is_composed_grounded_and_published() -> None:
    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 6, 18, tzinfo=UTC)
        with Session(engine) as session:
            source = Source(
                name="Offline captured feed",
                feed_url=f"https://offline.example/{uuid.uuid4()}.xml",
            )
            session.add(source)
            session.flush()
            workspace, _ = ensure_workspace(session)
            configure_profile(
                session,
                workspace,
                selected_source_ids=[source.id],
                include_phrases=["agency"],
                settings={
                    "model_route": {
                        "mode": "offline_fixture",
                        "provider": "callable",
                        "model": "offline-fixture-v1",
                    }
                },
            )
            article = Article(
                source_id=source.id,
                url=f"https://offline.example/story/{uuid.uuid4()}",
                url_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                title="Agency approves coastal resilience project",
                summary="The agency confirmed the coastal resilience project was approved.",
                published_at=now - datetime.timedelta(minutes=20),
            )
            session.add(article)
            session.flush()
            event = Event(
                title="Coastal resilience project approved",
                summary="Public agency decision reported by the configured feed.",
                hotness_score=74,
            )
            session.add(event)
            session.flush()
            session.add(EventArticle(event_id=event.id, article_id=article.id))
            session.commit()

            assert session.scalar(select(func.count()).select_from(Claim)) == 0
            assert session.scalar(select(func.count()).select_from(ClaimEvidence)) == 0
            assert session.scalar(select(func.count()).select_from(Report)) == 0

            decision = create_daily_run(session, workspace, now=now)
            token = decision.run.ownership_token
            assert token is not None
            run_id = decision.run.id
            session.commit()
            run = acquire_run(session, run_id, token, now=now)
            run.capture_started_at = now - datetime.timedelta(minutes=30)
            run.capture_ended_at = now
            run.coverage = {
                "feeds_attempted": 1,
                "feeds_succeeded": 1,
                "feeds_failed": 0,
                "feed_failures": [],
                "articles_captured": 1,
            }
            freeze_article_scopes(
                session,
                run_id,
                token,
                admitted_article_ids=[article.id],
                enrichment_article_ids=[article.id],
                now=now,
            )
            record_event_observations(session, run_id, token)
            preparations = prepare_article_claims(session, run_id, token)
            assert any(item.status == "supported" for item in preparations)
            snapshot = freeze_brief_snapshot(
                session,
                run_id,
                token,
                model_route={
                    "mode": "offline_fixture",
                    "provider": "callable",
                    "model": "offline-fixture-v1",
                },
                prepared_at=now,
            )
            snapshot_id = snapshot.id
            event_id = event.id
            workspace_id = workspace.id
            session.commit()

        result = generate_personal_brief(
            run_id,
            token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=_orchestrator,
        )

        assert result.published is True
        assert result.gate_outcome.value == "pass"
        assert result.selected_event_ids == (event_id,)
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            report = session.get(Report, result.report.id)
            link = session.get(PersonalReportLink, result.report.id)
            assert run.state == "succeeded"
            assert run.report_id == report.id
            assert report.status == "published"
            assert report.report_type == "personal_daily_brief"
            assert link.workspace_id == workspace_id
            assert link.run_id == run_id
            assert link.snapshot_id == snapshot_id
            sections = list(
                session.execute(
                    select(ReportSection)
                    .where(ReportSection.report_id == report.id)
                    .order_by(ReportSection.section_order)
                ).scalars()
            )
            assert len(sections) == result.section_count
            assert sections[0].title == "Executive Summary"
            assert sections[0].body == EXECUTIVE_TEXT
            assert sections[1].body == TOP_EVENT_TEXT
            assert sections[-1].body == FINAL_DISCLAIMER
            assert any("1 of 1 configured feeds" in section.body for section in sections)
            cited = {claim_id for section in sections for claim_id in section.evidence_refs or []}
            assert cited
            assert cited == set(
                session.execute(
                    select(ClaimEvidence.claim_id).where(ClaimEvidence.claim_id.in_(cited))
                ).scalars()
            )
            assert session.scalar(select(func.count()).select_from(LLMRun)) >= 4
            audit_jobs = list(
                session.execute(
                    select(Job).where(Job.job_key.like(f"personal:{run_id}:%"))
                ).scalars()
            )
            assert {job.job_type for job in audit_jobs} == {
                "personal_daily_brief_composition",
                "personal_daily_brief_grounding",
            }
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(EvidenceItem)
                    .where(EvidenceItem.source_type == "article")
                )
                == 1
            )


def test_all_feed_failure_is_failed_and_cannot_publish_as_quiet() -> None:
    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 7, 18, tzinfo=UTC)
        run_id, token, _ = _prepared_quiet_snapshot(
            engine,
            now=now,
            coverage={
                "feeds_attempted": 2,
                "feeds_succeeded": 0,
                "feeds_failed": 2,
                "feed_failures": [
                    {"source_id": "one", "code": "fetch_failed"},
                    {"source_id": "two", "code": "fetch_failed"},
                ],
                "articles_captured": 0,
            },
        )
        with pytest.raises(PersonalBriefGenerationError, match="not a quiet result"):
            generate_personal_brief(
                run_id,
                token,
                session_factory=lambda: Session(engine),
                orchestrator_factory=lambda *_: pytest.fail("all-feed failure called a model"),
            )
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            report = session.get(Report, run.report_id)
            assert run.state == "failed"
            assert run.error == {
                "code": "brief_generation_failed",
                "message": "Personal brief generation failed; an explicit retry may be available.",
            }
            assert report.status == "failed"
            assert session.scalar(select(func.count()).select_from(LLMRun)) == 0


def test_provider_failure_stays_retryable_and_retry_publishes_new_version() -> None:
    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 8, 18, tzinfo=UTC)
        run_id, token, workspace_id, _ = _prepared_snapshot(
            engine,
            now=now,
            coverage={
                "feeds_attempted": 1,
                "feeds_succeeded": 1,
                "feeds_failed": 0,
                "feed_failures": [],
                "articles_captured": 1,
            },
        )

        def failed_response(_request):
            return RuntimeError("offline provider fixture outage")

        first = generate_personal_brief(
            run_id,
            token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=lambda session, route: _provider_orchestrator(
                session, route, failed_response
            ),
        )
        assert first.published is False
        assert first.report.status == "failed"
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            workspace = session.get(PersonalWorkspace, workspace_id)
            assert run.state == "partially_failed"
            assert run.error["code"] == "model_processing_degraded"
            assert "outage" not in run.error["message"]
            workspace = session.get(PersonalWorkspace, workspace_id)
            retry = retry_run(session, workspace, run_id, now=now + datetime.timedelta(hours=1))
            retry_token = retry.run.ownership_token
            session.commit()
            acquire_run(
                session,
                run_id,
                retry_token,
                now=now + datetime.timedelta(hours=1),
            )
            session.commit()

        second = generate_personal_brief(
            run_id,
            retry_token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=_orchestrator,
        )
        assert second.published is True
        assert second.report.version == 2
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "succeeded"
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PersonalReportLink)
                    .where(PersonalReportLink.run_id == run_id)
                )
                == 2
            )


def test_personal_snapshot_freezes_policy_and_legacy_absence_never_adopts_new_default() -> None:
    with migrated_disposable_engine() as engine:
        _, _, _, snapshot_id = _prepared_snapshot(
            engine,
            now=datetime.datetime(2026, 9, 8, 18, tzinfo=UTC),
            coverage={"feeds_attempted": 1, "feeds_succeeded": 1, "feeds_failed": 0},
        )
        with Session(engine) as session:
            snapshot = session.get(PersonalBriefSnapshot, snapshot_id)
            assert snapshot.input_payload["composition_policy"] == (
                CompositionPolicy.PERSONAL_DESCRIPTIVE.value
            )
            inputs, _, _ = build_personal_brief_inputs(session, snapshot)
            assert inputs.composition_policy is CompositionPolicy.PERSONAL_DESCRIPTIVE
            original_hash = snapshot.input_hash
            # Model an older recorded input without changing any stored snapshot or report.
            old = SimpleNamespace(
                **{
                    column.name: getattr(snapshot, column.name)
                    for column in PersonalBriefSnapshot.__table__.columns
                }
            )
            old.input_payload = dict(snapshot.input_payload)
            del old.input_payload["composition_policy"]
            with pytest.raises(ValueError, match="hash or contract"):
                build_personal_brief_inputs(session, old)
            old.input_hash = hashlib.sha256(
                json.dumps(
                    old.input_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            legacy_inputs, _, _ = build_personal_brief_inputs(session, old)
            assert legacy_inputs.composition_policy is CompositionPolicy.LEGACY
            for invalid in ("unknown.v2", None):
                old.input_payload["composition_policy"] = invalid
                old.input_hash = hashlib.sha256(
                    json.dumps(
                        old.input_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                with pytest.raises(ValueError, match="CompositionPolicy"):
                    build_personal_brief_inputs(session, old)
            assert snapshot.input_hash == original_hash
            assert snapshot.input_payload["composition_policy"] == (
                CompositionPolicy.PERSONAL_DESCRIPTIVE.value
            )


@pytest.mark.parametrize("regeneration_failure", ["provider", "budget"])
def test_failed_personal_grounding_regeneration_cannot_publish(regeneration_failure) -> None:
    with migrated_disposable_engine() as engine:
        run_id, token, _, _ = _prepared_snapshot(
            engine,
            now=datetime.datetime(2026, 9, 8, 18, tzinfo=UTC),
            coverage={"feeds_attempted": 1, "feeds_succeeded": 1, "feeds_failed": 0},
        )
        requests = []

        def response(request):
            requests.append(request)
            if request.requested_schema == COMPOSITION_SCHEMA:
                assert request.prompt_name.startswith("personal_brief_")
                if request.context.get("grounding_regeneration"):
                    if regeneration_failure == "provider":
                        return RuntimeError("scripted regeneration outage")
                    text = " ".join("overflow" for _ in range(181))
                else:
                    text = "The captured publisher reports approval of a project to improve coastal resilience."
                result = _offline_provider_response(request)
                result["blocks"][0]["text"] = text
                return result
            result = _offline_provider_response(request)
            for verdict in result["verdicts"]:
                verdict["verdict"] = "unsupported"
            return result

        result = generate_personal_brief(
            run_id,
            token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=lambda session, route: _provider_orchestrator(
                session, route, response
            ),
        )
        assert any(request.context.get("grounding_regeneration") for request in requests)
        assert result.published is False
        assert result.report.status == "failed"
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "partially_failed"
            assert run.result["processing_degraded"] is True
            assert run.error["code"] == "model_processing_degraded"


def test_partial_capture_remains_partial_across_explicit_retry() -> None:
    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 9, 18, tzinfo=UTC)
        run_id, token, workspace_id, _ = _prepared_snapshot(
            engine,
            now=now,
            coverage={
                "feeds_attempted": 2,
                "feeds_succeeded": 1,
                "feeds_failed": 1,
                "feed_failures": [{"source_id": "two", "code": "fetch_failed"}],
                "articles_captured": 1,
            },
        )
        first = generate_personal_brief(
            run_id,
            token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=_orchestrator,
        )
        assert first.published is True
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "partially_failed"
            assert run.error["code"] == "partial_capture"
            workspace = session.get(PersonalWorkspace, workspace_id)
            retry = retry_run(session, workspace, run_id, now=now + datetime.timedelta(hours=1))
            retry_token = retry.run.ownership_token
            session.commit()
            acquire_run(
                session,
                run_id,
                retry_token,
                now=now + datetime.timedelta(hours=1),
            )
            session.commit()
        second = generate_personal_brief(
            run_id,
            retry_token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=lambda *_: pytest.fail(
                "partial-capture retry recomposed immutable inputs"
            ),
        )
        assert second.published is True
        assert second.report.id == first.report.id
        assert second.report.version == 1
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            workspace = session.get(PersonalWorkspace, workspace_id)
            assert run.state == "partially_failed"
            assert run.result["capture_partial"] is True
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PersonalReportLink)
                    .where(PersonalReportLink.run_id == run_id)
                )
                == 1
            )
            # Simulate the report commit succeeding while terminal bookkeeping is lost.
            job = session.get(Job, run.job_id)
            run.state = "failed"
            run.result = None
            run.error = {"code": "post_publication_failure", "message": "retry available"}
            job.state = "failed"
            session.commit()
            retry = retry_run(session, workspace, run_id, now=now + datetime.timedelta(hours=2))
            final_token = retry.run.ownership_token
            session.commit()
            acquire_run(
                session,
                run_id,
                final_token,
                now=now + datetime.timedelta(hours=2),
            )
            session.commit()
        recovered = generate_personal_brief(
            run_id,
            final_token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=lambda *_: pytest.fail(
                "post-publication recovery recomposed immutable partial inputs"
            ),
        )
        assert recovered.report.id == first.report.id
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "partially_failed"
            assert run.error["code"] == "partial_capture"
            assert run.result["capture_partial"] is True


def test_published_bookkeeping_recovery_reuses_report_without_model_calls() -> None:
    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 10, 18, tzinfo=UTC)
        run_id, token, _, _ = _prepared_snapshot(
            engine,
            now=now,
            coverage={
                "feeds_attempted": 1,
                "feeds_succeeded": 1,
                "feeds_failed": 0,
                "feed_failures": [],
                "articles_captured": 1,
            },
        )
        with Session(engine) as session:
            original_lease = session.get(PersonalRun, run_id).lease_expires_at
            control = session.get(PersonalWriterMode, True)
            original_control = {
                column.name: deepcopy(getattr(control, column.name))
                for column in PersonalWriterMode.__table__.columns
            }
        first = generate_personal_brief(
            run_id,
            token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=_orchestrator,
        )
        with Session(engine) as session:
            # Author a historical interrupted bookkeeping state with its original
            # complete ownership record, retaining the already published report.
            control = lock_processing_control(session)
            run = session.get(PersonalRun, run_id, with_for_update=True)
            job = session.get(Job, run.job_id)
            for field, value in original_control.items():
                setattr(control, field, deepcopy(value))
            run.state = "running"
            run.lease_expires_at = original_lease
            run.result = None
            run.error = None
            job.state = "running"
            lock_owned_run(session, run_id, token, now=now)
            session.commit()
        recovered = generate_personal_brief(
            run_id,
            token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=lambda *_: pytest.fail("recovery recomposed a published report"),
        )
        assert recovered.report.id == first.report.id
        assert recovered.report.version == 1
        with Session(engine) as session:
            assert session.get(PersonalRun, run_id).state == "succeeded"
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PersonalReportLink)
                    .where(PersonalReportLink.run_id == run_id)
                )
                == 1
            )


def test_main_session_acquisition_failure_marks_reserved_report_failed() -> None:
    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 11, 18, tzinfo=UTC)
        run_id, token, _, _ = _prepared_snapshot(
            engine,
            now=now,
            coverage={
                "feeds_attempted": 1,
                "feeds_succeeded": 1,
                "feeds_failed": 0,
                "feed_failures": [],
                "articles_captured": 1,
            },
        )
        calls = 0

        def session_factory():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("main session unavailable")
            return Session(engine)

        with pytest.raises(PersonalBriefGenerationError) as raised:
            generate_personal_brief(
                run_id,
                token,
                session_factory=session_factory,
                orchestrator_factory=_orchestrator,
            )
        assert raised.value.durable_failure_marked is True
        with Session(engine) as session:
            run = session.get(PersonalRun, run_id)
            assert run.state == "failed"
            assert session.get(Report, run.report_id).status == "failed"
