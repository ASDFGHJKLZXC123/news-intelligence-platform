"""The copyright gate: the 15-word reproduction bound, the quote rules, and precise findings.

Pure tests over :func:`check_block_copyright` and :func:`normalize_words`; no orchestrator and no
LLM. Every check is against stored snippets only -- no article body appears here, because none is
ever loaded.
"""

from __future__ import annotations

import uuid

from services.reports.copyright import (
    CopyrightViolation,
    SnippetSource,
    check_block_copyright,
    normalize_words,
)

#: 25 distinct words, so a run of any length up to 25 can be sliced from it deterministically.
_SENTENCE = " ".join(f"word{index}" for index in range(1, 26))


def _snippet(
    text: str, *, publisher: str | None = "Reuters", url: str | None = "https://example.test/a"
) -> SnippetSource:
    return SnippetSource(article_id=uuid.uuid4(), publisher=publisher, url=url, text=text)


def _run(block_text: str, *snippets: SnippetSource) -> tuple:
    return check_block_copyright(block_index=0, block_text=block_text, snippets=snippets)


def _first_n(n: int) -> str:
    return " ".join(f"word{index}" for index in range(1, n + 1))


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------


def test_normalization_folds_case_and_strips_punctuation() -> None:
    assert normalize_words("The Fed's RATE, cut!") == ("the", "fed", "s", "rate", "cut")
    assert normalize_words("   ") == ()


# --------------------------------------------------------------------------------------
# Reproduction: 15 allowed, 16 rejected
# --------------------------------------------------------------------------------------


def test_fifteen_consecutive_words_are_allowed() -> None:
    assert _run(_first_n(15), _snippet(_SENTENCE)) == ()


def test_sixteen_consecutive_words_are_rejected_with_a_precise_bounded_finding() -> None:
    snippet = _snippet(_SENTENCE)
    findings = _run(_first_n(16), snippet)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.violation is CopyrightViolation.EXCESSIVE_COPYING
    assert finding.word_count == 16
    assert finding.span == _first_n(16)  # the offending span, and nothing beyond it
    assert finding.article_id == snippet.article_id
    assert finding.url == snippet.url


def test_reproduction_matches_despite_case_and_punctuation() -> None:
    source = " ".join(f"Word{index}" for index in range(1, 17))  # 16 capitalized words
    block = "; ".join(f"word{index}!" for index in range(1, 17))  # same 16, lowercased, punctuated
    findings = _run(block, _snippet(source))
    assert findings and findings[0].word_count == 16


# --------------------------------------------------------------------------------------
# Quotes: <= 25 allowed, 26 rejected; attribution and link required
# --------------------------------------------------------------------------------------


def _quote_words(n: int) -> str:
    return " ".join(f"quoted{index}" for index in range(1, n + 1))


def test_a_25_word_attributed_linked_quote_is_allowed() -> None:
    quote = _quote_words(25)
    snippet = _snippet(quote, publisher="Reuters", url="https://example.test/a")
    block = f'Reuters reported that "{quote}" ahead of the open.'
    assert _run(block, snippet) == ()


def test_a_26_word_quote_is_rejected() -> None:
    quote = _quote_words(26)
    snippet = _snippet(quote, publisher="Reuters", url="https://example.test/a")
    block = f'Reuters reported "{quote}".'
    findings = _run(block, snippet)
    assert [f.violation for f in findings] == [CopyrightViolation.QUOTE_TOO_LONG]
    assert findings[0].word_count == 26


def test_an_unattributed_quote_is_rejected() -> None:
    quote = _quote_words(6)
    snippet = _snippet(quote, publisher="Reuters", url="https://example.test/a")
    block = f'An official said "{quote}".'  # the publisher is never named
    findings = _run(block, snippet)
    assert [f.violation for f in findings] == [CopyrightViolation.QUOTE_UNATTRIBUTED]


def test_a_quote_whose_source_has_no_url_is_rejected() -> None:
    quote = _quote_words(6)
    snippet = _snippet(quote, publisher="Reuters", url=None)
    block = f'Reuters said "{quote}".'
    findings = _run(block, snippet)
    assert [f.violation for f in findings] == [CopyrightViolation.QUOTE_MISSING_URL]


def test_a_quote_matching_no_snippet_fails_closed_as_unattributable() -> None:
    snippet = _snippet("entirely different source material", publisher="Reuters")
    block = 'Someone claimed "this quotation is not in any snippet".'
    findings = _run(block, snippet)
    assert [f.violation for f in findings] == [CopyrightViolation.QUOTE_UNATTRIBUTABLE]


# --------------------------------------------------------------------------------------
# Multiple snippets / sources
# --------------------------------------------------------------------------------------


def test_a_quote_is_matched_across_multiple_snippets() -> None:
    quote = _quote_words(8)
    other = _snippet("unrelated market colour", publisher="Bloomberg", url="https://example.test/b")
    real = _snippet(quote, publisher="Reuters", url="https://example.test/a")
    block = f'Reuters said "{quote}".'
    # The quote's source is the second snippet; attribution and link resolve against it.
    assert _run(block, other, real) == ()


def test_reproduction_reports_the_offending_source_among_several() -> None:
    innocent = _snippet("short unrelated text", publisher="Bloomberg", url="https://example.test/b")
    offender = _snippet(_SENTENCE, publisher="Reuters", url="https://example.test/a")
    findings = _run(_first_n(16), innocent, offender)
    assert len(findings) == 1
    assert findings[0].article_id == offender.article_id


def test_a_valid_quote_is_exempt_from_the_reproduction_bound() -> None:
    # A 20-word quote reproduces > 15 source words, but as an attributed, linked quote it is allowed;
    # the reproduction bound applies only to the non-quoted prose around it.
    quote = _quote_words(20)
    snippet = _snippet(quote, publisher="Reuters", url="https://example.test/a")
    block = f'Reuters reported "{quote}".'
    assert _run(block, snippet) == ()
