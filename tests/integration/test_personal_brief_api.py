"""Exact personal-report API reads against a freshly generated offline brief."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.main import app
from db.base import get_session
from db.models import (
    Article,
    EvidenceItem,
    PersonalReportLink,
    PersonalRun,
    Report,
    Source,
)
from db.models.core import REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
from db.models.personal import PERSONAL_REPORT_TYPE
from services.personal.briefs import generate_personal_brief
from services.personal.exports import PersonalBriefRepository
from tests.integration._stage7_db import migrated_disposable_engine
from tests.integration.test_personal_brief_generation import _orchestrator, _prepared_snapshot

pytestmark = pytest.mark.integration
UTC = datetime.UTC


@contextmanager
def _client(engine) -> Iterator[TestClient]:  # noqa: ANN001
    def _session_override() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = _session_override
    try:
        with TestClient(app, client=("127.0.0.1", 5000)) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _unpublished_version(
    session: Session,
    *,
    prior: Report,
    link: PersonalReportLink,
    version: int,
    status: str,
) -> uuid.UUID:
    report = Report(
        user_id=prior.user_id,
        report_type=PERSONAL_REPORT_TYPE,
        brief_date=prior.brief_date,
        event_id=None,
        title=prior.title,
        status=status,
        version=version,
        change_reason=f"offline test {status} version",
        stale=False,
        content_policy=REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY,
    )
    session.add(report)
    session.flush()
    session.add(
        PersonalReportLink(
            report_id=report.id,
            workspace_id=link.workspace_id,
            run_id=link.run_id,
            snapshot_id=link.snapshot_id,
            brief_date=link.brief_date,
            version=version,
        )
    )
    session.flush()
    return report.id


def test_personal_brief_routes_stay_on_exact_generated_snapshot() -> None:
    with migrated_disposable_engine() as engine:
        now = datetime.datetime(2026, 9, 8, 18, tzinfo=UTC)
        run_id, token, workspace_id, snapshot_id = _prepared_snapshot(
            engine,
            now=now,
            coverage={
                "feeds_configured": 1,
                "feeds_attempted": 1,
                "feeds_succeeded": 1,
                "feeds_failed": 0,
                "feeds_paused": 0,
                "feed_failures": [],
                "feed_pauses": [],
                "articles_captured": 1,
                "provider_secret": "must-not-cross-read-boundary",
            },
        )
        generated = generate_personal_brief(
            run_id,
            token,
            session_factory=lambda: Session(engine),
            orchestrator_factory=_orchestrator,
        )
        report_id = generated.report.id
        assert generated.published is True

        with _client(engine) as client:
            history = client.get("/api/v1/personal/briefs?limit=1&offset=0")
            assert history.status_code == 200
            assert history.json()["total"] == 1
            assert history.json()["items"][0]["id"] == str(report_id)
            assert history.json()["items"][0]["snapshot_id"] == str(snapshot_id)

            detail = client.get(f"/api/v1/personal/briefs/{report_id}")
            assert detail.status_code == 200
            detail_body = detail.json()
            assert detail_body["report"]["id"] == str(report_id)
            assert detail_body["report"]["status"] == "published"
            assert detail_body["snapshot"]["id"] == str(snapshot_id)
            assert detail_body["snapshot"]["run_id"] == str(run_id)
            assert detail_body["snapshot"]["coverage"]["articles_captured"] == 1
            assert "provider_secret" not in detail_body["snapshot"]["coverage"]
            claim_id = uuid.UUID(detail_body["citations"][0]["claim_id"])

            evidence_path = f"/api/v1/personal/briefs/{report_id}/claims/{claim_id}/evidence"
            evidence = client.get(evidence_path)
            assert evidence.status_code == 200
            evidence_body = evidence.json()
            assert evidence_body["report_id"] == str(report_id)
            assert len(evidence_body["evidence"][0]["snippet"]) <= 200
            assert evidence_body["evidence"][0]["source_type"] == "article"
            assert evidence_body["evidence"][0]["support_type"] == "supports"

            markdown_path = f"/api/v1/personal/briefs/{report_id}/export.md"
            pdf_path = f"/api/v1/personal/briefs/{report_id}/export.pdf"
            markdown = client.get(markdown_path)
            pdf = client.get(pdf_path)
            assert markdown.status_code == 200
            assert pdf.status_code == 200
            assert str(report_id) in markdown.text
            assert str(snapshot_id) in markdown.text
            assert "Feeds succeeded: 1" in markdown.text
            assert "Items fetched: Not recorded" in markdown.text
            assert "Pending items: Not recorded" in markdown.text
            assert str(claim_id) in markdown.text
            assert "must-not-cross-read-boundary" not in markdown.text
            assert str(report_id) in markdown.headers["content-disposition"]
            assert str(report_id) in pdf.headers["content-disposition"]
            assert pdf.content.startswith(b"%PDF-")
            before_evidence = evidence.content
            before_markdown = markdown.content
            before_pdf = pdf.content

            wrong_claim = client.get(
                f"/api/v1/personal/briefs/{report_id}/claims/{uuid.uuid4()}/evidence"
            )
            assert wrong_claim.status_code == 404
            assert client.get(f"/api/v1/personal/briefs/{uuid.uuid4()}").status_code == 404

        with Session(engine) as session:
            evidence_item_id = uuid.UUID(evidence_body["evidence"][0]["evidence_item_id"])
            article_id = uuid.UUID(evidence_body["evidence"][0]["article_id"])
            article = session.get(Article, article_id)
            evidence_item = session.get(EvidenceItem, evidence_item_id)
            source = session.get(Source, article.source_id)
            article.title = "MUTATED CURRENT ARTICLE"
            article.summary = "MUTATED CURRENT SUMMARY"
            article.url = "https://mutated.example/current"
            evidence_item.title = "MUTATED CURRENT EVIDENCE"
            evidence_item.publisher = "Mutated Publisher"
            evidence_item.url = "https://mutated.example/evidence"
            source.name = "Mutated current source"
            prior = session.get(Report, report_id)
            link = session.get(PersonalReportLink, report_id)
            failed_id = _unpublished_version(
                session, prior=prior, link=link, version=2, status="failed"
            )
            generating_id = _unpublished_version(
                session, prior=prior, link=link, version=3, status="generating"
            )
            session.commit()
            assert (
                PersonalBriefRepository(session, uuid.uuid4()).published_document(report_id) is None
            )

        with _client(engine) as client:
            assert client.get(evidence_path).content == before_evidence
            assert client.get(markdown_path).content == before_markdown
            assert client.get(pdf_path).content == before_pdf
            history = client.get("/api/v1/personal/briefs?limit=2&offset=0").json()
            assert history["total"] == 3
            assert [item["version"] for item in history["items"]] == [3, 2]
            assert [item["status"] for item in history["items"]] == ["generating", "failed"]
            for unpublished_id in (failed_id, generating_id):
                assert client.get(f"/api/v1/personal/briefs/{unpublished_id}").status_code == 404
                assert (
                    client.get(f"/api/v1/personal/briefs/{unpublished_id}/export.md").status_code
                    == 404
                )
                assert (
                    client.get(f"/api/v1/personal/briefs/{unpublished_id}/export.pdf").status_code
                    == 404
                )
            assert client.get(markdown_path).content == before_markdown
            assert client.get(pdf_path).content == before_pdf

        with Session(engine) as session:
            run = session.scalar(select(PersonalRun).where(PersonalRun.id == run_id))
            assert run.report_id == report_id


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
