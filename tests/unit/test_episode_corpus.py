"""The committed episode corpus, and the validator that guards it (no database, no network).

Two kinds of test. The first loads the *real* product data and asserts the spec's curation
contract on it: the exact count, every quota, and the invariants that make an onset embedding
trustworthy -- high-risk rows are sampled by hand (the 2007 fund freeze that starts the crisis arc,
the nested 2023 regional banking rows, the named counterexamples the spec asks for by name).

The second feeds the validator deliberately broken copies of that data and asserts it refuses them.
A validator that only ever sees valid input is not evidence of anything.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from db.models.core import EPISODE_OUTCOMES, EPISODE_TYPES
from services.analogies.corpus import (
    ADVERSE_OUTCOMES,
    CORPUS_DIR,
    CORPUS_TARGET,
    MIN_COUNTEREXAMPLE_RATE,
    MIN_CRISIS_EPISODES,
    MIN_EPISODES_PER_TYPE,
    MIN_NON_US_EPISODES,
    NON_US_EXCLUDED,
    REVIEW_CHECKLIST_KEYS,
    TEMPLATE_PATH,
    CorpusValidationError,
    find_hindsight_phrases,
    load_corpus,
)
from services.nlp.embedding_text import build_episode_onset_text

BNP = uuid.UUID("76ed905d-eee3-5359-9686-36956d958acf")
SVB = uuid.UUID("218ff70a-29e5-57e7-b29b-1f04bf901841")
REGIONAL_ARC = uuid.UUID("cb313db8-b722-5ba5-95b1-c7a1b0292dae")
GFC_ARC = uuid.UUID("dfb5d187-b0ea-502c-bf6f-29e127155bdd")

#: The counterexamples the historical-episode spec names explicitly.
SPEC_COUNTEREXAMPLES = (
    "long-term-capital-management-1998",
    "us-debt-ceiling-standoff-2011",
    "us-repo-market-stress-2019",
    "china-renminbi-devaluation-2015",
)

Mutation = Callable[[str, dict[str, Any]], None]


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def _clone(tmp_path: Path, mutate: Mutation | None = None) -> Path:
    """Copy the real corpus into a temp directory, optionally breaking one thing about it."""
    for path in sorted(CORPUS_DIR.glob("[0-9][0-9]_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if mutate is not None:
            mutate(path.name, payload)
        (tmp_path / path.name).write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def _episodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["episodes"]


def _problems(tmp_path: Path, mutate: Mutation) -> tuple[str, ...]:
    with pytest.raises(CorpusValidationError) as excinfo:
        load_corpus(_clone(tmp_path, mutate))
    return excinfo.value.problems


# --- the committed corpus ------------------------------------------------------------------
def test_corpus_holds_the_committed_target_count(corpus):
    assert corpus.quotas.total == CORPUS_TARGET == 100


def test_corpus_meets_every_curation_quota(corpus):
    quotas = corpus.quotas
    assert quotas.crisis >= MIN_CRISIS_EPISODES
    assert quotas.counterexample_rate >= MIN_COUNTEREXAMPLE_RATE
    assert quotas.non_us >= MIN_NON_US_EPISODES
    # The exact committed audit, so a silent drift in the data fails here rather than in production.
    assert (quotas.crisis, quotas.counterexamples, quotas.non_us) == (62, 38, 54)


def test_the_crisis_count_is_read_off_the_outcome_tags_not_inferred_from_the_absence_of_a_label(
    corpus,
):
    """"Crisis/stress" is a claim about what a row holds, so it is derived from what the row holds.

    ``total - counterexamples`` gives the same 62 today, and that is precisely the trap: it agrees
    by luck, because every non-counterexample in the committed corpus happens to carry an adverse
    outcome. Encoded that way, a purely benign episode -- outcomes of only ``contained``/``recovery``
    and never labelled a near-miss -- would pad the spec's ~60 crisis/stress rows simply by existing.
    """
    crisis = [episode for episode in corpus.episodes if episode.is_crisis]

    assert len(crisis) == corpus.quotas.crisis == 62
    assert all(ADVERSE_OUTCOMES & set(episode.outcomes) for episode in crisis)
    assert not any(episode.is_counterexample for episode in crisis)
    # The two definitions agree on today's data. The whole point is that they need not.
    assert corpus.quotas.crisis == corpus.quotas.total - corpus.quotas.counterexamples


def test_a_benign_episode_that_nobody_labelled_a_counterexample_is_not_a_crisis(corpus):
    """The row the old arithmetic would have counted, and the new definition does not."""
    benign = dataclasses.replace(
        next(episode for episode in corpus.episodes if episode.is_crisis),
        outcomes=("contained", "recovery"),
    )

    assert benign.is_counterexample is False  # so `total - counterexamples` would have counted it
    assert benign.is_crisis is False  # and it pads no quota


def test_a_counterexample_stays_out_of_the_crisis_count_even_carrying_an_adverse_tag(corpus):
    """A near-miss can be contained *via* a bailout and still be the benign outcome it is held up as.

    16 of the committed counterexamples carry an adverse tag for exactly this reason, so
    "has an adverse outcome" alone would quietly pull them back into the crisis bucket.
    """
    adverse_counterexamples = [
        episode
        for episode in corpus.episodes
        if episode.is_counterexample and ADVERSE_OUTCOMES & set(episode.outcomes)
    ]

    assert adverse_counterexamples
    assert not any(episode.is_crisis for episode in adverse_counterexamples)


def test_every_episode_type_has_real_breadth(corpus):
    assert set(corpus.quotas.by_type) == set(EPISODE_TYPES)
    assert min(corpus.quotas.by_type.values()) >= MIN_EPISODES_PER_TYPE


def test_named_spec_counterexamples_are_present_and_flagged(corpus):
    by_slug = corpus.by_slug
    for slug in SPEC_COUNTEREXAMPLES:
        assert by_slug[slug].is_counterexample, slug


def test_non_us_quota_counts_specific_geographies_not_global_rows(corpus):
    non_us = [e for e in corpus.episodes if e.geography not in NON_US_EXCLUDED]
    assert len(non_us) == corpus.quotas.non_us
    assert not any(e.geography in {"US", "GLOBAL"} for e in non_us)
    # Breadth, not one country repeated: the non-US rows span many jurisdictions.
    assert len({e.geography for e in non_us}) >= 20


def test_ids_names_and_slugs_are_unique_and_stable(corpus):
    assert len(corpus.by_id) == len(corpus.episodes)
    assert len(corpus.by_slug) == len(corpus.episodes)
    assert len({e.name for e in corpus.episodes}) == len(corpus.episodes)
    assert all(isinstance(e.episode_id, uuid.UUID) for e in corpus.episodes)


def test_parents_are_seeded_before_their_children(corpus):
    position = {episode.slug: index for index, episode in enumerate(corpus.episodes)}
    by_id = corpus.by_id
    children = [e for e in corpus.episodes if e.parent_episode_id is not None]
    assert len(children) == 20
    for child in children:
        parent = by_id[child.parent_episode_id]
        assert position[parent.slug] < position[child.slug]
        assert parent.parent_episode_id is None


def test_the_2008_arc_begins_at_the_2007_fund_freeze(corpus):
    """The spec's boundary rule, spelled out: onset is the first observable date, not the collapse."""
    arc = corpus.by_id[GFC_ARC]
    bnp = corpus.by_id[BNP]
    assert arc.onset_date.isoformat() == "2007-08-09"
    assert bnp.onset_date == arc.onset_date
    assert bnp.parent_episode_id == GFC_ARC
    # The famous collapse is a *child*, and it is dated after the arc's onset.
    lehman = corpus.by_slug["lehman-brothers-funding-run-2008"]
    assert lehman.parent_episode_id == GFC_ARC
    assert lehman.onset_date > arc.onset_date


