"""Owned personal daily workflow from bounded RSS capture through grounded publication."""

from __future__ import annotations

import datetime
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Date, Integer, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from db.models import (
    Article,
    EventArticle,
    PersonalCapture,
    PersonalEnrichmentTransfer,
    PersonalFeedReceipt,
    PersonalProfileRevision,
    PersonalRun,
    PersonalWorkspace,
    Report,
    Source,
)
from packages.providers.base import EmbeddingProvider, RSSProvider
from services.ingestion.capture_bounds import bound_rss_items
from services.ingestion.normalize import content_hash
from services.nlp.cluster_service import cluster_unclustered_articles
from services.nlp.embeddings import embed_unembedded_articles
from services.personal.briefs import PersonalBriefGenerationResult, generate_personal_brief
from services.personal.claims import prepare_article_claims
from services.personal.deadlines import RuntimeCancelled, bind_budget, check_budget, propagate_fatal
from services.personal.runs import acquire_run, finish_run, lock_owned_run
from services.personal.settings import processing_block_reason, validated_settings
from services.personal.snapshots import (
    MAX_RSS_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    freeze_article_scopes,
    freeze_brief_snapshot,
    record_event_observations,
)

MAX_FEED_ITEMS = 500
MAX_PENDING_CAPTURES = 2_000
MAX_NEW_ADMISSIONS_PER_RUN = 100
MAX_NEW_ADMISSIONS_PER_LOCAL_DAY = 100
_PERSONAL_INTAKE_LOCK = 8_426_622_100

SessionFactory = Callable[[], Session]
RSSProviderFactory = Callable[[Source], RSSProvider]
OrchestratorFactory = Callable[[Session, dict[str, Any]], object]
Clock = Callable[[], datetime.datetime]


class PersonalCoordinatorError(RuntimeError):
    """A workflow stage failed after the run was durably delivered."""


@dataclass(frozen=True)
class PersonalCoordinatorResult:
    run_id: uuid.UUID
    report: PersonalBriefGenerationResult | None
    feeds_attempted: int
    feeds_succeeded: int
    feeds_failed: int
    articles_captured: int
    articles_admitted: int
    events_observed: int
    claims_supported: int


@dataclass(frozen=True)
class _FeedCapture:
    fetched: int
    retained: int
    capacity_reached: bool
    status: str = "complete"
    bounds_reached: tuple[str, ...] = ()


def _utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("personal coordinator timestamps must be timezone-aware")
    return value.astimezone(datetime.UTC)


def _stage(run: PersonalRun, name: str, status: str, **details: Any) -> None:
    current = dict(run.stage_results or {})
    current[name] = {"status": status, **details}
    run.stage_results = current


def _profile(session: Session, run: PersonalRun) -> PersonalProfileRevision:
    profile = session.get(PersonalProfileRevision, run.profile_revision_id)
    if profile is None:
        raise RuntimeError("personal run profile revision is unavailable")
    return profile


def _pending_count(session: Session, workspace_id: uuid.UUID) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(PersonalCapture)
            .where(
                PersonalCapture.workspace_id == workspace_id,
                PersonalCapture.admitted_at.is_(None),
            )
        )
        or 0
    )


