"""Bounded, workspace-scoped reads and persistent personal event saves."""

from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import Integer, Text, and_, case, cast, delete, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db.models import (
    Article,
    Event,
    EventArticle,
    PersonalBriefSnapshot,
    PersonalRun,
    Source,
    WatchlistItem,
)
from db.models.personal import (
    PersonalProfileRevision,
    PersonalRunEventObservation,
    PersonalWorkspace,
)
from services.personal.contracts import normalize_phrase_tokens
from services.personal.workspace import require_owner
from services.reports.context import MAX_EXCERPT_CHARS, build_source_excerpt

MAX_RUN_EVENT_CANDIDATES = 2_000
EVENT_FILTER_CHUNK_SIZE = 100
MAX_EVENT_TITLE_CHARS = 4_096
MAX_EVENT_SUMMARY_CHARS = 16_384
MAX_OBSERVATION_SEARCH_BYTES = 262_144
MAX_RANKING_INPUT_BYTES = 8_192


def _iso(value: datetime.date | datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.UTC)
        return value.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")
    return value.isoformat()


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _text_matches(query: str, values: tuple[str, ...]) -> bool:
    phrase = normalize_phrase_tokens(query)
    if not phrase:
        return True
    for value in values:
        tokens = normalize_phrase_tokens(value)
        width = len(phrase)
        if any(tokens[index : index + width] == phrase for index in range(len(tokens) - width + 1)):
            return True
    return False


def _event_id_strings(event_id: uuid.UUID) -> tuple[str, ...]:
    canonical = str(event_id)
    compact = event_id.hex
    return (canonical, canonical.upper(), compact, compact.upper(), "{" + canonical + "}")


@dataclass(frozen=True)
class EventObservation:
    event: Event
    observation: PersonalRunEventObservation
    saved: bool


