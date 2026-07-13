"""Context assembly helpers used by LLM prompts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Vendor-neutral approximation. Both providers tokenize English prose at roughly four
#: characters per token; exact counts need a provider tokenizer, which the selection step
#: (a pre-prompt budget guard) does not warrant.
_CHARS_PER_TOKEN = 4


def estimate_token_count(value: str) -> int:
    """Approximate the token cost of ``value`` at ~4 characters per token."""

    text = " ".join(value.split())
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


@dataclass(frozen=True)
class RepresentativeArticleSelection:
    selected_articles: tuple[Mapping[str, Any], ...]
    used_token_budget: int
    selected_count: int
    dropped_count: int
    truncated_by_budget: bool


def select_representative_articles(
    articles: Sequence[Mapping[str, Any]],
    *,
    max_articles: int = 12,
    context_token_budget: int | None = None,
    score_key: str = "relevance_score",
    text_fields: tuple[str, ...] = ("summary", "title", "text", "body"),
) -> RepresentativeArticleSelection:
    """Select representative articles up to a fixed max and budget."""

    normalized = list(articles)
    normalized.sort(key=lambda item: float(item.get(score_key, 0.0) or 0.0), reverse=True)

    selected: list[Mapping[str, Any]] = []
    used_tokens = 0
    truncated_by_budget = False

    for article in normalized:
        if len(selected) >= max_articles:
            break

        text = ""
        for key in text_fields:
            if key in article and isinstance(article[key], str) and article[key].strip():
                text = str(article[key])
                break
        if not text:
            text = str(article)

        article_tokens = estimate_token_count(text)
        if context_token_budget is not None and used_tokens + article_tokens > context_token_budget:
            truncated_by_budget = True
            break

        selected.append(article)
        used_tokens += article_tokens

    dropped_count = len(normalized) - len(selected)
    return RepresentativeArticleSelection(
        selected_articles=tuple(selected),
        used_token_budget=used_tokens,
        selected_count=len(selected),
        dropped_count=max(0, dropped_count),
        truncated_by_budget=truncated_by_budget,
    )
