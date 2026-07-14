"""Durable ``event_analogies`` rows: pure mapping, and set reconciliation against a fake session.

Reconciliation is the property that matters here. An event's analogies are a set, so a rerun must
leave exactly what the latest run chose -- updating what it kept, inserting what it added, and
deleting what it dropped, including deleting *everything* when the honest answer became "no
reliable analogy". These tests pin all four transitions and the fact that none of them commits.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from db.models.core import EventAnalogy
from services.analogies.contracts import EpisodeCandidate
from services.analogies.persistence import (
    AnalogyPersistenceError,
    EventAnalogyRow,
    build_analogy_rows,
    reconcile_event_analogies,
)
from services.analogies.rerank import RerankedCandidate

EVENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
SVB_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTINENTAL_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
PARENT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")

MODEL = "text-embedding-3-small"
VERSION = "current"
CURRENT_REGIME = ("post_QE", "post_dodd_frank")


class FakeSession:
    """Only what reconciliation calls. It cannot commit, which is itself the assertion."""

    def __init__(self, existing: list[EventAnalogy] | None = None) -> None:
        self.existing = existing or []
        self.added: list[EventAnalogy] = []
        self.deleted: list[EventAnalogy] = []
        self.flushes = 0
        self.commits = 0

    def execute(self, _statement: Any) -> Any:
        rows = self.existing

        class _Result:
            def scalars(self) -> list[EventAnalogy]:
                return rows

        return _Result()

    def add(self, row: EventAnalogy) -> None:
        self.added.append(row)

    def delete(self, row: EventAnalogy) -> None:
        self.deleted.append(row)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:  # pragma: no cover - a call here is a bug, and would fail the test
        self.commits += 1
        raise AssertionError("a repository method must never commit; the worker owns the transaction")


def _candidate(
    episode_id: uuid.UUID,
    *,
    similarity: float = 0.82,
    regime_tags: tuple[str, ...] = CURRENT_REGIME,
    caveat_reasons: tuple[str, ...] = (),
    is_counterexample: bool = False,
) -> EpisodeCandidate:
    return EpisodeCandidate(
        episode_id=episode_id,
        name="2023 regional banking stress",
        episode_type="banking_stress",
        onset_date=datetime.date(2023, 3, 8),
        peak_date=None,
        end_date=None,
        onset_summary="Concentrated uninsured deposits face rapid withdrawals.",
        onset_indicators={"uninsured_deposit_pct": 94},
        geography="United States",
        affected_industries=("banking",),
        regime_tags=regime_tags,
        is_counterexample=is_counterexample,
        source_refs={"refs": ["fdic"]},
        parent_episode_id=PARENT_ID,
        similarity=similarity,
        regime_caveats_required=bool(caveat_reasons),
        regime_caveat_reasons=caveat_reasons,
    )


def _selection(
    episode_id: uuid.UUID = SVB_ID,
    *,
    score: float = 87.5,
    caveats: tuple[str, ...] = (),
    candidate: EpisodeCandidate | None = None,
) -> RerankedCandidate:
    return RerankedCandidate(
        candidate=candidate or _candidate(episode_id),
        similarity_score=score,
        explanation="Same deposit-run mechanism.",
        regime_caveats=caveats,
        confidence=0.7,
    )


def _rows(*selections: RerankedCandidate) -> tuple[EventAnalogyRow, ...]:
    return build_analogy_rows(
        selections,
        llm_run_id=RUN_ID,
        trace_id="trace-1",
        model=MODEL,
        model_version=VERSION,
        current_regime_tags=CURRENT_REGIME,
    )


def _existing(episode_id: uuid.UUID, *, score: float = 10.0) -> EventAnalogy:
    return EventAnalogy(
        event_id=EVENT_ID,
        historical_episode_id=episode_id,
        similarity_score=score,
        rationale="an earlier run's rationale",
        regime_caveats=[],
        limitations=[],
        shared_causes=[],
        evidence_refs={},
    )


# --- the pure mapping ------------------------------------------------------------------
def test_a_selection_maps_onto_the_durable_columns() -> None:
    row = _rows(_selection(caveats=("Pre-QE: no backstop.",)))[0]

    assert row.episode_id == SVB_ID
    assert row.similarity_score == 87.5  # the contract's 0-100 scale, on the 0-100 column
    assert row.rationale == "Same deposit-run mechanism."
    assert row.regime_caveats == ("Pre-QE: no backstop.",)
    assert row.llm_run_id == RUN_ID


def test_the_vector_similarity_rides_in_evidence_refs_not_on_the_score_column() -> None:
    row = _rows(_selection(score=95.0, candidate=_candidate(SVB_ID, similarity=0.61)))[0]

    assert row.similarity_score == 95.0
    assert row.evidence_refs["vector_similarity"] == 0.61
    assert row.evidence_refs["embedding_model"] == MODEL
    assert row.evidence_refs["embedding_model_version"] == VERSION
    assert row.evidence_refs["llm_confidence"] == 0.7
    assert row.evidence_refs["trace_id"] == "trace-1"
    assert row.evidence_refs["episode_source_refs"] == {"refs": ["fdic"]}


def test_limitations_are_deterministic_and_never_model_authored() -> None:
    candidate = _candidate(
        SVB_ID,
        regime_tags=("pre_QE",),
        caveat_reasons=("episode shares none of the current regime tags",),
        is_counterexample=True,
    )
    row = _rows(_selection(caveats=("model caveat",), candidate=candidate))[0]

    assert row.limitations == (
        "episode shares none of the current regime tags",
        "episode is a curated counterexample: a near-miss that resolved benignly",
    )
    # The model's own caveats stay on their own column, never merged into the derived list.
    assert row.regime_caveats == ("model caveat",)


def test_shared_causes_are_the_structural_overlap_the_two_rows_actually_have() -> None:
    row = _rows(_selection(candidate=_candidate(SVB_ID, regime_tags=("post_QE", "pre_fiat"))))[0]

    # Tags are compared in item 2's normalized (case-folded) vocabulary form, and are stored the
    # way they were compared -- `pre_fiat` is not shared with the current regime, so it is absent.
    assert row.shared_causes == ("episode_type:banking_stress", "regime:post_qe")


@pytest.mark.parametrize("score", [-0.01, 100.01, 101.0])
def test_a_score_off_the_contract_scale_is_refused_before_it_reaches_the_table(
    score: float,
) -> None:
    with pytest.raises(AnalogyPersistenceError, match="0-100"):
        _rows(_selection(score=score))


@pytest.mark.parametrize("score", [0.0, 100.0])
def test_the_score_bounds_themselves_are_accepted(score: float) -> None:
    assert _rows(_selection(score=score))[0].similarity_score == score


# --- reconciliation --------------------------------------------------------------------
def test_a_first_run_inserts_the_selected_pairs() -> None:
    session = FakeSession()

    outcome = reconcile_event_analogies(session, EVENT_ID, _rows(_selection()))

    assert (outcome.inserted, outcome.updated, outcome.removed) == (1, 0, 0)
    assert session.added[0].event_id == EVENT_ID
    assert session.added[0].historical_episode_id == SVB_ID
    assert session.added[0].similarity_score == 87.5
    assert session.flushes == 1
    assert session.commits == 0


def test_a_rerun_updates_the_pair_it_kept_rather_than_duplicating_it() -> None:
    """The unique (event, episode) constraint is respected by construction, not by luck."""
    session = FakeSession(existing=[_existing(SVB_ID, score=10.0)])

    outcome = reconcile_event_analogies(session, EVENT_ID, _rows(_selection(score=91.0)))

    assert (outcome.inserted, outcome.updated, outcome.removed) == (0, 1, 0)
    assert session.added == []
    assert session.existing[0].similarity_score == 91.0
    assert session.existing[0].rationale == "Same deposit-run mechanism."
    assert session.existing[0].llm_run_id == RUN_ID


def test_a_rerun_removes_the_pairs_it_no_longer_selects() -> None:
    stale = _existing(CONTINENTAL_ID)
    session = FakeSession(existing=[_existing(SVB_ID), stale])

    outcome = reconcile_event_analogies(session, EVENT_ID, _rows(_selection(SVB_ID)))

    assert (outcome.inserted, outcome.updated, outcome.removed) == (0, 1, 1)
    assert session.deleted == [stale]


def test_a_legitimate_no_match_clears_the_event_so_nothing_stale_is_served() -> None:
    previous = [_existing(SVB_ID), _existing(CONTINENTAL_ID)]
    session = FakeSession(existing=list(previous))

    outcome = reconcile_event_analogies(session, EVENT_ID, ())

    assert (outcome.inserted, outcome.updated, outcome.removed) == (0, 0, 2)
    assert session.deleted == previous
    assert session.flushes == 1


def test_the_same_episode_twice_in_one_set_is_refused() -> None:
    session = FakeSession()

    with pytest.raises(AnalogyPersistenceError, match="appears twice"):
        reconcile_event_analogies(session, EVENT_ID, _rows(_selection(), _selection()))


def test_reconciliation_never_commits() -> None:
    """The worker owns the transaction; a commit in here would strand a half-written rerun."""
    session = FakeSession(existing=[_existing(SVB_ID)])

    reconcile_event_analogies(session, EVENT_ID, _rows(_selection(CONTINENTAL_ID)))

    assert session.commits == 0
    assert session.flushes == 1