def _capture_one_source(
    session: Session,
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    source_id: uuid.UUID,
    *,
    provider_factory: RSSProviderFactory,
    captured_at: datetime.datetime,
    receipt_id: uuid.UUID,
) -> _FeedCapture:
    """Persist bounded pending metadata while the mode and owner locks remain held."""

    run = lock_owned_run(session, run_id, ownership_token)
    if run.scopes_frozen_at is not None:
        raise RuntimeError("frozen personal intake cannot capture additional candidates")
    receipt = session.get(PersonalFeedReceipt, receipt_id)
    if receipt is None or receipt.run_id != run.id:
        raise RuntimeError("capture receipt is unavailable")
    profile = _profile(session, run)
    if source_id not in profile.selected_source_ids:
        raise RuntimeError("capture source is outside the frozen personal profile")
    source = session.get(Source, source_id)
    if source is None or not source.active:
        raise RuntimeError("configured capture source is unavailable")

    # Check capacity before capture, then release every ownership/database lock
    # for the external call. Its response is revalidated before any business write.
    session.execute(select(func.pg_advisory_xact_lock(_PERSONAL_INTAKE_LOCK)))
    remaining = MAX_PENDING_CAPTURES - _pending_count(session, run.workspace_id)
    if remaining <= 0:
        receipt.status = "collection_paused_pending_capacity"
        receipt.ended_at = datetime.datetime.now(datetime.UTC)
        receipt.details = {
            "retained": 0,
            "entries_observed": None,
            "bounds_reached": ["pending_capacity"],
        }
        return _FeedCapture(fetched=0, retained=0, capacity_reached=True)

    session.expunge(source)
    session.rollback()
    check_budget()
    provider = provider_factory(source)
    capture_method = getattr(provider, "fetch_capture", None)
    captured = (
        capture_method(source.feed_url)
        if callable(capture_method)
        else bound_rss_items(provider.fetch(source.feed_url))
    )
    check_budget()
    run = lock_owned_run(session, run_id, ownership_token)
    if run.scopes_frozen_at is not None:
        raise RuntimeError("personal capture scope froze during external request")
    receipt = session.get(PersonalFeedReceipt, receipt_id)
    source = session.get(Source, source_id)
    profile = _profile(session, run)
    if (
        receipt is None
        or source is None
        or not source.active
        or source_id not in profile.selected_source_ids
    ):
        raise RuntimeError("personal capture identity changed during external request")
    session.execute(select(func.pg_advisory_xact_lock(_PERSONAL_INTAKE_LOCK)))
    remaining = MAX_PENDING_CAPTURES - _pending_count(session, run.workspace_id)
    items = captured.items

    retained = 0
    deferred_at_capacity = False
    for bounded in items:
        check_budget()
        item = bounded.item
        canonical_url = bounded.canonical_url
        digest = bounded.url_hash
        existing = session.execute(
            select(PersonalCapture.id).where(
                PersonalCapture.workspace_id == run.workspace_id,
                PersonalCapture.url_hash == digest,
            )
        ).first()
        if existing is not None:
            continue
        # Previously stored legacy or personal articles do not become new pending/admission
        # units merely because their URL was repeated by another feed.
        if session.scalar(select(Article.id).where(Article.url_hash == digest)) is not None:
            continue
        if retained >= remaining:
            deferred_at_capacity = True
            continue
        outcome = session.execute(
            insert(PersonalCapture)
            .values(
                id=uuid.uuid4(),
                workspace_id=run.workspace_id,
                run_id=run.id,
                source_id=source.id,
                canonical_url=canonical_url,
                original_url=bounded.original_url,
                url_hash=digest,
                title=item.title[:MAX_TITLE_CHARS],
                rss_summary=(item.summary[:MAX_RSS_SUMMARY_CHARS] if item.summary else None),
                published_at=item.published_at,
                captured_at=captured_at,
                article_id=None,
                truncated=bool(bounded.field_truncations),
                enrichment_state="pending_admission",
                receipt={
                    "schema": "personal-capture-receipt.v1",
                    "provider": item.provider_name,
                    "item_schema": item.schema_version,
                    "source_refs": list(item.source_refs),
                    "evidence_refs": list(item.evidence_refs),
                    "capture_receipt_id": str(receipt.id),
                    "field_truncations": list(bounded.field_truncations),
                },
            )
            .on_conflict_do_nothing(constraint="uq_personal_capture_workspace_url")
            .returning(PersonalCapture.id)
        ).scalar_one_or_none()
        retained += int(outcome is not None)
    session.flush()
    receipt.status = (
        "collection_paused_pending_capacity" if deferred_at_capacity else captured.status
    )
    receipt.ended_at = datetime.datetime.now(datetime.UTC)
    receipt.details = {
        "entries_observed": captured.entries_observed,
        "entries_processed": captured.entries_processed,
        "rejected_entries": captured.rejected_entries,
        "response_bytes": captured.response_bytes,
        "bounds_reached": [
            *captured.bounds_reached,
            *(["pending_capacity"] if deferred_at_capacity else []),
        ],
        "total_entries_known": captured.total_entries_known,
        "retained": retained,
    }
    return _FeedCapture(
        fetched=captured.entries_processed,
        retained=retained,
        capacity_reached=deferred_at_capacity,
        status=captured.status,
        bounds_reached=captured.bounds_reached,
    )


