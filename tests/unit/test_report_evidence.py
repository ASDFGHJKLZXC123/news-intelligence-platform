"""Evidence context: linked-only, supportive-only, bounded excerpts, dedup, order, bounds.

DB-free by construction. Every rule under test is a pure function over hand-built
:class:`EvidenceRow`s; the SQL that really enforces the linked-only join is exercised against a
live database in tests/integration/test_stage6_context_joins.py.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

import pytest

from services.reports.context import (
    ARTICLE_EVIDENCE_SOURCE_TYPE,
    MAX_CLAIMS_PER_EVENT,
    MAX_EXCERPT_CHARS,
    MAX_LINKS_PER_CLAIM,
    SUPPORTIVE_SUPPORT_TYPES,
    EventEvidenceContext,
    EvidenceRow,
    ExcerptOrigin,
    SourceExcerpt,
    build_evidence_context,
    build_source_excerpt,
    evidence_notes_for_events,
    is_supportive,
)
from services.reports.contracts import DataQuality, LinkedRisk, RiskProvenance, SelectedEvent

UTC = datetime.UTC


def _excerpt(text: str = "excerpt") -> SourceExcerpt:
    return SourceExcerpt(text=text, origin=ExcerptOrigin.SUMMARY, truncated=False)


def _row(
    *,
    event_id: uuid.UUID,
    claim_id: uuid.UUID,
    article_id: uuid.UUID,
    support_type: str = "supports",
    claim_confidence: float | None = 0.9,
    published_at: datetime.datetime | None = None,
    support_confidence: float | None = 0.8,
) -> EvidenceRow:
    return EvidenceRow(
        event_id=event_id,
        claim_id=claim_id,
        claim_text="claim text",
        claim_type="assertion",
        claim_confidence=claim_confidence,
        support_type=support_type,
        support_confidence=support_confidence,
        article_id=article_id,
        article_title="Headline",
        publisher="Reuters",
        url="https://example.test/a",
        published_at=published_at or datetime.datetime(2026, 7, 14, 4, tzinfo=UTC),
        source_credibility=0.7,
        excerpt=_excerpt(),
    )


def _selected(event_id: uuid.UUID, title: str = "Event", rank: int = 1) -> SelectedEvent:
    return SelectedEvent(
        rank=rank,
        event_id=event_id,
        title=title,
        hotness_score=70.0,
        max_linked_risk=LinkedRisk(score=50.0, provenance=RiskProvenance.EVENT_OBSERVATION),
        ranking_score=62.0,
        credibility_sum=1.0,
        developing=False,
    )


# --------------------------------------------------------------------------------------
# Frozen / no-ORM contract
# --------------------------------------------------------------------------------------


def test_evidence_values_are_frozen() -> None:
    context = EventEvidenceContext(event_id=uuid.uuid4(), claims=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.claims = ()  # type: ignore[misc]


def test_no_orm_or_session_handles_survive_in_returned_values() -> None:
    event_id = uuid.uuid4()
    contexts, _ = build_evidence_context(
        [_row(event_id=event_id, claim_id=uuid.uuid4(), article_id=uuid.uuid4())]
    )
    for context in contexts:
        for claim in context.claims:
            for link in claim.links:
                article = link.article
                for value in (article.article_id, article.title, article.excerpt.text):
                    assert not hasattr(value, "_sa_instance_state")


# --------------------------------------------------------------------------------------
# Supportive-only / linked-only exclusion
# --------------------------------------------------------------------------------------


def test_only_supports_is_supportive() -> None:
    assert SUPPORTIVE_SUPPORT_TYPES == frozenset({"supports"})
    assert is_supportive("supports")
    assert not is_supportive("contradicts")
    assert not is_supportive("contextual")


def test_contradictory_and_contextual_links_never_enter() -> None:
    event_id = uuid.uuid4()
    rows = [
        _row(
            event_id=event_id,
            claim_id=uuid.uuid4(),
            article_id=uuid.uuid4(),
            support_type="contradicts",
        ),
        _row(
            event_id=event_id,
            claim_id=uuid.uuid4(),
            article_id=uuid.uuid4(),
            support_type="contextual",
        ),
    ]
    contexts, _ = build_evidence_context(rows)
    assert contexts == () or all(not c.claims for c in contexts)


def test_a_claim_with_both_supportive_and_contradictory_links_keeps_only_the_supportive() -> None:
    event_id, claim_id = uuid.uuid4(), uuid.uuid4()
    rows = [
        _row(
            event_id=event_id, claim_id=claim_id, article_id=uuid.uuid4(), support_type="supports"
        ),
        _row(
            event_id=event_id,
            claim_id=claim_id,
            article_id=uuid.uuid4(),
            support_type="contradicts",
        ),
    ]
    (context,), _ = build_evidence_context(rows)
    (claim,) = context.claims
    assert len(claim.links) == 1
    assert all(link.support_type == "supports" for link in claim.links)


def test_article_evidence_source_type_is_exactly_article() -> None:
    # Exact by requirement: only `article` qualifies. `rss`/`news_api` are not accepted, even
    # though the canonical schema maps the legacy `article` value onto them.
    assert ARTICLE_EVIDENCE_SOURCE_TYPE == "article"


# --------------------------------------------------------------------------------------
# Dedup retaining multiple evidence links
# --------------------------------------------------------------------------------------


def test_identical_links_deduplicate() -> None:
    event_id, claim_id, article_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = _row(event_id=event_id, claim_id=claim_id, article_id=article_id)
    (context,), _ = build_evidence_context([row, row, dataclasses.replace(row)])
    (claim,) = context.claims
    assert len(claim.links) == 1


def test_two_articles_supporting_one_claim_are_both_kept() -> None:
    event_id, claim_id = uuid.uuid4(), uuid.uuid4()
    rows = [
        _row(event_id=event_id, claim_id=claim_id, article_id=uuid.uuid4()),
        _row(event_id=event_id, claim_id=claim_id, article_id=uuid.uuid4()),
    ]
    (context,), _ = build_evidence_context(rows)
    (claim,) = context.claims
    assert len({link.article.article_id for link in claim.links}) == 2


# --------------------------------------------------------------------------------------
# Order and bounds
# --------------------------------------------------------------------------------------


def test_links_are_ordered_newest_article_first() -> None:
    event_id, claim_id = uuid.uuid4(), uuid.uuid4()
    older = _row(
        event_id=event_id,
        claim_id=claim_id,
        article_id=uuid.uuid4(),
        published_at=datetime.datetime(2026, 7, 10, tzinfo=UTC),
    )
    newer = _row(
        event_id=event_id,
        claim_id=claim_id,
        article_id=uuid.uuid4(),
        published_at=datetime.datetime(2026, 7, 13, tzinfo=UTC),
    )
    (context,), _ = build_evidence_context([older, newer])
    (claim,) = context.claims
    assert claim.links[0].article.published_at == newer.published_at


def test_claims_are_ordered_by_confidence_then_evidence_count() -> None:
    event_id = uuid.uuid4()
    low = _row(
        event_id=event_id, claim_id=uuid.uuid4(), article_id=uuid.uuid4(), claim_confidence=0.4
    )
    high = _row(
        event_id=event_id, claim_id=uuid.uuid4(), article_id=uuid.uuid4(), claim_confidence=0.95
    )
    (context,), _ = build_evidence_context([low, high])
    assert context.claims[0].claim_confidence == 0.95


def test_null_claim_confidence_sorts_last_not_as_zero() -> None:
    event_id = uuid.uuid4()
    scored = _row(
        event_id=event_id, claim_id=uuid.uuid4(), article_id=uuid.uuid4(), claim_confidence=0.1
    )
    unscored = _row(
        event_id=event_id, claim_id=uuid.uuid4(), article_id=uuid.uuid4(), claim_confidence=None
    )
    (context,), _ = build_evidence_context([unscored, scored])
    assert context.claims[0].claim_confidence == 0.1
    assert context.claims[-1].claim_confidence is None


def test_per_claim_link_bound_truncates_and_discloses() -> None:
    event_id, claim_id = uuid.uuid4(), uuid.uuid4()
    dropped = 3
    rows = [
        _row(
            event_id=event_id,
            claim_id=claim_id,
            article_id=uuid.uuid4(),
            published_at=datetime.datetime(2026, 7, 1 + i, tzinfo=UTC),
        )
        for i in range(MAX_LINKS_PER_CLAIM + dropped)
    ]
    (context,), notes = build_evidence_context(rows)
    (claim,) = context.claims
    assert len(claim.links) == MAX_LINKS_PER_CLAIM
    # Dropping links -- not just claims -- must mark the context truncated and be declared with
    # the exact dropped count, or a clipped context reads exactly like a complete one.
    assert context.truncated is True
    (note,) = [n for n in notes if n.code is DataQuality.EVIDENCE_TRUNCATED]
    assert f"{dropped} supporting link(s)" in note.detail


def test_same_claim_article_link_on_two_events_is_kept_for_both() -> None:
    # One claim/article link that belongs to two selected events must survive in *both* event
    # contexts: keying dedup on `(claim_id, article_id, support_type)` alone would let the first
    # event's row swallow the second's. The event id is part of the identity.
    event_a, event_b = uuid.uuid4(), uuid.uuid4()
    claim_id, article_id = uuid.uuid4(), uuid.uuid4()
    rows = [
        _row(event_id=event_a, claim_id=claim_id, article_id=article_id),
        _row(event_id=event_b, claim_id=claim_id, article_id=article_id),
    ]
    contexts, _ = build_evidence_context(rows)
    by_event = {c.event_id: c for c in contexts}
    assert set(by_event) == {event_a, event_b}
    for event_id in (event_a, event_b):
        (claim,) = by_event[event_id].claims
        assert claim.claim_id == claim_id
        assert claim.article_ids == (article_id,)


def test_per_event_claim_bound_truncates_and_notes() -> None:
    event_id = uuid.uuid4()
    rows = [
        _row(
            event_id=event_id,
            claim_id=uuid.uuid4(),
            article_id=uuid.uuid4(),
            claim_confidence=0.5 + i / 100,
        )
        for i in range(MAX_CLAIMS_PER_EVENT + 4)
    ]
    (context,), notes = build_evidence_context(rows)
    assert len(context.claims) == MAX_CLAIMS_PER_EVENT
    assert context.truncated is True
    assert any(note.code is DataQuality.EVIDENCE_TRUNCATED for note in notes)


def test_events_are_returned_in_a_deterministic_total_order() -> None:
    ids = sorted((uuid.uuid4() for _ in range(3)), key=str)
    rows = [
        _row(event_id=eid, claim_id=uuid.uuid4(), article_id=uuid.uuid4()) for eid in reversed(ids)
    ]
    contexts, _ = build_evidence_context(rows)
    assert [c.event_id for c in contexts] == ids


# --------------------------------------------------------------------------------------
# Bounded excerpts and excerpt origin
# --------------------------------------------------------------------------------------


def test_summary_is_preferred_over_body() -> None:
    excerpt = build_source_excerpt("a summary", "a full body")
    assert excerpt.origin is ExcerptOrigin.SUMMARY
    assert excerpt.text == "a summary"


def test_body_excerpt_used_when_no_summary_and_records_origin() -> None:
    excerpt = build_source_excerpt(None, "the body text")
    assert excerpt.origin is ExcerptOrigin.BODY
    assert excerpt.text == "the body text"


def test_missing_summary_and_body_is_an_honest_empty() -> None:
    excerpt = build_source_excerpt(None, None)
    assert excerpt.origin is ExcerptOrigin.NONE
    assert excerpt.is_empty
    assert excerpt.truncated is False


def test_excerpt_is_bounded_and_marks_truncation() -> None:
    head = "x" * (MAX_EXCERPT_CHARS + 1)
    excerpt = build_source_excerpt(None, head, body_length=MAX_EXCERPT_CHARS + 500)
    assert len(excerpt.text) == MAX_EXCERPT_CHARS
    assert excerpt.truncated is True


def test_truncation_is_decided_on_true_length_not_the_slice() -> None:
    # A body of 300 chars beginning with three spaces arrives as a 201-char head whose stripped
    # length is 198 -- shorter than the limit. Deciding on the slice would call it complete.
    head = "   " + "y" * (MAX_EXCERPT_CHARS - 2)
    excerpt = build_source_excerpt(None, head, body_length=300)
    assert excerpt.truncated is True


def test_full_body_never_becomes_the_excerpt() -> None:
    # The bound is exactly the export copyright bound (<=200 chars), so nothing enters that the
    # export would have to strip back out.
    assert MAX_EXCERPT_CHARS == 200
    long_summary = "z" * 5_000
    excerpt = build_source_excerpt(long_summary, None)
    assert len(excerpt.text) == MAX_EXCERPT_CHARS


# --------------------------------------------------------------------------------------
# Missing-evidence quality note
# --------------------------------------------------------------------------------------


def test_selected_event_without_evidence_produces_a_note() -> None:
    covered, uncovered = uuid.uuid4(), uuid.uuid4()
    contexts, _ = build_evidence_context(
        [_row(event_id=covered, claim_id=uuid.uuid4(), article_id=uuid.uuid4())]
    )
    notes = evidence_notes_for_events(
        [_selected(covered), _selected(uncovered, title="Bare")], contexts
    )
    assert len(notes) == 1
    assert notes[0].code is DataQuality.NO_EVIDENCE_FOR_EVENT
    assert "Bare" in notes[0].detail


def test_claim_ids_are_claim_ids_only() -> None:
    event_id, claim_id = uuid.uuid4(), uuid.uuid4()
    (context,), _ = build_evidence_context(
        [_row(event_id=event_id, claim_id=claim_id, article_id=uuid.uuid4())]
    )
    assert context.claim_ids == (claim_id,)
