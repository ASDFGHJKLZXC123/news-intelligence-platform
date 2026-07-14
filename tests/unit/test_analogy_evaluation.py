"""The gold-set evaluator: metrics, batching, abstention, and what it is forbidden to do.

No database and no network. The session is the same recording fake the retrieval tests use -- it
answers each statement by the columns it selects -- so the evaluator runs against the real retrieval
core with real thresholds, and the ranking it is scored on is whatever this test puts in front of
it.

The two properties that would invalidate every number if they broke are asserted directly: the
embedded query text is built from the current event alone (no label, no outcome, no episode name),
and the evaluator writes nothing.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from db.models import EMBEDDING_DIM
from packages.providers.base import EmbeddingResult
from services.analogies.contracts import DEFAULT_MIN_SIMILARITY, RetrievalStatus
from services.analogies.corpus import CuratedEpisode, GoldPair, GoldSet, load_corpus
from services.analogies.evaluation import (
    build_gold_query_text,
    evaluate_gold_set,
    evaluate_pair,
)

MODEL = "text-embedding-3-small"
VERSION = "current"
VECTOR = (0.1,) * EMBEDDING_DIM


class RecordingProvider:
    dimension = EMBEDDING_DIM
    model_name = MODEL
    model_version = VERSION

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def request_sizes(self) -> list[int]:
        return [len(call) for call in self.calls]

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        self.calls.append(list(texts))
        return [
            EmbeddingResult(
                vector=(0.25,) * self.dimension,
                provider_name="fake",
                model_name=self.model_name,
                model_version=self.model_version,
                dimension=self.dimension,
                model_run_id="run",
            )
            for _ in texts
        ]


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Answers the ranking statement with the programmed rows; refuses to be written to."""

    def __init__(self, ranking: list[Any]) -> None:
        self.ranking = ranking
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        keys = set(statement.selected_columns.keys())
        if "distance" in keys:
            return FakeResult(self.ranking)
        return FakeResult([])

    def add(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the evaluator must not write")

    def commit(self) -> None:
        raise AssertionError("the evaluator must not commit")

    def flush(self) -> None:
        raise AssertionError("the evaluator must not flush")


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def _row(episode: CuratedEpisode, distance: float) -> SimpleNamespace:
    """A ranking row exactly as the retrieval statement projects it (no outcome columns)."""
    return SimpleNamespace(
        id=episode.episode_id,
        name=episode.name,
        episode_type=episode.episode_type,
        onset_date=episode.onset_date,
        peak_date=episode.peak_date,
        end_date=episode.end_date,
        onset_summary=episode.onset_summary,
        onset_indicators=episode.onset_indicators,
        geography=episode.geography,
        affected_industries=list(episode.affected_industries),
        regime_tags=list(episode.regime_tags),
        is_counterexample=episode.is_counterexample,
        source_refs=episode.source_refs,
        parent_episode_id=None,
        distance=distance,
    )


def _pair(
    corpus,
    pair_id: str,
    expected: str,
    *,
    acceptable: tuple[str, ...] = (),
    episode_types: tuple[str, ...] = ("banking_stress",),
) -> GoldPair:
    return GoldPair(
        pair_id=pair_id,
        title="Regional lender sells its bond book at a loss",
        summary="Uninsured deposits are concentrated and the securities book is underwater.",
        as_of=datetime.date(2026, 2, 11),
        episode_types=episode_types,
        regime_tags=("post_qe",),
        geographies=None,
        industries=None,
        expected_episode_ids=(corpus.by_slug[expected].episode_id,),
        acceptable_episode_ids=tuple(corpus.by_slug[slug].episode_id for slug in acceptable),
        expects_counterexample=False,
        notes="curation note that must never reach the embedding",
    )


def _evaluate(corpus, session: FakeSession, pairs: tuple[GoldPair, ...], **kwargs):
    return evaluate_gold_set(
        session, RecordingProvider(), corpus, GoldSet(pairs=pairs), **kwargs
    )


# --- the query text ------------------------------------------------------------------------
def test_query_text_is_the_current_event_and_nothing_else(corpus):
    """Structural proof: the labels are not inputs to the text, so they cannot leak into it."""
    pair = _pair(corpus, "gold-x", "svb-deposit-run-2023")
    text = build_gold_query_text(pair)

    relabelled = dataclasses.replace(
        pair,
        notes="the bank failed and depositors were made whole",
        expects_counterexample=True,
        expected_episode_ids=(corpus.by_slug["us-repo-market-stress-2019"].episode_id,),
    )
    assert build_gold_query_text(relabelled) == text

    svb = corpus.by_slug["svb-deposit-run-2023"]
    assert svb.outcome_summary not in text
    assert svb.resolution_mechanism not in text
    assert pair.notes not in text
    assert text.startswith(pair.title)


# --- metrics -------------------------------------------------------------------------------
def test_hit_rank_recall_and_reciprocal_rank(corpus):
    svb = corpus.by_slug["svb-deposit-run-2023"]
    first_republic = corpus.by_slug["first-republic-deposit-flight-2023"]
    signature = corpus.by_slug["signature-bank-closure-2023"]
    # Ranked: signature (0.90), svb (0.80), first_republic (0.70).
    session = FakeSession(
        [_row(signature, 0.10), _row(svb, 0.20), _row(first_republic, 0.30)]
    )
    pair = _pair(
        corpus, "gold-1", "svb-deposit-run-2023", acceptable=("first-republic-deposit-flight-2023",)
    )

    result = evaluate_pair(
        session, pair, VECTOR, corpus, model=MODEL, model_version=VERSION
    )

    assert result.status is RetrievalStatus.MATCHED
    assert result.hit_rank == 2
    assert result.reciprocal_rank == 0.5
    # Both the primary and the alternative were retrieved, so recall over the correct set is 1.0.
    assert result.recall == 1.0
    assert result.best_similarity == 0.9
    assert result.episode_type == "banking_stress"
    assert result.hit_at(3) and not result.hit_at(1)


def test_a_wrong_id_is_a_miss_not_a_hit(corpus):
    session = FakeSession([_row(corpus.by_slug["signature-bank-closure-2023"], 0.10)])
    pair = _pair(corpus, "gold-2", "svb-deposit-run-2023")

    result = evaluate_pair(session, pair, VECTOR, corpus, model=MODEL, model_version=VERSION)

    assert result.hit_rank is None
    assert result.recall == 0.0
    assert result.reciprocal_rank == 0.0
    # Not a threshold problem: the right episode was not in the top k at all.
    assert result.diagnostic_rank is None
    assert result.correct_similarity is None


def test_abstention_is_reported_as_an_answer_not_an_error(corpus):
    """Below 0.60 the system says "no reliable analogy" -- the spec's explicit, allowed answer."""
    svb = corpus.by_slug["svb-deposit-run-2023"]
    session = FakeSession([_row(svb, 0.55)])  # similarity 0.45, below the 0.60 threshold
    pair = _pair(corpus, "gold-3", "svb-deposit-run-2023")

    result = evaluate_pair(session, pair, VECTOR, corpus, model=MODEL, model_version=VERSION)

    assert result.status is RetrievalStatus.NO_RELIABLE_ANALOGY
    assert result.abstained
    assert result.hit_rank is None
    assert result.ranked_episode_ids == ()
    # The diagnostic pass shows the corpus *did* hold the right analogy; the threshold hid it.
    assert result.diagnostic_rank == 1
    assert result.correct_similarity == pytest.approx(0.45)


def test_report_aggregates_hit_rates_mrr_abstentions_and_threshold_diagnostics(corpus):
    svb = corpus.by_slug["svb-deposit-run-2023"]
    signature = corpus.by_slug["signature-bank-closure-2023"]
    repo = corpus.by_slug["us-repo-market-stress-2019"]

    session = FakeSession([_row(svb, 0.10), _row(signature, 0.20), _row(repo, 0.55)])
    pairs = (
        _pair(corpus, "gold-1", "svb-deposit-run-2023"),  # rank 1
        _pair(corpus, "gold-2", "signature-bank-closure-2023"),  # rank 2
        _pair(corpus, "gold-3", "us-repo-market-stress-2019"),  # below threshold -> not returned
    )

    report = _evaluate(corpus, session, pairs)

    assert report.pairs == 3
    assert report.min_similarity == DEFAULT_MIN_SIMILARITY == 0.60
    assert report.hit_at(1) == pytest.approx(1 / 3)
    assert report.hit_at(3) == pytest.approx(2 / 3)
    assert report.mrr == pytest.approx((1.0 + 0.5 + 0.0) / 3)
    assert report.recall_at_k == pytest.approx(2 / 3)
    # Every pair matched *something* above threshold, so nothing abstained; but one pair's correct
    # episode sits below 0.60, which is exactly what a recalibration needs to see.
    assert report.no_reliable_analogy == 0
    assert report.threshold.correct_above_threshold == 2
    assert report.threshold.correct_below_threshold == 1
    assert report.threshold.correct_absent == 0
    assert report.threshold.min_similarity == 0.60

    per_type = {metrics.episode_type: metrics for metrics in report.per_type}
    assert per_type["banking_stress"].pairs == 3
    assert per_type["banking_stress"].hits == 2


def test_every_gold_query_is_embedded_once_in_chunks_of_at_most_96(corpus):
    session = FakeSession([_row(corpus.by_slug["svb-deposit-run-2023"], 0.10)])
    pairs = tuple(
        _pair(corpus, f"gold-{index:03d}", "svb-deposit-run-2023") for index in range(100)
    )

    provider = RecordingProvider()
    report = evaluate_gold_set(session, provider, corpus, GoldSet(pairs=pairs))

    assert provider.request_sizes == [96, 4]
    assert report.embedding_requests == (96, 4)
    assert report.pairs == 100


def test_the_evaluator_reads_and_never_writes(corpus):
    session = FakeSession([_row(corpus.by_slug["svb-deposit-run-2023"], 0.10)])

    report = _evaluate(corpus, session, (_pair(corpus, "gold-1", "svb-deposit-run-2023"),))

    assert report.results[0].hit
    # Only SELECTs reached the session; add/commit/flush raise if the evaluator ever calls them.
    assert session.statements
    assert all(statement.is_select for statement in session.statements)


def test_the_report_is_deterministic_and_json_serializable(corpus):
    svb = corpus.by_slug["svb-deposit-run-2023"]
    session = FakeSession([_row(svb, 0.10)])
    pairs = (
        _pair(corpus, "gold-2", "svb-deposit-run-2023"),
        _pair(corpus, "gold-1", "svb-deposit-run-2023"),
    )

    first = _evaluate(corpus, session, pairs).as_dict()
    second = _evaluate(corpus, FakeSession([_row(svb, 0.10)]), pairs).as_dict()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    # Results are ordered by pair id, not by however the caller happened to list them.
    assert [result["pair_id"] for result in first["results"]] == ["gold-1", "gold-2"]
    assert first["metrics"]["hit_at_1"] == 1.0
    assert first["model"] == MODEL


def test_a_pair_whose_family_excludes_the_corpus_returns_no_analogy(corpus):
    """The hard filter runs before similarity: an empty family search abstains, it does not guess."""
    session = FakeSession([])
    pair = _pair(
        corpus, "gold-1", "sars-outbreak-2003", episode_types=("pandemic",)
    )

    result = evaluate_pair(session, pair, VECTOR, corpus, model=MODEL, model_version=VERSION)

    assert result.status is RetrievalStatus.NO_RELIABLE_ANALOGY
    assert result.considered_count == 0
    assert result.hit_rank is None
    assert uuid.UUID(str(result.correct_episode_ids[0])) in corpus.by_id