def test_svb_is_nested_under_the_2023_regional_arc(corpus):
    svb = corpus.by_id[SVB]
    assert svb.parent_episode_id == REGIONAL_ARC
    assert svb.episode_type == "banking_stress"
    assert svb.geography == "US"
    assert svb.onset_date.isoformat() == "2023-03-08"
    siblings = {
        e.slug for e in corpus.episodes if e.parent_episode_id == REGIONAL_ARC
    }
    assert siblings == {
        "svb-deposit-run-2023",
        "signature-bank-closure-2023",
        "first-republic-deposit-flight-2023",
    }


def test_onset_text_never_contains_outcome_facts(corpus):
    """Onset/outcome separation, asserted on every committed row rather than spot-checked."""
    for episode in corpus.episodes:
        embedded = build_episode_onset_text(
            onset_summary=episode.onset_summary, onset_indicators=episode.onset_indicators
        )
        assert find_hindsight_phrases(episode.onset_summary) == (), episode.slug
        assert episode.outcome_summary not in embedded, episode.slug
        assert episode.resolution_mechanism not in embedded, episode.slug
        # The name is hindsight-laden ("collapse", "failure", the year it ended) and is the one
        # field the embedding builder cannot even be handed.
        assert episode.name.lower() not in embedded.lower(), episode.slug