def _capture_selected_feeds(
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    *,
    session_factory: SessionFactory,
    provider_factory: RSSProviderFactory,
    clock: Clock,
) -> dict[str, Any]:
    with session_factory() as session:
        run = lock_owned_run(session, run_id, ownership_token)
        source_ids = tuple(sorted(_profile(session, run).selected_source_ids, key=str))
        if run.capture_started_at is None:
            run.capture_started_at = _utc(clock())
        session.commit()

    failures: list[dict[str, str]] = []
    pauses: list[dict[str, str]] = []
    fetched = 0
    retained = 0
    succeeded = 0
    attempted = 0
    for source_id in source_ids:
        check_budget()
        with session_factory() as receipt_session:
            owned = lock_owned_run(receipt_session, run_id, ownership_token)
            receipt = PersonalFeedReceipt(
                workspace_id=owned.workspace_id,
                run_id=owned.id,
                source_id=source_id,
                attempt=owned.attempt,
                started_at=_utc(clock()),
                status="capturing",
                details={},
            )
            receipt_session.add(receipt)
            receipt_session.flush()
            receipt_id = receipt.id
            receipt_session.commit()
        session = session_factory()
        try:
            capture = _capture_one_source(
                session,
                run_id,
                ownership_token,
                source_id,
                provider_factory=provider_factory,
                captured_at=_utc(clock()),
                receipt_id=receipt_id,
            )
            session.commit()
            if capture.fetched == 0 and capture.capacity_reached:
                pauses.append({"source_id": str(source_id), "code": "pending_capacity_reached"})
                continue
            attempted += 1
            if capture.status in {"response_too_large", "response_byte_limit"}:
                failures.append({"source_id": str(source_id), "code": capture.status})
                continue
            succeeded += 1
            fetched += capture.fetched
            retained += capture.retained
            if capture.capacity_reached:
                pauses.append({"source_id": str(source_id), "code": "pending_capacity_reached"})
            if capture.bounds_reached:
                pauses.append({"source_id": str(source_id), "code": capture.status})
        except Exception as exc:
            session.rollback()
            propagate_fatal(exc)
            attempted += 1
            failures.append({"source_id": str(source_id), "code": "feed_capture_failed"})
            with session_factory() as failure_session:
                lock_owned_run(failure_session, run_id, ownership_token)
                failed_receipt = failure_session.get(PersonalFeedReceipt, receipt_id)
                failed_receipt.status = "feed_capture_failed"
                failed_receipt.ended_at = _utc(clock())
                failed_receipt.details = {"error": "feed_capture_failed", "entries_observed": None}
                failure_session.commit()
        finally:
            session.close()

    with session_factory() as session:
        run = lock_owned_run(session, run_id, ownership_token)
        coverage = {
            "schema": "personal-capture-coverage.v1",
            "feeds_configured": len(source_ids),
            "feeds_attempted": attempted,
            "feeds_succeeded": succeeded,
            "feeds_failed": len(failures),
            "feeds_paused": len(source_ids) - attempted,
            "feed_failures": failures,
            "feed_pauses": pauses,
            "items_fetched": fetched,
            "articles_captured": retained,
            "pending_total": _pending_count(session, run.workspace_id),
            "pending_capacity": MAX_PENDING_CAPTURES,
        }
        run.capture_ended_at = _utc(clock())
        run.coverage = coverage
        status = "failed" if attempted > 0 and succeeded == 0 else "succeeded"
        _stage(run, "capture", status, **coverage)
        session.commit()
    return coverage


def _round_robin(rows: Sequence[PersonalCapture]) -> list[PersonalCapture]:
    by_source: dict[uuid.UUID, list[PersonalCapture]] = defaultdict(list)
    for row in rows:
        by_source[row.source_id].append(row)
    ordered: list[PersonalCapture] = []
    source_ids = sorted(by_source, key=str)
    while any(by_source.values()):
        for source_id in source_ids:
            if by_source[source_id]:
                ordered.append(by_source[source_id].pop(0))
    return ordered


def _pending_candidates(
    session: Session, run: PersonalRun, profile: PersonalProfileRevision
) -> list[PersonalCapture]:
    rows = list(
        session.execute(
            select(PersonalCapture)
            .join(Source, Source.id == PersonalCapture.source_id)
            .where(
                PersonalCapture.workspace_id == run.workspace_id,
                PersonalCapture.admitted_at.is_(None),
                PersonalCapture.source_id.in_(profile.selected_source_ids),
                Source.active.is_(True),
            )
            .order_by(
                PersonalCapture.source_id,
                PersonalCapture.captured_at,
                PersonalCapture.published_at.desc().nulls_last(),
                PersonalCapture.canonical_url,
                PersonalCapture.id,
            )
            .limit(MAX_PENDING_CAPTURES + 1)
            .with_for_update()
        ).scalars()
    )
    if len(rows) > MAX_PENDING_CAPTURES:
        raise RuntimeError("personal pending capture boundary is inconsistent")
    return _round_robin(rows)


