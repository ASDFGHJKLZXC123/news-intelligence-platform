"""Rule-based assertion-status cues (ADR 0005). No spaCy, no model, no network."""

from __future__ import annotations

import pytest

from services.nlp.assertions import (
    DENIED_CUES,
    SPECULATIVE_CUES,
    AssertionStatus,
    classify_assertion,
)

# One sentence per cue, so every cue in the ADR list is exercised as written.
_DENIED_SENTENCES = {
    "denied": "Acme denied the report.",
    "denies": "Acme denies the report.",
    "refuted": "Acme refuted the report.",
    "dismissed": "Acme dismissed the report.",
    "rejected claims of": "Acme rejected claims of a merger.",
    "not in talks": "Acme is not in talks with Globex.",
}
_SPECULATIVE_SENTENCES = {
    "reportedly": "Acme is reportedly buying Globex.",
    "rumored": "Acme is rumored to be buying Globex.",
    "rumors": "Rumors of an Acme bid spread.",
    "speculation": "Speculation about an Acme bid spread.",
    "considering": "Acme is considering a bid.",
    "exploring": "Acme is exploring a sale.",
    "in talks": "Acme is in talks with Globex.",
    "weighing": "Acme is weighing a bid.",
    "may": "Acme may bid for Globex.",
    "sources say": "Sources say Acme will bid.",
}


def test_the_adr_cue_lists_are_covered_by_these_tests() -> None:
    assert set(_DENIED_SENTENCES) == set(DENIED_CUES)
    assert set(_SPECULATIVE_SENTENCES) == set(SPECULATIVE_CUES)


@pytest.mark.parametrize("cue", DENIED_CUES)
def test_every_denied_cue_classifies_denied(cue: str) -> None:
    assert classify_assertion(_DENIED_SENTENCES[cue]) is AssertionStatus.DENIED


@pytest.mark.parametrize("cue", SPECULATIVE_CUES)
def test_every_speculative_cue_classifies_speculative(cue: str) -> None:
    assert classify_assertion(_SPECULATIVE_SENTENCES[cue]) is AssertionStatus.SPECULATIVE


def test_a_plain_statement_is_asserted() -> None:
    assert classify_assertion("Acme acquired Globex for $2 billion.") is AssertionStatus.ASSERTED


def test_empty_and_blank_sentences_are_asserted() -> None:
    assert classify_assertion("") is AssertionStatus.ASSERTED
    assert classify_assertion("   \n  ") is AssertionStatus.ASSERTED


def test_denied_outranks_the_speculative_cue_it_contains() -> None:
    # "not in talks" literally contains the speculative cue "in talks".
    assert classify_assertion("Acme said it is not in talks.") is AssertionStatus.DENIED


def test_denied_wins_when_both_cue_families_fire() -> None:
    sentence = "Acme denied that it is considering a bid, sources say."
    assert classify_assertion(sentence) is AssertionStatus.DENIED


def test_matching_is_case_insensitive() -> None:
    assert classify_assertion("ACME DENIED THE REPORT.") is AssertionStatus.DENIED
    assert classify_assertion("REPORTEDLY, Acme will bid.") is AssertionStatus.SPECULATIVE


@pytest.mark.parametrize(
    "sentence",
    [
        "The mayonnaise maker raised prices.",  # must not match "may"
        "The mayor visited Acme.",  # must not match "may"
        "Rumorsphere is a satire blog about Acme.",  # must not match "rumors"
        "Acme dismissedly filed the report.",  # must not match "dismissed"
    ],
)
def test_cues_only_match_whole_words(sentence: str) -> None:
    assert classify_assertion(sentence) is AssertionStatus.ASSERTED


def test_curly_punctuation_and_odd_whitespace_still_match() -> None:
    assert classify_assertion("Acme says it is “not in talks”.") is AssertionStatus.DENIED
    assert classify_assertion("Acme is in\n  talks with Globex.") is AssertionStatus.SPECULATIVE
    # A non-breaking space between the cue's words must behave like an ordinary one.
    assert classify_assertion("Acme is in\u00a0talks.") is AssertionStatus.SPECULATIVE


def test_status_values_match_the_llm_contract_enum() -> None:
    # Stage 3 converts with LLMAssertionStatus(status.value); the values must line up.
    from services.llm.contracts import AssertionStatus as LLMAssertionStatus

    for status in AssertionStatus:
        assert LLMAssertionStatus(status.value).value == status.value
