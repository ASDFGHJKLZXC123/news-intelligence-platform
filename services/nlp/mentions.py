"""Mention extraction: ADR 0005 stage 1 (spaCy NER, no LLM).

Extracts ORG/PERSON/GPE/PRODUCT spans from a batch of articles and tags every mention with
the assertion status of the sentence it sits in. Nothing here links, scores, or persists:
the typed results feed stage 2 (deterministic alias linking) and stage 3 (LLM adjudication),
which own `event_entities` — raw mentions are never written there.

The model (default ``en_core_web_trf``) is an explicit deployment prerequisite:
``python -m spacy download en_core_web_trf``. Importing this module imports no spaCy, loads
no model, and touches no network. The pipeline is loaded on the first extraction that has
text to process and cached per model name for the process lifetime; unit tests inject a
``SpacyLanguage``-shaped fake instead.
"""

from __future__ import annotations

import threading
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from packages.config.settings import get_settings
from services.nlp.assertions import AssertionStatus, classify_assertion


class MentionExtractionConfigurationError(RuntimeError):
    """Raised when the configured spaCy runtime or model is unavailable."""


class EntityLabel(StrEnum):
    """The only spaCy entity labels ADR 0005 stage 1 keeps."""

    ORG = "ORG"
    PERSON = "PERSON"
    GPE = "GPE"
    PRODUCT = "PRODUCT"


_KEPT_LABELS: Final[frozenset[str]] = frozenset(label.value for label in EntityLabel)

# Disabled only where it cannot cost a span or a sentence boundary. The parser stays (it is
# what segments sentences), and so do the transformer and the NER head; what is switched off
# is the lemmatizer/attribute_ruler tail, whose token annotations nothing downstream reads.
DISABLED_PIPELINE_COMPONENTS: Final[tuple[str, ...]] = ("attribute_ruler", "lemmatizer")


# --- The spaCy surface this service depends on ---------------------------------
class SpacySpan(Protocol):
    """The subset of ``spacy.tokens.Span`` read here (a sentence)."""

    text: str
    start_char: int
    end_char: int


class SpacyEntity(SpacySpan, Protocol):
    """A span that also carries an entity label."""

    label_: str


class SpacyDoc(Protocol):
    """The subset of ``spacy.tokens.Doc`` read here."""

    text: str
    ents: Iterable[SpacyEntity]
    sents: Iterable[SpacySpan]


class SpacyLanguage(Protocol):
    """The one spaCy entry point production uses: a batched ``pipe`` call."""

    def pipe(self, texts: Iterable[str], *, batch_size: int) -> Iterator[SpacyDoc]: ...


# --- Data transfer objects -----------------------------------------------------
@dataclass(frozen=True, slots=True)
class ArticleText:
    """One article's text to extract from; ``key`` is the caller's article identity."""

    key: str
    text: str


@dataclass(frozen=True, slots=True)
class SentenceContext:
    """A mention's sentence, plus the neighbours stage 3 quotes as adjudication context."""

    index: int
    text: str
    start_char: int
    end_char: int
    previous_text: str | None
    next_text: str | None

    @property
    def window_text(self) -> str:
        """The mention sentence +/- one adjacent sentence, without a second spaCy pass."""
        parts = (self.previous_text, self.text, self.next_text)
        return " ".join(part.strip() for part in parts if part and part.strip())


@dataclass(frozen=True, slots=True)
class EntityMention:
    """One extracted span with everything stages 2 and 3 need to link and adjudicate it."""

    article_key: str
    text: str
    label: EntityLabel
    start_char: int
    end_char: int
    sentence: SentenceContext
    assertion_status: AssertionStatus

    @property
    def start_in_sentence(self) -> int:
        """Mention start relative to its own sentence."""
        return self.start_char - self.sentence.start_char

    @property
    def end_in_sentence(self) -> int:
        """Mention end relative to its own sentence."""
        return self.end_char - self.sentence.start_char


@dataclass(frozen=True, slots=True)
class ArticleMentions:
    """One article's mentions in document order. Blank text yields no mentions."""

    article_key: str
    mentions: tuple[EntityMention, ...]


# --- Model loading -------------------------------------------------------------
_LANGUAGE_CACHE: dict[str, SpacyLanguage] = {}
_LANGUAGE_CACHE_LOCK: Final = threading.Lock()


def load_spacy_language(model_name: str) -> SpacyLanguage:
    """Return the named pipeline, loading it at most once per process.

    The lock is held across the load itself: two worker threads racing on a cold cache
    would otherwise each pay for a full transformer model.
    """
    with _LANGUAGE_CACHE_LOCK:
        language = _LANGUAGE_CACHE.get(model_name)
        if language is None:
            language = _load_language(model_name)
            _LANGUAGE_CACHE[model_name] = language
        return language


def _load_language(model_name: str) -> SpacyLanguage:
    try:
        import spacy
    except ImportError as exc:
        msg = (
            "spaCy is not installed, so mention extraction cannot run; install the "
            "`spacy` runtime pinned in requirements.txt"
        )
        raise MentionExtractionConfigurationError(msg) from exc
    try:
        return spacy.load(model_name, disable=list(DISABLED_PIPELINE_COMPONENTS))
    except OSError as exc:
        msg = (
            f"spaCy model {model_name!r} is not installed. The model is an explicit "
            f"deployment prerequisite, never an import-time download: run "
            f"`python -m spacy download {model_name}`."
        )
        raise MentionExtractionConfigurationError(msg) from exc


# --- Extraction ----------------------------------------------------------------
def extract_mentions(
    articles: Sequence[ArticleText],
    *,
    language: SpacyLanguage | None = None,
    model_name: str | None = None,
    batch_size: int | None = None,
) -> tuple[ArticleMentions, ...]:
    """Extract ORG/PERSON/GPE/PRODUCT mentions from a batch of articles.

    One ``nlp.pipe`` call per batch, never one call per article. Input order is preserved,
    the caller's sequence is never mutated, and blank-text articles return an empty result
    without reaching the model at all — so an empty or all-blank batch loads no model.
    """
    records = tuple(articles)
    extracted: list[tuple[EntityMention, ...]] = [() for _ in records]
    pending = [(index, record) for index, record in enumerate(records) if record.text.strip()]
    if pending:
        settings = get_settings()
        # Validated before the model is touched: a bad batch size must not cost a load.
        resolved_batch_size = _resolve_batch_size(batch_size, settings.ner_batch_size)
        pipeline = (
            load_spacy_language(model_name or settings.ner_model) if language is None else language
        )
        docs = pipeline.pipe([record.text for _, record in pending], batch_size=resolved_batch_size)
        for (index, record), doc in zip(pending, docs, strict=True):
            extracted[index] = _mentions_for_document(record.key, doc)
    return tuple(
        ArticleMentions(article_key=record.key, mentions=mentions)
        for record, mentions in zip(records, extracted, strict=True)
    )


def _resolve_batch_size(explicit: int | None, configured: int) -> int:
    size = configured if explicit is None else explicit
    if size < 1:
        msg = f"batch_size must be a positive number of articles, got {size}"
        raise ValueError(msg)
    return size


def _mentions_for_document(article_key: str, doc: SpacyDoc) -> tuple[EntityMention, ...]:
    """Map one document's entities onto sentences, in document order, without duplicates."""
    sentences = _sentence_contexts(doc)
    sentence_starts = [sentence.start_char for sentence in sentences]
    statuses: dict[int, AssertionStatus] = {}
    mentions: list[EntityMention] = []
    seen: set[tuple[int, int, str]] = set()
    for entity in doc.ents:
        if entity.label_ not in _KEPT_LABELS:
            continue
        span = (entity.start_char, entity.end_char, entity.label_)
        if span in seen:
            continue
        seen.add(span)
        # The containing sentence is the last one that starts at or before the mention.
        sentence = sentences[max(bisect_right(sentence_starts, entity.start_char) - 1, 0)]
        if sentence.index not in statuses:
            statuses[sentence.index] = classify_assertion(sentence.text)
        mentions.append(
            EntityMention(
                article_key=article_key,
                text=entity.text,
                label=EntityLabel(entity.label_),
                start_char=entity.start_char,
                end_char=entity.end_char,
                sentence=sentence,
                assertion_status=statuses[sentence.index],
            )
        )
    mentions.sort(key=lambda mention: (mention.start_char, mention.end_char))
    return tuple(mentions)


def _sentence_contexts(doc: SpacyDoc) -> tuple[SentenceContext, ...]:
    """Sentences in document order, each carrying its immediate neighbours' text.

    A pipeline that produces no sentence boundaries (a model configured without a parser)
    degrades to one sentence spanning the document rather than losing mention context.
    """
    try:
        spans = [(sent.text, sent.start_char, sent.end_char) for sent in doc.sents]
    except ValueError:  # spaCy raises when no component set sentence boundaries
        spans = []
    if not spans:
        spans = [(doc.text, 0, len(doc.text))]
    return tuple(
        SentenceContext(
            index=index,
            text=text,
            start_char=start,
            end_char=end,
            previous_text=spans[index - 1][0] if index > 0 else None,
            next_text=spans[index + 1][0] if index + 1 < len(spans) else None,
        )
        for index, (text, start, end) in enumerate(spans)
    )