def _admissions_used_on_local_date(
    session: Session, run: PersonalRun, admitted_at: datetime.datetime
) -> int:
    timezone = session.scalar(
        select(PersonalWorkspace.timezone).where(PersonalWorkspace.id == run.workspace_id)
    )
    if not timezone:
        raise RuntimeError("personal workspace timezone is unavailable")
    admission_date = admitted_at.astimezone(ZoneInfo(timezone)).date()
    return int(
        session.scalar(
            select(func.count(func.distinct(PersonalCapture.url_hash))).where(
                PersonalCapture.workspace_id == run.workspace_id,
                PersonalCapture.admitted_at.is_not(None),
                PersonalCapture.admission_charged.is_(True),
                cast(func.timezone(timezone, PersonalCapture.admitted_at), Date) == admission_date,
            )
        )
        or 0
    )


def _article_for_capture(session: Session, capture: PersonalCapture) -> uuid.UUID:
    article_id = session.scalar(select(Article.id).where(Article.url_hash == capture.url_hash))
    if article_id is not None:
        return article_id
    return session.scalar(
        insert(Article)
        .values(
            source_id=capture.source_id,
            url=capture.canonical_url,
            url_hash=capture.url_hash,
            title=capture.title,
            summary=capture.rss_summary,
            published_at=capture.published_at,
            content_hash=content_hash(capture.title, capture.rss_summary or ""),
            raw_payload={
                "provider_name": (capture.receipt or {}).get("provider", "unknown"),
                "schema_version": (capture.receipt or {}).get("item_schema", "unknown"),
                "personal_capture_id": str(capture.id),
                "personal_truncated": capture.truncated,
                "personal_field_truncations": (capture.receipt or {}).get("field_truncations", []),
            },
        )
        .on_conflict_do_nothing(index_elements=[Article.url_hash])
        .returning(Article.id)
    ) or session.scalar(select(Article.id).where(Article.url_hash == capture.url_hash))


