"""CORE-01: conservative claim/evidence preparation from frozen personal article revisions.

The producer emits source-reported assertions, not independently verified facts.  A claim is an
exact complete span; attribution, negation, modality, comparisons and temporal wording are never
rewritten.  Pattern checks only decide whether a span is safe enough to offer to the existing
composition/grounding/copyright pipeline.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db.models import (
    Claim,
    ClaimEvidence,
    EvidenceItem,
    PersonalArticleRevision,
    PersonalClaimPreparation,
    PersonalRun,
)
from services.personal.runs import lock_owned_run

MAX_SENTENCES_PER_ARTICLE = 3
MAX_CLAIM_SPAN_CHARS = 200
MAX_RETAINED_CONTEXT_CHARS = 2_512
MAX_CANDIDATES_PER_RUN = 100
CLAIM_SCHEMA = "source-reported-assertion.v1"
_CLAIM_NAMESPACE = uuid.UUID("ad1cb9a6-601c-4d38-9fe0-a40fe6b3134a")
_EVIDENCE_NAMESPACE = uuid.UUID("1dd48e9e-4f2c-4f39-b913-b11986ae552a")

_FACTUAL_PREDICATES = re.compile(
    r"\b(announc(?:ed|es)|report(?:ed|s)|said|confirm(?:ed|s)|approv(?:ed|es)|"
    r"rais(?:ed|es)|lower(?:ed|s)|increas(?:ed|es)|decreas(?:ed|es)|launch(?:ed|es)|"
    r"sign(?:ed|s)|fil(?:ed|es)|acquir(?:ed|es)|releas(?:ed|es)|record(?:ed|s)|"
    r"reach(?:ed|es)|fell|rose|closed|opened|vot(?:ed|es)|won|lost)\b",
    re.IGNORECASE,
)
_UNQUALIFIED_FORECAST_OR_OPINION = re.compile(
    r"\b(could|may|might|would|should|expect(?:ed|s)?|forecast(?:s|ed)?|predict(?:s|ed)?|"
    r"opinion|imagine|likely|possibly|potential(?:ly)?)\b",
    re.IGNORECASE,
)
_DEPENDENT_START = re.compile(
    r"^\s*(this|that|these|those|it|they|he|she|which|who|while|although|because)\b",
    re.IGNORECASE,
)
_LISTICLE_START = re.compile(
    r"^\s*(?:top\s+)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:reasons|ways|things)\b",
    re.IGNORECASE,
)
_ABBREVIATIONS = frozenset(
    {"dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e"}
)


@dataclass(frozen=True)
class ClaimCandidate:
    source_field: str
    start: int
    end: int
    text: str


def _key(text: str) -> str:
    # The exact source span is the identity. Case/whitespace variants remain separate claims;
    # their sidecars still retain normalized matching inputs for inspection.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def article_revision_hash(revision: PersonalArticleRevision) -> str:
    payload = {
        "article_id": str(revision.article_id),
        "source_id": str(revision.source_id),
        "title": revision.retained_title,
        "rss_summary": revision.retained_summary,
        "url": revision.retained_url,
        "publisher": revision.retained_publisher,
        "published_at": (
            revision.retained_published_at.isoformat()
            if revision.retained_published_at is not None
            else None
        ),
        "truncated": revision.truncated,
        "provenance": revision.provenance,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _eligible(text: str, *, complete: bool, truncated: bool) -> bool:
    stripped = text.strip()
    if truncated or not complete or not (20 <= len(stripped) <= MAX_CLAIM_SPAN_CHARS):
        return False
    terminal = stripped.rstrip("\"'”’")
    if "<" in stripped or ">" in stripped or terminal.endswith("?"):
        return False
    if (
        _DEPENDENT_START.search(stripped)
        or _LISTICLE_START.search(stripped)
        or _UNQUALIFIED_FORECAST_OR_OPINION.search(stripped)
    ):
        return False
    predicate = _FACTUAL_PREDICATES.search(stripped)
    if predicate is None:
        return False
    # Require a lexical subject before the predicate. A number alone is never a factual gate.
    subject_tokens = re.findall(r"[^\W_]+", stripped[: predicate.start()], re.UNICODE)
    return len(subject_tokens) >= 1


def _sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Split complete sentences without breaking decimal values or common abbreviations."""

    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char not in ".!?":
            index += 1
            continue
        if char == ".":
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if previous.isdigit() and following.isdigit():
                index += 1
                continue
            word_match = re.search(r"([A-Za-z](?:[A-Za-z.]*)?)$", text[start:index])
            word = word_match.group(1) if word_match else ""
            if (
                word.casefold() in _ABBREVIATIONS
                or (len(word) == 1 and word.isupper())
                or re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]", word)
            ):
                index += 1
                continue
        end = index + 1
        if end < len(text) and text[end] in "\"'”’":
            end += 1
        if end == len(text) or text[end].isspace():
            spans.append((start, end))
            start = end
        index = end
    return tuple(spans)