def test_onset_indicators_are_typed_and_dated_inside_the_onset_window(corpus):
    for episode in corpus.episodes:
        payload = episode.onset_indicators
        assert payload["schema"] == "onset_indicators_v1"
        indicators = payload["indicators"]
        assert len(indicators) >= 2, episode.slug
        source_ids = {source["id"] for source in episode.source_refs["sources"]}
        for indicator in indicators:
            assert set(indicator) == {"key", "label", "value", "unit", "as_of", "source_id"}
            assert isinstance(indicator["value"], bool | int | float | str)
            if indicator["source_id"] is not None:
                assert indicator["source_id"] in source_ids


def test_outcomes_and_sources_are_auditable(corpus):
    for episode in corpus.episodes:
        assert episode.outcomes, episode.slug
        assert set(episode.outcomes) <= set(EPISODE_OUTCOMES)
        sources = episode.source_refs["sources"]
        assert sources, episode.slug
        for source in sources:
            assert source["url"].startswith("https://")
            assert source["publisher"] and source["title"] and source["accessed"]
        assert "original prose" in episode.license_note.lower()


def test_review_state_is_honest_and_machine_readable(corpus):
    """The corpus claims automated validation and *pending* human review. It claims nothing else."""
    assert corpus.quotas.human_reviewed == 0
    for episode in corpus.episodes:
        review = episode.review
        assert review["status"] == "llm_drafted_pending_human_review"
        assert review["human_signoff"] == {
            "reviewer": None,
            "reviewed_at": None,
            "decision": None,
        }
        assert set(review["checklist"]) == set(REVIEW_CHECKLIST_KEYS)
        assert not any(review["checklist"].values())
        # The review block travels with the row into the database, so a served analogy is auditable.
        assert episode.source_refs["review"] == review


def test_authoring_template_sits_next_to_the_data_and_matches_the_validator():
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert TEMPLATE_PATH.parent == CORPUS_DIR
    assert set(template["review_checklist"]) == set(REVIEW_CHECKLIST_KEYS)
    assert {step["actor"] for step in template["workflow"]["steps"]} == {
        "llm",
        "automated",
        "human",
    }
    assert "human_reviewed" in template["review_statuses"]
    # The template is not corpus data: the loader's glob must not pick it up.
    assert not TEMPLATE_PATH.match("[0-9][0-9]_*.json")


# --- the validator -------------------------------------------------------------------------
def test_duplicate_id_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[1]["id"] = _episodes(payload)[0]["id"]

    assert any("duplicate id" in problem for problem in _problems(tmp_path, mutate))


def test_duplicate_slug_and_name_are_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            first, second = _episodes(payload)[0], _episodes(payload)[1]
            second["slug"] = first["slug"]
            second["name"] = first["name"]

    problems = _problems(tmp_path, mutate)
    assert any("duplicate slug" in problem for problem in problems)
    assert any("duplicate name" in problem for problem in problems)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("episode_type", "bank_run", "unknown episode_type"),
        ("geography", "ZZ", "unknown geography"),
        ("outcomes", ["apocalypse"], "unknown value"),
        ("regime_tags", ["post_vibes"], "unknown value"),
        ("affected_industries", ["vibes"], "unknown value"),
        ("id", "not-a-uuid", "is not a UUID"),
        ("onset_date", "2007-13-45", "is not an ISO date"),
        ("version", 0, "version must be a positive integer"),
    ],
)
def test_malformed_field_is_rejected(tmp_path, field, value, expected):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0][field] = value

    assert any(expected in problem for problem in _problems(tmp_path, mutate))


def test_boundary_violation_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            episode = _episodes(payload)[0]
            episode["peak_date"] = "2006-01-01"
            episode["end_date"] = "2006-06-01"

    problems = _problems(tmp_path, mutate)
    assert any("precedes onset_date" in problem for problem in problems)


def test_hindsight_in_onset_summary_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0]["onset_summary"] += (
                " The bank would later fail, and in hindsight the run was obvious."
            )

    problems = _problems(tmp_path, mutate)
    assert any("hindsight phrase" in problem for problem in problems)


