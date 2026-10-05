from __future__ import annotations

import uuid

import pytest

from db.models import PersonalArticleRevision
from services.personal.claims import (
    MAX_SENTENCES_PER_ARTICLE,
    article_revision_hash,
    extract_claim_candidates,
)


def _revision(*, title: str = "Coverage update", summary: str | None) -> PersonalArticleRevision:
    revision = PersonalArticleRevision(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        article_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        content_hash="0" * 64,
        retained_title=title,
        retained_summary=summary,
        retained_url="https://example.test/article",
        retained_publisher="Example Wire",
        retained_published_at=None,
        provenance={"schema": "test"},
        truncated=False,
    )
    revision.content_hash = article_revision_hash(revision)
    return revision


@pytest.mark.parametrize(
    "summary,expected",
    [
        (
            "The central bank announced a rate cut to 0.25.",
            "The central bank announced a rate cut to 0.25.",
        ),
        (
            "The agency reported a total of 250.",
            "The agency reported a total of 250.",
        ),
        (
            "Dr. Lee confirmed the central bank cut rates.",
            "Dr. Lee confirmed the central bank cut rates.",
        ),
        (
            "The agency confirmed it did not approve the project.",
            "The agency confirmed it did not approve the project.",
        ),
        (
            "The regulator denied a claim:\nAcme released faulty devices.",
            "The regulator denied a claim:\nAcme released faulty devices.",
        ),
        (
            "The regulator denied a claim:\nAcme released faulty devices.",
            "The regulator denied a claim:\nAcme released faulty devices.",
        ),
    ],
)
def test_extracts_exact_complete_source_reported_assertions(summary: str, expected: str) -> None:
    candidate = extract_claim_candidates(_revision(summary=summary))[0]
    assert candidate.source_field == "summary"
    assert candidate.text == expected
    assert summary[candidate.start : candidate.end] == expected


@pytest.mark.parametrize(
    "text",
    [
        'Did officials confirm that the agency approved the project?"',
        "Top 10 reasons the company reported a loss.",
        "Five reasons the company announced a new policy.",
        "Analysts expect the company reported 5% growth.",
        "Imagine the company announced 20% growth.",
        "It reported the result after the meeting.",
        "The agency announced the plan",
    ],
)
def test_abstains_on_questions_listicles_forecasts_dependent_or_incomplete_fragments(
    text: str,
) -> None:
    assert extract_claim_candidates(_revision(summary=text)) == ()


def test_truncated_revision_abstains_and_candidate_bound_is_explicit() -> None:
    revision = _revision(
        summary="A confirmed the first result. B confirmed the second result. "
        "C confirmed the third result. D confirmed the fourth result."
    )
    assert len(extract_claim_candidates(revision)) == MAX_SENTENCES_PER_ARTICLE
    revision.truncated = True
    assert extract_claim_candidates(revision) == ()


def test_revision_hash_changes_with_pinned_source_metadata() -> None:
    revision = _revision(summary="The agency confirmed the project was approved.")
    original = article_revision_hash(revision)
    revision.retained_publisher = "Changed Wire"
    assert article_revision_hash(revision) != original
