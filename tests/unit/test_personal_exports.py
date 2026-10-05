"""Immutable personal report citation reduction."""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid

from db.models import PersonalBriefSnapshot
from services.personal.exports import build_snapshot_view_and_citations
from services.reports.lifecycle import ReportSectionSnapshot

UTC = datetime.UTC


def _hash(payload) -> str:  # noqa: ANN001
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_one_canonical_claim_keeps_every_distinct_frozen_article_support() -> None:
    run_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    event_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    prepared_at = datetime.datetime(2026, 9, 8, 18, tzinfo=UTC)
    articles = [
        {
            "article_id": str(uuid.uuid4()),
            "source_id": str(uuid.uuid4()),
            "title": "Agency confirmed the coastal project was approved.",
            "rss_summary": "Agency confirmed the coastal project was approved.",
            "url": f"https://example.test/{index}",
            "publisher": f"Publisher {index}",
            "published_at": prepared_at.isoformat(),
            "content_hash": uuid.uuid4().hex,
            "truncated": False,
        }
        for index in (1, 2)
    ]
    claim_text = articles[0]["title"]
    payload = {
        "schema": "personal-brief-input.v1",
        "run_id": str(run_id),
        "workspace_id": str(workspace_id),
        "profile_revision_id": str(profile_id),
        "local_date": "2026-09-08",
        "captured": {"start": prepared_at.isoformat(), "end": prepared_at.isoformat()},
        "coverage": {"feeds_succeeded": 2},
        "candidates": [
            {
                "event_id": str(event_id),
                "source_inputs": {"articles": articles},
            }
        ],
        "claims": [
            {
                "claim_id": str(claim_id),
                "evidence_item_id": str(uuid.uuid4()),
                "article_id": article["article_id"],
                "source_field": "title",
                "exact_excerpt": claim_text,
                "citable": True,
            }
            for article in articles
        ],
        "model_route": {"mode": "offline_fixture"},
        "prepared_at": prepared_at.isoformat(),
    }
    snapshot = PersonalBriefSnapshot(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        run_id=run_id,
        profile_revision_id=profile_id,
        candidate_event_ids=[event_id],
        selected_event_ids=[event_id],
        input_payload=payload,
        input_hash=_hash(payload),
        model_route=payload["model_route"],
        input_contract="personal-brief-input.v1",
        prepared_at=prepared_at,
    )
    section = ReportSectionSnapshot(
        id=uuid.uuid4(),
        report_id=uuid.uuid4(),
        section_order=0,
        title="Summary",
        body="A grounded summary.",
        blocks=(),
        evidence_refs=(claim_id,),
        grounding_status="passed",
    )

    _, citations = build_snapshot_view_and_citations(snapshot, (section,))

    assert len(citations) == 1
    assert citations[0].claim_id == claim_id
    assert citations[0].text == claim_text
    assert len(citations[0].evidence) == 2
    assert {item.article_id for item in citations[0].evidence} == {
        uuid.UUID(article["article_id"]) for article in articles
    }