def extract_claim_candidates(revision: PersonalArticleRevision) -> tuple[ClaimCandidate, ...]:
    """Return at most three exact, complete assertion spans from one pinned revision."""

    candidates: list[ClaimCandidate] = []
    title = revision.retained_title
    title_complete = bool(title.strip()) and not title.rstrip().endswith((":", ";", ",", "…"))
    if _eligible(title, complete=title_complete, truncated=revision.truncated):
        start = len(title) - len(title.lstrip())
        end = len(title.rstrip())
        candidates.append(ClaimCandidate("title", start, end, title[start:end]))

    summary = revision.retained_summary or ""
    for raw_start, raw_end in _sentence_spans(summary):
        raw = summary[raw_start:raw_end]
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        start = raw_start + leading
        end = raw_start + trailing
        text = summary[start:end]
        if _eligible(text, complete=True, truncated=revision.truncated):
            candidates.append(ClaimCandidate("summary", start, end, text))
        if len(candidates) >= MAX_SENTENCES_PER_ARTICLE:
            break
    return tuple(candidates[:MAX_SENTENCES_PER_ARTICLE])


def _abstention_key(reason: str) -> str:
    return hashlib.sha256(f"abstained:{reason}".encode()).hexdigest()


def _record_abstention(
    session: Session,
    *,
    run: PersonalRun,
    revision: PersonalArticleRevision,
    reason: str,
) -> None:
    session.execute(
        insert(PersonalClaimPreparation)
        .values(
            id=uuid.uuid4(),
            run_id=run.id,
            article_revision_id=revision.id,
            article_id=revision.article_id,
            normalized_claim_key=_abstention_key(reason),
            source_field="summary",
            span_start=0,
            span_end=0,
            exact_excerpt="",
            status="abstained",
            validation={"schema": CLAIM_SCHEMA, "reason": reason},
        )
        .on_conflict_do_nothing(constraint="uq_personal_claim_preparation_identity")
    )


