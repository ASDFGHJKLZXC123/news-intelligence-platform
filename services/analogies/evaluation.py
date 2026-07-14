"""Offline evaluation of episode retrieval against the labelled gold set (ADR 0004, Stage 5).

This is the instrument, not the tuner. It measures what the shipped retrieval path actually does --
same embedding space, same hard filters, same 0.60 threshold, same k -- and reports it. It does not
adjust a weight, does not touch a threshold, and does not write a row: its database use is read-only
from the application's point of view, and it calls
:func:`services.analogies.retrieval.retrieve_analogy_candidates` directly, so a gold query is scored
without inventing an ``Event`` to hang it on.

Two properties are load-bearing and are tested:

* **The query carries no outcome.** A gold pair's text is built by
  :func:`services.nlp.embedding_text.build_event_embedding_text` from the title and the as-if-live
  summary. The labels -- which episode is correct, whether it was a counterexample -- are never in
  the embedded text. An evaluator that leaked them would be measuring itself.
* **Abstention is a result, not a failure.** "No reliable analogy" is an allowed answer, so it is
  counted and reported, never scored as a miss to be optimised away. The threshold diagnostics
  exist to make the 0.60 boundary *visible* to a later recalibration (Stage 9), which is where that
  decision belongs.

Embedding is batched at ADR 0004's 96-text cap, so a 40-pair gold set is one request.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from packages.providers.base import EmbeddingProvider
from packages.providers.openai_embeddings import MAX_EMBEDDING_BATCH_SIZE
from services.analogies.contracts import (
    DEFAULT_MIN_SIMILARITY,
    DEFAULT_TOP_K,
    AnalogyQuery,
    AnalogyRetrievalResult,
    RetrievalStatus,
)
from services.analogies.corpus import (
    CorpusValidationError,
    EpisodeCorpus,
    GoldPair,
    GoldSet,
    load_corpus_and_gold,
    quota_report,
)
from services.analogies.retrieval import retrieve_analogy_candidates
from services.nlp.embedding_text import build_event_embedding_text
from services.nlp.embeddings import embed_texts, resolve_embedding_identity

#: Ranks reported alongside k. Fixed, so two runs are comparable.
HIT_RATE_RANKS = (1, 3)
#: The unthresholded diagnostic pass. Not a tuning knob: it answers "where would the correct
#: episode have ranked if nothing were filtered out", which is what a recalibration needs to see.
DIAGNOSTIC_MIN_SIMILARITY = 0.0


def build_gold_query_text(pair: GoldPair) -> str:
    """The text embedded for one gold pair: the current event's title and as-if-live summary.

    Deliberately built from the same function the event-embedding path uses, and deliberately
    given no access to the pair's labels or to any episode's outcome.
    """
    return build_event_embedding_text(title=pair.title, summary=pair.summary)


@dataclass(frozen=True)
class GoldPairResult:
    """One scored pair. Everything a later threshold recalibration would want to look at."""

    pair_id: str
    episode_type: str
    status: RetrievalStatus
    ranked_episode_ids: tuple[uuid.UUID, ...]
    correct_episode_ids: tuple[uuid.UUID, ...]
    #: 1-based rank of the first correct episode among the returned matches; ``None`` if none was.
    hit_rank: int | None
    recall: float
    considered_count: int
    best_similarity: float | None
    #: Rank and similarity of the best correct episode *before* the threshold was applied. When
    #: ``hit_rank`` is None but this is not, the corpus held the right analogy and 0.60 hid it.
    diagnostic_rank: int | None
    correct_similarity: float | None

    @property
    def abstained(self) -> bool:
        return self.status is not RetrievalStatus.MATCHED

    @property
    def hit(self) -> bool:
        return self.hit_rank is not None

    @property
    def reciprocal_rank(self) -> float:
        return 0.0 if self.hit_rank is None else 1.0 / self.hit_rank

    def hit_at(self, rank: int) -> bool:
        return self.hit_rank is not None and self.hit_rank <= rank

    def as_dict(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "episode_type": self.episode_type,
            "status": str(self.status),
            "abstained": self.abstained,
            "hit": self.hit,
            "hit_rank": self.hit_rank,
            "reciprocal_rank": round(self.reciprocal_rank, 6),
            "recall": round(self.recall, 6),
            "considered_count": self.considered_count,
            "best_similarity": self.best_similarity,
            "diagnostic_rank": self.diagnostic_rank,
            "correct_similarity": self.correct_similarity,
            "ranked_episode_ids": [str(value) for value in self.ranked_episode_ids],
            "correct_episode_ids": [str(value) for value in sorted(self.correct_episode_ids)],
        }


@dataclass(frozen=True)
class TypeMetrics:
    episode_type: str
    pairs: int
    hits: int
    abstentions: int

    @property
    def hit_rate(self) -> float:
        return self.hits / self.pairs if self.pairs else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode_type": self.episode_type,
            "pairs": self.pairs,
            "hits": self.hits,
            "abstentions": self.abstentions,
            "hit_rate": round(self.hit_rate, 6),
        }


@dataclass(frozen=True)
class ThresholdDiagnostics:
    """How the fixed 0.60 threshold behaved. Reported; never adjusted here."""

    min_similarity: float
    abstentions: int
    #: Pairs where the correct episode was retrieved and cleared the threshold.
    correct_above_threshold: int
    #: Pairs where the correct episode was in the top k but scored below the threshold. This is the
    #: number a recalibration is actually about.
    correct_below_threshold: int
    #: Pairs where the correct episode was not in the top k at all -- a retrieval miss, not a
    #: threshold miss. Lowering the threshold would not fix these.
    correct_absent: int
    mean_best_similarity: float | None
    mean_correct_similarity: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_similarity": self.min_similarity,
            "abstentions": self.abstentions,
            "correct_above_threshold": self.correct_above_threshold,
            "correct_below_threshold": self.correct_below_threshold,
            "correct_absent": self.correct_absent,
            "mean_best_similarity": self.mean_best_similarity,
            "mean_correct_similarity": self.mean_correct_similarity,
        }


@dataclass(frozen=True)
class EvaluationReport:
    """The whole run, deterministically ordered and JSON-serializable."""

    model: str
    model_version: str
    top_k: int
    min_similarity: float
    results: tuple[GoldPairResult, ...]
    per_type: tuple[TypeMetrics, ...]
    threshold: ThresholdDiagnostics
    embedding_requests: tuple[int, ...]

    @property
    def pairs(self) -> int:
        return len(self.results)

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.reciprocal_rank for result in self.results) / len(self.results)

    @property
    def recall_at_k(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.recall for result in self.results) / len(self.results)

    @property
    def no_reliable_analogy(self) -> int:
        return sum(1 for result in self.results if result.abstained)

    def hit_at(self, rank: int) -> float:
        if not self.results:
            return 0.0
        return sum(1 for result in self.results if result.hit_at(rank)) / len(self.results)

    def as_dict(self) -> dict[str, Any]:
        pairs = self.pairs
        return {
            "model": self.model,
            "model_version": self.model_version,
            "top_k": self.top_k,
            "min_similarity": self.min_similarity,
            "pairs": pairs,
            "embedding_requests": list(self.embedding_requests),
            "metrics": {
                **{f"hit_at_{rank}": round(self.hit_at(rank), 6) for rank in HIT_RATE_RANKS},
                f"hit_at_{self.top_k}": round(self.hit_at(self.top_k), 6),
                f"recall_at_{self.top_k}": round(self.recall_at_k, 6),
                "mrr": round(self.mrr, 6),
                "no_reliable_analogy": self.no_reliable_analogy,
                "no_reliable_analogy_rate": round(
                    self.no_reliable_analogy / pairs if pairs else 0.0, 6
                ),
            },
            "threshold_diagnostics": self.threshold.as_dict(),
            "per_type": [metrics.as_dict() for metrics in self.per_type],
            "results": [result.as_dict() for result in self.results],
        }


def _query(pair: GoldPair, vector: Sequence[float], *, model: str, model_version: str,
           top_k: int, min_similarity: float) -> AnalogyQuery:
    return AnalogyQuery(
        vector=tuple(float(value) for value in vector),
        episode_types=frozenset(pair.episode_types),
        model=model,
        model_version=model_version,
        regime_tags=frozenset(pair.regime_tags),
        geographies=None if pair.geographies is None else frozenset(pair.geographies),
        industries=None if pair.industries is None else frozenset(pair.industries),
        top_k=top_k,
        min_similarity=min_similarity,
    )


def _first_correct(
    result: AnalogyRetrievalResult, correct: frozenset[uuid.UUID]
) -> tuple[int | None, float | None]:
    """1-based rank and similarity of the first correct episode among the matches."""
    for rank, match in enumerate(result.matches, start=1):
        if match.candidate.episode_id in correct:
            return rank, match.candidate.similarity
    return None, None


def evaluate_pair(
    session: Session,
    pair: GoldPair,
    vector: Sequence[float],
    corpus: EpisodeCorpus,
    *,
    model: str,
    model_version: str,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> GoldPairResult:
    """Score one pair against the live retrieval core. Reads; never writes."""
    correct = pair.correct_episode_ids
    scored = retrieve_analogy_candidates(
        session,
        _query(
            pair,
            vector,
            model=model,
            model_version=model_version,
            top_k=top_k,
            min_similarity=min_similarity,
        ),
    )
    hit_rank, _ = _first_correct(scored, correct)
    ranked = tuple(match.candidate.episode_id for match in scored.matches)
    found = sum(1 for episode_id in correct if episode_id in set(ranked))

    # The same neighbours with the threshold taken off: it separates "the corpus does not hold
    # this analogy" from "it does, and 0.60 rejected it". Nothing is tuned on the answer.
    unthresholded = retrieve_analogy_candidates(
        session,
        _query(
            pair,
            vector,
            model=model,
            model_version=model_version,
            top_k=top_k,
            min_similarity=DIAGNOSTIC_MIN_SIMILARITY,
        ),
    )
    diagnostic_rank, correct_similarity = _first_correct(unthresholded, correct)

    return GoldPairResult(
        pair_id=pair.pair_id,
        episode_type=corpus.by_id[pair.expected_episode_ids[0]].episode_type,
        status=scored.status,
        ranked_episode_ids=ranked,
        correct_episode_ids=tuple(sorted(correct)),
        hit_rank=hit_rank,
        recall=found / len(correct) if correct else 0.0,
        considered_count=scored.considered_count,
        best_similarity=scored.best_similarity,
        diagnostic_rank=diagnostic_rank,
        correct_similarity=correct_similarity,
    )


def _per_type(results: Sequence[GoldPairResult]) -> tuple[TypeMetrics, ...]:
    pairs = Counter(result.episode_type for result in results)
    hits = Counter(result.episode_type for result in results if result.hit)
    abstentions = Counter(result.episode_type for result in results if result.abstained)
    return tuple(
        TypeMetrics(
            episode_type=episode_type,
            pairs=count,
            hits=hits[episode_type],
            abstentions=abstentions[episode_type],
        )
        for episode_type, count in sorted(pairs.items())
    )


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _threshold_diagnostics(
    results: Sequence[GoldPairResult], min_similarity: float
) -> ThresholdDiagnostics:
    below = [
        result
        for result in results
        if result.hit_rank is None and result.diagnostic_rank is not None
    ]
    return ThresholdDiagnostics(
        min_similarity=min_similarity,
        abstentions=sum(1 for result in results if result.abstained),
        correct_above_threshold=sum(1 for result in results if result.hit),
        correct_below_threshold=len(below),
        correct_absent=sum(
            1 for result in results if result.hit_rank is None and result.diagnostic_rank is None
        ),
        mean_best_similarity=_mean(
            [r.best_similarity for r in results if r.best_similarity is not None]
        ),
        mean_correct_similarity=_mean(
            [r.correct_similarity for r in results if r.correct_similarity is not None]
        ),
    )


def evaluate_gold_set(
    session: Session,
    provider: EmbeddingProvider,
    corpus: EpisodeCorpus,
    gold: GoldSet,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
    batch_size: int = MAX_EMBEDDING_BATCH_SIZE,
) -> EvaluationReport:
    """Embed every gold query once, score them all, and report. No writes, no rerank, no tuning."""
    model, model_version = resolve_embedding_identity(
        provider, embedding_model, embedding_model_version
    )
    pairs = tuple(sorted(gold.pairs, key=lambda pair: pair.pair_id))
    texts = [build_gold_query_text(pair) for pair in pairs]
    embeddings = embed_texts(
        provider, texts, model=model, model_version=model_version, batch_size=batch_size
    )
    requests = tuple(
        min(batch_size, len(texts) - start) for start in range(0, len(texts), batch_size)
    )

    results = tuple(
        evaluate_pair(
            session,
            pair,
            embedding.vector,
            corpus,
            model=model,
            model_version=model_version,
            top_k=top_k,
            min_similarity=min_similarity,
        )
        for pair, embedding in zip(pairs, embeddings, strict=True)
    )
    return EvaluationReport(
        model=model,
        model_version=model_version,
        top_k=top_k,
        min_similarity=min_similarity,
        results=results,
        per_type=_per_type(results),
        threshold=_threshold_diagnostics(results, min_similarity),
        embedding_requests=requests,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: validate the data offline, or run the full evaluation against the seeded corpus."""
    from packages.config.logging import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(
        description="Evaluate historical-episode retrieval against the labelled gold set."
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="load and validate the corpus and gold set offline; no database, no API key",
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=DEFAULT_MIN_SIMILARITY,
        help="the shipped threshold; overriding it here reports, it does not tune",
    )
    args = parser.parse_args(argv)

    try:
        corpus, gold = load_corpus_and_gold()
    except CorpusValidationError as exc:
        print(f"corpus validation failed:\n{exc}", file=sys.stderr)
        return 1

    if args.validate_only:
        print(json.dumps(quota_report(corpus, gold), indent=2, sort_keys=True))
        return 0

    # Imported here so `--validate-only` needs no database engine and no settings for one.
    from db.base import SessionLocal
    from db.seed.seed import build_provider

    provider = build_provider()
    session = SessionLocal()
    try:
        report = evaluate_gold_set(
            session,
            provider,
            corpus,
            gold,
            top_k=args.top_k,
            min_similarity=args.min_similarity,
        )
    finally:
        # Read-only by contract: roll back rather than commit, whatever happened.
        session.rollback()
        session.close()
        provider.close()

    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DIAGNOSTIC_MIN_SIMILARITY",
    "HIT_RATE_RANKS",
    "EvaluationReport",
    "GoldPairResult",
    "ThresholdDiagnostics",
    "TypeMetrics",
    "build_gold_query_text",
    "evaluate_gold_set",
    "evaluate_pair",
]
