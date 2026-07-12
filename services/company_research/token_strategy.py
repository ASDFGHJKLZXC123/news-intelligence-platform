"""Token-use controls for company research extraction."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ZERO_TOKEN_TASKS = frozenset(
    {
        "sec_xbrl_fact",
        "financial_formula",
        "market_quote",
        "price_history",
        "fred_observation",
        "gdelt_metadata",
        "eia_observation",
        "world_bank_indicator",
        "ofac_match",
        "peer_metric",
    }
)

LLM_ALLOWED_TASKS = frozenset(
    {
        "business_model_extraction",
        "risk_factor_summary",
        "management_strategy_summary",
        "industry_narrative_synthesis",
        "explanation_generation",
    }
)


@dataclass(frozen=True)
class LLMExtractionRequest:
    company_id: str
    accession_number: str
    section_hash: str
    prompt_version: str
    model_version: str
    task_type: str
    evidence_refs: tuple[Mapping[str, Any], ...] = ()
    rerun_reasons: tuple[str, ...] = ()


def llm_cache_key(request: LLMExtractionRequest) -> str:
    """Return the canonical cache key for an LLM extraction request."""

    basis = "|".join(
        (
            request.company_id,
            request.accession_number,
            request.section_hash,
            request.prompt_version,
            request.model_version,
            request.task_type,
        )
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def should_run_llm_extraction(
    request: LLMExtractionRequest,
    *,
    cached_keys: Sequence[str] | None = None,
    allow_lazy: bool = True,
) -> tuple[bool, str]:
    """Decide whether a request is allowed to spend LLM tokens."""

    if request.task_type in ZERO_TOKEN_TASKS:
        return False, "zero-token deterministic task"
    if request.task_type not in LLM_ALLOWED_TASKS:
        return False, "task type is not approved for LLM extraction"
    if not allow_lazy and not request.rerun_reasons:
        return False, "lazy token use required; no rerun reason supplied"
    if not request.evidence_refs:
        return False, "LLM extraction requires evidence refs"
    key = llm_cache_key(request)
    if key in set(cached_keys or []):
        return False, "cached extraction exists"
    if not request.rerun_reasons:
        return False, "no rerun condition supplied"
    return True, "LLM extraction allowed"


def validate_llm_output(output: Mapping[str, Any]) -> list[str]:
    """Return validation errors for LLM-derived company research output."""

    errors: list[str] = []
    evidence_refs = output.get("evidence_refs")
    if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, str | bytes | bytearray) or not evidence_refs:
        errors.append("LLM output requires non-empty evidence_refs")
    if not output.get("prompt_version"):
        errors.append("LLM output requires prompt_version")
    if not output.get("model_version"):
        errors.append("LLM output requires model_version")
    if not output.get("section_hash"):
        errors.append("LLM output requires section_hash")
    return errors
