"""Canonical embedding text builders (ADR 0004): separation, truncation, determinism."""

from __future__ import annotations

import inspect

from services.llm.selection import estimate_token_count
from services.nlp.embedding_text import (
    EMBEDDING_TOKEN_BUDGET,
    MODEL_TOKEN_LIMIT,
    build_article_embedding_text,
    build_episode_onset_text,
    build_event_embedding_text,
    serialize_onset_indicators,
)

TITLE = "Regional bank halts withdrawals"
SUMMARY = "Depositors queue as the lender freezes accounts."


def _long(words: int) -> str:
    return " ".join(f"word{index}" for index in range(words))


# --- articles -------------------------------------------------------------------------
def test_article_text_is_title_then_lede_then_body() -> None:
    text = build_article_embedding_text(title=TITLE, summary=SUMMARY, body="Body sentence.")

    assert text == f"{TITLE}\n\n{SUMMARY}\n\nBody sentence."


def test_article_text_normalizes_whitespace_so_rewrapping_does_not_change_the_vector() -> None:
    wrapped = build_article_embedding_text(title=TITLE, body="Body\n   sentence.\t\tMore.")
    flat = build_article_embedding_text(title=TITLE, body="Body sentence. More.")

    assert wrapped == flat


def test_article_without_a_lede_or_body_is_just_the_title() -> None:
    assert build_article_embedding_text(title=TITLE) == TITLE
    assert build_article_embedding_text(title=TITLE, summary=None, body=None) == TITLE


def test_article_body_is_truncated_to_the_nominal_budget_and_the_head_survives_intact() -> None:
    text = build_article_embedding_text(title=TITLE, summary=SUMMARY, body=_long(40_000))

    assert text.startswith(f"{TITLE}\n\n{SUMMARY}\n\n")
    assert estimate_token_count(text) <= EMBEDDING_TOKEN_BUDGET


def test_a_title_and_lede_that_alone_fill_the_budget_are_both_preserved() -> None:
    title = _long(3_000)
    assert EMBEDDING_TOKEN_BUDGET < estimate_token_count(title) < MODEL_TOKEN_LIMIT

    text = build_article_embedding_text(title=title, summary=SUMMARY, body="Body.")

    assert text == f"{title}\n\n{SUMMARY}"  # kept whole, not cut back into the budget
    assert "Body." not in text  # the body is what gives way
    assert estimate_token_count(text) > EMBEDDING_TOKEN_BUDGET


def test_a_title_past_the_hard_limit_still_survives_whole_and_the_lede_gives_way() -> None:
    """The pathological case: nothing can fit beside a title this size, and the title still wins."""
    title = _long(5_000)
    assert estimate_token_count(title) > MODEL_TOKEN_LIMIT

    text = build_article_embedding_text(title=title, summary=SUMMARY, body="Body.")

    assert text == title


def test_a_runaway_lede_is_trimmed_only_by_the_hard_model_limit_never_the_title() -> None:
    text = build_article_embedding_text(title=TITLE, summary=_long(10_000), body="Body.")

    assert text.startswith(f"{TITLE}\n\n")
    assert EMBEDDING_TOKEN_BUDGET < estimate_token_count(text) <= MODEL_TOKEN_LIMIT


def test_article_text_is_deterministic() -> None:
    args = {"title": TITLE, "summary": SUMMARY, "body": _long(20_000)}

    assert build_article_embedding_text(**args) == build_article_embedding_text(**args)


# --- events ---------------------------------------------------------------------------
def test_event_text_is_the_canonical_title_and_summary() -> None:
    assert build_event_embedding_text(title=TITLE, summary=SUMMARY) == f"{TITLE}\n\n{SUMMARY}"
    assert build_event_embedding_text(title=TITLE) == TITLE


def test_event_summary_is_truncated_to_the_budget_and_the_title_is_not() -> None:
    text = build_event_embedding_text(title=TITLE, summary=_long(40_000))

    assert text.startswith(f"{TITLE}\n\n")
    assert estimate_token_count(text) <= EMBEDDING_TOKEN_BUDGET


# --- historical episodes --------------------------------------------------------------
def test_episode_text_is_onset_summary_plus_serialized_indicators() -> None:
    text = build_episode_onset_text(
        onset_summary="Concentrated uninsured deposits face rapid withdrawals.",
        onset_indicators={"uninsured_deposit_pct": 94, "held_to_maturity_losses_usd_bn": 15},
    )

    assert text.startswith("Concentrated uninsured deposits face rapid withdrawals.\n\n")
    assert "uninsured_deposit_pct" in text
    assert "94" in text


def test_indicator_serialization_is_key_order_independent() -> None:
    one = build_episode_onset_text(onset_summary="Onset.", onset_indicators={"b": 1, "a": 2})
    two = build_episode_onset_text(onset_summary="Onset.", onset_indicators={"a": 2, "b": 1})

    assert one == two
    assert serialize_onset_indicators({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_episode_without_indicators_is_just_the_onset_summary() -> None:
    assert build_episode_onset_text(onset_summary="Onset.") == "Onset."
    assert build_episode_onset_text(onset_summary="Onset.", onset_indicators={}) == "Onset."


def test_episode_builder_cannot_be_handed_an_outcome_at_all() -> None:
    """The look-ahead-bias guarantee is structural: there is no outcome parameter to pass."""
    parameters = set(inspect.signature(build_episode_onset_text).parameters)

    assert parameters == {"onset_summary", "onset_indicators", "token_budget"}
    for hindsight in ("outcome_summary", "outcomes", "resolution_mechanism", "name"):
        assert hindsight not in parameters


def test_episode_indicators_are_truncated_before_the_onset_summary_is() -> None:
    onset = "Concentrated uninsured deposits face rapid withdrawals."

    text = build_episode_onset_text(
        onset_summary=onset, onset_indicators={"notes": _long(40_000)}
    )

    assert text.startswith(f"{onset}\n\n")
    assert estimate_token_count(text) <= EMBEDDING_TOKEN_BUDGET
