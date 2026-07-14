"""The labelled retrieval gold set, and the validator that guards it (no database, no network).

The gold set is the only thing standing between "we picked 0.60 because it sounded right" and a
calibrated threshold, so its own integrity has to be checked mechanically: every expected episode
must exist, must be reachable (retrieval never ranks a parent arc), and must live in a family the
pair actually searches. And the query text must not leak the answer -- a gold query that mentions
how the episode ended would be scoring the retriever on a question no live event can ask.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from db.models.core import EPISODE_TYPES
from services.analogies.corpus import (
    GOLD_MAX,
    GOLD_MIN,
    GOLD_PATH,
    NON_US_EXCLUDED,
    CorpusValidationError,
    find_future_years,
    find_hindsight_phrases,
    load_corpus,
    load_gold_set,
)
from services.analogies.evaluation import build_gold_query_text
from services.nlp.embedding_text import build_event_embedding_text

Mutation = Callable[[dict[str, Any]], None]


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture(scope="module")
def gold(corpus):
    return load_gold_set(corpus)


def _clone(tmp_path: Path, mutate: Mutation) -> Path:
    payload = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    mutate(payload)
    path = tmp_path / "analogy_gold.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _problems(corpus, tmp_path: Path, mutate: Mutation) -> tuple[str, ...]:
    with pytest.raises(CorpusValidationError) as excinfo:
        load_gold_set(corpus, _clone(tmp_path, mutate))
    return excinfo.value.problems


# --- the committed gold set ----------------------------------------------------------------
def test_gold_set_holds_the_committed_pair_count(gold):
    assert len(gold.pairs) == 40
    assert GOLD_MIN <= len(gold.pairs) <= GOLD_MAX


def test_pair_ids_are_unique(gold):
    assert len({pair.pair_id for pair in gold.pairs}) == len(gold.pairs)


def test_gold_set_covers_every_episode_type(gold):
    assert gold.covered_types == set(EPISODE_TYPES)


def test_gold_set_covers_crises_counterexamples_us_and_non_us(corpus, gold):
    counterexamples = [pair for pair in gold.pairs if pair.expects_counterexample]
    crises = [pair for pair in gold.pairs if not pair.expects_counterexample]
    assert len(counterexamples) >= 10
    assert len(crises) >= 10

    target_ids = {i for pair in gold.pairs for i in pair.correct_episode_ids}
    targets = [corpus.by_id[i] for i in target_ids]
    assert any(episode.geography in NON_US_EXCLUDED for episode in targets)
    assert len({e.geography for e in targets if e.geography not in NON_US_EXCLUDED}) >= 10
    # The labels agree with the corpus: a pair that expects a near-miss points at a near-miss.
    for pair in counterexamples:
        expected = [corpus.by_id[i] for i in pair.expected_episode_ids]
        assert any(episode.is_counterexample for episode in expected), pair.pair_id


def test_expected_episodes_exist_are_leaves_and_match_the_pair_family(corpus, gold):
    parents = corpus.parent_ids
    for pair in gold.pairs:
        assert pair.expected_episode_ids
        for episode_id in pair.correct_episode_ids:
            episode = corpus.by_id[episode_id]
            # A parent arc can never be returned by retrieval, so it can never be a right answer.
            assert episode_id not in parents, pair.pair_id
            assert episode.episode_type in pair.episode_types, pair.pair_id


def test_primary_and_alternative_targets_are_distinct(gold):
    for pair in gold.pairs:
        assert not set(pair.expected_episode_ids) & set(pair.acceptable_episode_ids)
        assert pair.correct_episode_ids == frozenset(
            pair.expected_episode_ids
        ) | frozenset(pair.acceptable_episode_ids)


def test_query_text_is_built_only_from_the_as_if_live_event(corpus, gold):
    """The embedded query holds the title and summary, and nothing that reveals the answer."""
    for pair in gold.pairs:
        text = build_gold_query_text(pair)
        assert text == build_event_embedding_text(title=pair.title, summary=pair.summary)
        assert pair.notes not in text
        assert find_hindsight_phrases(text) == (), pair.pair_id
        assert find_future_years(text, pair.as_of.year) == (), pair.pair_id
        for episode_id in pair.correct_episode_ids:
            episode = corpus.by_id[episode_id]
            assert episode.outcome_summary not in text, pair.pair_id
            assert episode.resolution_mechanism not in text, pair.pair_id
            assert episode.name.lower() not in text.lower(), pair.pair_id


# --- the validator -------------------------------------------------------------------------
def test_a_gold_reference_to_an_unknown_episode_is_rejected(corpus, tmp_path):
    unknown = str(uuid.uuid4())

    def mutate(payload: dict[str, Any]) -> None:
        payload["pairs"][0]["expected_episode_ids"] = [unknown]

    problems = _problems(corpus, tmp_path, mutate)
    assert any(f"{unknown} is not in the corpus" in problem for problem in problems)


def test_a_gold_reference_to_a_parent_arc_is_rejected(corpus, tmp_path):
    arc = next(iter(corpus.parent_ids))

    def mutate(payload: dict[str, Any]) -> None:
        payload["pairs"][0]["expected_episode_ids"] = [str(arc)]

    problems = _problems(corpus, tmp_path, mutate)
    assert any("is a parent arc and is never ranked" in problem for problem in problems)


def test_a_family_incompatible_target_is_rejected(corpus, tmp_path):
    def mutate(payload: dict[str, Any]) -> None:
        # gold-001 searches banking_stress; point it at a pandemic episode.
        payload["pairs"][0]["expected_episode_ids"] = [
            str(corpus.by_slug["sars-outbreak-2003"].episode_id)
        ]

    problems = _problems(corpus, tmp_path, mutate)
    assert any("which the pair's filter" in problem for problem in problems)


def test_outcome_leakage_in_the_query_text_is_rejected(corpus, tmp_path):
    def mutate(payload: dict[str, Any]) -> None:
        payload["pairs"][0]["summary"] += (
            " The bank would later fail and depositors were eventually made whole."
        )

    problems = _problems(corpus, tmp_path, mutate)
    assert any("leaks the outcome" in problem for problem in problems)


def test_duplicate_pair_ids_are_rejected(corpus, tmp_path):
    def mutate(payload: dict[str, Any]) -> None:
        payload["pairs"][1]["pair_id"] = payload["pairs"][0]["pair_id"]

    assert any("duplicate pair id" in problem for problem in _problems(corpus, tmp_path, mutate))


def test_a_pair_with_no_expected_episode_is_rejected(corpus, tmp_path):
    def mutate(payload: dict[str, Any]) -> None:
        payload["pairs"][0]["expected_episode_ids"] = []

    problems = _problems(corpus, tmp_path, mutate)
    assert any("needs at least one expected episode" in problem for problem in problems)


def test_a_gold_set_outside_the_required_size_is_rejected(corpus, tmp_path):
    def mutate(payload: dict[str, Any]) -> None:
        del payload["pairs"][5:]

    problems = _problems(corpus, tmp_path, mutate)
    assert any(f"the spec requires {GOLD_MIN}-{GOLD_MAX}" in problem for problem in problems)


def test_missing_type_coverage_is_rejected(corpus, tmp_path):
    def mutate(payload: dict[str, Any]) -> None:
        payload["pairs"] = [
            pair for pair in payload["pairs"] if "pandemic" not in pair["episode_types"]
        ]

    problems = _problems(corpus, tmp_path, mutate)
    assert any("covers no pandemic pair" in problem for problem in problems)
