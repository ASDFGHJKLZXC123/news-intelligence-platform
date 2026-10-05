"""Pure-row evidence capture tests: no services, providers, or existing database."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import uuid
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from db.models import (
    Event,
    PersonalBriefSnapshot,
    PersonalCapture,
    PersonalFeedReceipt,
    PersonalProfileRevision,
    PersonalReportLink,
    PersonalRun,
    PersonalRunEventObservation,
    PersonalWorkspace,
    Report,
    WatchlistItem,
)
from db.models.personal_spending import PersonalLegacyUsage, PersonalPaidRequest
from services.personal import trial_capture
from services.personal.exports import PersonalCitation, PersonalEvidence
from services.personal.trial_capture import TrialCaptureError, capture_session_evidence
from services.reports.lifecycle import ReportSectionSnapshot, SectionBlock

STAMP = dt.datetime(2026, 10, 4, 21, tzinfo=dt.UTC)
SECRET = "must-never-be-retained-secret"


def _hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def fixture_population(count=27, *, with_snapshot=True):
    workspace = PersonalWorkspace(
        id=uuid.uuid4(), owner_id=uuid.uuid4(), timezone="America/Los_Angeles"
    )
    source_id = uuid.uuid4()
    profile = PersonalProfileRevision(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        revision=3,
        selected_source_ids=[source_id],
        include_phrases=["energy"],
        exclude_phrases=["celebrity"],
        execution_profile="assisted",
        schema_revision="personal-profile.v2",
        created_at=STAMP,
        settings={
            "ai_enabled": False,
            "monthly_allowance_usd": "1.00",
            "run_allowance_usd": "0.10",
            "api_key": SECRET,
            "model_route": {
                "mode": "live",
                "api_key": SECRET,
                "generation": {"provider": "openai", "model": "model", "api_key": SECRET},
            },
        },
    )
    workspace.active_profile_revision_id = (
        uuid.uuid4()
    )  # active profile must never replace frozen one
    run = PersonalRun(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        profile_revision_id=profile.id,
        job_id=uuid.uuid4(),
        local_date=dt.date(2026, 10, 4),
        execution_mode="personal",
        state="succeeded",
        attempt=2,
        max_attempts=3,
        delivery_transport="standalone",
        created_at=STAMP,
        updated_at=STAMP,
        started_at=STAMP,
        capture_started_at=STAMP,
        capture_ended_at=STAMP,
        scopes_frozen_at=STAMP,
        admitted_article_ids=[],
        enrichment_article_ids=[],
        event_ids=[],
        coverage={"articles_captured": count, "api_key": SECRET},
        stage_results={"report": {"status": "skipped", "reason": "ai_disabled", "secret": SECRET}},
        result={"published": False, "api_key": SECRET},
        error={"code": "partial_capture", "message": SECRET},
    )
    observations = []
    candidates = []
    for index in range(count):
        article_id, event_id = uuid.uuid4(), uuid.uuid4()
        source = {
            "revision_id": str(uuid.uuid4()),
            "article_id": str(article_id),
            "source_id": str(source_id),
            "title": f"Energy approval {index}",
            "rss_summary": f"Agency approved energy project {index}.",
            "url": f"https://{SECRET}:password@example.test/{index}?api_key={SECRET}&page=1",
            "publisher": "Publisher",
            "published_at": STAMP.isoformat(),
            "content_hash": "a" * 64,
            "truncated": False,
            "arbitrary_credentials": SECRET,
        }
        inputs = {
            "schema": "personal-event-observation.v1",
            "event_title": f"Frozen Energy Event {index}",
            "event_summary": f"Frozen development {index}",
            "articles": [source],
            "secret": SECRET,
        }
        ranking = {
            "source_count": 1,
            "hotness": None,
            "candidate_rank": index + 1,
            "sort_key": [index],
        }
        observation = PersonalRunEventObservation(
            id=uuid.uuid4(),
            run_id=run.id,
            event_id=event_id,
            revision=1,
            qualifying_article_ids=[article_id],
            source_inputs=inputs,
            ranking_inputs=ranking,
            observed_at=STAMP,
        )
        observations.append(observation)
        candidates.append(
            {
                "observation_id": str(observation.id),
                "event_id": str(event_id),
                "revision": 1,
                "source_inputs": inputs,
                "ranking": ranking,
            }
        )
        run.admitted_article_ids.append(article_id)
        run.enrichment_article_ids.append(article_id)
        run.event_ids.append(event_id)
    snapshot = None
    if with_snapshot:
        payload = {
            "schema": "personal-brief-input.v1",
            "run_id": str(run.id),
            "workspace_id": str(workspace.id),
            "profile_revision_id": str(profile.id),
            "local_date": run.local_date.isoformat(),
            "candidates": candidates,
            "claims": [],
            "captured": {"start": STAMP.isoformat(), "end": STAMP.isoformat()},
            "coverage": {"articles_captured": count},
            "model_route": {},
            "prepared_at": STAMP.isoformat(),
        }
        snapshot = PersonalBriefSnapshot(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            run_id=run.id,
            profile_revision_id=profile.id,
            candidate_event_ids=run.event_ids,
            selected_event_ids=run.event_ids[:5],
            input_payload=payload,
            input_hash=_hash(payload),
            input_contract="personal-brief-input.v1",
            model_route={},
            prepared_at=STAMP,
        )
        run.snapshot_id = snapshot.id
    rows = {
        PersonalRunEventObservation: observations,
        PersonalFeedReceipt: [],
        PersonalCapture: [],
        PersonalReportLink: [],
        PersonalPaidRequest: [],
        PersonalLegacyUsage: [],
        WatchlistItem: [],
    }
    objects = {
        (PersonalRun, run.id): run,
        (PersonalWorkspace, workspace.id): workspace,
        (PersonalProfileRevision, profile.id): profile,
    }
    if snapshot:
        objects[PersonalBriefSnapshot, snapshot.id] = snapshot
    session = MagicMock()
    session.in_transaction.return_value = True
    session.new = session.dirty = session.deleted = set()
    session.no_autoflush = nullcontext()
    session.get_bind.return_value.dialect.name = "sqlite"
    session.get.side_effect = lambda model, identity: objects.get((model, identity))
    queries = []

    def scalars(statement):
        queries.append(statement)
        return rows[statement.column_descriptions[0]["entity"]]

    session.scalars.side_effect = scalars
    session.scalar.return_value = workspace.owner_id
    return SimpleNamespace(
        run=run,
        profile=profile,
        workspace=workspace,
        snapshot=snapshot,
        observations=observations,
        rows=rows,
        objects=objects,
        session=session,
        queries=queries,
    )


def test_every_page_and_uncapped_candidate_is_frozen_from_original_profile_without_writes():
    data = fixture_population()
    original = copy.deepcopy(data.snapshot.input_payload)
    # Mutable current event and article tables must never provide the group material.
    evidence = capture_session_evidence(data.session, data.run.id)
    assert len(evidence["groups"]) == 27
    assert len(evidence["snapshot"]["selected_event_ids"]) == 5
    assert evidence["group_population_basis"] == "all_frozen_snapshot_candidates"
    assert evidence["groups"][0]["snapshot_event_title"] == "Frozen Energy Event 0"
    assert evidence["profile"]["profile_revision_id"] == str(data.profile.id)
    assert evidence["profile"]["ai_enabled"] is False
    assert evidence["processing_date"] == "2026-10-04"
    assert evidence["human_session_observed"] is False
    assert evidence["attempts"][0]["raw_terminal_outcome"] is None
    assert evidence["run"]["attempt"] == 2
    assert SECRET not in json.dumps(evidence)
    assert data.snapshot.input_payload == original
    for method in (
        data.session.add,
        data.session.flush,
        data.session.commit,
        data.session.delete,
        data.session.execute,
    ):
        method.assert_not_called()
    assert all(query.column_descriptions[0]["entity"] is not Event for query in data.queries)
    assert all(
        "run_id" in str(query)
        for query in data.queries
        if query.column_descriptions[0]["entity"] is PersonalRunEventObservation
    )


def test_pre_snapshot_keeps_latest_observed_membership_and_marks_incomplete():
    data = fixture_population(1, with_snapshot=False)
    old = data.observations[0]
    newer = PersonalRunEventObservation(
        id=uuid.uuid4(),
        run_id=data.run.id,
        event_id=old.event_id,
        revision=2,
        qualifying_article_ids=old.qualifying_article_ids,
        source_inputs={**old.source_inputs, "event_title": "New observed energy development"},
        ranking_inputs=old.ranking_inputs,
        observed_at=STAMP + dt.timedelta(minutes=2),
    )
    data.rows[PersonalRunEventObservation] = [old, newer]
    evidence = capture_session_evidence(data.session, data.run.id)
    group = evidence["groups"][0]
    assert group["observation_revision"] == 2
    assert group["article_ids"] == [str(old.qualifying_article_ids[0])]
    assert group["completed_brief_snapshot"] is False
    assert group["snapshot_id"] is None
    assert "incomplete_pre_snapshot_observation" in group["findings"]


def test_missing_retained_source_stays_in_population_as_unverifiable():
    data = fixture_population(1, with_snapshot=False)
    data.observations[0].source_inputs = {"schema": "personal-event-observation.v1", "articles": []}
    evidence = capture_session_evidence(data.session, data.run.id)
    assert len(evidence["groups"]) == 1
    group = evidence["groups"][0]
    assert group["evidence_status"] == "unverifiable"
    assert group["missing_article_ids"] == group["article_ids"]


def test_structural_profile_and_scope_exclusions_are_explicit():
    data = fixture_population(3, with_snapshot=False)
    data.observations[0].source_inputs["articles"][0]["source_id"] = str(uuid.uuid4())
    data.observations[1].source_inputs["articles"][0]["title"] = "Celebrity energy headline"
    data.run.enrichment_article_ids.remove(data.observations[2].qualifying_article_ids[0])
    evidence = capture_session_evidence(data.session, data.run.id)
    assert evidence["groups"] == []
    assert {item["reason"] for item in evidence["exclusions"]} >= {
        "source_not_in_frozen_profile",
        "does_not_match_frozen_interest",
        "outside_frozen_enrichment_scope",
    }


def test_snapshot_pins_original_observation_even_after_new_revision():
    data = fixture_population(1)
    old = data.observations[0]
    data.rows[PersonalRunEventObservation].append(
        PersonalRunEventObservation(
            id=uuid.uuid4(),
            run_id=data.run.id,
            event_id=old.event_id,
            revision=2,
            qualifying_article_ids=old.qualifying_article_ids,
            source_inputs={**old.source_inputs, "event_title": "Changed"},
            ranking_inputs=old.ranking_inputs,
            observed_at=STAMP + dt.timedelta(hours=1),
        )
    )
    evidence = capture_session_evidence(data.session, data.run.id)
    assert evidence["groups"][0]["observation_revision"] == 1
    assert evidence["groups"][0]["snapshot_event_title"] == "Frozen Energy Event 0"


@pytest.mark.parametrize(
    "condition",
    [
        "absent_run",
        "absent_profile",
        "wrong_workspace",
        "unknown_state",
        "legacy",
        "dirty",
        "no_transaction",
        "hash",
    ],
)
def test_ineligible_or_inconsistent_capture_is_rejected(condition):
    data = fixture_population(1)
    if condition == "absent_run":
        del data.objects[PersonalRun, data.run.id]
    elif condition == "absent_profile":
        del data.objects[PersonalProfileRevision, data.profile.id]
    elif condition == "wrong_workspace":
        data.profile.workspace_id = uuid.uuid4()
    elif condition == "unknown_state":
        data.run.state = "unknown"
    elif condition == "legacy":
        data.run.execution_mode = "legacy"
    elif condition == "dirty":
        data.session.dirty = {data.run}
    elif condition == "no_transaction":
        data.session.in_transaction.return_value = False
    else:
        data.snapshot.input_hash = "0" * 64
    with pytest.raises((LookupError, TrialCaptureError)):
        capture_session_evidence(data.session, data.run.id)
    data.session.commit.assert_not_called()
    data.session.flush.assert_not_called()


@pytest.mark.parametrize(
    "isolation,read_only", [("READ COMMITTED", "on"), ("REPEATABLE READ", "off")]
)
def test_postgres_consistency_and_read_only_are_verified_before_orm_reads(isolation, read_only):
    data = fixture_population(1)
    data.session.get_bind.return_value.dialect.name = "postgresql"
    data.session.connection.return_value.get_isolation_level.return_value = isolation
    data.session.scalar.return_value = read_only
    with pytest.raises(TrialCaptureError):
        capture_session_evidence(data.session, data.run.id)
    data.session.get.assert_not_called()


@pytest.mark.parametrize("state", ["queued", "running", "interrupted"])
def test_nonterminal_sessions_remain_observable_without_invented_completion(state):
    data = fixture_population(0, with_snapshot=False)
    data.run.state = state
    data.profile.settings.pop("ai_enabled")
    evidence = capture_session_evidence(data.session, data.run.id)
    assert evidence["terminal_outcome_observed"] is False
    assert evidence["attempts"][-1]["observed_state"] == state
    assert evidence["attempts"][-1]["raw_terminal_outcome"] is None
    assert evidence["profile"]["ai_enabled"] is None
    assert "nonterminal_outcome_incomplete_at_capture" in evidence["findings"]


def test_uncertain_charges_never_disappear_and_zero_run_allowance_is_not_reset():
    data = fixture_population(0, with_snapshot=False)
    data.profile.settings["run_allowance_usd"] = "0"
    request = SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=data.workspace.id,
        run_id=data.run.id,
        attempt=1,
        accounting_month=STAMP.date().replace(day=1),
        status="uncertain",
        role="generation",
        route={"provider": "openai", "model": "model", "api_key": SECRET},
        reserved_usd=Decimal("0.04"),
        actual_usd=None,
        input_token_bound=100,
        output_token_bound=50,
        input_tokens=None,
        output_tokens=None,
        reserved_at=STAMP,
        dispatch_attempt_at=STAMP,
        reconciled_at=None,
    )
    legacy = SimpleNamespace(
        llm_run_id=uuid.uuid4(), accounting_month=request.accounting_month, actual_usd=None
    )
    data.rows[PersonalPaidRequest] = [request]
    data.rows[PersonalLegacyUsage] = [legacy]
    evidence = capture_session_evidence(data.session, data.run.id)
    assert evidence["costs"]["run_known_obligation_usd"] == "0.04"
    assert evidence["costs"]["run_frozen_profile_remaining_usd"] == "0"
    month = next(
        row
        for row in evidence["costs"]["monthly_observations"]
        if row["accounting_month"] == "2026-10-01"
    )
    assert month["unknown_charge_present"] is True
    assert month["frozen_profile_remaining_usd"] is None
    assert evidence["attempts"][0]["paid_request_ids"] == [str(request.id)]
    assert SECRET not in json.dumps(evidence)


def test_saved_snapshot_preserves_missing_item_and_does_not_claim_user_exercise():
    data = fixture_population(1, with_snapshot=False)
    saved_id = uuid.uuid4()
    event_id = data.observations[0].event_id
    data.rows[WatchlistItem] = [
        SimpleNamespace(id=saved_id, item_id=str(event_id), created_at=STAMP)
    ]
    evidence = capture_session_evidence(data.session, data.run.id)
    assert evidence["saved"] == [
        {
            "saved_id": str(saved_id),
            "event_id": str(event_id),
            "saved_at": "2026-10-04T21:00:00Z",
            "available": False,
            "in_captured_group_population": True,
        }
    ]
    assert "saved_snapshot_does_not_prove_saving_reopening_or_no_prior_loss" in evidence["findings"]


def test_missing_published_material_is_a_population_gap_not_a_successful_empty_population(
    monkeypatch,
):
    data = fixture_population(1)
    report_id = uuid.uuid4()
    data.rows[PersonalReportLink] = [
        SimpleNamespace(report_id=report_id, version=1, snapshot_id=data.snapshot.id)
    ]
    data.objects[Report, report_id] = SimpleNamespace(status="published")
    monkeypatch.setattr(
        trial_capture,
        "PersonalBriefRepository",
        lambda *_: SimpleNamespace(published_document=lambda _: None),
    )
    evidence = capture_session_evidence(data.session, data.run.id)
    assert evidence["summaries"] == []
    assert evidence["summary_population_complete"] is False
    assert "published_summary_population_incomplete" in evidence["findings"]
    assert evidence["exclusions"][0]["reason"] == "published_immutable_material_unverifiable"


def test_withheld_event_section_is_preserved_as_structural_exclusion_not_summary():
    data = fixture_population(1)
    report_id = uuid.uuid4()

    def section(order, title, body):
        return ReportSectionSnapshot(
            uuid.uuid4(), report_id, order, title, body, (), (), "data_quality_note"
        )

    document = SimpleNamespace(
        report=SimpleNamespace(id=report_id, version=1),
        citations=(),
        sections=(
            section(1, "Executive Summary", "No claims"),
            section(2, "Frozen Energy Event 0", "Summary withheld: no supported material."),
            section(3, "Disclaimer", "Disclaimer"),
        ),
    )
    exclusions = []
    assert trial_capture._summary_units(document, data.snapshot, exclusions) == []
    assert exclusions[0]["reason"] == "no_rendered_event_summary_grounding_withheld"
    assert exclusions[0]["retained_section"]["body"].startswith("Summary withheld")


def test_full_published_units_keep_heading_body_context_claims_and_all_support_without_invented_time(
    monkeypatch,
):
    data = fixture_population(2)
    report_id = uuid.uuid4()
    first_claim, second_claim = uuid.uuid4(), uuid.uuid4()
    claims = [first_claim, second_claim]
    citations = []
    for candidate, claim_id in zip(data.snapshot.input_payload["candidates"], claims, strict=True):
        article = candidate["source_inputs"]["articles"][0]
        evidence_id = uuid.uuid4()
        data.snapshot.input_payload["claims"].append(
            {
                "preparation_id": str(uuid.uuid4()),
                "article_revision_id": article["revision_id"],
                "article_id": article["article_id"],
                "claim_id": str(claim_id),
                "evidence_item_id": str(evidence_id),
                "source_field": "summary",
                "span": [0, len(article["rss_summary"])],
                "exact_excerpt": article["rss_summary"],
                "status": "supported",
                "citable": True,
                "validation": {"api_key": SECRET},
            }
        )
        citations.append(
            PersonalCitation(
                claim_id=claim_id,
                text=article["rss_summary"],
                evidence=(
                    PersonalEvidence(
                        evidence_id,
                        uuid.UUID(article["article_id"]),
                        "summary",
                        article["title"],
                        "Publisher",
                        article["url"],
                        STAMP,
                    ),
                ),
            )
        )
    data.snapshot.input_hash = _hash(data.snapshot.input_payload)

    def section(order, title, body, references=()):
        return ReportSectionSnapshot(
            uuid.uuid4(),
            report_id,
            order,
            title,
            body,
            (SectionBlock(body, tuple(str(item) for item in references)),) if references else (),
            tuple(references),
            "passed" if references else "data_quality_note",
        )

    sections = (
        section(1, "Executive Summary", "Both developments happened.", claims),
        section(2, "Frozen Energy Event 0", "First rendered summary.", [first_claim]),
        section(3, "Frozen Energy Event 1", "Second rendered summary.", [second_claim]),
        section(4, "Conclusion", "Both remain current.", claims),
        section(5, "Disclaimer", "Fixed descriptive disclaimer."),
    )
    report = SimpleNamespace(
        id=report_id,
        version=2,
        status="published",
        title="Personal brief",
        created_at=STAMP,
        updated_at=STAMP + dt.timedelta(days=1),
    )
    document = SimpleNamespace(report=report, sections=sections, citations=tuple(citations))
    link = SimpleNamespace(report_id=report_id, version=2, snapshot_id=data.snapshot.id)
    data.rows[PersonalReportLink] = [link]
    data.objects[Report, report_id] = report
    monkeypatch.setattr(
        trial_capture,
        "PersonalBriefRepository",
        lambda *_: SimpleNamespace(published_document=lambda _: document),
    )
    evidence = capture_session_evidence(data.session, data.run.id)
    assert len(evidence["summaries"]) == 2
    for unit in evidence["summaries"]:
        assert unit["report_id"] == str(report_id)
        assert unit["version"] == 2
        assert unit["published_at"] is None
        assert "publication_timestamp_unverifiable" in unit["findings"]
        assert unit["heading"].startswith("Frozen Energy Event")
        assert unit["body"].endswith("rendered summary.")
        assert unit["introduction"]["body"] == "Both developments happened."
        assert unit["conclusion"][0]["body"] == "Both remain current."
        assert len(unit["citations"]) == 2
        assert len(unit["claims"]) == 2
        assert len(unit["source_references"]) == 2
    assert SECRET not in json.dumps(evidence)