def prepare_article_claims(
    session: Session,
    run_id: uuid.UUID,
    ownership_token: uuid.UUID,
) -> tuple[PersonalClaimPreparation, ...]:
    """Persist the canonical Claim -> supportive EvidenceItem -> Article path idempotently."""

    run: PersonalRun = lock_owned_run(session, run_id, ownership_token)
    revisions = list(
        session.execute(
            select(PersonalArticleRevision)
            .where(PersonalArticleRevision.run_id == run.id)
            .order_by(PersonalArticleRevision.article_id)
            .limit(len(run.enrichment_article_ids) + 1)
        ).scalars()
    )
    if len(revisions) > len(run.enrichment_article_ids):
        raise RuntimeError("article revision scope exceeds the frozen enrichment scope")
    if {item.article_id for item in revisions} != set(run.enrichment_article_ids):
        raise RuntimeError("every frozen enrichment article must have one retained revision")

    prepared: list[PersonalClaimPreparation] = []
    candidate_count = 0
    for revision in revisions:
        if (
            len(revision.retained_title) + len(revision.retained_summary or "")
            > MAX_RETAINED_CONTEXT_CHARS
        ):
            raise RuntimeError("retained claim context exceeds its 2512-character bound")
        if article_revision_hash(revision) != revision.content_hash:
            raise RuntimeError("retained article revision hash does not match its pinned fields")
        candidates = extract_claim_candidates(revision)
        if not candidates:
            _record_abstention(
                session,
                run=run,
                revision=revision,
                reason="no_complete_supported_assertion",
            )
            continue
        available = max(0, MAX_CANDIDATES_PER_RUN - candidate_count)
        selected_candidates = candidates[:available]
        if len(selected_candidates) < len(candidates):
            _record_abstention(
                session,
                run=run,
                revision=revision,
                reason="candidate_capacity_deferred",
            )
        for candidate in selected_candidates:
            candidate_count += 1
            claim_key = _key(candidate.text)
            source_text = (
                revision.retained_title
                if candidate.source_field == "title"
                else revision.retained_summary or ""
            )
            if source_text[candidate.start : candidate.end] != candidate.text:
                raise RuntimeError("claim source span no longer matches the pinned revision")
            claim_id = uuid.uuid5(_CLAIM_NAMESPACE, claim_key)
            evidence_id = uuid.uuid5(_EVIDENCE_NAMESPACE, str(revision.article_id))
            inserted_evidence_id = session.execute(
                insert(EvidenceItem)
                .values(
                    id=evidence_id,
                    source_type="article",
                    source_id=str(revision.article_id),
                    title=revision.retained_title,
                    publisher=revision.retained_publisher,
                    url=revision.retained_url,
                    published_at=revision.retained_published_at,
                    raw_ref=None,
                    evidence_metadata={"canonical_article_id": str(revision.article_id)},
                )
                .on_conflict_do_nothing(constraint="uq_evidence_items_source")
                .returning(EvidenceItem.id)
            ).scalar_one_or_none()
            evidence_id = session.execute(
                select(EvidenceItem.id).where(
                    EvidenceItem.source_type == "article",
                    EvidenceItem.source_id == str(revision.article_id),
                )
            ).scalar_one()
            session.execute(
                insert(Claim)
                .values(
                    id=claim_id,
                    claim_text=candidate.text,
                    claim_type="source_reported_assertion",
                    confidence_score=None,
                    created_by_run_id=None,
                )
                .on_conflict_do_nothing(index_elements=[Claim.id])
            )
            existing_text = session.execute(
                select(Claim.claim_text).where(Claim.id == claim_id)
            ).scalar_one()
            if existing_text != candidate.text:
                raise RuntimeError("deterministic claim identity collision")
            inserted_support = session.execute(
                insert(ClaimEvidence)
                .values(
                    claim_id=claim_id,
                    evidence_item_id=evidence_id,
                    support_type="supports",
                    confidence_score=None,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ClaimEvidence.claim_id,
                        ClaimEvidence.evidence_item_id,
                        ClaimEvidence.support_type,
                    ]
                )
                .returning(ClaimEvidence.claim_id)
            ).scalar_one_or_none()
            session.execute(
                insert(PersonalClaimPreparation)
                .values(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    article_revision_id=revision.id,
                    article_id=revision.article_id,
                    claim_id=claim_id,
                    evidence_item_id=evidence_id,
                    normalized_claim_key=claim_key,
                    source_field=candidate.source_field,
                    span_start=candidate.start,
                    span_end=candidate.end,
                    exact_excerpt=candidate.text,
                    status="supported",
                    validation={
                        "schema": CLAIM_SCHEMA,
                        "assertion_kind": "source_reported",
                        "truth_verified": False,
                        "qualifiers_preserved": True,
                        "evidence_created_by_personal": inserted_evidence_id is not None,
                        "support_created_by_personal": inserted_support is not None,
                    },
                )
                .on_conflict_do_nothing(constraint="uq_personal_claim_preparation_identity")
            )
    session.flush()
    prepared = list(
        session.execute(
            select(PersonalClaimPreparation)
            .where(PersonalClaimPreparation.run_id == run.id)
            .order_by(PersonalClaimPreparation.article_id, PersonalClaimPreparation.id)
        ).scalars()
    )
    return tuple(prepared)


__all__ = [
    "CLAIM_SCHEMA",
    "MAX_CANDIDATES_PER_RUN",
    "MAX_CLAIM_SPAN_CHARS",
    "MAX_RETAINED_CONTEXT_CHARS",
    "MAX_SENTENCES_PER_ARTICLE",
    "ClaimCandidate",
    "extract_claim_candidates",
    "prepare_article_claims",
]
