"""Shared fakes and row builders for the ADR 0005 stage-3 unit tests.

Not a test module (pytest collects ``test_*``), just the fixtures the adjudication,
persistence, parent-exposure, and pipeline suites all need: a dict-backed session, an
extractor that never touches spaCy, and an adjudicator that never touches an LLM.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from typing import Any

from db.models import (
    Article,
    EntityAlias,
    EntityProfile,
    EntityRelationship,
    Event,
    EventArticle,
    Source,
)
from services.entities.adjudication import AdjudicationDecision, MentionAdjudication
from services.entities.news_linking import MentionLinkResult
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import (
    ArticleMentions,
    ArticleText,
    EntityLabel,
    EntityMention,
    SentenceContext,
)
from services.provider_data.common import normalize_alias, normalize_name


class FakeSession:
    """The dict-backed session the identity suites use, with the reads these services make."""

    def __init__(self, *rows: Any) -> None:
        self.items: list[Any] = list(rows)
        self.flushes = 0

    def add(self, obj: Any) -> None:
        self.items.append(obj)

    def flush(self) -> None:
        self.flushes += 1

    def find_one(self, model: type[Any], **criteria: Any) -> Any | None:
        for item in self.find_all(model, **criteria):
            return item
        return None

    def find_all(self, model: type[Any], **criteria: Any) -> list[Any]:
        return [
            item
            for item in self.items
            if isinstance(item, model)
            and all(getattr(item, key) == value for key, value in criteria.items())
        ]

    def all_of(self, model: type[Any]) -> list[Any]:
        return [item for item in self.items if isinstance(item, model)]


class FakeExtractor:
    """One batched call, recorded. Returns the mentions a test scripted per article key."""

    def __init__(self, mentions_by_key: dict[str, tuple[EntityMention, ...]] | None = None) -> None:
        self.mentions_by_key = mentions_by_key or {}
        self.calls: list[tuple[ArticleText, ...]] = []

    def __call__(self, articles: Sequence[ArticleText]) -> tuple[ArticleMentions, ...]:
        batch = tuple(articles)
        self.calls.append(batch)
        return tuple(
            ArticleMentions(
                article_key=article.key, mentions=self.mentions_by_key.get(article.key, ())
            )
            for article in batch
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeAdjudicator:
    """Stands in for the LLM: records every mention it was asked about, answers from a script."""

    def __init__(self, decide: Any = None) -> None:
        self._decide = decide
        self.calls: list[tuple[EntityMention, MentionLinkResult]] = []

    def adjudicate(self, mention: EntityMention, result: MentionLinkResult) -> MentionAdjudication:
        self.calls.append((mention, result))
        if self._decide is not None:
            return self._decide(mention, result)
        top = result.candidates[0]
        return MentionAdjudication(
            target_id=result.target_id,
            decision=AdjudicationDecision.SELECTED,
            reason="fake selection",
            entity_id=top.entity_id,
            confidence_score=top.score,
            trace_id="trace-fake",
        )

    def nil(self, _mention: EntityMention, result: MentionLinkResult) -> MentionAdjudication:
        return MentionAdjudication(
            target_id=result.target_id,
            decision=AdjudicationDecision.NIL,
            reason="fake NIL",
            trace_id="trace-fake",
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


def profile(name: str, **kwargs: Any) -> EntityProfile:
    return EntityProfile(
        id=kwargs.pop("id", uuid.uuid4()),
        canonical_name=name,
        normalized_name=normalize_name(name),
        **kwargs,
    )


def alias(
    entity: EntityProfile,
    surface: str,
    *,
    alias_type: str = "legal_name",
    source: str = "sec-edgar",
) -> EntityAlias:
    return EntityAlias(
        id=uuid.uuid4(),
        entity_id=entity.id,
        alias=surface,
        normalized_alias=normalize_alias(surface),
        alias_type=alias_type,
        source=source,
    )


def parent_edge(
    parent: EntityProfile,
    child: EntityProfile,
    *,
    provider: str = "gleif",
    confidence_score: float | None = 1.0,
    valid_from: datetime.date | None = None,
    valid_to: datetime.date | None = None,
    relationship_type: str = "parent_of",
) -> EntityRelationship:
    return EntityRelationship(
        id=uuid.uuid4(),
        parent_entity_id=parent.id,
        child_entity_id=child.id,
        relationship_type=relationship_type,
        provider=provider,
        confidence_score=confidence_score,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def mention(
    surface: str = "Acme",
    *,
    article_key: str,
    sentence: str | None = None,
    previous_text: str | None = None,
    next_text: str | None = None,
    label: EntityLabel = EntityLabel.ORG,
    assertion_status: AssertionStatus = AssertionStatus.ASSERTED,
    start_char: int = 0,
) -> EntityMention:
    text = sentence if sentence is not None else f"{surface} appeared in the piece today."
    return EntityMention(
        article_key=article_key,
        text=surface,
        label=label,
        start_char=start_char,
        end_char=start_char + len(surface),
        sentence=SentenceContext(
            index=1 if previous_text else 0,
            text=text,
            start_char=0,
            end_char=len(text),
            previous_text=previous_text,
            next_text=next_text,
        ),
        assertion_status=assertion_status,
    )


def event(**kwargs: Any) -> Event:
    return Event(
        id=kwargs.pop("id", uuid.uuid4()),
        title=kwargs.pop("title", "Acme in the news"),
        article_count=kwargs.pop("article_count", 1),
        source_count=kwargs.pop("source_count", 1),
        **kwargs,
    )


def source(**kwargs: Any) -> Source:
    return Source(
        id=kwargs.pop("id", uuid.uuid4()),
        name=kwargs.pop("name", "Example Wire"),
        source_type=kwargs.pop("source_type", "rss"),
        feed_url=kwargs.pop("feed_url", "https://wire.example.com/feed"),
    )


def article(feed: Source, **kwargs: Any) -> Article:
    identifier = kwargs.pop("id", uuid.uuid4())
    return Article(
        id=identifier,
        source_id=feed.id,
        url=kwargs.pop("url", f"https://wire.example.com/{identifier}"),
        url_hash=kwargs.pop("url_hash", uuid.uuid4().hex),
        title=kwargs.pop("title", "Acme reported revenue"),
        body=kwargs.pop("body", "Acme reported revenue for the period."),
        summary=kwargs.pop("summary", None),
        language=kwargs.pop("language", "en"),
        published_at=kwargs.pop("published_at", datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)),
        **kwargs,
    )


def event_article(subject: Event, item: Article) -> EventArticle:
    return EventArticle(event_id=subject.id, article_id=item.id)
