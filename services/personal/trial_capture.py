"""Read-only, private Phase 5 evidence capture from one recorded personal run.

The caller supplies a clean, already-open consistent read-only transaction. On
PostgreSQL this means REPEATABLE READ (or SERIALIZABLE) and READ ONLY. This module
never starts a run, invokes a provider, commits, flushes, or changes database rows.
Its retained RSS text is private review material, not a public demonstration.
Database observations cannot establish that a human read, saved, reopened, or
exported anything, or supply a quality/usefulness judgment.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from db.models import (
    Event,
    PersonalBriefSnapshot,
    PersonalCapture,
    PersonalFeedReceipt,
    PersonalProfileRevision,
    PersonalReportLink,
    PersonalRun,
    PersonalRunEventObservation,
    PersonalWorkspace,
    Report,
    WatchlistItem,
)
from db.models.personal_spending import PersonalLegacyUsage, PersonalPaidRequest
from services.personal.contracts import article_matches_profile
from services.personal.exports import PersonalBriefReadError, PersonalBriefRepository
from services.personal.settings import safe_model_route

CAPTURE_SCHEMA = "personal-trial-session-evidence.v1"
_COUNTS = (
    "feeds_configured",
    "feeds_attempted",
    "feeds_succeeded",
    "feeds_failed",
    "feeds_paused",
    "items_fetched",
    "articles_captured",
    "articles_admitted",
    "pending_total",
    "pending_capacity",
)
_SETTINGS = (
    "daily_article_limit",
    "run_article_limit",
    "enrichment_article_limit",
    "max_enabled_feeds",
    "ai_enabled",
    "monthly_allowance_usd",
    "run_allowance_usd",
)
_SOURCE_FIELDS = (
    "revision_id",
    "article_id",
    "source_id",
    "title",
    "rss_summary",
    "publisher",
    "published_at",
    "content_hash",
    "truncated",
)
_CLAIM_FIELDS = (
    "preparation_id",
    "article_revision_id",
    "article_id",
    "claim_id",
    "evidence_item_id",
    "source_field",
    "span",
    "exact_excerpt",
    "status",
    "article_revision_hash",
    "citable",
)
_TOKEN_QUERY = re.compile(r"token|key|secret|password|credential|signature|authorization", re.I)
_CODE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


class TrialCaptureError(ValueError):
    """Evidence is ineligible, or its frozen identity is inconsistent."""


def _iso(value: dt.datetime | dt.date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    return value.isoformat()


def _json(value: Any) -> Any:
    """Convert only explicitly selected fields; never serialize ORM rows/dictionaries whole."""
    if isinstance(value, (dt.datetime, dt.date)):
        return _iso(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _code(value: Any) -> str | None:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _url(value: Any) -> str | None:
    """Remove URL credentials and credential-like query parameters from private records too."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        query = urlencode(
            [(key, item) for key, item in parse_qsl(parsed.query) if not _TOKEN_QUERY.search(key)]
        )
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except ValueError:
        return None


def _route(value: Any) -> dict[str, Any]:
    route = safe_model_route(value)
    for role in ("generation", "embedding"):
        if "price_source_url" in route.get(role, {}):
            route[role]["price_source_url"] = _url(route[role]["price_source_url"])
    return route


def _coverage(value: Any) -> dict[str, Any]:
    raw = _mapping(value)
    result = {key: raw[key] for key in _COUNTS if type(raw.get(key)) is int and raw[key] >= 0}
    for key in ("feed_failures", "feed_pauses"):
        if isinstance(raw.get(key), list):
            result[key] = [
                {"source_id": str(row.get("source_id")), "code": _code(row.get("code"))}
                for row in raw[key]
                if isinstance(row, dict)
            ]
    return result


def _safe_source(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: row[key]
        for key in _SOURCE_FIELDS
        if key in row
        and (
            row[key] is None
            or isinstance(row[key], str)
            or (key == "truncated" and type(row[key]) is bool)
        )
    }
    result["url"] = _url(row.get("url"))
    result["url_redacted"] = result["url"] != row.get("url")
    return _json(result)


