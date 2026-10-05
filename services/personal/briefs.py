"""Generate personal daily briefs from one immutable personal snapshot.

The personal workflow deliberately reuses the production report composer, grounding gate and
lifecycle.  Its only adapter is the input boundary: selected events and citable source-reported
assertions come from ``PersonalBriefSnapshot.input_payload`` instead of the mutable global event
window/context repositories.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    Claim,
    ClaimEvidence,
    EvidenceItem,
    Job,
    PersonalBriefSnapshot,
    PersonalProfileRevision,
    PersonalReportLink,
    PersonalRun,
    PersonalWorkspace,
    Report,
    ReportSection,
)
from db.models.core import REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY
from db.models.personal import PERSONAL_REPORT_TYPE
from services.llm.repository import SQLAlchemyLLMRuntimeRepository
from services.personal.audit_repository import PersonalAuditRepository
from services.personal.deadlines import bind_budget, cancellation_from, check_budget
from services.personal.runs import finish_run, lock_owned_run
from services.reports.composition import DraftDegradationCode, compose_brief
from services.reports.context import (
    BriefContext,
    ClaimContext,
    EventEvidenceContext,
    EvidenceArticle,
    EvidenceLink,
    ExcerptOrigin,
    SourceExcerpt,
)
from services.reports.contracts import (
    BriefInputs,
    CompositionPolicy,
    DataQuality,
    DataQualityNote,
    ExecutiveSummaryInputs,
    LinkedRisk,
    RiskProvenance,
    RiskRadar,
    SelectedEvent,
)
from services.reports.grounding import GateOutcome, run_grounding_gate
from services.reports.lifecycle import (
    ReportLifecycleRepository,
    ReportSnapshot,
    ReportStatus,
    generated_by_run_id_for,
)
from services.reports.material import build_brief_material
from services.reports.window import BriefWindow

_PERSONAL_LLM_JOB_NAMESPACE = uuid.UUID("467884d1-d9d8-48fd-8308-40c43d3fb56c")
_PERSONAL_REPORT_LOCK_NAMESPACE = 0x5032
_MAX_CLAIMS_PER_EVENT = 10


class PersonalBriefGenerationError(RuntimeError):
    """An unexpected post-reservation fault, after best-effort durable failure marking."""

    def __init__(
        self,
        report_id: uuid.UUID,
        cause: BaseException,
        *,
        durable_failure_marked: bool,
    ) -> None:
        self.report_id = report_id
        self.cause = cause
        self.durable_failure_marked = durable_failure_marked
        super().__init__(
            f"personal brief {report_id} failed: {type(cause).__name__}: {cause}; "
            f"durable failure marked: {durable_failure_marked}"
        )


@dataclass(frozen=True)
class PersonalBriefGenerationResult:
    report: ReportSnapshot
    run_id: uuid.UUID
    snapshot_id: uuid.UUID
    selected_event_ids: tuple[uuid.UUID, ...]
    gate_outcome: GateOutcome
    section_count: int
    published: bool


class _RunsOrchestrator(Protocol):
    def run(self, request: Any) -> Any: ...


class PersonalNamespacedOrchestrator:
    """Put composer/grounding audit Jobs in a personal-run namespace.

    The shared composer creates one stable Job per global brief date.  Reusing that key would let a
    personal report overwrite a legacy Job row through ``Session.merge``.  This wrapper replaces
    only the Job identity; prompts, schemas, claim whitelists, routing and provider execution remain
    the production path.
    """

    def __init__(self, delegate: _RunsOrchestrator, run_id: uuid.UUID) -> None:
        self._delegate = delegate
        self._run_id = run_id

    def run(self, request: Any) -> Any:
        original = request.job
        job_key = f"personal:{self._run_id}:{original.job_type}"
        now = datetime.datetime.now(datetime.UTC)
        job = Job(
            id=uuid.uuid5(_PERSONAL_LLM_JOB_NAMESPACE, job_key),
            job_key=job_key,
            job_type=f"personal_{original.job_type}"[:128],
            state="queued",
            attempt=original.attempt,
            max_attempts=original.max_attempts,
            related_ids={
                **(original.related_ids or {}),
                "personal_run_id": str(self._run_id),
            },
            error=None,
            safe_to_rerun=True,
            created_at=now,
            updated_at=now,
        )
        return self._delegate.run(replace(request, job=job))

    def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_instant(value: object, *, field: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise ValueError(f"personal snapshot {field} is missing")
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"personal snapshot {field} is not timezone-aware")
    return parsed


def _capture_quality(payload: dict[str, Any]) -> tuple[DataQualityNote, bool]:
    """Validate exact capture accounting; return its rendered note and partial flag."""

    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("personal snapshot capture coverage is missing")
    configured = coverage.get("feeds_configured", coverage.get("feeds_attempted"))
    attempted = coverage.get("feeds_attempted")
    succeeded = coverage.get("feeds_succeeded")
    failed = coverage.get("feeds_failed")
    paused = coverage.get("feeds_paused", 0)
    counts = (configured, attempted, succeeded, failed, paused)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
        raise ValueError("personal snapshot capture feed counts are invalid")
    if (
        configured < 1
        or attempted < 0
        or succeeded < 0
        or failed < 0
        or paused < 0
        or succeeded + failed != attempted
        or attempted + paused != configured
    ):
        raise ValueError("personal snapshot capture feed counts are inconsistent")
    failures = coverage.get("feed_failures", [])
    if not isinstance(failures, list) or len(failures) != failed:
        raise ValueError("personal snapshot capture failure details are inconsistent")
    if succeeded == 0 and paused == 0:
        raise ValueError("all configured personal feeds failed; this is not a quiet result")
    captured = coverage.get("articles_captured", coverage.get("captured", 0))
    if isinstance(captured, bool) or not isinstance(captured, int) or captured < 0:
        raise ValueError("personal snapshot captured article count is invalid")
    pauses = coverage.get("feed_pauses", [])
    if not isinstance(pauses, list):
        raise ValueError("personal snapshot capture pause details are invalid")
    partial = failed > 0 or paused > 0 or bool(pauses)
    detail = (
        f"Capture completed for {succeeded} of {configured} configured feeds; "
        f"{captured} articles were retained, {failed} feeds failed, and "
        f"{paused} feeds were paused at pending capacity."
    )
    return DataQualityNote(DataQuality.CAPTURE_COVERAGE, detail), partial


def _source_articles(payload: dict[str, Any]) -> dict[uuid.UUID, dict[str, Any]]:
    articles: dict[uuid.UUID, dict[str, Any]] = {}
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("personal snapshot candidates are invalid")
    for candidate in candidates:
        source_inputs = candidate.get("source_inputs") if isinstance(candidate, dict) else None
        rows = source_inputs.get("articles") if isinstance(source_inputs, dict) else None
        if not isinstance(rows, list):
            raise ValueError("personal snapshot candidate sources are invalid")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("personal snapshot source article is invalid")
            try:
                article_id = uuid.UUID(row["article_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("personal snapshot source article identity is invalid") from exc
            previous = articles.setdefault(article_id, row)
            if previous != row:
                raise ValueError("personal snapshot repeats an article with different source data")
    return articles


def _validated_claim_ids(session: Session, claims: list[dict[str, Any]]) -> set[uuid.UUID]:
    identities: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]] = []
    for item in claims:
        try:
            identities.append(
                (
                    uuid.UUID(item["claim_id"]),
                    uuid.UUID(item["evidence_item_id"]),
                    uuid.UUID(item["article_id"]),
                    item["exact_excerpt"],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("personal snapshot citable claim identity is invalid") from exc
    if not identities:
        return set()
    claim_ids = {item[0] for item in identities}
    evidence_ids = {item[1] for item in identities}
    stored_claims = {
        row.id: row.claim_text
        for row in session.execute(select(Claim).where(Claim.id.in_(claim_ids))).scalars()
    }
    evidence = {
        row.id: row
        for row in session.execute(
            select(EvidenceItem).where(EvidenceItem.id.in_(evidence_ids))
        ).scalars()
    }
    links = set(
        session.execute(
            select(ClaimEvidence.claim_id, ClaimEvidence.evidence_item_id).where(
                ClaimEvidence.claim_id.in_(claim_ids),
                ClaimEvidence.evidence_item_id.in_(evidence_ids),
                ClaimEvidence.support_type == "supports",
            )
        ).all()
    )
    for claim_id, evidence_id, article_id, exact_excerpt in identities:
        item = evidence.get(evidence_id)
        if stored_claims.get(claim_id) != exact_excerpt:
            raise ValueError("personal snapshot claim no longer matches its exact source assertion")
        if (
            item is None
            or item.source_type != "article"
            or item.source_id != str(article_id)
            or (claim_id, evidence_id) not in links
        ):
            raise ValueError("personal snapshot claim lacks its supportive canonical article link")
    return claim_ids


def build_personal_brief_inputs(
    session: Session, snapshot: PersonalBriefSnapshot
) -> tuple[BriefInputs, BriefContext, bool]:
    """Translate one immutable personal snapshot into the shared descriptive brief contracts."""

    payload = snapshot.input_payload
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "personal-brief-input.v1"
        or _canonical_hash(payload) != snapshot.input_hash
    ):
        raise ValueError("personal brief snapshot hash or contract is invalid")
    if payload.get("run_id") != str(snapshot.run_id):
        raise ValueError("personal brief snapshot run identity is invalid")
    # Absence identifies a pre-policy snapshot. Never reinterpret an immutable retry using
    # today's personal defaults; malformed/unknown identities fail before any model dispatch.
    composition_policy = CompositionPolicy(
        payload.get("composition_policy", CompositionPolicy.LEGACY.value)
    )
    if payload.get("model_route") != snapshot.model_route:
        raise ValueError("personal brief snapshot model route identity is invalid")
    profile = session.get(PersonalProfileRevision, snapshot.profile_revision_id)
    profile_route = (profile.settings or {}).get("model_route") if profile is not None else None
    if profile is None or profile_route != snapshot.model_route:
        raise ValueError("personal brief snapshot route differs from its frozen profile")
    capture_note, capture_partial = _capture_quality(payload)

    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("personal brief snapshot candidates are invalid")
    candidates_by_event: dict[uuid.UUID, dict[str, Any]] = {}
    ordered_event_ids: list[uuid.UUID] = []
    for candidate in raw_candidates:
        try:
            event_id = uuid.UUID(candidate["event_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("personal snapshot candidate event identity is invalid") from exc
        if event_id in candidates_by_event:
            raise ValueError("personal snapshot repeats a candidate event")
        candidates_by_event[event_id] = candidate
        ordered_event_ids.append(event_id)
    if ordered_event_ids != list(snapshot.candidate_event_ids):
        raise ValueError("personal snapshot candidate ordering is invalid")
    if ordered_event_ids[:5] != list(snapshot.selected_event_ids):
        raise ValueError("personal snapshot selected event ordering is invalid")

    source_articles = _source_articles(payload)
    selected: list[SelectedEvent] = []
    for rank, event_id in enumerate(snapshot.selected_event_ids, start=1):
        candidate = candidates_by_event[event_id]
        source_inputs = candidate.get("source_inputs")
        ranking = candidate.get("ranking")
        if not isinstance(source_inputs, dict) or not isinstance(ranking, dict):
            raise ValueError("personal snapshot candidate inputs are invalid")
        title = source_inputs.get("event_title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("personal snapshot event title is invalid")
        hotness_value = ranking.get("hotness")
        hotness = float(hotness_value) if hotness_value is not None else None
        selected.append(
            SelectedEvent(
                rank=rank,
                event_id=event_id,
                title=title,
                hotness_score=hotness,
                max_linked_risk=LinkedRisk(0.0, RiskProvenance.NONE),
                # Personal P2 ranking is source-count/hotness/recency.  This monotonic adapter
                # preserves its already-frozen order without pretending it is a risk score.
                ranking_score=float(len(snapshot.selected_event_ids) - rank + 1),
                credibility_sum=float(ranking.get("source_count") or 0),
                developing=False,
            )
        )

    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        raise ValueError("personal snapshot claims are invalid")
    citable = [
        item for item in raw_claims if isinstance(item, dict) and item.get("citable") is True
    ]
    _validated_claim_ids(session, citable)

    claims_by_article: dict[uuid.UUID, list[ClaimContext]] = {}
    for item in citable:
        article_id = uuid.UUID(item["article_id"])
        source = source_articles.get(article_id)
        if source is None:
            raise ValueError("personal snapshot citable claim is outside candidate sources")
        exact_excerpt = item["exact_excerpt"]
        if not isinstance(exact_excerpt, str) or not exact_excerpt:
            raise ValueError("personal snapshot citable excerpt is invalid")
        if len(exact_excerpt) > 200:
            raise ValueError("personal snapshot citable excerpt exceeds the source-text bound")
        origin = ExcerptOrigin.PUBLISHER_RSS
        link = EvidenceLink(
            article=EvidenceArticle(
                article_id=article_id,
                title=str(source.get("title") or "Untitled source"),
                publisher=(str(source["publisher"]) if source.get("publisher") else None),
                url=(str(source["url"]) if source.get("url") else None),
                published_at=(
                    _parse_instant(source["published_at"], field="article published_at")
                    if source.get("published_at")
                    else None
                ),
                source_credibility=None,
                excerpt=SourceExcerpt(
                    text=exact_excerpt,
                    origin=origin,
                    truncated=False,
                ),
            ),
            support_type="supports",
            support_confidence=None,
        )
        claims_by_article.setdefault(article_id, []).append(
            ClaimContext(
                claim_id=uuid.UUID(item["claim_id"]),
                claim_text=exact_excerpt,
                claim_type="source_reported_assertion",
                claim_confidence=None,
                links=(link,),
            )
        )

    evidence_contexts: list[EventEvidenceContext] = []
    quality: list[DataQualityNote] = [
        capture_note,
        DataQualityNote(
            DataQuality.NO_RISK_OBSERVATIONS,
            "Personal Phase 2 does not compute or narrate risk scores.",
        ),
        DataQualityNote(
            DataQuality.NO_RELIABLE_ANALOGY,
            "Historical analogy generation is disabled for the personal Phase 2 workflow.",
        ),
        DataQualityNote(
            DataQuality.NO_PRIOR_BRIEF,
            "No prior personal brief is injected into this immutable snapshot.",
        ),
    ]
    for event in selected:
        candidate = candidates_by_event[event.event_id]
        articles = candidate["source_inputs"]["articles"]
        event_claims: list[ClaimContext] = []
        seen: set[uuid.UUID] = set()
        for article in articles:
            article_id = uuid.UUID(article["article_id"])
            for claim in claims_by_article.get(article_id, ()):
                if claim.claim_id not in seen:
                    seen.add(claim.claim_id)
                    event_claims.append(claim)
        truncated = len(event_claims) > _MAX_CLAIMS_PER_EVENT
        if truncated:
            quality.append(
                DataQualityNote(
                    DataQuality.EVIDENCE_TRUNCATED,
                    f"Event {event.title!r} had more than {_MAX_CLAIMS_PER_EVENT} citable "
                    "source assertions; the remainder were withheld.",
                )
            )
        evidence_contexts.append(
            EventEvidenceContext(
                event_id=event.event_id,
                claims=tuple(event_claims[:_MAX_CLAIMS_PER_EVENT]),
                truncated=truncated,
            )
        )
    if not selected:
        quality.append(
            DataQualityNote(
                DataQuality.NO_EVENTS_IN_WINDOW,
                "The configured personal sources produced no matching event candidates.",
            )
        )

    captured = payload.get("captured")
    if not isinstance(captured, dict):
        raise ValueError("personal snapshot capture window is invalid")
    start = _parse_instant(captured.get("start"), field="capture start")
    end = _parse_instant(captured.get("end"), field="capture end")
    local_date = datetime.date.fromisoformat(payload["local_date"])
    window = BriefWindow(brief_date=local_date, start=start, end=end)
    events = tuple(selected)
    inputs = BriefInputs(
        window=window,
        top_events=events,
        executive_summary=ExecutiveSummaryInputs(
            alert_state_changes=(), top_events=events[:2], largest_risk_move=None
        ),
        risk_radar=RiskRadar(current=(), previous=(), moves=()),
        prior_brief=None,
        data_quality_notes=tuple(quality),
        composition_policy=composition_policy,
    )
    context = BriefContext(
        evidence=tuple(evidence_contexts),
        analogies=(),
        forecasts=(),
        data_quality_notes=(),
    )
    return inputs, context, capture_partial


def _report_lock_key(workspace_id: uuid.UUID, local_date: datetime.date) -> int:
    digest = hashlib.sha256(f"{workspace_id}:{local_date.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:4], byteorder="big", signed=True)


def _create_personal_report(
    session: Session,
    run: PersonalRun,
    snapshot: PersonalBriefSnapshot,
) -> Report:
    session.execute(
        select(
            func.pg_advisory_xact_lock(
                _PERSONAL_REPORT_LOCK_NAMESPACE,
                _report_lock_key(run.workspace_id, run.local_date),
            )
        )
    )
    version = (
        session.scalar(
            select(func.max(PersonalReportLink.version)).where(
                PersonalReportLink.workspace_id == run.workspace_id,
                PersonalReportLink.brief_date == run.local_date,
            )
        )
        or 0
    ) + 1
    report = Report(
        user_id=session.scalar(
            select(PersonalWorkspace.owner_id).where(PersonalWorkspace.id == run.workspace_id)
        ),
        report_type=PERSONAL_REPORT_TYPE,
        brief_date=run.local_date,
        event_id=None,
        title=f"Personal News Brief — {run.local_date.isoformat()}",
        status=ReportStatus.GENERATING.value,
        version=version,
        change_reason=(None if version == 1 else f"personal run retry attempt {run.attempt}"),
        stale=False,
        content_policy=REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY,
    )
    session.add(report)
    session.flush()
    session.add(
        PersonalReportLink(
            report_id=report.id,
            workspace_id=run.workspace_id,
            run_id=run.id,
            snapshot_id=snapshot.id,
            brief_date=run.local_date,
            version=version,
        )
    )
    run.report_id = report.id
    session.flush()
    return report


def _published_recovery(
    session: Session,
    run: PersonalRun,
    snapshot: PersonalBriefSnapshot,
    ownership_token: uuid.UUID,
) -> PersonalBriefGenerationResult | None:
    """Finish a run whose report published before its terminal run update committed."""

    if run.report_id is None:
        return None
    report = session.get(Report, run.report_id)
    link = session.get(PersonalReportLink, run.report_id)
    if report is None or link is None or report.status != ReportStatus.PUBLISHED.value:
        return None
    if (
        link.run_id != run.id
        or link.workspace_id != run.workspace_id
        or link.snapshot_id != snapshot.id
        or link.brief_date != run.local_date
        or link.version != report.version
    ):
        raise ValueError("published personal recovery report identity is invalid")
    prior_result = run.result if isinstance(run.result, dict) else {}
    _, capture_partial = _capture_quality(snapshot.input_payload)
    terminal_state = "partially_failed" if capture_partial else "succeeded"
    report_snapshot = ReportLifecycleRepository(session).get_report(report.id)
    if report_snapshot is None:
        raise ValueError("published personal recovery report is unavailable")
    section_count = int(
        session.scalar(
            select(func.count())
            .select_from(ReportSection)
            .where(ReportSection.report_id == report.id)
        )
        or 0
    )
    finish_run(
        session,
        run.id,
        ownership_token,
        state=terminal_state,
        result={
            **prior_result,
            "report_id": str(report.id),
            "snapshot_id": str(snapshot.id),
            "gate_outcome": GateOutcome.PASS.value,
            "published": True,
            "section_count": section_count,
            "attempt": run.attempt,
            "capture_partial": capture_partial,
            "recovered_after_publication": True,
        },
        error=(
            {
                "code": "partial_capture",
                "message": "The brief published from partial configured-feed coverage.",
            }
            if capture_partial
            else None
        ),
    )
    session.commit()
    return PersonalBriefGenerationResult(
        report=report_snapshot,
        run_id=run.id,
        snapshot_id=snapshot.id,
        selected_event_ids=tuple(snapshot.selected_event_ids),
        gate_outcome=GateOutcome.PASS,
        section_count=section_count,
        published=True,
    )


def generate_personal_brief(
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    *,
    session_factory: Any,
    orchestrator_factory: Any,
    paid_block_reason: Callable[[], str | None] | None = None,
) -> PersonalBriefGenerationResult:
    """Compose, ground, persist and publish one personal run's frozen snapshot."""

    start_session = session_factory()
    try:
        run = lock_owned_run(start_session, run_id, ownership_token)
        if run.snapshot_id is None:
            raise ValueError("personal run has no frozen brief snapshot")
        snapshot = start_session.get(PersonalBriefSnapshot, run.snapshot_id)
        if snapshot is None:
            raise ValueError("personal run brief snapshot is unavailable")
        recovered = _published_recovery(start_session, run, snapshot, ownership_token)
        if recovered is not None:
            return recovered
        if run.report_id is not None:
            previous = start_session.get(Report, run.report_id)
            previous_result = run.result if isinstance(run.result, dict) else {}
            explicit_later_attempt = previous_result.get("attempt") not in {None, run.attempt}
            if previous is not None and not (
                previous.status == ReportStatus.FAILED.value
                or (previous.status == ReportStatus.PUBLISHED.value and explicit_later_attempt)
            ):
                raise ValueError(
                    "personal run has an unfinished report; use explicit retry after failure"
                )
        report = _create_personal_report(start_session, run, snapshot)
        report_id = report.id
        snapshot_id = snapshot.id
        start_session.commit()
    except Exception:
        start_session.rollback()
        raise
    finally:
        start_session.close()

    try:
        session = session_factory()
    except BaseException as cause:
        if cancellation_from(cause) is not None:
            with bind_budget(None):
                _mark_failed(
                    run_id,
                    ownership_token,
                    report_id=report_id,
                    session_factory=session_factory,
                    cause=cause,
                )
            raise cancellation_from(cause) from cause
        marked = _mark_failed(
            run_id,
            ownership_token,
            report_id=report_id,
            session_factory=session_factory,
            cause=cause,
        )
        raise PersonalBriefGenerationError(
            report_id, cause, durable_failure_marked=marked
        ) from cause
    try:
        run = lock_owned_run(session, run_id, ownership_token)
        if run.report_id != report_id or run.snapshot_id != snapshot_id:
            raise ValueError("personal run report/snapshot identity changed")
        snapshot = session.get(PersonalBriefSnapshot, snapshot_id)
        if snapshot is None:
            raise ValueError("personal brief snapshot is unavailable")
        inputs, context, capture_partial = build_personal_brief_inputs(session, snapshot)
        material = build_brief_material(inputs, context, prediction_backed_outputs_enabled=False)
        delegate = orchestrator_factory(session, dict(snapshot.model_route))
        repository = getattr(delegate, "_repository", None)
        if isinstance(repository, SQLAlchemyLLMRuntimeRepository) and repository.commit_on_write:
            raise ValueError(
                "personal brief orchestration must join the owned database transaction"
            )
        selected_event_ids = tuple(snapshot.selected_event_ids)
        # Provider composition uses immutable in-memory snapshot material. Audit
        # writes each reacquire ownership in an independent short transaction.
        if isinstance(repository, SQLAlchemyLLMRuntimeRepository):
            delegate._repository = PersonalAuditRepository(
                session_factory,
                run_id,
                ownership_token,
                ledger_accounted=getattr(repository, "personal_paid_ledger_accounted", False),
            )
        session.rollback()
        check_budget()
        orchestrator = PersonalNamespacedOrchestrator(delegate, run_id=run_id)
        try:
            draft = compose_brief(
                orchestrator,
                inputs=inputs,
                context=context,
                material=material,
                prediction_backed_outputs_enabled=False,
            )
            check_budget()
            run = lock_owned_run(session, run_id, ownership_token)
            lifecycle = ReportLifecycleRepository(session)
            lifecycle.transition(report_id, ReportStatus.GROUNDING_CHECK)
            session.commit()
            gate = run_grounding_gate(orchestrator, inputs=inputs, context=context, draft=draft)
        finally:
            orchestrator.close()

        check_budget()
        run = lock_owned_run(session, run_id, ownership_token)
        if run.report_id != report_id or run.snapshot_id != snapshot_id:
            raise ValueError("personal report identity changed during composition")
        lifecycle = ReportLifecycleRepository(session)
        sections = lifecycle.persist_sections(report_id, gate)
        generated_by = generated_by_run_id_for(gate)
        if generated_by is not None:
            lifecycle.attach_generated_by_run_id(report_id, generated_by)
        processing_degraded = any(
            degradation.code
            in {DraftDegradationCode.COMPOSITION_FAILED, DraftDegradationCode.BUDGET_UNMET}
            for section in draft.sections
            for degradation in section.degradations
        ) or any(
            section.regenerated
            and (
                not section.regeneration_attempts
                or not section.regeneration_attempts[-1].within_budget
            )
            for section in gate.sections
        )
        paid_reason = paid_block_reason() if paid_block_reason else None
        deferred_paid = paid_reason in {"allowance_reached", "ai_disabled", "configuration_missing"}
        if gate.outcome is GateOutcome.PASS and not processing_degraded and not paid_reason:
            final = lifecycle.publish(report_id, gate)
            terminal_state = "partially_failed" if capture_partial else "succeeded"
        else:
            final = lifecycle.transition(report_id, ReportStatus.FAILED)
            terminal_state = "partially_failed" if processing_degraded else "failed"
        if deferred_paid:
            # The report stays failed/unpublished; the bounded daily update can finish with
            # an explicit allowance limitation rather than inventing successful prose.
            terminal_state = "partially_failed" if capture_partial else "succeeded"
            stages = dict(run.stage_results or {})
            stages["report"] = {"status": "blocked", "reason": paid_reason}
            run.stage_results = stages
            session.flush()
        finish_run(
            session,
            run_id,
            ownership_token,
            state=terminal_state,
            result={
                "report_id": str(report_id),
                "snapshot_id": str(snapshot_id),
                "gate_outcome": gate.outcome.value,
                "published": final.status == ReportStatus.PUBLISHED.value,
                "section_count": len(sections),
                "attempt": run.attempt,
                "capture_partial": capture_partial,
                "processing_degraded": processing_degraded,
                **({"paid_work_reason": paid_reason} if paid_reason else {}),
            },
            error=_terminal_error(
                terminal_state,
                gate_outcome=gate.outcome,
                capture_partial=capture_partial,
                processing_degraded=processing_degraded,
            ),
        )
        session.commit()
        return PersonalBriefGenerationResult(
            report=final,
            run_id=run_id,
            snapshot_id=snapshot_id,
            selected_event_ids=selected_event_ids,
            gate_outcome=gate.outcome,
            section_count=len(sections),
            published=final.status == ReportStatus.PUBLISHED.value,
        )
    except BaseException as cause:
        session.rollback()
        session.close()
        if cancellation_from(cause) is not None:
            with bind_budget(None):
                _mark_failed(
                    run_id,
                    ownership_token,
                    report_id=report_id,
                    session_factory=session_factory,
                    cause=cause,
                )
            raise cancellation_from(cause) from cause
        marked = _mark_failed(
            run_id,
            ownership_token,
            report_id=report_id,
            session_factory=session_factory,
            cause=cause,
        )
        raise PersonalBriefGenerationError(
            report_id, cause, durable_failure_marked=marked
        ) from cause
    finally:
        if session.is_active:
            session.close()


