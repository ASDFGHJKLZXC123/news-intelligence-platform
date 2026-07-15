"""The copyright gate: deterministic reproduction and quotation checks (report-generation spec).

Composer prose must not launder article text. The spec draws two lines and this module enforces
both, deterministically and against *stored snippets only* -- an article body is never loaded, so
the check is over the bounded excerpts the context layer already selected:

* **Reproduction.** Prose outside a quotation may not reproduce **more than 15 consecutive**
  normalized words from any source snippet. Exactly 15 is allowed; 16 fails. This is the "write
  in your own words" rule, checked as consecutive-word containment.
* **Quotation.** A direct quote may be at most **25 words** (26 fails), and must be both
  *attributed* (its source publisher named in the block) and *linked* (that source carries a
  URL). A quote whose source cannot be established in the available snippets fails **closed** --
  an unlabelled reproduction is not given the benefit of the doubt.

Only composer-generated prose is passed here; deterministic tables, lists, the disclaimer and the
data-quality notes never are, so fixed template content is never mistaken for source copying.
Findings carry the offending source, the word span and its length, and the reason -- never the
article's full text, which this module never sees.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

#: Reproduction bound (spec): "> 15 consecutive words ... fails". Exactly 15 is allowed, so a run
#: is offending only once it exceeds this -- i.e. at length 16.
MAX_CONSECUTIVE_WORDS = 15

#: Quote bound (spec): "quotes are limited to <= 25 words". Exactly 25 allowed; 26 fails.
MAX_QUOTE_WORDS = 25

#: Word = maximal run of letters/digits, case-folded. Punctuation and case are normalized away so
#: the reproduction check turns on words, not on a source's spacing or capitalization; ``_`` is
#: excluded so it cannot glue two words into one.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

#: A direct quote is text inside a matched pair of double quotation marks, straight or smart.
#: Single quotes are deliberately not treated as quotation: an apostrophe is not a quote mark, and
#: pairing on it would split contractions into spurious "quotes".
_QUOTE_RE = re.compile(r'"([^"]*)"|“([^”]*)”')


def normalize_words(text: str) -> tuple[str, ...]:
    """The one deterministic word tokenizer, applied to both prose and snippets.

    Case-folded and stripped to letter/digit tokens, so ``"The Fed's rate."`` and ``the fed s
    rate`` tokenize identically. Two runs over the same text always yield the same tuple.
    """

    return tuple(_WORD_RE.findall(text.casefold()))


@dataclass(frozen=True)
class SnippetSource:
    """One stored source snippet available to a block, and where it came from. Never a body."""

    article_id: uuid.UUID
    publisher: str | None
    url: str | None
    text: str


class CopyrightViolation(StrEnum):
    """Why a block failed the copyright gate. One per offending span or quote."""

    #: > 15 consecutive normalized words of non-quoted prose reproduce a source snippet.
    EXCESSIVE_COPYING = "excessive_copying"
    #: A direct quote exceeds 25 words.
    QUOTE_TOO_LONG = "quote_too_long"
    #: A direct quote's source is in the snippets and linked, but its publisher is not named.
    QUOTE_UNATTRIBUTED = "quote_unattributed"
    #: A direct quote's source is in the snippets but that source carries no URL to link to.
    QUOTE_MISSING_URL = "quote_missing_url"
    #: A direct quote cannot be matched to any available snippet -- source unestablishable, so the
    #: gate fails closed rather than publish an unlabelled reproduction.
    QUOTE_UNATTRIBUTABLE = "quote_unattributable"


@dataclass(frozen=True)
class CopyrightFinding:
    """Precise, bounded evidence of one violation. Carries the offending span, never a body."""

    violation: CopyrightViolation
    block_index: int
    word_count: int
    #: The offending normalized word span (the composer's own prose), bounded by construction.
    span: str
    article_id: uuid.UUID | None
    publisher: str | None
    url: str | None
    detail: str


def _split_quotes(text: str) -> tuple[list[str], list[str]]:
    """Partition ``text`` into its quoted inner strings and the non-quoted segments around them.

    An unbalanced quotation mark matches nothing and simply stays in a non-quoted segment, where
    it is checked as ordinary prose -- deterministic, and it never swallows the rest of the block.
    """

    quotes: list[str] = []
    segments: list[str] = []
    last = 0
    for match in _QUOTE_RE.finditer(text):
        segments.append(text[last : match.start()])
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        quotes.append(inner or "")
        last = match.end()
    segments.append(text[last:])
    return quotes, segments


def _longest_run(prose: Sequence[str], snippet: Sequence[str]) -> tuple[int, int]:
    """Longest contiguous run of ``prose`` tokens that appears consecutively in ``snippet``.

    Returns ``(length, start)`` where ``start`` indexes ``prose``. A textbook longest-common-
    substring DP over token sequences; both inputs are tiny (snippets are <= 200 chars, prose is
    a section), so the quadratic cost is immaterial.
    """

    best_len = 0
    best_start = 0
    if not prose or not snippet:
        return 0, 0
    previous = [0] * (len(snippet) + 1)
    for i in range(1, len(prose) + 1):
        current = [0] * (len(snippet) + 1)
        for j in range(1, len(snippet) + 1):
            if prose[i - 1] == snippet[j - 1]:
                run = previous[j - 1] + 1
                current[j] = run
                if run > best_len:
                    best_len = run
                    best_start = i - run
        previous = current
    return best_len, best_start


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """Is ``needle`` a contiguous sublist of ``haystack``? Empty needle is never contained."""

    n = len(needle)
    if n == 0:
        return False
    return any(tuple(haystack[i : i + n]) == tuple(needle) for i in range(len(haystack) - n + 1))


def _reproduction_finding(
    block_index: int, segments: Sequence[str], snippets: Sequence[SnippetSource]
) -> CopyrightFinding | None:
    """The single longest > 15-word non-quoted reproduction across the block, or ``None``.

    One finding, not one per snippet: the longest offending run is the precise evidence, and
    reporting every source a phrase happens to appear in would bury it. Ties resolve
    deterministically by segment order, then snippet order.
    """

    worst: tuple[int, int, SnippetSource, tuple[str, ...]] | None = None
    for seg_index, segment in enumerate(segments):
        prose = normalize_words(segment)
        if len(prose) <= MAX_CONSECUTIVE_WORDS:
            continue
        for snippet in snippets:
            run_len, start = _longest_run(prose, normalize_words(snippet.text))
            if run_len > MAX_CONSECUTIVE_WORDS and (worst is None or run_len > worst[0]):
                worst = (run_len, seg_index, snippet, prose[start : start + run_len])
    if worst is None:
        return None
    run_len, _, snippet, span = worst
    return CopyrightFinding(
        violation=CopyrightViolation.EXCESSIVE_COPYING,
        block_index=block_index,
        word_count=run_len,
        span=" ".join(span),
        article_id=snippet.article_id,
        publisher=snippet.publisher,
        url=snippet.url,
        detail=(
            f"{run_len} consecutive words reproduce source {snippet.article_id} "
            f"(limit {MAX_CONSECUTIVE_WORDS})."
        ),
    )


def _quote_findings(
    block_index: int,
    block_text: str,
    quotes: Sequence[str],
    snippets: Sequence[SnippetSource],
) -> list[CopyrightFinding]:
    """Every failing quote in the block: over-length, unlinked, unattributed, or unestablishable."""

    findings: list[CopyrightFinding] = []
    haystack_text = block_text.casefold()
    for quote in quotes:
        tokens = normalize_words(quote)
        if not tokens:
            continue
        matched = [s for s in snippets if _contains(normalize_words(s.text), tokens)]
        source = matched[0] if matched else None
        span = " ".join(tokens)

        if len(tokens) > MAX_QUOTE_WORDS:
            findings.append(
                CopyrightFinding(
                    violation=CopyrightViolation.QUOTE_TOO_LONG,
                    block_index=block_index,
                    word_count=len(tokens),
                    span=span,
                    article_id=source.article_id if source else None,
                    publisher=source.publisher if source else None,
                    url=source.url if source else None,
                    detail=f"direct quote is {len(tokens)} words (limit {MAX_QUOTE_WORDS}).",
                )
            )

        if not matched:
            findings.append(
                CopyrightFinding(
                    violation=CopyrightViolation.QUOTE_UNATTRIBUTABLE,
                    block_index=block_index,
                    word_count=len(tokens),
                    span=span,
                    article_id=None,
                    publisher=None,
                    url=None,
                    detail="quoted text matches no available source snippet; source cannot be established.",
                )
            )
            continue

        linked = [s for s in matched if s.url]
        if not linked:
            findings.append(
                CopyrightFinding(
                    violation=CopyrightViolation.QUOTE_MISSING_URL,
                    block_index=block_index,
                    word_count=len(tokens),
                    span=span,
                    article_id=source.article_id if source else None,
                    publisher=source.publisher if source else None,
                    url=None,
                    detail="quote's source carries no URL to link to.",
                )
            )
            continue

        attributed = [s for s in linked if s.publisher and s.publisher.casefold() in haystack_text]
        if not attributed:
            findings.append(
                CopyrightFinding(
                    violation=CopyrightViolation.QUOTE_UNATTRIBUTED,
                    block_index=block_index,
                    word_count=len(tokens),
                    span=span,
                    article_id=linked[0].article_id,
                    publisher=linked[0].publisher,
                    url=linked[0].url,
                    detail="quote is not attributed to its source publisher in the block.",
                )
            )
    return findings


def check_block_copyright(
    *, block_index: int, block_text: str, snippets: Sequence[SnippetSource]
) -> tuple[CopyrightFinding, ...]:
    """Every copyright violation in one composer block, deterministically, against snippets only.

    Quoted spans are exempt from the reproduction rule (they are governed by the quote rule) and
    are checked separately: an attributed, linked quote of <= 25 words is allowed to reproduce
    source words verbatim; non-quoted prose is not.
    """

    quotes, segments = _split_quotes(block_text)
    findings: list[CopyrightFinding] = []
    reproduction = _reproduction_finding(block_index, segments, snippets)
    if reproduction is not None:
        findings.append(reproduction)
    findings.extend(_quote_findings(block_index, block_text, quotes, snippets))
    return tuple(findings)


__all__ = [
    "MAX_CONSECUTIVE_WORDS",
    "MAX_QUOTE_WORDS",
    "CopyrightFinding",
    "CopyrightViolation",
    "SnippetSource",
    "check_block_copyright",
    "normalize_words",
]
