"""`build_brief_context` and the `BoundedRead` scan contract, DB-free.

The per-event/per-claim reductions live in test_report_evidence.py and test_report_parallels.py;
this file covers the aggregate loader and the global-scan truncation signal it must surface. A
pure fake repository stands in for SQL, so no database and no seeded rows are needed to prove
that a scan which hit its bound is *declared*, not silently absorbed.
"""

from __future__ import annotations

import datetime
import uuid

from services.reports.context import (
    AnalogyContext,
    BoundedRead,
    EvidenceRow,
    ExcerptOrigin,
    ForecastScenarioRow,
    HistoricalOnset,
    HistoricalOutcomeContext,
    SourceExcerpt,
    build_brief_context,
)
from services.reports.contracts import DataQuality, LinkedRisk, RiskProvenance, SelectedEvent

UTC = datetime.UTC


def _selected(event_id: uuid.UUID, title: str = "Event") -> SelectedEvent:
    return SelectedEvent(
        rank=1,
        event_id=event_id,
        title=title,
        hotness_score=70.0,
        max_linked_risk=LinkedRisk(score=50.0, provenance=RiskProvenance.EVENT_OBSERVATION),
        ranking_score=62.0,
        credibility_sum=1.0,
        developing=False,
    )


def _evidence_row(event_id: uuid.UUID) -> EvidenceRow:
    return EvidenceRow(
        event_id=event_id,
        claim_id=uuid.uuid4(),
        claim_text="claim",
        claim_type="assertion",
        claim_confidence=0.9,
        support_type="supports",
        support_confidence=0.8,
        article_id=uuid.uuid4(),
        article_title="Headline",
        publisher="Reuters",
        url="https://example.test/a",
        published_at=datetime.datetime(2026, 7, 14, tzinfo=UTC),
        source_credibility=0.7,
        excerpt=SourceExcerpt(text="x", origin=ExcerptOrigin.SUMMARY, truncated=False),
    )


def _analogy(event_id: uuid.UUID) -> AnalogyContext:
    return AnalogyContext(
        event_id=event_id,
        similarity=80.0,
        rationale="r",
        limitations=(),
        shared_causes=(),
        regime_caveats=(),
        evidence_refs=None,
        onset=HistoricalOnset(
            episode_id=uuid.uuid4(),
            name="1998 LTCM",
            episode_type="banking_crisis",
            onset_date=datetime.date(1998, 8, 1),
            onset_summary="s",
            geography="US",
            regime_tags=(),
            is_counterexample=False,
            source_refs=None,
            parent=None,
        ),
        outcome=HistoricalOutcomeContext(
            outcome_summary=None,
            outcomes=(),
            resolution_mechanism=None,
            peak_date=None,
            end_date=None,
        ),
    )


def _forecast_set(event_id: uuid.UUID) -> tuple[ForecastScenarioRow, ...]:
    set_id = uuid.uuid4()
    probs = {"base_case": 0.4, "upside_case": 0.3, "downside_case": 0.2, "tail_risk_case": 0.1}
    return tuple(
        ForecastScenarioRow(
            event_id=event_id,
            scenario_set_id=set_id,
            scenario_name=name,
            probability=prob,
            risk_score=60.0,
            severity="high",
            horizon="0_6m",
            confidence=0.7,
            evidence_refs=None,
            created_at=datetime.datetime(2026, 7, 14, tzinfo=UTC),
        )
        for name, prob in probs.items()
    )


class _FakeContextRepository:
    """Returns exactly the BoundedReads it is handed. Records no policy; that is the point."""

    def __init__(
        self,
        evidence: BoundedRead[EvidenceRow],
        analogies: BoundedRead[AnalogyContext],
        forecasts: BoundedRead[ForecastScenarioRow],
    ) -> None:
        self._evidence = evidence
        self._analogies = analogies
        self._forecasts = forecasts

    def evidence_for_events(self, event_ids):  # noqa: ANN001, ANN201 - test double
        return self._evidence

    def analogies_for_events(self, event_ids):  # noqa: ANN001, ANN201 - test double
        return self._analogies

    def forecasts_for_events(self, event_ids):  # noqa: ANN001, ANN201 - test double
        return self._forecasts


