"""Mention extraction (ADR 0005 stage 1).

Every test here runs against an injected spaCy-shaped fake: the unit suite never installs,
loads, or downloads ``en_core_web_trf``, and never touches the network. The fake computes
real character offsets from the article text, so offset/Unicode assertions mean something.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import types
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.nlp.mentions import (
    DISABLED_PIPELINE_COMPONENTS,
    ArticleText,
    EntityLabel,
    MentionExtractionConfigurationError,
    SpacyDoc,
    extract_mentions,
    load_spacy_language,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SENTENCE_PATTERN = re.compile(r"[^.!?]+[.!?]*")


# --- The spaCy-shaped fake seam ------------------------------------------------
@dataclass(frozen=True)
class FakeSpan:
    """A ``spacy.tokens.Span`` stand-in (sentence or entity)."""

    text: str
    start_char: int
    end_char: int
    label_: str = ""


@dataclass(frozen=True)
class FakeDoc:
    """A ``spacy.tokens.Doc`` stand-in."""

    text: str
    ents: tuple[FakeSpan, ...]
    sents: tuple[FakeSpan, ...]


def _fake_sentences(text: str) -> tuple[FakeSpan, ...]:
    spans = []
    for match in _SENTENCE_PATTERN.finditer(text):
        chunk = match.group()
        stripped = chunk.strip()
        if not stripped:
            continue
        start = match.start() + (len(chunk) - len(chunk.lstrip()))
        spans.append(FakeSpan(stripped, start, start + len(stripped)))
    return tuple(spans)


class FakeSpacyLanguage:
    """Minimal ``SpacyLanguage``: labels every occurrence of the given surface forms."""

    def __init__(
        self,
        entity_labels: Mapping[str, str],
        *,
        doc_factory: Callable[[str], SpacyDoc] | None = None,
    ) -> None:
        self._entity_labels = dict(entity_labels)
        self._doc_factory = doc_factory
        self.pipe_calls: list[tuple[tuple[str, ...], int]] = []

    def pipe(self, texts: Iterable[str], *, batch_size: int) -> Iterator[SpacyDoc]:
        batch = tuple(texts)
        self.pipe_calls.append((batch, batch_size))
        factory = self._doc_factory or self._build_doc
        return iter([factory(text) for text in batch])

    def _build_doc(self, text: str) -> FakeDoc:
        ents = sorted(
            (
                FakeSpan(surface, match.start(), match.end(), label)
                for surface, label in self._entity_labels.items()
                for match in re.finditer(re.escape(surface), text)
            ),
            key=lambda span: (span.start_char, span.end_char),
        )
        return FakeDoc(text=text, ents=tuple(ents), sents=_fake_sentences(text))


_LABELS = {
    "Acme Corp": "ORG",
    "Tim Cook": "PERSON",
    "Germany": "GPE",
    "iPhone": "PRODUCT",
    "Tuesday": "DATE",  # not an ADR 0005 label
    "$2 billion": "MONEY",  # not an ADR 0005 label
}


# --- Labels --------------------------------------------------------------------
def test_extracts_the_four_adr_labels_and_drops_every_other_one() -> None:
    text = "Acme Corp and Tim Cook met in Germany on Tuesday about the iPhone for $2 billion."
    language = FakeSpacyLanguage(_LABELS)

    (result,) = extract_mentions([ArticleText("a1", text)], language=language)

    assert [(m.text, m.label) for m in result.mentions] == [
        ("Acme Corp", EntityLabel.ORG),
        ("Tim Cook", EntityLabel.PERSON),
        ("Germany", EntityLabel.GPE),
        ("iPhone", EntityLabel.PRODUCT),
    ]
    assert all(isinstance(m.label, EntityLabel) for m in result.mentions)


# --- Batching ------------------------------------------------------------------
def test_a_batch_is_one_pipe_call_at_the_configured_batch_size() -> None:
    language = FakeSpacyLanguage(_LABELS)
    articles = [ArticleText(f"a{i}", f"Acme Corp grew in {i}.") for i in range(5)]

    results = extract_mentions(articles, language=language, batch_size=3)

    assert len(language.pipe_calls) == 1  # never one call per article
    texts, batch_size = language.pipe_calls[0]
    assert texts == tuple(article.text for article in articles)
    assert batch_size == 3
    assert [r.article_key for r in results] == ["a0", "a1", "a2", "a3", "a4"]


def test_batch_size_defaults_to_the_configured_setting() -> None:
    language = FakeSpacyLanguage(_LABELS)

    extract_mentions([ArticleText("a1", "Acme Corp grew.")], language=language)

    assert language.pipe_calls[0][1] == 16  # Settings.ner_batch_size default


@pytest.mark.parametrize("batch_size", [0, -1])
def test_a_non_positive_batch_size_override_is_rejected(batch_size: int) -> None:
    language = FakeSpacyLanguage(_LABELS)

    with pytest.raises(ValueError, match="positive"):
        extract_mentions(
            [ArticleText("a1", "Acme Corp grew.")], language=language, batch_size=batch_size
        )


# --- Offsets, sentences, and adjacent context ----------------------------------
def test_mentions_carry_exact_offsets_sentence_context_and_adjacent_sentences() -> None:
    text = "Markets opened flat. Acme Corp denied the report. Tim Cook declined to comment."
    language = FakeSpacyLanguage(_LABELS)

    (result,) = extract_mentions([ArticleText("a1", text)], language=language)
    acme, cook = result.mentions

    # Document offsets slice the surface form back out of the original text.
    assert text[acme.start_char : acme.end_char] == "Acme Corp"
    assert text[cook.start_char : cook.end_char] == "Tim Cook"
    # Sentence offsets slice the containing sentence back out.
    assert text[acme.sentence.start_char : acme.sentence.end_char] == acme.sentence.text
    assert acme.sentence.text == "Acme Corp denied the report."
    # Mention position relative to its own sentence.
    assert acme.start_in_sentence == 0
    assert acme.end_in_sentence == len("Acme Corp")
    assert acme.sentence.text[acme.start_in_sentence : acme.end_in_sentence] == "Acme Corp"
    # Sentence +/- 1, so stage 3 builds its context window without rerunning spaCy.
    assert acme.sentence.previous_text == "Markets opened flat."
    assert acme.sentence.next_text == "Tim Cook declined to comment."
    assert acme.sentence.window_text == text.strip()


def test_edge_sentences_have_no_missing_neighbour() -> None:
    text = "Acme Corp grew."
    language = FakeSpacyLanguage(_LABELS)

    (result,) = extract_mentions([ArticleText("a1", text)], language=language)
    (mention,) = result.mentions

    assert mention.sentence.index == 0
    assert mention.sentence.previous_text is None
    assert mention.sentence.next_text is None
    assert mention.sentence.window_text == "Acme Corp grew."


def test_offsets_are_character_accurate_across_unicode() -> None:
    text = "Björn Éclair 🚀 met Acme Corp in Germany."
    language = FakeSpacyLanguage(_LABELS)

    (result,) = extract_mentions([ArticleText("a1", text)], language=language)

    for mention in result.mentions:
        assert text[mention.start_char : mention.end_char] == mention.text
    assert [m.text for m in result.mentions] == ["Acme Corp", "Germany"]


# --- Assertion status ----------------------------------------------------------
def test_assertion_status_applies_to_every_mention_in_the_sentence() -> None:
    text = (
        "Acme Corp denied that Tim Cook joined the board. "
        "Germany is reportedly weighing a rule. "
        "The iPhone shipped."
    )
    language = FakeSpacyLanguage(_LABELS)

    (result,) = extract_mentions([ArticleText("a1", text)], language=language)
    by_text = {mention.text: mention.assertion_status for mention in result.mentions}

    assert by_text["Acme Corp"] == "denied"
    assert by_text["Tim Cook"] == "denied"  # same sentence, same status
    assert by_text["Germany"] == "speculative"
    assert by_text["iPhone"] == "asserted"


# --- Determinism: order, duplicates, empties, no mutation ----------------------
def test_mentions_are_returned_in_document_order() -> None:
    text = "Germany hosted Acme Corp. Tim Cook praised the iPhone."
    language = FakeSpacyLanguage(_LABELS)

    (result,) = extract_mentions([ArticleText("a1", text)], language=language)

    starts = [mention.start_char for mention in result.mentions]
    assert starts == sorted(starts)
    assert [m.text for m in result.mentions] == ["Germany", "Acme Corp", "Tim Cook", "iPhone"]


def test_a_surface_form_repeated_at_different_offsets_yields_one_mention_each() -> None:
    text = "Acme Corp grew. Acme Corp hired."
    language = FakeSpacyLanguage(_LABELS)

    (result,) = extract_mentions([ArticleText("a1", text)], language=language)

    assert len(result.mentions) == 2
    assert [m.start_char for m in result.mentions] == [0, 16]
    assert result.mentions[0].sentence.index == 0
    assert result.mentions[1].sentence.index == 1


def test_an_identical_span_emitted_twice_is_kept_once() -> None:
    text = "Acme Corp grew."
    duplicate = FakeSpan("Acme Corp", 0, 9, "ORG")
    language = FakeSpacyLanguage(
        _LABELS,
        doc_factory=lambda body: FakeDoc(
            text=body, ents=(duplicate, duplicate), sents=_fake_sentences(body)
        ),
    )

    (result,) = extract_mentions([ArticleText("a1", text)], language=language)

    assert len(result.mentions) == 1


def test_an_empty_batch_returns_nothing_and_never_calls_the_model() -> None:
    language = FakeSpacyLanguage(_LABELS)

    assert extract_mentions([], language=language) == ()
    assert language.pipe_calls == []


def test_blank_text_yields_an_empty_result_without_reaching_the_model() -> None:
    language = FakeSpacyLanguage(_LABELS)
    articles = [
        ArticleText("blank", "   \n  "),
        ArticleText("real", "Acme Corp grew."),
        ArticleText("empty", ""),
    ]

    results = extract_mentions(articles, language=language)

    assert [r.article_key for r in results] == ["blank", "real", "empty"]
    assert results[0].mentions == ()
    assert results[2].mentions == ()
    assert [m.text for m in results[1].mentions] == ["Acme Corp"]
    # Only the article with text was ever piped.
    assert language.pipe_calls[0][0] == ("Acme Corp grew.",)


def test_multi_article_results_stay_aligned_and_the_caller_input_is_not_mutated() -> None:
    language = FakeSpacyLanguage(_LABELS)
    articles = [
        ArticleText("a1", "Acme Corp grew."),
        ArticleText("a2", "Tim Cook visited Germany."),
    ]
    snapshot = list(articles)

    results = extract_mentions(articles, language=language)

    assert [m.article_key for r in results for m in r.mentions] == ["a1", "a2", "a2"]
    assert [r.article_key for r in results] == ["a1", "a2"]
    assert [m.text for m in results[1].mentions] == ["Tim Cook", "Germany"]
    assert articles == snapshot  # the caller's sequence is untouched


# --- Model loading: errors, caching, and the no-import guarantee ----------------
def test_a_missing_spacy_library_raises_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "spacy", None)  # makes `import spacy` raise

    with pytest.raises(MentionExtractionConfigurationError, match="spaCy is not installed"):
        load_spacy_language("missing-library-model")


def test_a_missing_model_raises_a_configuration_error_naming_the_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load(name: str, **kwargs: object) -> object:
        raise OSError(f"[E050] Can't find model '{name}'")

    monkeypatch.setitem(sys.modules, "spacy", types.SimpleNamespace(load=load))

    with pytest.raises(MentionExtractionConfigurationError) as excinfo:
        load_spacy_language("missing-pipeline-model")

    message = str(excinfo.value)
    assert "python -m spacy download missing-pipeline-model" in message
    assert "deployment prerequisite" in message


def test_the_pipeline_is_loaded_once_and_disables_only_safe_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    sentinel = FakeSpacyLanguage(_LABELS)

    def load(name: str, *, disable: list[str]) -> FakeSpacyLanguage:
        calls.append((name, tuple(disable)))
        return sentinel

    monkeypatch.setitem(sys.modules, "spacy", types.SimpleNamespace(load=load))

    first = load_spacy_language("cached-model")
    second = load_spacy_language("cached-model")

    assert first is sentinel and second is sentinel
    assert calls == [("cached-model", DISABLED_PIPELINE_COMPONENTS)]  # cached, not reloaded
    # The parser (sentence boundaries), transformer, and NER head are never disabled.
    assert set(DISABLED_PIPELINE_COMPONENTS).isdisjoint({"parser", "transformer", "ner"})


def test_importing_the_service_loads_no_model_and_touches_no_network() -> None:
    probe = """
import sys

from services.nlp.mentions import ArticleText, extract_mentions

assert "spacy" not in sys.modules, "importing the service must not import spaCy"
assert extract_mentions([]) == ()
assert extract_mentions([ArticleText("blank", "  ")])[0].mentions == ()
assert "spacy" not in sys.modules, "a text-free batch must not load a model"
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": str(_PROJECT_ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout
