"""The ADR 0005 pipeline for one event: extract, link, adjudicate, persist.

Stage 1 (spaCy mentions), stage 2 (deterministic linking), and stage 3 (LLM adjudication) are
each self-contained. This is what runs them in order over one event's articles, and it is the
only place that decides what gets *written*:

* ``entity_resolution_runs`` -- one row per mention, whatever it linked to, including nothing.
  Ambiguous and NIL mentions are auditable and feed the weekly review queue.
* ``event_entities`` -- one row per accepted (event, entity) pair, carrying the assertion status
  in ``role``. Nothing else in the pipeline writes here.

Cheap-first is enforced by construction, not by convention: the extractor is called **once** for
the whole batch of articles, and the adjudicator is called only for mentions that landed in the
ambiguous band with candidates. An event whose mentions all accept or all NIL spends zero tokens.

Both collaborators are injected. In production they are the lazily-loaded spaCy pipeline and an
orchestrator-backed adjudicator; in tests they are fakes, so the unit suite loads no model, opens
no socket, and needs no API key.

Nothing here commits. The caller owns the transaction, so one event is one unit of work, and a
provider failure mid-event rolls back to the state the retry expects to find.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from db.models import Article, Event, EventArticle, Source
from services.entities.adjudication import (
    REASON_NO_ADJUDICATOR,
    AdjudicationDecision,
    MentionAdjudication,
    MentionAdjudicatorProtocol,
    record_adjudication,
)
from services.entities.event_links import persist_event_entity_link, role_for_assertion
from services.entities.news_linking import (
    ArticleLinkingContext,
    LinkBand,
    MentionLinkResult,
    NewsEntityLinker,
)
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import ArticleMentions, ArticleText, EntityMention
from services.provider_data.common import find_all, find_one, json_safe

#: The stage-1 model is English (``en_core_web_trf``), so only English articles may be fed to it.
#: An article whose language nobody recorded is *not* evidence that it is not English -- the RSS
#: feeds routinely omit the field -- so it is processed rather than silently dropped.
ENGLISH_LANGUAGE: Final = "en"

SKIP_NON_ENGLISH: Final = "language is not english"
SKIP_EMPTY: Final = "article has no text"
SKIP_MISSING: Final = "article row is missing"

#: One ``nlp.pipe`` call per event, over every eligible article.
MentionExtractor = Callable[[Sequence[ArticleText]], Sequence[ArticleMentions]]


class EventNotFoundError(ValueError):
    """Raised when the event to link does not exist."""


@dataclass(frozen=True, slots=True)
class SkippedArticle:
    """One article the pipeline deliberately did not extract from, and why."""

    article_id: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


@dataclass(frozen=True, slots=True)
class MentionOutcome:
    """What happened to one mention, end to end. Serializable, because the task returns it."""

    article_key: str
    surface: str
    start_char: int
    end_char: int
    band: LinkBand
    assertion_status: AssertionStatus
    reason: str
    adjudicated: bool = False
    adjudication_decision: AdjudicationDecision | None = None
    entity_id: uuid.UUID | None = None
    confidence_score: float | None = None
    role: str | None = None

    @property
    def linked(self) -> bool:
        return self.entity_id is not None

    def as_dict(self) -> dict[str, Any]:
        return json_safe(self)


@dataclass(frozen=True, slots=True)
class EventLinkingResult:
    """The immutable, JSON-serializable outcome of linking one event."""

    event_id: uuid.UUID
    articles_total: int
    articles_processed: int
    skipped_articles: tuple[SkippedArticle, ...]
    mentions: tuple[MentionOutcome, ...]
    extraction_calls: int

    @property
    def accepted_count(self) -> int:
        """Mentions the deterministic band accepted on its own -- no LLM was called for them."""
        return sum(1 for item in self.mentions if item.band is LinkBand.ACCEPT and item.linked)

    @property
    def adjudicated_count(self) -> int:
        return sum(1 for item in self.mentions if item.adjudicated)

    @property
    def linked_count(self) -> int:
        return sum(1 for item in self.mentions if item.linked)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "articles_total": self.articles_total,
            "articles_processed": self.articles_processed,
            "articles_skipped": len(self.skipped_articles),
            "skipped_articles": [item.as_dict() for item in self.skipped_articles],
            "extraction_calls": self.extraction_calls,
            "mentions_total": len(self.mentions),
            "mentions_accepted": self.accepted_count,
            "mentions_adjudicated": self.adjudicated_count,
            "links_persisted": self.linked_count,
            "mentions": [item.as_dict() for item in self.mentions],
        }


@dataclass(frozen=True, slots=True)
class _ArticleRecord:
    """One article's linking inputs, snapshotted off the ORM row before any model or LLM call."""

    article_id: uuid.UUID
    text: str
    url: str | None
    published_on: datetime.date | None
    source_category: str | None

    @property
    def key(self) -> str:
        return str(self.article_id)


def link_event_entities(
    session: Any,
    event_id: uuid.UUID,
    *,
    extractor: MentionExtractor,
    adjudicator: MentionAdjudicatorProtocol | None = None,
) -> EventLinkingResult:
    """Run ADR 0005 over one event's articles and persist what it accepts.

    Deterministic throughout: articles are processed in a stable order, mentions in document
    order, and every write is an upsert keyed by something the inputs already determine. Running
    it twice over unchanged data produces the same rows with the same scores.
    """

    event = find_one(session, Event, id=event_id)
    if event is None:
        msg = f"event does not exist: {event_id}"
        raise EventNotFoundError(msg)

    records, skipped = _article_records(session, event_id)
    extracted: Sequence[ArticleMentions] = ()
    if records:
        # The one extraction call. Batching is not an optimization here -- it is the ADR's
        # "CPU-batch in Celery", and a per-article call would pay the transformer's fixed cost
        # once per article.
        extracted = extractor(
            tuple(ArticleText(key=record.key, text=record.text) for record in records)
        )

    location_terms = _location_terms(event)
    linker = NewsEntityLinker(session)
    outcomes: list[MentionOutcome] = []
    for record, article_mentions in zip(records, extracted, strict=True):
        context = ArticleLinkingContext(
            article_key=record.key,
            published_on=record.published_on,
            url=record.url,
            source_category=record.source_category,
            text=record.text,
            location_terms=location_terms,
        )
        for index, mention in enumerate(article_mentions.mentions):
            outcomes.append(
                _link_one(
                    session,
                    linker=linker,
                    mention=mention,
                    context=_with_co_mentions(context, article_mentions.mentions, index),
                    event_id=event_id,
                    adjudicator=adjudicator,
                )
            )

    return EventLinkingResult(
        event_id=event_id,
        articles_total=len(records) + len(skipped),
        articles_processed=len(records),
        skipped_articles=skipped,
        mentions=tuple(outcomes),
        extraction_calls=1 if records else 0,
    )


def _link_one(
    session: Any,
    *,
    linker: NewsEntityLinker,
    mention: EntityMention,
    context: ArticleLinkingContext,
    event_id: uuid.UUID,
    adjudicator: MentionAdjudicatorProtocol | None,
) -> MentionOutcome:
    """Link one mention deterministically, adjudicate it only if the band says to, then persist."""

    result = linker.link(mention, context, persist_run=True)

    if result.should_attach:
        return _attach(
            session,
            event_id=event_id,
            mention=mention,
            result=result,
            entity_id=result.matched_entity_id,
            confidence_score=result.confidence_score,
        )

    if result.band is not LinkBand.ADJUDICATE or not result.candidates:
        # NIL: nothing scored high enough to be worth a token. The run row is already written,
        # so the surface still reaches the weekly review queue.
        return _unlinked(mention, result)

    if adjudicator is None:
        return _unlinked(mention, result, reason=REASON_NO_ADJUDICATOR)

    # Only here, and only here, does the pipeline spend an LLM call.
    adjudication = adjudicator.adjudicate(mention, result)
    record_adjudication(session, result, adjudication)
    if not adjudication.should_attach:
        return _unlinked(mention, result, adjudication=adjudication)

    return _attach(
        session,
        event_id=event_id,
        mention=mention,
        result=result,
        entity_id=adjudication.entity_id,
        confidence_score=adjudication.confidence_score,
        adjudication=adjudication,
    )


def _attach(
    session: Any,
    *,
    event_id: uuid.UUID,
    mention: EntityMention,
    result: MentionLinkResult,
    entity_id: uuid.UUID | None,
    confidence_score: float | None,
    adjudication: MentionAdjudication | None = None,
) -> MentionOutcome:
    """Persist one accepted link. The mention keeps its own entity: a brand never becomes a parent."""

    if entity_id is None or confidence_score is None:  # defensive: neither path can produce this
        return _unlinked(mention, result, adjudication=adjudication)

    persist_event_entity_link(
        session,
        event_id=event_id,
        entity_profile_id=entity_id,
        confidence_score=confidence_score,
        assertion_status=mention.assertion_status,
    )
    return MentionOutcome(
        article_key=result.article_key,
        surface=result.surface,
        start_char=result.start_char,
        end_char=result.end_char,
        band=result.band,
        assertion_status=mention.assertion_status,
        reason=result.reason if adjudication is None else adjudication.reason,
        adjudicated=adjudication is not None,
        adjudication_decision=None if adjudication is None else adjudication.decision,
        entity_id=entity_id,
        confidence_score=confidence_score,
        role=role_for_assertion(mention.assertion_status),
    )