class PersonalRepository:
    def __init__(self, session: Session, workspace: PersonalWorkspace):
        self.session = session
        self.workspace = workspace

    def run(self, run_id: uuid.UUID | None = None) -> PersonalRun | None:
        base = select(PersonalRun).where(PersonalRun.workspace_id == self.workspace.id)
        if run_id is not None:
            return self.session.execute(base.where(PersonalRun.id == run_id)).scalar_one_or_none()
        readable = (
            select(PersonalRun.id)
            .where(
                PersonalRun.workspace_id == self.workspace.id,
                (
                    (PersonalRun.state == "succeeded")
                    | PersonalRun.id.in_(select(PersonalRunEventObservation.run_id))
                    | (
                        PersonalRun.snapshot_id.is_not(None)
                        & (
                            (
                                PersonalRun.coverage["feeds_succeeded"].astext.cast(Integer)
                                > 0
                            )
                            | (PersonalRun.coverage["feeds_paused"].astext.cast(Integer) > 0)
                        )
                    )
                ),
            )
            .order_by(PersonalRun.local_date.desc(), PersonalRun.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        run_id = self.session.execute(select(readable)).scalar_one_or_none()
        return self.session.get(PersonalRun, run_id) if run_id is not None else None

    def latest_run(self) -> PersonalRun | None:
        return self.session.execute(
            select(PersonalRun)
            .where(PersonalRun.workspace_id == self.workspace.id)
            .order_by(PersonalRun.local_date.desc(), PersonalRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def latest_successful_run(self) -> PersonalRun | None:
        return self.session.execute(
            select(PersonalRun)
            .where(
                PersonalRun.workspace_id == self.workspace.id,
                PersonalRun.state == "succeeded",
            )
            .order_by(PersonalRun.local_date.desc(), PersonalRun.updated_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def profile_for_run(self, run: PersonalRun) -> PersonalProfileRevision:
        return self.session.get(PersonalProfileRevision, run.profile_revision_id)

    def _latest_observations(self, run: PersonalRun) -> list[PersonalRunEventObservation]:
        latest = (
            select(
                PersonalRunEventObservation.event_id,
                func.max(PersonalRunEventObservation.revision).label("revision"),
            )
            .where(PersonalRunEventObservation.run_id == run.id)
            .group_by(PersonalRunEventObservation.event_id)
            .subquery()
        )
        rows = list(
            self.session.execute(
                select(PersonalRunEventObservation)
                .join(
                    latest,
                    and_(
                        latest.c.event_id == PersonalRunEventObservation.event_id,
                        latest.c.revision == PersonalRunEventObservation.revision,
                    ),
                )
                .where(PersonalRunEventObservation.run_id == run.id)
                .limit(MAX_RUN_EVENT_CANDIDATES + 1)
            ).scalars()
        )
        if len(rows) > MAX_RUN_EVENT_CANDIDATES:
            raise RuntimeError(
                f"personal run exceeds the {MAX_RUN_EVENT_CANDIDATES} candidate read boundary"
            )
        return rows

    def _snapshot_observations(
        self, run: PersonalRun, snapshot: PersonalBriefSnapshot
    ) -> list[PersonalRunEventObservation]:
        candidates = snapshot.input_payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > MAX_RUN_EVENT_CANDIDATES:
            raise RuntimeError("personal snapshot has an invalid candidate observation list")
        try:
            observation_ids = [uuid.UUID(item["observation_id"]) for item in candidates]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("personal snapshot has an invalid observation identity") from exc
        if len(set(observation_ids)) != len(observation_ids):
            raise RuntimeError("personal snapshot repeats a candidate observation")
        rows = list(
            self.session.execute(
                select(PersonalRunEventObservation).where(
                    PersonalRunEventObservation.run_id == run.id,
                    PersonalRunEventObservation.id.in_(observation_ids),
                )
            ).scalars()
        )
        by_id = {item.id: item for item in rows}
        if len(by_id) != len(observation_ids):
            raise RuntimeError("personal snapshot observation is unavailable")
        ordered = [by_id[item_id] for item_id in observation_ids]
        for payload, observation in zip(candidates, ordered, strict=True):
            if (
                payload.get("event_id") != str(observation.event_id)
                or payload.get("revision") != observation.revision
            ):
                raise RuntimeError("personal snapshot observation identity changed")
        if [item.event_id for item in ordered] != list(snapshot.candidate_event_ids):
            raise RuntimeError("personal snapshot candidate order differs from its observations")
        return ordered

    def _observation_for_event(
        self, run: PersonalRun, event_id: uuid.UUID
    ) -> PersonalRunEventObservation | None:
        snapshot = self.session.execute(
            select(PersonalBriefSnapshot).where(PersonalBriefSnapshot.run_id == run.id)
        ).scalar_one_or_none()
        if snapshot is not None:
            return next(
                (
                    item
                    for item in self._snapshot_observations(run, snapshot)
                    if item.event_id == event_id
                ),
                None,
            )
        return self.session.execute(
            select(PersonalRunEventObservation)
            .where(
                PersonalRunEventObservation.run_id == run.id,
                PersonalRunEventObservation.event_id == event_id,
            )
            .order_by(PersonalRunEventObservation.revision.desc())
            .limit(1)
        ).scalar_one_or_none()

    def list_events(
        self,
        *,
        run_id: uuid.UUID | None,
        query: str | None,
        saved_only: bool,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int, PersonalRun | None, PersonalRun | None]:
        run = self.run(run_id)
        latest = self.latest_run()
        if run is None:
            return [], 0, None, latest
        snapshot = self.session.execute(
            select(PersonalBriefSnapshot).where(PersonalBriefSnapshot.run_id == run.id)
        ).scalar_one_or_none()
        observation = PersonalRunEventObservation
        if snapshot is not None:
            candidates = snapshot.input_payload.get("candidates")
            if not isinstance(candidates, list) or len(candidates) > MAX_RUN_EVENT_CANDIDATES:
                raise RuntimeError("personal snapshot has an invalid candidate observation list")
            try:
                expected = {
                    uuid.UUID(item["observation_id"]): (
                        uuid.UUID(item["event_id"]),
                        int(item["revision"]),
                        index,
                    )
                    for index, item in enumerate(candidates)
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("personal snapshot has an invalid observation identity") from exc
            if len(expected) != len(candidates):
                raise RuntimeError("personal snapshot repeats a candidate observation")
            identity_rows = self.session.execute(
                select(observation.id, observation.event_id, observation.revision).where(
                    observation.run_id == run.id,
                    observation.id.in_(list(expected)),
                )
            ).all()
            if len(identity_rows) != len(expected):
                raise RuntimeError("personal snapshot observation is unavailable")
            for identity in identity_rows:
                expected_event, expected_revision, _index = expected[identity.id]
                if (identity.event_id, identity.revision) != (
                    expected_event,
                    expected_revision,
                ):
                    raise RuntimeError("personal snapshot observation identity changed")
            ordered_events = [value[0] for value in expected.values()]
            if ordered_events != list(snapshot.candidate_event_ids):
                raise RuntimeError(
                    "personal snapshot candidate order differs from its observations"
                )
            observation_filter = observation.id.in_(list(expected))
            ordering = (
                case(
                    {item_id: values[2] for item_id, values in expected.items()},
                    value=observation.id,
                    else_=len(expected),
                )
                if expected
                else observation.id
            )
        else:
            latest_revisions = (
                select(
                    observation.event_id,
                    func.max(observation.revision).label("revision"),
                )
                .where(observation.run_id == run.id)
                .group_by(observation.event_id)
                .subquery()
            )
            observation_filter = and_(
                observation.run_id == run.id,
                observation.event_id == latest_revisions.c.event_id,
                observation.revision == latest_revisions.c.revision,
            )
            ordering = observation.ranking_inputs["sort_key"]

        candidate_count = int(
            self.session.scalar(
                select(func.count())
                .select_from(observation)
                .where(observation_filter)
            )
            or 0
        )
        if candidate_count > MAX_RUN_EVENT_CANDIDATES:
            raise RuntimeError(
                f"personal run exceeds the {MAX_RUN_EVENT_CANDIDATES} candidate read boundary"
            )

        owner_id = self.workspace.owner_id
        if owner_id is None:
            saved_expression = literal(False)
        else:
            normalized_pointer = func.replace(
                func.replace(
                    func.replace(func.lower(WatchlistItem.item_id), "{", ""), "}", ""
                ),
                "-",
                "",
            )
            normalized_event_id = func.replace(cast(observation.event_id, Text), "-", "")
            saved_expression = exists(
                select(WatchlistItem.id).where(
                    WatchlistItem.user_id == owner_id,
                    WatchlistItem.item_type == "event",
                    normalized_pointer == normalized_event_id,
                )
            )

        source_text = cast(observation.source_inputs, Text)
        ranking_text = cast(observation.ranking_inputs, Text)
        base = (
            select(
                observation.id.label("observation_id"),
                observation.event_id,
                observation.revision,
                func.cardinality(observation.qualifying_article_ids).label("article_count"),
                func.left(Event.title, MAX_EVENT_TITLE_CHARS + 1).label("event_title"),
                func.length(Event.title).label("event_title_length"),
                func.left(Event.summary, MAX_EVENT_SUMMARY_CHARS + 1).label("event_summary"),
                func.length(Event.summary).label("event_summary_length"),
                func.left(source_text, MAX_OBSERVATION_SEARCH_BYTES + 1).label("source_text"),
                func.length(source_text).label("source_text_length"),
                func.left(ranking_text, MAX_RANKING_INPUT_BYTES + 1).label("ranking_text"),
                func.length(ranking_text).label("ranking_text_length"),
                saved_expression.label("saved"),
            )
            .join(Event, Event.id == observation.event_id)
            .where(observation_filter)
        )
        if saved_only:
            base = base.where(saved_expression)
        ordered = base.order_by(ordering, observation.event_id)

        def serialize(row: Any) -> tuple[dict[str, Any], tuple[str, ...]]:
            if (
                (row.event_title_length or 0) > MAX_EVENT_TITLE_CHARS
                or (row.event_summary_length or 0) > MAX_EVENT_SUMMARY_CHARS
                or (row.source_text_length or 0) > MAX_OBSERVATION_SEARCH_BYTES
                or (row.ranking_text_length or 0) > MAX_RANKING_INPUT_BYTES
            ):
                raise RuntimeError("personal event observation exceeds its bounded read contract")
            try:
                source_inputs = json.loads(row.source_text)
                ranking = json.loads(row.ranking_text)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("personal event observation payload is invalid") from exc
            source_articles = source_inputs.get("articles")
            if not isinstance(source_articles, list):
                raise RuntimeError("personal event observation source articles are invalid")
            searchable = (
                row.event_title,
                *(str(item.get("title") or "") for item in source_articles),
                *(str(item.get("rss_summary") or "") for item in source_articles),
            )
            return (
                {
                    "id": str(row.event_id),
                    "title": row.event_title,
                    "summary": row.event_summary,
                    "saved": bool(row.saved),
                    "status": "complete"
                    if run.state in {"succeeded", "partially_failed"}
                    else "incomplete",
                    "source_count": ranking.get("source_count", 0),
                    "article_count": int(row.article_count or 0),
                    "hotness_score": float(ranking["hotness"])
                    if ranking.get("hotness") is not None
                    else None,
                    "newest_publication_at": ranking.get("newest_publication_at"),
                    "observation_revision": row.revision,
                },
                searchable,
            )

        if not query:
            total = int(
                self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
            )
            page = self.session.execute(ordered.limit(limit).offset(offset)).all()
            return [serialize(row)[0] for row in page], total, run, latest

        rows: list[dict[str, Any]] = []
        total = 0
        scan_offset = 0
        while scan_offset < candidate_count:
            chunk = self.session.execute(
                ordered.limit(EVENT_FILTER_CHUNK_SIZE).offset(scan_offset)
            ).all()
            if not chunk:
                break
            for raw_row in chunk:
                item, searchable = serialize(raw_row)
                if not _text_matches(query, searchable):
                    continue
                if offset <= total < offset + limit:
                    rows.append(item)
                total += 1
            scan_offset += len(chunk)
        return rows, total, run, latest

    def event_detail(self, event_id: uuid.UUID, run_id: uuid.UUID | None) -> dict[str, Any] | None:
        event = self.session.get(Event, event_id)
        if event is None:
            return None
        run = self.run(run_id) if run_id is not None else None
        if run_id is not None and run is None:
            raise LookupError("personal run not found")
        in_scope_ids: set[uuid.UUID] = set()
        if run is not None:
            observation = self._observation_for_event(run, event_id)
            if observation is not None:
                in_scope_ids = set(observation.qualifying_article_ids)
        total = int(
            self.session.scalar(
                select(func.count())
                .select_from(EventArticle)
                .where(EventArticle.event_id == event_id)
            )
            or 0
        )
        saved = False
        if self.workspace.owner_id is not None:
            saved = (
                self.session.execute(
                    select(WatchlistItem.id).where(
                        WatchlistItem.user_id == self.workspace.owner_id,
                        WatchlistItem.item_type == "event",
                        WatchlistItem.item_id == str(event_id),
                    )
                ).first()
                is not None
            )
        return {
            "id": str(event.id),
            "title": event.title,
            "summary": event.summary,
            "event_type": event.event_type,
            "country": event.country,
            "region": event.region,
            "saved": saved,
            "membership_total": total,
            "run_id": str(run.id) if run is not None else None,
            "in_scope_article_ids": [str(value) for value in sorted(in_scope_ids, key=str)],
            "updated_at": _iso(event.updated_at),
        }

    def event_sources(
        self, event_id: uuid.UUID, *, run_id: uuid.UUID | None, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        detail = self.event_detail(event_id, run_id)
        if detail is None:
            return None
        in_scope = {uuid.UUID(value) for value in detail["in_scope_article_ids"]}
        selected_run = self.run(run_id) if run_id is not None else None
        frozen_enrichment = (
            set(selected_run.enrichment_article_ids) if selected_run is not None else set()
        )
        summary_head = func.left(Article.summary, MAX_EXCERPT_CHARS + 1)
        body_head = func.left(Article.body, MAX_EXCERPT_CHARS + 1)
        base = (
            select(
                Article.id.label("article_id"),
                Article.title,
                Article.url,
                Article.published_at,
                Article.source_id,
                Source.name.label("publisher"),
                summary_head.label("summary_head"),
                body_head.label("body_head"),
                func.length(Article.summary).label("summary_length"),
                func.length(Article.body).label("body_length"),
            )
            .join(EventArticle, EventArticle.article_id == Article.id)
            .join(Source, Source.id == Article.source_id)
            .where(EventArticle.event_id == event_id)
        )
        total = int(self.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
        rows = self.session.execute(
            base.order_by(Article.published_at.desc().nullslast(), Article.id)
            .limit(limit)
            .offset(offset)
        ).all()
        result = []
        for row in rows:
            excerpt = build_source_excerpt(
                row.summary_head,
                row.body_head,
                summary_length=row.summary_length,
                body_length=row.body_length,
            )
            result.append(
                {
                    "article_id": str(row.article_id),
                    "source_id": str(row.source_id),
                    "title": row.title,
                    "publisher": row.publisher or None,
                    "published_at": _iso(row.published_at),
                    "excerpt": None if excerpt.is_empty else excerpt.text,
                    "excerpt_origin": None if excerpt.is_empty else excerpt.origin.value,
                    "excerpt_truncated": excerpt.truncated,
                    "url": _safe_url(row.url),
                    "in_run_scope": (
                        row.article_id in frozen_enrichment if run_id is not None else None
                    ),
                    "qualifies_for_brief": row.article_id in in_scope
                    if run_id is not None
                    else None,
                }
            )
        return result, total

    def save(self, event_id: uuid.UUID) -> WatchlistItem | None:
        owner_id = require_owner(self.workspace)
        event = self.session.get(Event, event_id)
        if event is None:
            return None
        existing = (
            self.session.execute(
                select(WatchlistItem).where(
                    WatchlistItem.user_id == owner_id,
                    WatchlistItem.item_type == "event",
                    WatchlistItem.item_id.in_(_event_id_strings(event_id)),
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return existing
        stmt = (
            insert(WatchlistItem)
            .values(
                id=uuid.uuid4(),
                user_id=owner_id,
                item_type="event",
                item_id=str(event_id),
                label=event.title,
                item_metadata={"personal_workspace_id": str(self.workspace.id)},
                alert_enabled=False,
            )
            .on_conflict_do_nothing(constraint="uq_watchlist_items_user_type_item")
            .returning(WatchlistItem.id)
        )
        inserted = self.session.execute(stmt).scalar_one_or_none()
        if inserted is not None:
            return self.session.get(WatchlistItem, inserted)
        return self.session.execute(
            select(WatchlistItem).where(
                WatchlistItem.user_id == owner_id,
                WatchlistItem.item_type == "event",
                WatchlistItem.item_id == str(event_id),
            )
        ).scalar_one()

    def unsave(self, event_id: uuid.UUID) -> None:
        owner_id = require_owner(self.workspace)
        self.session.execute(
            delete(WatchlistItem).where(
                WatchlistItem.user_id == owner_id,
                WatchlistItem.item_type == "event",
                WatchlistItem.item_id.in_(_event_id_strings(event_id)),
            )
        )

    def saved(self, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        owner_id = require_owner(self.workspace)
        base = select(WatchlistItem).where(
            WatchlistItem.user_id == owner_id, WatchlistItem.item_type == "event"
        )
        total = int(self.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
        items = list(
            self.session.execute(
                base.order_by(WatchlistItem.created_at.desc(), WatchlistItem.id.desc())
                .limit(limit)
                .offset(offset)
            ).scalars()
        )
        normalized_by_item_id: dict[str, str] = {}
        event_ids: set[uuid.UUID] = set()
        for item in items:
            try:
                parsed = uuid.UUID(item.item_id.strip("{}"))
                normalized_by_item_id[item.item_id] = str(parsed)
                event_ids.add(parsed)
            except ValueError:
                pass
        events = {
            str(event.id): event
            for event in self.session.execute(
                select(Event).where(Event.id.in_(list(event_ids)))
            ).scalars()
        }
        return [
            {
                "id": str(item.id),
                "event_id": normalized_by_item_id.get(item.item_id, item.item_id),
                "label": item.label,
                "saved_at": _iso(item.created_at),
                "available": normalized_by_item_id.get(item.item_id) in events,
                "event": (
                    {
                        "id": normalized_by_item_id[item.item_id],
                        "title": events[normalized_by_item_id[item.item_id]].title,
                        "summary": events[normalized_by_item_id[item.item_id]].summary,
                    }
                    if normalized_by_item_id.get(item.item_id) in events
                    else None
                ),
            }
            for item in items
        ], total

    def unsave_entry(self, entry_id: uuid.UUID) -> bool:
        owner_id = require_owner(self.workspace)
        item = self.session.execute(
            select(WatchlistItem).where(
                WatchlistItem.id == entry_id,
                WatchlistItem.user_id == owner_id,
                WatchlistItem.item_type == "event",
            )
        ).scalar_one_or_none()
        if item is not None:
            self.session.delete(item)
            return True
        return False