# --------------------------------------------------------------------------------------
# BoundedRead contract
# --------------------------------------------------------------------------------------


def test_bounded_read_over_read_flags_truncation_and_never_returns_more_than_limit() -> None:
    # limit + 1 fetched rows -> truncated, and the extra row is dropped.
    over = BoundedRead.of([1, 2, 3, 4], limit=3)
    assert over.truncated is True
    assert over.rows == (1, 2, 3)
    assert len(over.rows) == over.limit


def test_bounded_read_exactly_at_limit_is_not_truncated() -> None:
    # A scan of exactly `limit` rows is complete, not truncated -- truncation is not inferred
    # from len == limit.
    at = BoundedRead.of([1, 2, 3], limit=3)
    assert at.truncated is False
    assert at.rows == (1, 2, 3)


# --------------------------------------------------------------------------------------
# Global-scan truncation notes
# --------------------------------------------------------------------------------------


def test_no_scan_truncation_emits_no_scan_notes() -> None:
    event_id = uuid.uuid4()
    repo = _FakeContextRepository(
        evidence=BoundedRead.of([_evidence_row(event_id)], limit=2_000),
        analogies=BoundedRead.of([_analogy(event_id)], limit=200),
        forecasts=BoundedRead.of(list(_forecast_set(event_id)), limit=500),
    )
    context = build_brief_context(repo, [_selected(event_id)])
    codes = {note.code for note in context.data_quality_notes}
    assert DataQuality.EVIDENCE_SCAN_TRUNCATED not in codes
    assert DataQuality.ANALOGY_SCAN_TRUNCATED not in codes
    assert DataQuality.FORECAST_SCAN_TRUNCATED not in codes
    # The reductions still flowed through.
    assert context.evidence and context.analogies and context.forecasts


def test_evidence_scan_truncation_is_declared() -> None:
    event_id = uuid.uuid4()
    repo = _FakeContextRepository(
        evidence=BoundedRead(rows=(_evidence_row(event_id),), limit=2_000, truncated=True),
        analogies=BoundedRead.of([_analogy(event_id)], limit=200),
        forecasts=BoundedRead.of(list(_forecast_set(event_id)), limit=500),
    )
    context = build_brief_context(repo, [_selected(event_id)])
    note = next(
        n for n in context.data_quality_notes if n.code is DataQuality.EVIDENCE_SCAN_TRUNCATED
    )
    assert "2000-row bound" in note.detail


def test_analogy_scan_truncation_is_declared() -> None:
    event_id = uuid.uuid4()
    repo = _FakeContextRepository(
        evidence=BoundedRead.of([_evidence_row(event_id)], limit=2_000),
        analogies=BoundedRead(rows=(_analogy(event_id),), limit=200, truncated=True),
        forecasts=BoundedRead.of(list(_forecast_set(event_id)), limit=500),
    )
    context = build_brief_context(repo, [_selected(event_id)])
    assert any(
        n.code is DataQuality.ANALOGY_SCAN_TRUNCATED for n in context.data_quality_notes
    )


def test_forecast_scan_truncation_is_declared() -> None:
    event_id = uuid.uuid4()
    repo = _FakeContextRepository(
        evidence=BoundedRead.of([_evidence_row(event_id)], limit=2_000),
        analogies=BoundedRead.of([_analogy(event_id)], limit=200),
        forecasts=BoundedRead(rows=_forecast_set(event_id), limit=500, truncated=True),
    )
    context = build_brief_context(repo, [_selected(event_id)])
    assert any(
        n.code is DataQuality.FORECAST_SCAN_TRUNCATED for n in context.data_quality_notes
    )


def test_no_events_short_circuits_without_touching_the_repository() -> None:
    class _Exploding:
        def evidence_for_events(self, event_ids):  # noqa: ANN001, ANN201
            raise AssertionError("should not be called on a quiet day")

        def analogies_for_events(self, event_ids):  # noqa: ANN001, ANN201
            raise AssertionError("should not be called on a quiet day")

        def forecasts_for_events(self, event_ids):  # noqa: ANN001, ANN201
            raise AssertionError("should not be called on a quiet day")

    context = build_brief_context(_Exploding(), [])
    assert context.evidence == ()
    assert context.analogies == ()
    assert context.forecasts == ()
    assert context.data_quality_notes == ()
