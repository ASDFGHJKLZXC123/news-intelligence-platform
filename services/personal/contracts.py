"""Pure personal workflow rules: interest matching, ordering and local dates."""

from __future__ import annotations

import datetime
import re
import unicodedata
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_PHRASES = 20
MAX_PHRASE_CHARS = 80
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_phrase_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(_TOKEN_RE.findall(normalized))


def validate_phrases(
    values: list[str] | tuple[str, ...], *, name: str
) -> tuple[tuple[str, ...], ...]:
    if len(values) > MAX_PHRASES:
        raise ValueError(f"{name} supports at most {MAX_PHRASES} phrases")
    result: list[tuple[str, ...]] = []
    for value in values:
        if len(value) > MAX_PHRASE_CHARS:
            raise ValueError(f"{name} phrases must be at most {MAX_PHRASE_CHARS} characters")
        tokens = normalize_phrase_tokens(value)
        if not tokens:
            raise ValueError(f"{name} contains a phrase with no letter or digit tokens")
        result.append(tokens)
    return tuple(result)


def _contains_phrase(haystack: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    return any(
        haystack[index : index + width] == phrase for index in range(len(haystack) - width + 1)
    )


def article_matches_profile(
    title: str,
    summary: str | None,
    *,
    include_phrases: list[str] | tuple[str, ...],
    exclude_phrases: list[str] | tuple[str, ...],
) -> bool:
    includes = validate_phrases(include_phrases, name="include_phrases")
    excludes = validate_phrases(exclude_phrases, name="exclude_phrases")
    fields = (normalize_phrase_tokens(title), normalize_phrase_tokens(summary or ""))
    if any(_contains_phrase(field, phrase) for phrase in excludes for field in fields):
        return False
    return not includes or any(
        _contains_phrase(field, phrase) for phrase in includes for field in fields
    )


def personal_local_date(now: datetime.datetime, timezone: str) -> datetime.date:
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone {timezone!r}") from exc
    return now.astimezone(zone).date()


@dataclass(frozen=True)
class RankedEvent:
    event_id: uuid.UUID
    source_count: int
    hotness: float | None
    newest_publication: datetime.datetime | None


def _ranking_key(item: RankedEvent) -> tuple[int, int, float, int, float, str]:
    published = item.newest_publication
    if published is not None:
        if published.tzinfo is None:
            published = published.replace(tzinfo=datetime.UTC)
        published_value = -published.astimezone(datetime.UTC).timestamp()
    else:
        published_value = 0.0
    return (
        -item.source_count,
        item.hotness is None,
        -(item.hotness or 0.0),
        published is None,
        published_value,
        str(item.event_id),
    )


def rank_events(items: list[RankedEvent] | tuple[RankedEvent, ...]) -> tuple[RankedEvent, ...]:
    return tuple(sorted(items, key=_ranking_key))
