"""Filing text extraction tests for company research."""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace

from services.company_research import (
    build_company_research_profile,
    extract_filing_sections,
    filing_business_metadata,
    normalize_filing_text,
    section_hash,
    validate_company_research_profile,
)


def test_extract_filing_sections_normalizes_html_and_hashes_sections() -> None:
    html = """
    <html><body>
      <h1>Item 1. Business</h1>
      <p>Example Corp sells cloud software and support services to enterprise customers.</p>
      <h1>Item 1A. Risk Factors</h1>
      <p>We depend on a limited number of cloud infrastructure suppliers.</p>
      <h1>Item 7. Management's Discussion and Analysis</h1>
      <p>Management is investing in sales capacity and product development.</p>
    </body></html>
    """

    extraction = extract_filing_sections(
        html,
        accession_number="0000000001-26-000001",
        source_url="https://www.sec.gov/example.htm",
    )

    sections = extraction["sections"]
    assert set(sections) == {"item_1_business", "item_1a_risk_factors", "item_7_mda"}
    assert "cloud software" in sections["item_1_business"]["text"]
    assert sections["item_1_business"]["hash"] == section_hash(
        "item_1_business",
        sections["item_1_business"]["text"],
    )
    assert extraction["section_hashes"]["item_1a_risk_factors"] == sections["item_1a_risk_factors"]["hash"]
    assert "<h1>" not in normalize_filing_text(html)


def test_filing_business_metadata_feeds_profile_with_section_sources() -> None:
    extraction = extract_filing_sections(
        """
        Item 1. Business Example Corp sells industrial automation products and services.
        Item 1A. Risk Factors Supply-chain disruption could delay customer deliveries.
        Item 7. Management's Discussion and Analysis Management is expanding recurring services.
        """,
        accession_number="0000000001-26-000001",
        source_url="https://www.sec.gov/example.htm",
    )
    metadata = filing_business_metadata(extraction)
    company = SimpleNamespace(
        id=uuid.uuid4(),
        cik="0000000001",
        name="Example Corp",
        ticker="EXM",
        exchange="NYSE",
        sic="3569",
        sic_description="General Industrial Machinery",
        fiscal_year_end="1231",
        company_metadata=metadata,
    )

    profile = build_company_research_profile(company, as_of=datetime.date(2026, 6, 20))

    assert validate_company_research_profile(profile) == []
    business = {field["key"]: field for field in profile["researchChecklist"]["business"]}
    industry = {field["key"]: field for field in profile["researchChecklist"]["industry"]}
    assert business["business_model"]["available"] is True
    assert business["business_model"]["source"] == "SEC filing section item_1_business (0000000001-26-000001)"
    assert business["management_strategy_team"]["source"] == "SEC filing section item_7_mda (0000000001-26-000001)"
    assert industry["geopolitical_risks"]["source"] == "SEC filing section item_1a_risk_factors (0000000001-26-000001)"
    assert "section_hash" in metadata["business_description_source"]