def _transaction(session: Session) -> None:
    if not session.in_transaction():
        raise TrialCaptureError("caller must open a consistent read-only transaction")
    if session.new or session.dirty or session.deleted:
        raise TrialCaptureError("evidence capture requires a clean session without pending writes")
    if session.get_bind().dialect.name == "postgresql":
        if session.connection().get_isolation_level() not in {"REPEATABLE READ", "SERIALIZABLE"}:
            raise TrialCaptureError("evidence capture requires a consistent PostgreSQL transaction")
        if session.scalar(text("SHOW transaction_read_only")) != "on":
            raise TrialCaptureError("evidence capture requires a read-only PostgreSQL transaction")


def _group(
    candidate: dict[str, Any],
    run: PersonalRun,
    profile: PersonalProfileRevision,
    *,
    completed_snapshot: bool,
    exclusions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        event_id = str(uuid.UUID(str(candidate["event_id"])))
        membership = [str(uuid.UUID(str(item))) for item in candidate["article_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise TrialCaptureError("frozen group identity is invalid") from exc
    if len(membership) != len(set(membership)):
        raise TrialCaptureError("frozen group repeats an article identity")
    scope = {str(item) for item in run.enrichment_article_ids}
    selected = {str(item) for item in profile.selected_source_ids}
    source_inputs = _mapping(candidate.get("source_inputs"))
    rows = source_inputs.get("articles")
    if not isinstance(rows, list):
        rows = []
    by_article: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        article_id = str(row.get("article_id"))
        if article_id in by_article and by_article[article_id] != row:
            raise TrialCaptureError("frozen article is repeated with different source inputs")
        by_article[article_id] = row
    included: list[str] = []
    sources: list[dict[str, Any]] = []
    missing: list[str] = []
    for article_id in membership:
        row = by_article.get(article_id)
        reason = None
        if article_id not in scope:
            reason = "outside_frozen_enrichment_scope"
        elif row and row.get("source_id") is not None and str(row.get("source_id")) not in selected:
            reason = "source_not_in_frozen_profile"
        elif (
            row
            and isinstance(row.get("title"), str)
            and row["title"].strip()
            and not article_matches_profile(
                row["title"],
                row.get("rss_summary") if isinstance(row.get("rss_summary"), str) else None,
                include_phrases=profile.include_phrases,
                exclude_phrases=profile.exclude_phrases,
            )
        ):
            reason = "does_not_match_frozen_interest"
        if reason:
            exclusions.append(
                {
                    "kind": "group_article",
                    "event_id": event_id,
                    "article_id": article_id,
                    "reason": reason,
                }
            )
            continue
        included.append(article_id)
        if (
            row is None
            or not isinstance(row.get("title"), str)
            or not row["title"].strip()
            or row.get("source_id") is None
        ):
            missing.append(article_id)
        if row is not None:
            sources.append(_safe_source(row))
    if not included:
        exclusions.append(
            {"kind": "group", "event_id": event_id, "reason": "no_qualifying_frozen_members"}
        )
        return None
    findings = []
    if not completed_snapshot:
        findings.append("incomplete_pre_snapshot_observation")
    if missing:
        findings.append("retained_source_inputs_missing")
    ranking = _mapping(candidate.get("ranking"))
    return {
        "event_id": event_id,
        "article_ids": included,
        "observed_article_ids": membership,
        "observation_id": candidate.get("observation_id"),
        "observation_revision": candidate.get("revision"),
        "observed_at": candidate.get("observed_at"),
        "snapshot_id": str(run.snapshot_id) if completed_snapshot else None,
        "source_inputs": sources,
        "source_input_schema": source_inputs.get("schema"),
        "snapshot_event_title": source_inputs.get("event_title")
        if isinstance(source_inputs.get("event_title"), str)
        else None,
        "snapshot_event_summary": source_inputs.get("event_summary")
        if isinstance(source_inputs.get("event_summary"), str)
        else None,
        "ranking": {
            key: ranking[key]
            for key in (
                "candidate_rank",
                "source_count",
                "hotness",
                "newest_publication_at",
                "sort_key",
            )
            if key in ranking
        },
        "evidence_status": "unverifiable" if missing else "retained",
        "completed_brief_snapshot": completed_snapshot,
        "missing_article_ids": missing,
        "findings": findings,
    }


def _groups(session, run, profile, exclusions):
    snapshot = session.get(PersonalBriefSnapshot, run.snapshot_id) if run.snapshot_id else None
    if run.snapshot_id and snapshot is None:
        raise TrialCaptureError("run's frozen snapshot is unavailable")
    observations = list(
        session.scalars(
            select(PersonalRunEventObservation)
            .where(PersonalRunEventObservation.run_id == run.id)
            .order_by(
                PersonalRunEventObservation.event_id, PersonalRunEventObservation.revision.desc()
            )
        )
    )
    by_id = {str(item.id): item for item in observations}
    if snapshot is not None:
        payload = _mapping(snapshot.input_payload)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if (
            snapshot.run_id != run.id
            or snapshot.workspace_id != run.workspace_id
            or snapshot.profile_revision_id != run.profile_revision_id
            or snapshot.input_contract != "personal-brief-input.v1"
            or payload.get("schema") != snapshot.input_contract
            or payload.get("run_id") != str(run.id)
            or payload.get("workspace_id") != str(run.workspace_id)
            or payload.get("profile_revision_id") != str(run.profile_revision_id)
            or payload.get("local_date") != run.local_date.isoformat()
            or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != snapshot.input_hash
        ):
            raise TrialCaptureError("run's frozen snapshot contract is inconsistent")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            raise TrialCaptureError("frozen snapshot candidates are invalid")
        if [row.get("event_id") for row in candidates] != [
            str(item) for item in snapshot.candidate_event_ids
        ]:
            raise TrialCaptureError("frozen snapshot candidate ordering is inconsistent")
        material = []
        for row in candidates:
            observation = by_id.get(str(row.get("observation_id")))
            sources = _mapping(row.get("source_inputs"))
            articles = sources.get("articles")
            if not isinstance(articles, list):
                raise TrialCaptureError("frozen snapshot membership is invalid")
            source_article_ids = [
                item.get("article_id") for item in articles if isinstance(item, dict)
            ]
            article_ids = (
                [str(item) for item in observation.qualifying_article_ids]
                if observation
                else source_article_ids
            )
            if observation is not None and (
                str(observation.event_id) != row.get("event_id")
                or observation.revision != row.get("revision")
                or observation.source_inputs != sources
            ):
                raise TrialCaptureError("snapshot differs from its pinned event observation")
            if len(source_article_ids) != len(set(source_article_ids)) or not set(
                source_article_ids
            ) <= set(article_ids):
                raise TrialCaptureError("snapshot source identity differs from pinned membership")
            material.append(
                {
                    **row,
                    "article_ids": article_ids,
                    "observed_at": _iso(observation.observed_at) if observation else None,
                }
            )
            if observation is None:
                exclusions.append(
                    {
                        "kind": "evidence_gap",
                        "event_id": row.get("event_id"),
                        "reason": "pinned_observation_unavailable_snapshot_retained",
                    }
                )
        basis = "all_frozen_snapshot_candidates"
    else:
        latest = {}
        for row in observations:
            previous = latest.get(row.event_id)
            if previous is None or row.revision > previous.revision:
                latest[row.event_id] = row
        material = [
            {
                "event_id": str(row.event_id),
                "article_ids": row.qualifying_article_ids,
                "observation_id": str(row.id),
                "revision": row.revision,
                "observed_at": _iso(row.observed_at),
                "source_inputs": row.source_inputs,
                "ranking": row.ranking_inputs,
            }
            for row in sorted(latest.values(), key=lambda row: str(row.event_id))
        ]
        basis = "latest_run_observations_at_capture_incomplete_pre_snapshot"
    groups = []
    for row in material:
        group = _group(
            row, run, profile, completed_snapshot=snapshot is not None, exclusions=exclusions
        )
        if group is not None:
            groups.append(group)
    if len({row["event_id"] for row in groups}) != len(groups):
        raise TrialCaptureError("group population repeats a stable event identity")
    return groups, snapshot, basis


def _section(section) -> dict[str, Any]:
    return {
        "section_id": str(section.id),
        "section_order": section.section_order,
        "heading": section.title,
        "body": section.body,
        "blocks": [
            {"text": block.text, "claim_ids": list(block.claim_ids)} for block in section.blocks
        ],
        "claim_ids": [str(item) for item in section.evidence_refs],
        "grounding_status": section.grounding_status,
    }


def _summary_units(document, snapshot: PersonalBriefSnapshot, exclusions) -> list[dict[str, Any]]:
    """Retain entire report context: no heading or introductory claim disappears from review."""
    payload = _mapping(snapshot.input_payload)
    candidates = {row["event_id"]: row for row in payload["candidates"]}
    selected = [str(item) for item in snapshot.selected_event_ids]
    # Shared material contract writes Executive Summary first, then selected events in order.
    # Titles alone never prove event identity. Refuse malformed layouts rather than guess.
    sections = document.sections
    if (
        not sections
        or sections[0].title != "Executive Summary"
        or len(sections) < len(selected) + 2
    ):
        raise TrialCaptureError("published personal report lacks its event section layout")
    event_sections = sections[1 : 1 + len(selected)]
    for event_id, section in zip(selected, event_sections, strict=True):
        if section.title != _mapping(candidates[event_id].get("source_inputs")).get("event_title"):
            raise TrialCaptureError("published event heading differs from its immutable snapshot")
    contexts = [sections[0], *sections[1 + len(selected) :]]
    claims = [row for row in payload.get("claims", []) if isinstance(row, dict)]
    sources = {}
    claim_events: dict[str, set[str]] = {}
    for event_id in selected:
        for row in _mapping(candidates[event_id].get("source_inputs")).get("articles", []):
            sources[row["article_id"]] = _safe_source(row)
            for claim in claims:
                if claim.get("article_id") == row["article_id"] and claim.get("claim_id"):
                    claim_events.setdefault(claim["claim_id"], set()).add(event_id)
    citations = {
        str(citation.claim_id): {
            "claim_id": str(citation.claim_id),
            "text": citation.text,
            "evidence": [
                {
                    "evidence_item_id": str(item.evidence_item_id),
                    "article_id": str(item.article_id),
                    "source_field": item.source_field,
                    "source_input": sources.get(str(item.article_id)),
                }
                for item in citation.evidence
            ],
        }
        for citation in document.citations
    }
    units = []
    for event_id, section in zip(selected, event_sections, strict=True):
        if section.grounding_status != "passed" or not section.body.strip():
            exclusions.append(
                {
                    "kind": "summary_unit",
                    "report_id": str(document.report.id),
                    "version": document.report.version,
                    "event_id": event_id,
                    "reason": "no_rendered_event_summary_grounding_withheld",
                    "retained_section": _section(section),
                }
            )
            continue
        relevant_ids = {str(item) for item in section.evidence_refs}
        for context in contexts:
            # Context is preserved in full, including cross-event assertions. Every cited claim
            # is retained; attribution is separately recorded rather than inferred from clicks.
            relevant_ids.update(str(item) for item in context.evidence_refs)
        frozen_claims = [
            {key: row[key] for key in _CLAIM_FIELDS if key in row}
            for row in claims
            if row.get("claim_id") in relevant_ids
        ]
        units.append(
            {
                "report_id": str(document.report.id),
                "version": document.report.version,
                "snapshot_id": str(snapshot.id),
                "event_id": event_id,
                "published_at": None,
                "publication_time_basis": "unavailable_no_immutable_publication_timestamp",
                "observed_report_created_at": _iso(document.report.created_at),
                "observed_report_updated_at": _iso(document.report.updated_at),
                "heading": section.title,
                "body": section.body,
                "rendered_summary": section.body,
                "event_section": _section(section),
                "introduction": _section(sections[0]),
                "conclusion": [_section(item) for item in contexts[1:]],
                "report_heading": document.report.title,
                "report_context": [_section(item) for item in contexts],
                "claims": frozen_claims,
                "citations": [
                    citations[item] for item in sorted(relevant_ids) if item in citations
                ],
                "claim_event_attribution": {
                    item: sorted(claim_events.get(item, set())) for item in sorted(relevant_ids)
                },
                "source_references": [sources[item] for item in sorted(sources)],
                "source_inputs": [sources[item] for item in sorted(sources)],
                "findings": ["publication_timestamp_unverifiable"],
            }
        )
    return units


def _money(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _paid_row(row) -> dict[str, Any]:
    return _json(
        {
            "request_id": row.id,
            "run_id": row.run_id,
            "attempt": row.attempt,
            "accounting_month": row.accounting_month,
            "status": row.status,
            "role": row.role,
            "route": _route({row.role: row.route}).get(row.role, {}),
            **{
                key: getattr(row, key)
                for key in (
                    "input_token_bound",
                    "output_token_bound",
                    "reserved_usd",
                    "actual_usd",
                    "input_tokens",
                    "output_tokens",
                    "reserved_at",
                    "dispatch_attempt_at",
                    "reconciled_at",
                )
            },
        }
    )


def _costs(session, run, settings, observed_at):
    workspace_rows = list(
        session.scalars(
            select(PersonalPaidRequest)
            .where(PersonalPaidRequest.workspace_id == run.workspace_id)
            .order_by(PersonalPaidRequest.reserved_at, PersonalPaidRequest.id)
        )
    )
    run_rows = [row for row in workspace_rows if row.run_id == run.id]
    months = sorted(
        {observed_at.date().replace(day=1), *(row.accounting_month for row in run_rows)}
    )
    legacy = list(
        session.scalars(
            select(PersonalLegacyUsage)
            .where(PersonalLegacyUsage.accounting_month.in_(months))
            .order_by(PersonalLegacyUsage.llm_run_id)
        )
    )

    def obligation(row):
        if row.status == "reconciled":
            return _money(row.actual_usd)
        if row.status in {"reserved", "dispatching", "uncertain"}:
            return _money(row.reserved_usd)
        return Decimal(0)

    run_values = [obligation(row) for row in run_rows]
    run_total = sum((item for item in run_values if item is not None), Decimal(0))
    month_limit = _money(settings.get("monthly_allowance_usd"))
    run_limit = _money(settings.get("run_allowance_usd"))
    if run_limit is None and settings.get("run_allowance_usd") is None:
        run_limit = month_limit
    if month_limit is not None and run_limit is not None:
        run_limit = min(month_limit, run_limit)
    monthly = []
    for month in months:
        rows = [row for row in workspace_rows if row.accounting_month == month]
        old = [row for row in legacy if row.accounting_month == month]
        values = [obligation(row) for row in rows] + [_money(row.actual_usd) for row in old]
        known = sum((item for item in values if item is not None), Decimal(0))
        unknown = any(item is None for item in values)
        monthly.append(
            {
                "accounting_month": month,
                "workspace_requests": [_paid_row(row) for row in rows],
                "legacy_application_usage": [
                    {"llm_run_id": row.llm_run_id, "actual_usd": row.actual_usd} for row in old
                ],
                "known_obligation_usd": known,
                "unknown_charge_present": unknown,
                "frozen_profile_remaining_usd": (
                    max(Decimal(0), month_limit - known)
                    if month_limit is not None and not unknown
                    else None
                ),
            }
        )
    return {
        "run_requests": [_paid_row(row) for row in run_rows],
        "run_known_obligation_usd": run_total,
        "run_unknown_charge_present": any(item is None for item in run_values),
        "run_frozen_profile_remaining_usd": (
            max(Decimal(0), run_limit - run_total)
            if run_limit is not None and None not in run_values
            else None
        ),
        "monthly_observations": monthly,
        "remaining_basis": "frozen_profile_comparison_at_capture_not_dispatch_permission",
        "findings": [
            "legacy_import_completeness_not_verified",
            "live_allowance_changes_not_reconstructed",
        ],
    }


def capture_session_evidence(session: Session, run_id: uuid.UUID) -> dict[str, Any]:
    """Freeze DB evidence without claiming a reading session or human review occurred.

    Failed or unfinished sessions retain their observed state without claiming a
    terminal outcome. The entire candidate population is independent of pagination.
    ``published_at`` remains null until an actual immutable publication record exists.
    Missing publication chronology blocks deterministic summary selection downstream.
    """
    _transaction(session)
    with session.no_autoflush:
        run = session.get(PersonalRun, run_id)
        if run is None:
            raise LookupError("personal run not found")
        if run.execution_mode != "personal" or run.state not in {
            "queued",
            "running",
            "succeeded",
            "partially_failed",
            "failed",
            "interrupted",
        }:
            raise TrialCaptureError("evidence capture requires an eligible personal run")
        terminal = run.state in {"succeeded", "partially_failed", "failed"}
        workspace = session.get(PersonalWorkspace, run.workspace_id)
        profile = session.get(PersonalProfileRevision, run.profile_revision_id)
        if workspace is None or profile is None or profile.workspace_id != run.workspace_id:
            raise TrialCaptureError("run's workspace or frozen profile is unavailable")
        observed_at = dt.datetime.now(dt.UTC)
        exclusions: list[dict[str, Any]] = []
        groups, snapshot, basis = _groups(session, run, profile, exclusions)
        settings = {
            key: profile.settings[key]
            for key in _SETTINGS
            if key in _mapping(profile.settings)
            and (
                profile.settings[key] is None or isinstance(profile.settings[key], (str, int, bool))
            )
        }
        settings["model_route"] = _route(_mapping(profile.settings).get("model_route"))
        receipts = list(
            session.scalars(
                select(PersonalFeedReceipt)
                .where(
                    PersonalFeedReceipt.run_id == run.id,
                    PersonalFeedReceipt.workspace_id == run.workspace_id,
                )
                .order_by(PersonalFeedReceipt.started_at, PersonalFeedReceipt.id)
            )
        )
        captures = list(
            session.scalars(
                select(PersonalCapture)
                .where(
                    PersonalCapture.workspace_id == run.workspace_id,
                    or_(
                        PersonalCapture.run_id == run.id,
                        PersonalCapture.admitted_run_id == run.id,
                        PersonalCapture.processing_run_id == run.id,
                    ),
                )
                .order_by(PersonalCapture.captured_at, PersonalCapture.id)
            )
        )
        grouped_article_ids = {item for row in groups for item in row["article_ids"]}
        admitted = {str(item) for item in run.admitted_article_ids}
        collection = [
            {
                "capture_id": str(row.id),
                "source_id": str(row.source_id),
                "article_id": str(row.article_id) if row.article_id else None,
                "captured_by_run_id": str(row.run_id),
                "admitted_run_id": str(row.admitted_run_id) if row.admitted_run_id else None,
                "processing_run_id": str(row.processing_run_id) if row.processing_run_id else None,
                "captured_at": _iso(row.captured_at),
                "admitted_at": _iso(row.admitted_at),
                "published_at": _iso(row.published_at),
                "enrichment_state": row.enrichment_state,
                "admission_charged": row.admission_charged,
                "truncated": row.truncated,
                "title": row.title,
                "rss_summary": row.rss_summary,
                "url": _url(row.original_url or row.canonical_url),
            }
            for row in captures
        ]
        summaries = []
        report_records = []
        links = list(
            session.scalars(
                select(PersonalReportLink)
                .where(
                    PersonalReportLink.workspace_id == run.workspace_id,
                    PersonalReportLink.run_id == run.id,
                )
                .order_by(PersonalReportLink.version, PersonalReportLink.report_id)
            )
        )
        repository = PersonalBriefRepository(session, run.workspace_id)
        for link in links:
            report = session.get(Report, link.report_id)
            report_records.append(
                {
                    "report_id": str(link.report_id),
                    "version": link.version,
                    "snapshot_id": str(link.snapshot_id),
                    "status": report.status if report else "unavailable",
                }
            )
            if report is None or report.status != "published":
                exclusions.append(
                    {
                        "kind": "summary_report",
                        "report_id": str(link.report_id),
                        "reason": "not_successfully_published",
                    }
                )
                continue
            try:
                document = repository.published_document(link.report_id)
                linked_snapshot = session.get(PersonalBriefSnapshot, link.snapshot_id)
                if document is None or linked_snapshot is None:
                    raise TrialCaptureError("published report's immutable material is unavailable")
                summaries.extend(_summary_units(document, linked_snapshot, exclusions))
            except (PersonalBriefReadError, TrialCaptureError) as exc:
                # Broken publication remains visible and cannot supply an easier substitute unit.
                exclusions.append(
                    {
                        "kind": "evidence_gap",
                        "report_id": str(link.report_id),
                        "reason": "published_immutable_material_unverifiable",
                        "error_type": type(exc).__name__,
                    }
                )
        saved = []
        if workspace.owner_id is not None:
            items = list(
                session.scalars(
                    select(WatchlistItem)
                    .where(
                        WatchlistItem.user_id == workspace.owner_id,
                        WatchlistItem.item_type == "event",
                    )
                    .order_by(WatchlistItem.created_at, WatchlistItem.id)
                )
            )
            for row in items:
                try:
                    event_id = uuid.UUID(row.item_id.strip("{}"))
                except (ValueError, AttributeError):
                    saved.append(
                        {
                            "saved_id": str(row.id),
                            "event_id": None,
                            "saved_at": _iso(row.created_at),
                            "available": False,
                            "finding": "invalid_saved_event_identity",
                        }
                    )
                    continue
                saved.append(
                    {
                        "saved_id": str(row.id),
                        "event_id": str(event_id),
                        "saved_at": _iso(row.created_at),
                        "available": session.get(Event, event_id) is not None,
                        "in_captured_group_population": str(event_id)
                        in {g["event_id"] for g in groups},
                    }
                )
        stages = {}
        for key, raw in _mapping(run.stage_results).items():
            if key not in {
                "capture",
                "admission",
                "scope",
                "embedding",
                "grouping",
                "claim_preparation",
                "observations",
                "snapshot",
                "workflow",
                "report",
            }:
                continue
            row = _mapping(raw)
            stages[key] = {
                field: _code(row[field]) for field in ("status", "reason", "code") if field in row
            }
            stages[key].update(
                {
                    field: row[field]
                    for field in (
                        *_COUNTS,
                        "count",
                        "article_count",
                        "event_count",
                        "run_limit",
                        "local_day_limit",
                        "articles_selected_for_enrichment",
                        "articles_embedded",
                        "articles_clustered",
                        "events_created",
                        "events_observed",
                        "supported",
                        "abstained",
                    )
                    if type(row.get(field)) is int and row[field] >= 0
                }
            )
        costs = _costs(session, run, settings, observed_at)
        result = {
            "schema": CAPTURE_SCHEMA,
            "observed_at": observed_at,
            "processing_date": run.local_date,
            "run_id": run.id,
            "timezone": workspace.timezone,
            "terminal_outcome_observed": terminal,
            "privacy": "private_retained_source_review_material",
            "human_session_observed": False,
            "human_judgments_supplied": False,
            "run": {
                "run_id": run.id,
                "job_id": run.job_id,
                "workspace_id": run.workspace_id,
                "profile_revision_id": run.profile_revision_id,
                "local_date": run.local_date,
                "state": run.state,
                "attempt": run.attempt,
                "max_attempts": run.max_attempts,
                "execution_mode": run.execution_mode,
                "delivery_transport": run.delivery_transport,
                "created_at": run.created_at,
                "updated_at": run.updated_at,
                "started_at": run.started_at,
                "capture_started_at": run.capture_started_at,
                "capture_ended_at": run.capture_ended_at,
                "scopes_frozen_at": run.scopes_frozen_at,
                "admitted_article_ids": run.admitted_article_ids,
                "enrichment_article_ids": run.enrichment_article_ids,
                "snapshot_id": run.snapshot_id,
                "report_id": run.report_id,
                "error_code": _code(_mapping(run.error).get("code")),
                "stages": stages,
                "brief_published": _mapping(run.result).get("published") is True,
                "no_brief_reason": _code(
                    _mapping(_mapping(run.stage_results).get("report")).get("reason")
                ),
            },
            "profile": {
                "profile_revision_id": profile.id,
                "revision": profile.revision,
                "schema_revision": profile.schema_revision,
                "created_at": profile.created_at,
                "selected_source_ids": profile.selected_source_ids,
                "include_phrases": profile.include_phrases,
                "exclude_phrases": profile.exclude_phrases,
                "execution_profile": profile.execution_profile,
                "ai_enabled": settings.get("ai_enabled")
                if type(settings.get("ai_enabled")) is bool
                else None,
                "settings": settings,
            },
            "calendar": {
                "persisted_timezone_at_capture": workspace.timezone,
                "processing_date": run.local_date,
                "date_basis": "original_logical_run_not_retry_wall_clock",
            },
            "coverage": _coverage(run.coverage),
            "feed_receipts": [
                {
                    "receipt_id": row.id,
                    "source_id": row.source_id,
                    "attempt": row.attempt,
                    "started_at": row.started_at,
                    "ended_at": row.ended_at,
                    "status": row.status,
                    "counts": {
                        key: row.details[key]
                        for key in (
                            "items_fetched",
                            "articles_captured",
                            "accepted_items",
                            "rejected_items",
                        )
                        if type(_mapping(row.details).get(key)) is int and row.details[key] >= 0
                    },
                    "error_code": _code(_mapping(row.details).get("code")),
                }
                for row in receipts
            ],
            "collection": collection,
            "counts": {
                "observed_captures_created_by_run": sum(row.run_id == run.id for row in captures),
                "admitted_articles": len(admitted),
                "enrichment_articles": len(run.enrichment_article_ids),
                "deferred_admitted_articles": len(
                    admitted - {str(item) for item in run.enrichment_article_ids}
                ),
                "qualifying_groups": len(groups),
                "qualifying_grouped_articles": len(grouped_article_ids),
                "raw_admitted_articles_outside_qualifying_groups": len(
                    admitted - grouped_article_ids
                ),
                "published_summary_units": len(summaries),
            },
            "groups": groups,
            "group_population_basis": basis,
            "summaries": summaries,
            "summary_population_complete": not any(
                row.get("reason") == "published_immutable_material_unverifiable"
                for row in exclusions
            ),
            "summary_chronology_verifiable": not summaries,
            "reports": report_records,
            "snapshot": {
                "snapshot_id": snapshot.id,
                "input_hash": snapshot.input_hash,
                "input_contract": snapshot.input_contract,
                "prepared_at": snapshot.prepared_at,
                "candidate_event_ids": snapshot.candidate_event_ids,
                "selected_event_ids": snapshot.selected_event_ids,
            }
            if snapshot
            else None,
            "costs": costs,
            "saved": saved,
            "exclusions": exclusions,
            "attempts": [
                {
                    "run_id": run.id,
                    "attempt": number,
                    "observed_state": run.state if number == run.attempt else None,
                    "raw_terminal_outcome": run.state
                    if number == run.attempt and terminal
                    else None,
                    "feed_receipt_ids": [row.id for row in receipts if row.attempt == number],
                    "paid_request_ids": [
                        row["request_id"]
                        for row in costs["run_requests"]
                        if row["attempt"] == number
                    ],
                    "identity_basis": "persisted_run_id_and_attempt_number_no_attempt_table",
                    "finding": "prior_raw_attempt_outcome_unavailable"
                    if number < run.attempt
                    else None,
                }
                for number in range(1, run.attempt + 1)
            ],
            "findings": ([] if terminal else ["nonterminal_outcome_incomplete_at_capture"])
            + (
                []
                if type(settings.get("ai_enabled")) is bool
                else ["frozen_ai_enablement_unverifiable"]
            )
            + (
                ["published_summary_population_incomplete"]
                if any(
                    row.get("reason") == "published_immutable_material_unverifiable"
                    for row in exclusions
                )
                else []
            )
            + [
                "database_capture_does_not_prove_actual_reading_session",
                "prior_attempt_raw_outcomes_not_retained_by_run_schema",
                "saved_snapshot_does_not_prove_saving_reopening_or_no_prior_loss",
                "source_inspection_and_export_exercise_require_session_log",
                "timezone_history_not_frozen_by_run_schema",
            ],
        }
        return _json(result)


__all__ = ["CAPTURE_SCHEMA", "TrialCaptureError", "capture_session_evidence"]
