"""Canonical embedding texts for articles, events, and historical episodes (ADR 0004).

One text per subject, built one way, so the same content always lands on the same vector.
Whitespace is normalized before anything is measured or cut: a body that differs only in
line wrapping must not embed differently from the same body re-wrapped.

The episode builder takes *only* onset arguments. That is the point of its signature: the
historical-episode spec forbids outcome text (``outcome_summary``, ``outcomes``,
``resolution_mechanism``, and the hindsight-laden ``name``) from ever reaching the vector
retrieval matches against, and a builder that cannot be passed those fields cannot leak them.

Budgets use the Stage 2 estimator (~4 characters per token). It is an approximation, which is
why the target is ADR 0004's ~6,000 tokens rather than the model's hard 8,191: the gap absorbs
the error instead of a truncated request failing at the API.
"""

from __future__ import annotations

import json
from typing import Any

from services.llm.selection import estimate_token_count

#: ADR 0004's nominal budget: "truncated to ~6,000 tokens (model limit 8,191)".
EMBEDDING_TOKEN_BUDGET = 6_000

#: The model's hard input ceiling. Only ever trims a runaway lede, never the title.
MODEL_TOKEN_LIMIT = 8_191

_CHARS_PER_TOKEN = 4
_SEPARATOR = "\n\n"


def normalize_text(value: str | None) -> str:
    """Collapse all whitespace runs to single spaces so the text is canonical."""
    return " ".join((value or "").split())


def serialize_onset_indicators(indicators: Any) -> str:
    """Serialize the onset indicators deterministically (sorted keys, no incidental spacing)."""
    if not indicators:
        return ""
    return json.dumps(
        indicators, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )


def _truncate_to_tokens(text: str, budget: int) -> str:
    """Cut normalized text to a token budget, on a word boundary, deterministically."""
    if budget <= 0:
        return ""
    if estimate_token_count(text) <= budget:
        return text
    clipped = text[: budget * _CHARS_PER_TOKEN]
    head, boundary, _tail = clipped.rpartition(" ")
    return (head if boundary else clipped).rstrip()


def _join(head: str, tail: str) -> str:
    """Append a section only when it survived truncation."""
    if not tail:
        return head
    if not head:
        return tail
    return f"{head}{_SEPARATOR}{tail}"


def build_article_embedding_text(
    *,
    title: str,
    summary: str | None = None,
    body: str | None = None,
    token_budget: int = EMBEDDING_TOKEN_BUDGET,
) -> str:
    """``title + "\\n\\n" + lede/summary + normalized body``, body truncated first (ADR 0004).

    The title is never truncated and never dropped -- it is the densest signal an article has,
    and clustering compares these vectors against each other. When title and lede alone already
    fill the nominal budget, they are kept whole rather than cut back into it; only the model's
    hard limit can trim the lede, and even then the title survives intact.
    """
    title_text = normalize_text(title)
    summary_text = normalize_text(summary)
    body_text = normalize_text(body)

    head = _join(title_text, summary_text)
    head_tokens = estimate_token_count(head)
    if head_tokens >= token_budget:
        return _join(
            title_text,
            _truncate_to_tokens(
                summary_text, MODEL_TOKEN_LIMIT - estimate_token_count(title_text) - 1
            ),
        )
    # The separator collapses to one space under the estimator's normalization; one token of
    # headroom covers it.
    return _join(head, _truncate_to_tokens(body_text, token_budget - head_tokens - 1))


def build_event_embedding_text(
    *,
    title: str,
    summary: str | None = None,
    token_budget: int = EMBEDDING_TOKEN_BUDGET,
) -> str:
    """Canonical event title + event summary (ADR 0004). The title is never truncated."""
    title_text = normalize_text(title)
    summary_text = normalize_text(summary)
    budget = token_budget - estimate_token_count(title_text) - 1
    return _join(title_text, _truncate_to_tokens(summary_text, budget))


def build_episode_onset_text(
    *,
    onset_summary: str,
    onset_indicators: Any = None,
    token_budget: int = EMBEDDING_TOKEN_BUDGET,
) -> str:
    """``onset_summary`` + serialized ``onset_indicators``, and nothing else.

    Onset fields only, by construction: this function takes no outcome argument, so no caller
    can put hindsight into the embedded text (historical-episode spec, "onset/outcome
    separation"). Retrieval never sees outcomes; they are joined in after matching.
    """
    summary_text = normalize_text(onset_summary)
    indicators_text = normalize_text(serialize_onset_indicators(onset_indicators))
    budget = token_budget - estimate_token_count(summary_text) - 1
    return _join(summary_text, _truncate_to_tokens(indicators_text, budget))


__all__ = [
    "EMBEDDING_TOKEN_BUDGET",
    "MODEL_TOKEN_LIMIT",
    "build_article_embedding_text",
    "build_episode_onset_text",
    "build_event_embedding_text",
    "normalize_text",
    "serialize_onset_indicators",
]