def test_a_future_year_in_onset_summary_is_rejected(tmp_path):
    """An observer in 2007 cannot cite 2008. The cheapest possible look-ahead detector."""

    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0]["onset_summary"] += " Losses continued through 2009."

    problems = _problems(tmp_path, mutate)
    assert any("later than the onset year" in problem for problem in problems)


def test_indicator_dated_after_the_onset_window_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0]["onset_indicators"][0]["as_of"] = "2007-12-31"

    problems = _problems(tmp_path, mutate)
    assert any("outside the onset window" in problem for problem in problems)


@pytest.mark.parametrize(
    "indicators",
    [
        "not-a-list",
        [{"key": "x", "label": "y", "value": 1, "unit": "percent", "as_of": "2007-08-01"}],
    ],
)
def test_untyped_indicators_are_rejected(tmp_path, indicators):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0]["onset_indicators"] = indicators

    problems = _problems(tmp_path, mutate)
    assert any("at least 2 typed signals" in problem for problem in problems)


def test_unknown_indicator_unit_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0]["onset_indicators"][0]["unit"] = "furlongs"

    assert any("unknown unit" in problem for problem in _problems(tmp_path, mutate))


def test_a_placeholder_source_domain_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0]["source_refs"][0]["url"] = "https://example.com/whatever"

    problems = _problems(tmp_path, mutate)
    assert any("not an allowlisted institutional source domain" in p for p in problems)


def test_missing_sources_and_license_are_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0]["source_refs"] = []
            _episodes(payload)[0]["license_note"] = "All rights reserved."

    problems = _problems(tmp_path, mutate)
    assert any("source_refs must be a non-empty list" in p for p in problems)
    assert any("license_note must state" in p for p in problems)


def test_missing_review_checklist_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0]["review"] = {
                "status": "llm_drafted_pending_human_review",
                "human_signoff": {"reviewer": None, "reviewed_at": None, "decision": None},
            }

    assert any("review checklist must hold" in p for p in _problems(tmp_path, mutate))


def test_claiming_human_review_without_a_reviewer_is_rejected(tmp_path):
    """The format cannot be used to manufacture a sign-off that did not happen."""

    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            review = json.loads(json.dumps(payload["defaults"]["review"]))
            review["status"] = "human_reviewed"
            _episodes(payload)[0]["review"] = review

    problems = _problems(tmp_path, mutate)
    assert any("no reviewer/date is recorded" in p for p in problems)
    assert any("checklist is not complete" in p for p in problems)


def test_unknown_parent_and_self_parent_are_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            _episodes(payload)[0]["parent_slug"] = "arc-that-does-not-exist"
            _episodes(payload)[1]["parent_slug"] = _episodes(payload)[1]["slug"]

    problems = _problems(tmp_path, mutate)
    assert any("does not exist" in p for p in problems)
    assert any("is its own parent" in p for p in problems)


def test_a_child_seeded_before_its_parent_is_rejected(tmp_path):
    """Foreign keys are checked on a fresh database, so seed order is part of the contract."""

    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            episodes = _episodes(payload)
            episodes[0]["parent_slug"] = episodes[-1]["slug"]

    assert any("is seeded after it" in p for p in _problems(tmp_path, mutate))


def test_a_grandparent_chain_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "01_banking_stress.json":
            episodes = _episodes(payload)
            # bnp is already a child; making the next row its child would build a 3-deep chain,
            # and retrieval would then silently drop bnp from ranking as a parent.
            episodes[1]["parent_slug"] = episodes[0]["slug"]

    assert any("is itself a child" in p for p in _problems(tmp_path, mutate))


def test_quota_shortfalls_are_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        for episode in _episodes(payload):
            episode["is_counterexample"] = False
            if episode["geography"] not in {"US", "GLOBAL"}:
                episode["geography"] = "US"

    problems = _problems(tmp_path, mutate)
    assert any("counterexamples are 0.0%" in p for p in problems)
    assert any("specific non-US geography" in p for p in problems)


def test_missing_type_coverage_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name == "07_pandemic.json":
            for episode in _episodes(payload):
                episode["episode_type"] = "geopolitical"

    problems = _problems(tmp_path, mutate)
    assert any("episode type 'pandemic' has 1 episode(s)" in p for p in problems)


def test_a_corpus_below_the_minimum_size_is_rejected(tmp_path):
    def mutate(name: str, payload: dict[str, Any]) -> None:
        if name != "00_arcs.json":
            del payload["episodes"][2:]

    problems = _problems(tmp_path, mutate)
    assert any("the spec requires 80-120" in p for p in problems)
    assert any("crisis/stress episodes" in p for p in problems)
