"""Company research profile assembly services."""

from services.company_research.contract import (
    COMPANY_RESEARCH_CHECKLIST,
    COMPANY_RESEARCH_CATEGORIES,
    COMPANY_RESEARCH_FIELD_TOTAL,
    INFORMATION_NOT_AVAILABLE,
    assert_valid_company_research_profile,
    validate_company_research_profile,
)
from services.company_research.identity import (
    build_company_universe,
    resolve_company_identity,
)
from services.company_research.industry_context import industry_context_values
from services.company_research.filing_text import (
    extract_filing_sections,
    filing_business_metadata,
    normalize_filing_text,
    section_hash,
)
from services.company_research.peers import (
    build_peer_group,
    peer_comparison_text,
)
from services.company_research.profile import (
    build_company_research_profile,
)
from services.company_research.refresh_policy import (
    due_refresh_jobs,
    refresh_reasons_for_profile,
    should_schedule_llm_refresh,
)
from services.company_research.token_strategy import (
    LLMExtractionRequest,
    llm_cache_key,
    should_run_llm_extraction,
    validate_llm_output,
)

__all__ = [
    "COMPANY_RESEARCH_CHECKLIST",
    "COMPANY_RESEARCH_CATEGORIES",
    "COMPANY_RESEARCH_FIELD_TOTAL",
    "INFORMATION_NOT_AVAILABLE",
    "LLMExtractionRequest",
    "assert_valid_company_research_profile",
    "build_company_universe",
    "build_company_research_profile",
    "build_peer_group",
    "due_refresh_jobs",
    "extract_filing_sections",
    "filing_business_metadata",
    "industry_context_values",
    "llm_cache_key",
    "normalize_filing_text",
    "peer_comparison_text",
    "resolve_company_identity",
    "section_hash",
    "should_run_llm_extraction",
    "refresh_reasons_for_profile",
    "should_schedule_llm_refresh",
    "validate_llm_output",
    "validate_company_research_profile",
]