def _admit_pending(
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    *,
    session_factory: SessionFactory,
    admitted_at: datetime.datetime,
) -> tuple[uuid.UUID, ...]:
    with session_factory() as session:
        run = lock_owned_run(session, run_id, ownership_token)
        if run.scopes_frozen_at is not None:
            return tuple(run.admitted_article_ids)
        profile = _profile(session, run)
        session.execute(select(func.pg_advisory_xact_lock(_PERSONAL_INTAKE_LOCK)))
        already = list(
            session.execute(
                select(PersonalCapture)
                .where(PersonalCapture.admitted_run_id == run.id)
                .order_by(
                    cast(
                        PersonalCapture.receipt["admission_ordinal"].astext,
                        Integer,
                    ).nulls_last(),
                    PersonalCapture.admitted_at,
                    PersonalCapture.id,
                )
                .with_for_update()
            ).scalars()
        )
        frozen = validated_settings(profile)
        workspace = session.execute(
            select(PersonalWorkspace)
            .where(PersonalWorkspace.id == run.workspace_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        ).scalar_one()
        live = validated_settings(
            session.get(PersonalProfileRevision, workspace.active_profile_revision_id)
        )
        run_limit = frozen["run_article_limit"]
        daily_limit = min(frozen["daily_article_limit"], live["daily_article_limit"])
        remaining_run = run_limit - sum(row.admission_charged for row in already)
        remaining_day = daily_limit - _admissions_used_on_local_date(session, run, admitted_at)
        allowance = max(0, min(remaining_run, remaining_day))
        selected = _pending_candidates(session, run, profile)[:allowance]
        ordinals = [
            value
            for row in already
            if isinstance(value := (row.receipt or {}).get("admission_ordinal"), int)
            and not isinstance(value, bool)
        ]
        next_ordinal = max(ordinals, default=-1) + 1
        for capture in selected:
            already_exists = session.scalar(
                select(Article.id).where(Article.url_hash == capture.url_hash)
            )
            article_id = _article_for_capture(session, capture)
            if article_id is None:
                raise RuntimeError("admitted article identity could not be resolved")
            capture.article_id = article_id
            capture.admitted_run_id = run.id
            capture.admitted_at = admitted_at
            capture.admission_charged = already_exists is None
            capture.processing_run_id = run.id
            capture.enrichment_state = "admitted"
            capture.receipt = {
                **(capture.receipt or {}),
                "admission_ordinal": next_ordinal,
            }
            next_ordinal += 1
        session.flush()
        admitted = tuple(
            dict.fromkeys(
                row.article_id for row in [*already, *selected] if row.article_id is not None
            )
        )
        _stage(
            run,
            "admission",
            "succeeded",
            articles_admitted=len(admitted),
            run_limit=run_limit,
            local_day_limit=daily_limit,
        )
        session.commit()
        return admitted


def _freeze_enrichment_scope(
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    admitted_article_ids: Sequence[uuid.UUID],
    *,
    session_factory: SessionFactory,
    now: datetime.datetime,
    ai_block_reason: str | None = None,
) -> tuple[uuid.UUID, ...]:
    with session_factory() as session:
        run = lock_owned_run(session, run_id, ownership_token)
        if run.scopes_frozen_at is not None:
            return tuple(run.enrichment_article_ids)
        profile = _profile(session, run)
        limit = validated_settings(profile)["enrichment_article_limit"]
        prior: list[PersonalCapture] = []
        ai_reason = ai_block_reason or _ai_reason(profile)
        if ai_reason is None:
            processing_run = aliased(PersonalRun)
            # Claims are made under the same intake/ownership transaction as the immutable
            # scope. No active or failed retry scope is transferable, even after exhaustion.
            session.execute(select(func.pg_advisory_xact_lock(_PERSONAL_INTAKE_LOCK)))
            prior = list(
                session.execute(
                    select(PersonalCapture)
                    .join(Source, Source.id == PersonalCapture.source_id)
                    .join(processing_run, processing_run.id == PersonalCapture.processing_run_id)
                    .where(
                        PersonalCapture.workspace_id == run.workspace_id,
                        PersonalCapture.admitted_run_id != run.id,
                        PersonalCapture.admitted_at.is_not(None),
                        PersonalCapture.article_id.is_not(None),
                        PersonalCapture.source_id.in_(profile.selected_source_ids),
                        Source.active.is_(True),
                        ~select(EventArticle.article_id)
                        .where(EventArticle.article_id == PersonalCapture.article_id)
                        .exists(),
                        processing_run.state == "succeeded",
                        PersonalCapture.enrichment_state.in_(
                            (
                                "disabled_by_profile",
                                "ai_disabled",
                                "paid_runtime_disabled",
                                "configuration_missing",
                                "ordinary_live_disabled",
                                "execution_route_unavailable",
                                "deferred_budget",
                                "deferred_enrichment_capacity",
                            )
                        ),
                    )
                    .order_by(PersonalCapture.admitted_at, PersonalCapture.article_id)
                    .limit(limit)
                    .with_for_update(of=PersonalCapture)
                ).scalars()
            )
        for capture in prior:
            session.add(
                PersonalEnrichmentTransfer(
                    workspace_id=run.workspace_id,
                    article_id=capture.article_id,
                    from_run_id=capture.processing_run_id,
                    to_run_id=run.id,
                    reason=capture.enrichment_state,
                    transferred_at=now,
                )
            )
            capture.processing_run_id = run.id
            capture.enrichment_state = "selected"
        enrichment = (
            tuple(dict.fromkeys([*(row.article_id for row in prior), *admitted_article_ids]))[
                :limit
            ]
            if ai_reason is None
            else ()
        )
        for capture in session.scalars(
            select(PersonalCapture).where(
                PersonalCapture.workspace_id == run.workspace_id,
                PersonalCapture.admitted_run_id == run.id,
            )
        ):
            capture.processing_run_id = run.id
            capture.enrichment_state = (
                "deferred_budget"
                if ai_reason == "allowance_reached"
                else ai_reason
                if ai_reason is not None
                else "selected"
                if capture.article_id in enrichment
                else "deferred_enrichment_capacity"
            )
        session.flush()
        freeze_article_scopes(
            session,
            run.id,
            ownership_token,
            admitted_article_ids=tuple(admitted_article_ids),
            enrichment_article_ids=enrichment,
            now=now,
        )
        coverage = dict(run.coverage or {})
        coverage["pending_total"] = _pending_count(session, run.workspace_id)
        coverage["articles_admitted"] = len(admitted_article_ids)
        run.coverage = coverage
        _stage(
            run,
            "scope",
            "succeeded",
            articles_admitted=len(admitted_article_ids),
            articles_selected_for_enrichment=len(enrichment),
        )
        session.commit()
        return enrichment


def _ai_reason(profile: PersonalProfileRevision) -> str | None:
    if profile.schema_revision == "personal-profile.v2":
        return processing_block_reason(profile)
    # Preserve the dedicated completed Phase 2 verification and deterministic fixtures;
    # its one-attempt authorization is never converted to a recurring allowance.
    return "disabled_by_profile" if profile.execution_profile == "raw" else None


def _mark_stage_failure(
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    *,
    session_factory: SessionFactory,
    code: str,
) -> None:
    with session_factory() as session:
        try:
            run = lock_owned_run(session, run_id, ownership_token)
            for capture in session.scalars(
                select(PersonalCapture).where(
                    PersonalCapture.processing_run_id == run.id,
                    PersonalCapture.enrichment_state == "selected",
                )
            ):
                capture.enrichment_state = "provider_failed"
            _stage(run, "workflow", "failed", code=code)
            session.flush()
            finish_run(
                session,
                run_id,
                ownership_token,
                state="failed",
                error={
                    "code": code,
                    "message": "Personal update failed; an explicit retry may be available.",
                },
            )
            session.commit()
        except Exception:
            session.rollback()


def _published_recovery_available(session: Session, run: PersonalRun) -> bool:
    if run.snapshot_id is None or run.report_id is None:
        return False
    report = session.get(Report, run.report_id)
    return report is not None and report.status == "published"


def _finish_raw(
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    *,
    session_factory: SessionFactory,
    reason: str = "disabled_by_profile",
) -> None:
    with session_factory() as session:
        run = lock_owned_run(session, run_id, ownership_token)
        for stage in ("embedding", "grouping", "claim_preparation", "snapshot", "report"):
            if (run.stage_results or {}).get(stage, {}).get("status") == "succeeded":
                continue
            _stage(
                run,
                stage,
                "disabled"
                if reason in {"disabled_by_profile", "ai_disabled", "paid_runtime_disabled"}
                else "blocked",
                reason=reason,
            )
        for capture in session.scalars(
            select(PersonalCapture).where(
                PersonalCapture.processing_run_id == run.id,
                PersonalCapture.enrichment_state == "selected",
            )
        ):
            capture.enrichment_state = (
                "deferred_budget" if reason == "allowance_reached" else reason
            )
        session.flush()
        coverage = run.coverage or {}
        partial = bool(
            int(coverage.get("feeds_failed") or 0)
            or int(coverage.get("feeds_paused") or 0)
            or coverage.get("feed_pauses")
        )
        finish_run(
            session,
            run.id,
            ownership_token,
            state="partially_failed" if partial else "succeeded",
            result={
                "mode": "raw",
                "capture_partial": partial,
                "message": "RSS articles are available; AI processing is limited.",
                "reason": reason,
            },
            error=(
                {
                    "code": "partial_capture",
                    "message": "The update is available with incomplete feed coverage.",
                }
                if partial
                else None
            ),
        )
        session.commit()


class _UnlockedEmbeddingProvider:
    """Validate a returned embedding before the existing adapter persists it."""

    def __init__(self, delegate, session, run_id, token):
        self.delegate, self.session, self.run_id, self.token = delegate, session, run_id, token
        self.model_name, self.model_version = delegate.model_name, delegate.model_version

    def embed(self, texts):
        self.session.rollback()
        check_budget()
        results = self.delegate.embed(texts)
        check_budget()
        lock_owned_run(self.session, self.run_id, self.token)
        return results


def run_personal_daily(
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    *,
    session_factory: SessionFactory,
    rss_provider_factory: RSSProviderFactory,
    embedding_provider: EmbeddingProvider,
    orchestrator_factory: OrchestratorFactory,
    now: datetime.datetime,
    clock: Clock | None = None,
    ai_block_reason: str | None = None,
    paid_block_reason: Callable[[], str | None] | None = None,
    generation: int | None = None,
    on_acquired: Callable[[PersonalRun], None] | None = None,
    rotate_token: bool = False,
) -> PersonalCoordinatorResult:
    """Run all personal stages, fencing every write transaction by owner token and mode."""

    now = _utc(now)
    clock = clock or (lambda: datetime.datetime.now(datetime.UTC))
    try:
        check_budget()
        with session_factory() as session:
            run = acquire_run(
                session,
                run_id,
                ownership_token,
                now=now,
                generation=generation,
                rotate_token=rotate_token,
            )
            ownership_token = run.ownership_token
            recover_published = _published_recovery_available(session, run)
            existing_coverage = dict(run.coverage or {})
            existing_article_count = len(run.enrichment_article_ids)
            ai_reason = ai_block_reason or _ai_reason(_profile(session, run))
            for disabled_stage in (
                "entity_linking",
                "event_embeddings",
                "historical_analogies",
                "forecasting",
            ):
                _stage(run, disabled_stage, "disabled", reason="disabled_by_profile")
            scopes_frozen = run.scopes_frozen_at is not None
            session.commit()
            session.refresh(run)
            session.expunge(run)

        if on_acquired is not None:
            on_acquired(run)
        check_budget()

        if recover_published:
            report = generate_personal_brief(
                run_id,
                ownership_token,
                session_factory=session_factory,
                orchestrator_factory=orchestrator_factory,
            )
            return PersonalCoordinatorResult(
                run_id=run_id,
                report=report,
                feeds_attempted=int(existing_coverage.get("feeds_attempted") or 0),
                feeds_succeeded=int(existing_coverage.get("feeds_succeeded") or 0),
                feeds_failed=int(existing_coverage.get("feeds_failed") or 0),
                articles_captured=int(existing_coverage.get("articles_captured") or 0),
                articles_admitted=existing_article_count,
                events_observed=len(report.selected_event_ids),
                claims_supported=0,
            )

        if scopes_frozen:
            check_budget()
            with session_factory() as session:
                run = lock_owned_run(session, run_id, ownership_token)
                coverage = dict(run.coverage or {})
                article_ids = tuple(run.enrichment_article_ids)
                admitted_ids = tuple(run.admitted_article_ids)
                session.rollback()
        else:
            # Existing pending work gets the first admission slots. Capture then fills freed
            # pending capacity, and a second pass uses any admission allowance still available.
            _admit_pending(
                run_id,
                ownership_token,
                session_factory=session_factory,
                admitted_at=_utc(clock()),
            )
            coverage = _capture_selected_feeds(
                run_id,
                ownership_token,
                session_factory=session_factory,
                provider_factory=rss_provider_factory,
                clock=clock,
            )
            if coverage["feeds_attempted"] > 0 and coverage["feeds_succeeded"] == 0:
                _mark_stage_failure(
                    run_id,
                    ownership_token,
                    session_factory=session_factory,
                    code="all_feeds_failed",
                )
                raise PersonalCoordinatorError("all configured personal feeds failed")
            admitted_ids = _admit_pending(
                run_id,
                ownership_token,
                session_factory=session_factory,
                admitted_at=_utc(clock()),
            )
            article_ids = _freeze_enrichment_scope(
                run_id,
                ownership_token,
                admitted_ids,
                session_factory=session_factory,
                now=_utc(clock()),
                ai_block_reason=ai_reason,
            )

        if ai_reason is not None:
            _finish_raw(run_id, ownership_token, session_factory=session_factory, reason=ai_reason)
            return PersonalCoordinatorResult(
                run_id=run_id,
                report=None,
                feeds_attempted=int(coverage.get("feeds_attempted") or 0),
                feeds_succeeded=int(coverage.get("feeds_succeeded") or 0),
                feeds_failed=int(coverage.get("feeds_failed") or 0),
                articles_captured=int(coverage.get("articles_captured") or 0),
                articles_admitted=len(admitted_ids),
                events_observed=0,
                claims_supported=0,
            )

        check_budget()
        with session_factory() as session:
            run = lock_owned_run(session, run_id, ownership_token)
            embedded = embed_unembedded_articles(
                session,
                _UnlockedEmbeddingProvider(embedding_provider, session, run_id, ownership_token),
                embedding_model=embedding_provider.model_name,
                embedding_model_version=embedding_provider.model_version,
                article_ids=article_ids,
            )
            _stage(run, "embedding", "succeeded", articles_embedded=embedded)
            session.commit()

        check_budget()
        with session_factory() as session:
            run = lock_owned_run(session, run_id, ownership_token)
            clustered = cluster_unclustered_articles(
                session,
                embedding_model=embedding_provider.model_name,
                embedding_model_version=embedding_provider.model_version,
                article_ids=article_ids,
            )
            for capture in session.scalars(
                select(PersonalCapture).where(
                    PersonalCapture.processing_run_id == run.id,
                    PersonalCapture.article_id.in_(article_ids),
                )
            ):
                if (
                    session.scalar(
                        select(EventArticle.event_id)
                        .where(EventArticle.article_id == capture.article_id)
                        .limit(1)
                    )
                    is not None
                ):
                    capture.enrichment_state = "grouped"
            _stage(
                run,
                "grouping",
                "succeeded",
                events_created=clustered.events_created,
                articles_clustered=clustered.articles_clustered,
            )
            # record_event_observations refreshes the locked identity-map row. Flush the JSON
            # stage update first so that refresh cannot replace it with the prior database value.
            session.flush()
            observations = record_event_observations(session, run.id, ownership_token)
            _stage(run, "observations", "succeeded", events_observed=len(observations))
            session.commit()

        check_budget()
        with session_factory() as session:
            run = lock_owned_run(session, run_id, ownership_token)
            preparations = prepare_article_claims(session, run.id, ownership_token)
            supported = sum(item.status == "supported" for item in preparations)
            _stage(
                run,
                "claim_preparation",
                "succeeded",
                supported=supported,
                abstained=sum(item.status == "abstained" for item in preparations),
            )
            session.commit()

        check_budget()
        with session_factory() as session:
            run = lock_owned_run(session, run_id, ownership_token)
            route = (_profile(session, run).settings or {}).get("model_route")
            if not isinstance(route, dict) or not route:
                raise RuntimeError("personal profile has no explicit model route")
            snapshot = freeze_brief_snapshot(
                session,
                run.id,
                ownership_token,
                model_route=route,
                prepared_at=_utc(clock()),
            )
            _stage(run, "snapshot", "succeeded", snapshot_id=str(snapshot.id))
            session.commit()

        report = generate_personal_brief(
            run_id,
            ownership_token,
            session_factory=session_factory,
            orchestrator_factory=orchestrator_factory,
            paid_block_reason=paid_block_reason,
        )
        return PersonalCoordinatorResult(
            run_id=run_id,
            report=report,
            feeds_attempted=int(coverage.get("feeds_attempted") or 0),
            feeds_succeeded=int(coverage.get("feeds_succeeded") or 0),
            feeds_failed=int(coverage.get("feeds_failed") or 0),
            articles_captured=int(coverage.get("articles_captured") or 0),
            articles_admitted=len(admitted_ids),
            events_observed=len(observations),
            claims_supported=supported,
        )
    except RuntimeCancelled:
        with bind_budget(None):
            _mark_stage_failure(
                run_id, ownership_token, session_factory=session_factory, code="runtime_cancelled"
            )
        raise
    except PersonalCoordinatorError:
        raise
    except Exception as exc:
        try:
            propagate_fatal(exc)
        except RuntimeCancelled:
            with bind_budget(None):
                _mark_stage_failure(
                    run_id,
                    ownership_token,
                    session_factory=session_factory,
                    code="runtime_cancelled",
                )
            raise
        from services.personal.spending import PaidWorkBlocked

        if isinstance(exc, PaidWorkBlocked) and exc.code in {
            "allowance_reached",
            "ai_disabled",
            "configuration_missing",
        }:
            _finish_raw(run_id, ownership_token, session_factory=session_factory, reason=exc.code)
            check_budget()
            with session_factory() as session:
                deferred = session.get(PersonalRun, run_id)
                retained = deferred.coverage or {}
                return PersonalCoordinatorResult(
                    run_id=run_id,
                    report=None,
                    feeds_attempted=int(retained.get("feeds_attempted") or 0),
                    feeds_succeeded=int(retained.get("feeds_succeeded") or 0),
                    feeds_failed=int(retained.get("feeds_failed") or 0),
                    articles_captured=int(retained.get("articles_captured") or 0),
                    articles_admitted=len(deferred.admitted_article_ids),
                    events_observed=len(deferred.event_ids),
                    claims_supported=0,
                )
        _mark_stage_failure(
            run_id,
            ownership_token,
            session_factory=session_factory,
            code="personal_workflow_failed",
        )
        raise PersonalCoordinatorError("personal daily workflow failed") from exc


__all__ = [
    "MAX_FEED_ITEMS",
    "MAX_PENDING_CAPTURES",
    "PersonalCoordinatorError",
    "PersonalCoordinatorResult",
    "run_personal_daily",
]