def _unlinked(
    mention: EntityMention,
    result: MentionLinkResult,
    *,
    adjudication: MentionAdjudication | None = None,
    reason: str | None = None,
) -> MentionOutcome:
    return MentionOutcome(
        article_key=result.article_key,
        surface=result.surface,
        start_char=result.start_char,
        end_char=result.end_char,
        band=result.band,
        assertion_status=mention.assertion_status,
        reason=reason or (result.reason if adjudication is None else adjudication.reason),
        adjudicated=adjudication is not None,
        adjudication_decision=None if adjudication is None else adjudication.decision,
    )


def _article_records(
    session: Any, event_id: uuid.UUID
) -> tuple[tuple[_ArticleRecord, ...], tuple[SkippedArticle, ...]]:
    """The event's articles, in a stable order, split into what is extractable and what is not."""

    article_ids = sorted(
        {row.article_id for row in find_all(session, EventArticle, event_id=event_id)}, key=str
    )
    records: list[_ArticleRecord] = []
    skipped: list[SkippedArticle] = []
    for article_id in article_ids:
        article = find_one(session, Article, id=article_id)
        if article is None:
            skipped.append(SkippedArticle(article_id=str(article_id), reason=SKIP_MISSING))
            continue
        if not _is_english(article.language):
            skipped.append(SkippedArticle(article_id=str(article_id), reason=SKIP_NON_ENGLISH))
            continue
        text = _article_text(article)
        if not text:
            skipped.append(SkippedArticle(article_id=str(article_id), reason=SKIP_EMPTY))
            continue
        records.append(
            _ArticleRecord(
                article_id=article.id,
                text=text,
                url=article.url,
                published_on=None if article.published_at is None else article.published_at.date(),
                source_category=_source_category(session, article.source_id),
            )
        )
    return tuple(records), tuple(skipped)


def _article_text(article: Article) -> str:
    """Title plus body, or title plus summary when there is no body -- never both.

    A summary is usually a truncation of the body, so concatenating the two would extract the
    same lead sentences twice and let one mention count as two co-mentions of itself.
    """

    parts = (article.title, article.body or article.summary)
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _is_english(language: str | None) -> bool:
    """``en``, ``en-US``, ``EN_gb``, or unrecorded. Anything else is not for the English model."""

    if language is None or not language.strip():
        return True
    primary = language.strip().casefold().replace("_", "-").split("-", 1)[0]
    return primary == ENGLISH_LANGUAGE


def _source_category(session: Any, source_id: uuid.UUID | None) -> str | None:
    """The article's source category, for ADR 0005's "financial source raises company prior".

    The committed ``sources`` table carries no editorial category column; the closest field is
    ``source_type``, which is a transport ("rss"). It is passed through as the category rather
    than invented elsewhere: the linker matches it against the ADR's financial categories, so a
    transport string simply scores zero, and a deployment that classifies its sources gets the
    signal without a schema change. Adding a real column is a migration, and out of scope here.
    """

    if source_id is None:
        return None
    source = find_one(session, Source, id=source_id)
    if source is None:
        return None
    return (source.source_type or "").strip() or None


def _location_terms(event: Event) -> tuple[str, ...]:
    """The event's country, which is what ``EntityProfile.country`` is compared against."""

    country = (event.country or "").strip()
    return (country,) if country else ()


def _with_co_mentions(
    context: ArticleLinkingContext, mentions: Sequence[EntityMention], index: int
) -> ArticleLinkingContext:
    """This mention's own context: every *other* surface in the article supports it, it does not.

    Deduplicated in document order, so two mentions of "Acme" contribute one co-mention surface
    rather than two, and the linker sees the same context whichever of them it is scoring.
    """

    others = dict.fromkeys(
        mention.text
        for position, mention in enumerate(mentions)
        if position != index and mention.text.strip()
    )
    return replace(context, co_mention_surfaces=tuple(others))
