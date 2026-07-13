"""Rule-based assertion-status classification (ADR 0005).

The cue vocabulary is the ADR's, verbatim. Classification runs on the sentence that
contains a mention, is case-insensitive and word-aware (so "mayonnaise" never trips the
"may" cue), and tolerates typographic punctuation and irregular whitespace. ``denied``
outranks ``speculative`` when cues overlap, which is what makes "not in talks" a denial
rather than the "in talks" speculation it literally contains.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final


class AssertionStatus(StrEnum):
    """Claim status of the sentence a mention appears in (ADR 0005).

    Defined here rather than imported from ``services.llm.contracts``: extraction is the
    LLM-free stage of the pipeline, and importing the LLM package would drag its HTTP,
    Redis, and database wiring into a spaCy worker. The serialized values are identical to
    the LLM contract enum, so a later stage converts with ``LLMAssertionStatus(value)``.
    """

    ASSERTED = "asserted"
    DENIED = "denied"
    SPECULATIVE = "speculative"


DENIED_CUES: Final[tuple[str, ...]] = (
    "denied",
    "denies",
    "refuted",
    "dismissed",
    "rejected claims of",
    "not in talks",
)

SPECULATIVE_CUES: Final[tuple[str, ...]] = (
    "reportedly",
    "rumored",
    "rumors",
    "speculation",
    "considering",
    "exploring",
    "in talks",
    "weighing",
    "may",
    "sources say",
)

# Curly quotes are folded to their ASCII forms so a cue the copy desk typeset as
# "not in talks" matches exactly like the plain one.
_PUNCTUATION_FOLD: Final = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_WHITESPACE_RUN: Final = re.compile(r"\s+")


def _compile_cues(cues: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Compile each cue word-bounded, with any run of whitespace allowed between words."""
    return tuple(
        re.compile(r"\b" + r"\s+".join(re.escape(word) for word in cue.split()) + r"\b")
        for cue in cues
    )


_DENIED_PATTERNS: Final = _compile_cues(DENIED_CUES)
_SPECULATIVE_PATTERNS: Final = _compile_cues(SPECULATIVE_CUES)


def _normalize(sentence: str) -> str:
    """Fold punctuation, collapse whitespace, and casefold — for matching only."""
    return _WHITESPACE_RUN.sub(" ", sentence.translate(_PUNCTUATION_FOLD)).strip().casefold()


def classify_assertion(sentence: str) -> AssertionStatus:
    """Classify one sentence: denied outranks speculative, and anything else is asserted."""
    normalized = _normalize(sentence)
    if any(pattern.search(normalized) for pattern in _DENIED_PATTERNS):
        return AssertionStatus.DENIED
    if any(pattern.search(normalized) for pattern in _SPECULATIVE_PATTERNS):
        return AssertionStatus.SPECULATIVE
    return AssertionStatus.ASSERTED