def _mark_failed(
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
    *,
    report_id: uuid.UUID,
    session_factory: Any,
    cause: BaseException,
) -> bool:
    try:
        session = session_factory()
    except Exception:
        return False
    try:
        lock_owned_run(session, run_id, ownership_token)
        lifecycle = ReportLifecycleRepository(session)
        report = lifecycle.get_report(report_id)
        if report is None:
            return False
        if report.status not in {ReportStatus.PUBLISHED.value, ReportStatus.FAILED.value}:
            lifecycle.transition(report_id, ReportStatus.FAILED)
        finish_run(
            session,
            run_id,
            ownership_token,
            state="failed",
            error={
                "code": "brief_generation_failed",
                "message": "Personal brief generation failed; an explicit retry may be available.",
            },
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def _terminal_error(
    state: str,
    *,
    gate_outcome: GateOutcome,
    capture_partial: bool,
    processing_degraded: bool,
) -> dict[str, str] | None:
    if state == "succeeded":
        return None
    if gate_outcome is GateOutcome.BLOCKED:
        return {
            "code": "publication_gate_blocked",
            "message": "The brief did not pass its publication checks.",
        }
    if processing_degraded:
        return {
            "code": "model_processing_degraded",
            "message": "Brief generation stopped because model-generated sections failed.",
        }
    if capture_partial:
        return {
            "code": "partial_capture",
            "message": "The brief published from partial configured-feed coverage.",
        }
    return {
        "code": "personal_processing_incomplete",
        "message": "Personal processing was incomplete.",
    }


__all__ = [
    "PersonalBriefGenerationError",
    "PersonalBriefGenerationResult",
    "PersonalNamespacedOrchestrator",
    "build_personal_brief_inputs",
    "generate_personal_brief",
]
