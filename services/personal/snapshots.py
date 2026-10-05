"""Freeze personal article revisions, event observations and reproducible brief inputs."""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    Article,
    Claim,
    ClaimEvidence,
    Event,
    EventArticle,
    EvidenceItem,
    PersonalArticleRevision,
    PersonalBriefSnapshot,
    PersonalClaimPreparation,
    PersonalEnrichmentTransfer,
    PersonalProfileRevision,
    PersonalRun,
    PersonalRunEventObservation,
    Source,
)
from services.personal.claims import article_revision_hash
from services.personal.contracts import RankedEvent, article_matches_profile, rank_events
from services.personal.runs import PersonalOwnershipLost, lock_owned_run
from services.personal.settings import validated_settings
from services.reports.contracts import CompositionPolicy

MAX_TITLE_CHARS = 512
MAX_RSS_SUMMARY_CHARS = 2_000
MAX_URL_CHARS = 4_096
MAX_ENRICHMENT_ARTICLES = 100
MAX_EVENT_MEMBERSHIPS = 2_000


def _iso(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _revision_source_input(revision: PersonalArticleRevision) -> dict[str, Any]:
    return {
        "revision_id": str(revision.id),
        "article_id": str(revision.article_id),
        "source_id": str(revision.source_id),
        "title": revision.retained_title,
        "rss_summary": revision.retained_summary,
        "url": revision.retained_url,
        "publisher": revision.retained_publisher,
        "published_at": _iso(revision.retained_published_at),
        "content_hash": revision.content_hash,
        "truncated": revision.truncated,
    }


def freeze_article_scopes(
    session: Session,
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    *,
    admitted_article_ids: list[uuid.UUID] | tuple[uuid.UUID, ...],
    enrichment_article_ids: list[uuid.UUID] | tuple[uuid.UUID, ...],
    now: datetime.datetime,
) -> tuple[PersonalArticleRevision, ...]:
    run = lock_owned_run(session, run_id, ownership_token)
    admitted = tuple(dict.fromkeys(admitted_article_ids))
    enrichment = tuple(dict.fromkeys(enrichment_article_ids))
    profile = session.get(PersonalProfileRevision, run.profile_revision_id)
    enrichment_limit = validated_settings(profile)["enrichment_article_limit"]
    if len(enrichment) > enrichment_limit:
        raise ValueError("personal enrichment scope exceeds the frozen run ceiling")
    # Serialize scope ownership even when two brand-new logical dates have not frozen rows yet.
    session.execute(select(func.pg_advisory_xact_lock(5_787_338_468_505_949_251)))
    competing = session.scalars(
        select(PersonalRun)
        .where(
            PersonalRun.id != run.id,
            PersonalRun.enrichment_article_ids.overlap(list(enrichment)),
        )
        .with_for_update()
    ).all()
    transfers = set(
        session.scalars(
            select(PersonalEnrichmentTransfer.article_id).where(
                PersonalEnrichmentTransfer.to_run_id == run.id,
            )
        )
    )
    for other in competing:
        overlap = set(other.enrichment_article_ids) & set(enrichment)
        if other.state != "succeeded" or not overlap <= transfers:
            raise PersonalOwnershipLost(
                "an article in this scope belongs to another personal run; transfer is explicit"
            )
    if run.scopes_frozen_at is not None:
        if (
            tuple(run.admitted_article_ids) != admitted
            or tuple(run.enrichment_article_ids) != enrichment
        ):
            raise PersonalOwnershipLost("a frozen personal scope cannot be replaced on retry")
    else:
        run.admitted_article_ids = list(admitted)
        run.enrichment_article_ids = list(enrichment)
        run.scopes_frozen_at = now.astimezone(datetime.UTC)

    title_head = func.left(Article.title, MAX_TITLE_CHARS + 1)
    summary_head = func.left(Article.summary, MAX_RSS_SUMMARY_CHARS + 1)
    url_head = func.left(Article.url, MAX_URL_CHARS + 1)
    rows = session.execute(
        select(
            Article.id.label("article_id"),
            Article.source_id,
            title_head.label("title"),
            summary_head.label("summary"),
            url_head.label("url"),
            func.length(Article.title).label("title_length"),
            func.length(Article.summary).label("summary_length"),
            func.length(Article.url).label("url_length"),
            Article.published_at,
            Article.fetched_at,
            Article.raw_payload["personal_truncated"].as_boolean().label("rss_truncated"),
            Source.name.label("publisher"),
            Source.feed_url,
        )
        .join(Source, Source.id == Article.source_id)
        .where(Article.id.in_(enrichment))
        .order_by(Article.id)
        .limit(enrichment_limit + 1)
    ).all()
    if len(rows) != len(enrichment):
        raise ValueError("every enrichment article must exist and resolve to a source")
    existing = {
        item.article_id: item
        for item in session.execute(
            select(PersonalArticleRevision).where(PersonalArticleRevision.run_id == run.id)
        ).scalars()
    }
    revisions: list[PersonalArticleRevision] = []
    for row in rows:
        current = existing.get(row.article_id)
        if current is not None:
            revisions.append(current)
            continue
        truncated = (
            bool(row.rss_truncated)
            or row.title_length > MAX_TITLE_CHARS
            or (row.summary_length or 0) > MAX_RSS_SUMMARY_CHARS
            or row.url_length > MAX_URL_CHARS
        )
        revision = PersonalArticleRevision(
            run_id=run.id,
            article_id=row.article_id,
            source_id=row.source_id,
            content_hash="0" * 64,
            retained_title=row.title[:MAX_TITLE_CHARS],
            retained_summary=(row.summary[:MAX_RSS_SUMMARY_CHARS] if row.summary else None),
            retained_url=row.url[:MAX_URL_CHARS],
            retained_publisher=row.publisher,
            retained_published_at=row.published_at,
            provenance={
                "schema": "personal-article-revision.v1",
                "source_field": "rss_title_and_summary",
                "feed_url": row.feed_url,
                "fetched_at": _iso(row.fetched_at),
            },
            truncated=truncated,
        )
        revision.content_hash = article_revision_hash(revision)
        session.add(revision)
        revisions.append(revision)
    session.flush()
    return tuple(revisions)


def record_event_observations(
    session: Session,
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
) -> tuple[PersonalRunEventObservation, ...]:
    run = lock_owned_run(session, run_id, ownership_token)
    if run.scopes_frozen_at is None:
        raise RuntimeError("article scopes must freeze before event observations")
    profile = session.get(PersonalProfileRevision, run.profile_revision_id)
    if profile is None:
        raise RuntimeError("run profile revision is unavailable")
    revisions = {
        item.article_id: item
        for item in session.execute(
            select(PersonalArticleRevision).where(PersonalArticleRevision.run_id == run.id)
        ).scalars()
    }
    links = session.execute(
        select(Event, EventArticle.article_id)
        .join(EventArticle, EventArticle.event_id == Event.id)
        .where(EventArticle.article_id.in_(run.enrichment_article_ids))
        .order_by(Event.id, EventArticle.article_id)
        .limit(MAX_EVENT_MEMBERSHIPS + 1)
    ).all()
    if len(links) > MAX_EVENT_MEMBERSHIPS:
        raise RuntimeError("event-membership read exceeded the frozen enrichment bound")
    members: dict[uuid.UUID, list[PersonalArticleRevision]] = defaultdict(list)
    events: dict[uuid.UUID, Event] = {}
    selected_sources = set(profile.selected_source_ids)
    for event, article_id in links:
        revision = revisions[article_id]
        if revision.source_id not in selected_sources:
            continue
        if article_matches_profile(
            revision.retained_title,
            revision.retained_summary,
            include_phrases=profile.include_phrases,
            exclude_phrases=profile.exclude_phrases,
        ):
            events[event.id] = event
            members[event.id].append(revision)

    ranked = rank_events(
        [
            RankedEvent(
                event_id=event_id,
                source_count=len({item.source_id for item in event_members}),
                hotness=(
                    float(events[event_id].hotness_score)
                    if events[event_id].hotness_score is not None
                    else None
                ),
                newest_publication=max(
                    (
                        item.retained_published_at
                        for item in event_members
                        if item.retained_published_at
                    ),
                    default=None,
                ),
            )
            for event_id, event_members in members.items()
        ]
    )
    observations: list[PersonalRunEventObservation] = []
    for rank_index, ranked_event in enumerate(ranked):
        event_members = members[ranked_event.event_id]
        source_inputs = {
            "schema": "personal-event-observation.v1",
            "event_title": events[ranked_event.event_id].title,
            "event_summary": events[ranked_event.event_id].summary,
            "articles": [_revision_source_input(item) for item in event_members],
        }
        published = ranked_event.newest_publication
        sort_key = [
            -ranked_event.source_count,
            int(ranked_event.hotness is None),
            -(ranked_event.hotness or 0.0),
            int(published is None),
            -(published.timestamp() if published is not None else 0.0),
            str(ranked_event.event_id),
        ]
        ranking_inputs = {
            "source_count": ranked_event.source_count,
            "hotness": ranked_event.hotness,
            "newest_publication_at": _iso(published),
            "sort_key": sort_key,
            "candidate_rank": rank_index + 1,
        }
        previous = session.execute(
            select(PersonalRunEventObservation)
            .where(
                PersonalRunEventObservation.run_id == run.id,
                PersonalRunEventObservation.event_id == ranked_event.event_id,
            )
            .order_by(PersonalRunEventObservation.revision.desc())
            .limit(1)
        ).scalar_one_or_none()
        if (
            previous is not None
            and previous.source_inputs == source_inputs
            and previous.ranking_inputs == ranking_inputs
        ):
            observations.append(previous)
            continue
        observation = PersonalRunEventObservation(
            run_id=run.id,
            event_id=ranked_event.event_id,
            revision=(previous.revision + 1 if previous else 1),
            qualifying_article_ids=[item.article_id for item in event_members],
            source_inputs=source_inputs,
            ranking_inputs=ranking_inputs,
        )
        session.add(observation)
        observations.append(observation)
    run.event_ids = sorted({*run.event_ids, *(item.event_id for item in observations)}, key=str)
    session.flush()
    return tuple(observations)


def refresh_terminal_manifest(session: Session, run) -> dict[str, Any]:
    """Pin every candidate's latest observation revision, including before a failed run."""

    observations = list(
        session.execute(
            select(PersonalRunEventObservation)
            .where(PersonalRunEventObservation.run_id == run.id)
            .order_by(
                PersonalRunEventObservation.event_id,
                PersonalRunEventObservation.revision.desc(),
            )
        ).scalars()
    )
    latest: dict[uuid.UUID, PersonalRunEventObservation] = {}
    for item in observations:
        latest.setdefault(item.event_id, item)
    ordered = sorted(latest.values(), key=lambda item: tuple(item.ranking_inputs["sort_key"]))
    manifest = {
        "schema": "personal-review-manifest.v1",
        "observations": [
            {
                "event_id": str(item.event_id),
                "observation_id": str(item.id),
                "revision": item.revision,
            }
            for item in ordered
        ],
    }
    run.terminal_manifest = manifest
    session.flush()
    return manifest


def freeze_brief_snapshot(
    session: Session,
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    *,
    model_route: dict[str, Any],
    prepared_at: datetime.datetime,
) -> PersonalBriefSnapshot:
    run = lock_owned_run(session, run_id, ownership_token)
    existing = session.execute(
        select(PersonalBriefSnapshot).where(PersonalBriefSnapshot.run_id == run.id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    manifest = refresh_terminal_manifest(session, run)
    observation_ids = [uuid.UUID(item["observation_id"]) for item in manifest["observations"]]
    by_id = {
        item.id: item
        for item in session.execute(
            select(PersonalRunEventObservation).where(
                PersonalRunEventObservation.id.in_(observation_ids)
            )
        ).scalars()
    }
    ordered = [by_id[item_id] for item_id in observation_ids]
    revision_by_article = {
        item.article_id: item
        for item in session.execute(
            select(PersonalArticleRevision).where(PersonalArticleRevision.run_id == run.id)
        ).scalars()
    }
    for observation in ordered:
        source_inputs = observation.source_inputs
        source_articles = source_inputs.get("articles")
        if source_inputs.get("schema") != "personal-event-observation.v1" or not isinstance(
            source_articles, list
        ):
            raise RuntimeError("event observation has an invalid frozen source contract")
        article_ids: list[uuid.UUID] = []
        for source_article in source_articles:
            try:
                article_id = uuid.UUID(source_article["article_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("event observation has an invalid article identity") from exc
            revision = revision_by_article.get(article_id)
            if revision is None or source_article != _revision_source_input(revision):
                raise RuntimeError("event observation source text differs from its pinned revision")
            article_ids.append(article_id)
        if article_ids != list(observation.qualifying_article_ids):
            raise RuntimeError("event observation membership differs from its pinned sources")
    selected_qualifying_articles = {
        article_id for item in ordered[:5] for article_id in item.qualifying_article_ids
    }
    revisions = {item.id: item for item in revision_by_article.values()}
    claim_rows = list(
        session.execute(
            select(PersonalClaimPreparation)
            .where(PersonalClaimPreparation.run_id == run.id)
            .order_by(PersonalClaimPreparation.article_id, PersonalClaimPreparation.id)
        ).scalars()
    )
    claim_ids = {item.claim_id for item in claim_rows if item.claim_id is not None}
    evidence_ids = {
        item.evidence_item_id for item in claim_rows if item.evidence_item_id is not None
    }
    claims = {
        item.id: item
        for item in session.execute(select(Claim).where(Claim.id.in_(claim_ids))).scalars()
    }
    evidence = {
        item.id: item
        for item in session.execute(
            select(EvidenceItem).where(EvidenceItem.id.in_(evidence_ids))
        ).scalars()
    }
    supportive_links = set(
        session.execute(
            select(ClaimEvidence.claim_id, ClaimEvidence.evidence_item_id).where(
                ClaimEvidence.claim_id.in_(claim_ids),
                ClaimEvidence.evidence_item_id.in_(evidence_ids),
                ClaimEvidence.support_type == "supports",
            )
        ).all()
    )
    for item in claim_rows:
        revision = revisions.get(item.article_revision_id)
        if revision is None or revision.run_id != run.id or revision.article_id != item.article_id:
            raise RuntimeError("claim preparation does not match its frozen article revision")
        if article_revision_hash(revision) != revision.content_hash:
            raise RuntimeError("claim preparation references a modified article revision")
        if item.status != "supported":
            continue
        source_text = (
            revision.retained_title
            if item.source_field == "title"
            else revision.retained_summary or ""
        )
        if (
            item.span_end > len(source_text)
            or source_text[item.span_start : item.span_end] != item.exact_excerpt
        ):
            raise RuntimeError("supported claim span does not match its frozen source")
        claim = claims.get(item.claim_id)
        evidence_item = evidence.get(item.evidence_item_id)
        if claim is None or claim.claim_text != item.exact_excerpt:
            raise RuntimeError("supported preparation does not resolve to its exact claim")
        if (
            evidence_item is None
            or evidence_item.source_type != "article"
            or evidence_item.source_id != str(item.article_id)
            or (item.claim_id, item.evidence_item_id) not in supportive_links
        ):
            raise RuntimeError("supported preparation lacks its canonical article evidence link")
    payload = {
        "schema": "personal-brief-input.v1",
        "composition_policy": CompositionPolicy.PERSONAL_DESCRIPTIVE.value,
        "run_id": str(run.id),
        "workspace_id": str(run.workspace_id),
        "profile_revision_id": str(run.profile_revision_id),
        "local_date": run.local_date.isoformat(),
        "captured": {"start": _iso(run.capture_started_at), "end": _iso(run.capture_ended_at)},
        "coverage": run.coverage,
        "admitted_article_ids": [str(value) for value in run.admitted_article_ids],
        "enrichment_article_ids": [str(value) for value in run.enrichment_article_ids],
        "candidates": [
            {
                "observation_id": str(item.id),
                "event_id": str(item.event_id),
                "revision": item.revision,
                "ranking": item.ranking_inputs,
                "source_inputs": item.source_inputs,
            }
            for item in ordered
        ],
        "claims": [
            {
                "preparation_id": str(item.id),
                "article_revision_id": str(item.article_revision_id),
                "article_id": str(item.article_id),
                "claim_id": str(item.claim_id) if item.claim_id else None,
                "evidence_item_id": str(item.evidence_item_id) if item.evidence_item_id else None,
                "source_field": item.source_field,
                "span": [item.span_start, item.span_end],
                "exact_excerpt": item.exact_excerpt,
                "status": item.status,
                "validation": item.validation,
                "article_revision_hash": revisions[item.article_revision_id].content_hash,
                "citable": (
                    item.status == "supported" and item.article_id in selected_qualifying_articles
                ),
            }
            for item in claim_rows
        ],
        "model_route": model_route,
        "prepared_at": _iso(prepared_at),
    }
    snapshot = PersonalBriefSnapshot(
        workspace_id=run.workspace_id,
        run_id=run.id,
        profile_revision_id=run.profile_revision_id,
        candidate_event_ids=[item.event_id for item in ordered],
        selected_event_ids=[item.event_id for item in ordered[:5]],
        input_payload=payload,
        input_hash=_canonical_hash(payload),
        model_route=model_route,
        input_contract="personal-brief-input.v1",
        prepared_at=prepared_at,
    )
    session.add(snapshot)
    session.flush()
    run.snapshot_id = snapshot.id
    session.flush()
    return snapshot


__all__ = [
    "MAX_ENRICHMENT_ARTICLES",
    "MAX_EVENT_MEMBERSHIPS",
    "freeze_article_scopes",
    "freeze_brief_snapshot",
    "record_event_observations",
    "refresh_terminal_manifest",
]
