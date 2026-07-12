"""Token strategy tests for company research extraction."""

from __future__ import annotations

from services.company_research import (
    LLMExtractionRequest,
    llm_cache_key,
    should_run_llm_extraction,
    validate_llm_output,
)


def _request(**overrides) -> LLMExtractionRequest:
    values = {
        "company_id": "entity:1",
        "accession_number": "0000000001-26-000001",
        "section_hash": "abc123",
        "prompt_version": "business-summary.v1",
        "model_version": "llm-model.v1",
        "task_type": "business_model_extraction",
        "evidence_refs": ({"source_type": "filing_section", "source_id": "abc123"},),
        "rerun_reasons": ("new_annual_filing",),
    }
    values.update(overrides)
    return LLMExtractionRequest(**values)


def test_llm_cache_key_is_stable_and_sensitive_to_versions() -> None:
    request = _request()
    same = _request()
    changed_prompt = _request(prompt_version="business-summary.v2")

    assert llm_cache_key(request) == llm_cache_key(same)
    assert llm_cache_key(request) != llm_cache_key(changed_prompt)


def test_zero_token_tasks_are_never_allowed_to_spend_tokens() -> None:
    allowed, reason = should_run_llm_extraction(_request(task_type="sec_xbrl_fact"))

    assert allowed is False
    assert reason == "zero-token deterministic task"


def test_llm_extraction_requires_evidence_rerun_reason_and_cache_miss() -> None:
    request = _request()
    allowed, reason = should_run_llm_extraction(request)
    cached_allowed, cached_reason = should_run_llm_extraction(request, cached_keys=[llm_cache_key(request)])
    no_evidence_allowed, no_evidence_reason = should_run_llm_extraction(_request(evidence_refs=()))
    no_reason_allowed, no_reason_reason = should_run_llm_extraction(_request(rerun_reasons=()))

    assert (allowed, reason) == (True, "LLM extraction allowed")
    assert (cached_allowed, cached_reason) == (False, "cached extraction exists")
    assert (no_evidence_allowed, no_evidence_reason) == (False, "LLM extraction requires evidence refs")
    assert (no_reason_allowed, no_reason_reason) == (False, "no rerun condition supplied")


def test_llm_output_validation_requires_audit_fields() -> None:
    assert validate_llm_output(
        {
            "text": "Business summary",
            "evidence_refs": [{"source_type": "filing_section", "source_id": "abc123"}],
            "prompt_version": "business-summary.v1",
            "model_version": "llm-model.v1",
            "section_hash": "abc123",
        }
    ) == []
    assert validate_llm_output({"text": "Business summary"}) == [
        "LLM output requires non-empty evidence_refs",
        "LLM output requires prompt_version",
        "LLM output requires model_version",
        "LLM output requires section_hash",
    ]
