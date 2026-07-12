"""Industry context tests for company research profiles."""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace

from services.company_research import (
    build_company_research_profile,
    industry_context_values,
    validate_company_research_profile,
)
from services.company_research.contract import INFORMATION_NOT_AVAILABLE


def _company(metadata: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        cik="0000000001",
        name="Example Energy Co.",
        ticker="EXE",
        exchange="NYSE",
        sic="1311",
        sic_description="Crude Petroleum and Natural Gas",
        fiscal_year_end="1231",
        company_metadata=metadata,
    )


def _field_lookup(profile: dict, category: str) -> dict[str, dict]:
    return {field["key"]: field for field in profile["researchChecklist"][category]}


def test_industry_context_values_only_reads_explicit_context() -> None:
    metadata = {
        "industry": "Energy",
        "sector": "Energy",
        "industry_context": {
            "macroeconomic_factors": "Oil demand and dollar strength are the active macro signals.",
            "geopolitical_risks": "Shipping-lane escalation is elevated.",
            "regulatory_pressure": "Methane and permitting rules are active.",
            "commodity_exposure": "Crude oil and natural gas.",
            "source": "GDELT/EIA/FRED context snapshot",
        },
    }

    values = industry_context_values(metadata)

    assert "industry_size" not in values
    assert values["macroeconomic_factors"]["source"] == "GDELT/EIA/FRED context snapshot"
    assert values["regulation"]["value"] == "Methane and permitting rules are active."
    assert values["raw_material_exposure"]["value"] == "Crude oil and natural gas."


def test_profile_populates_industry_context_without_overstating_unsupported_fields() -> None:
    profile = build_company_research_profile(
        _company(
            {
                "industry": "Energy",
                "sector": "Energy",
                "industry_context": {
                    "macroeconomic_factors": "Higher rates and crude demand are the active external signals.",
                    "geopolitical_risks": "Gulf shipping disruption risk is elevated.",
                    "regulation": "Emissions and permitting policy are active.",
                    "raw_material_exposure": "Crude oil, natural gas, and refining margins.",
                    "customer_demand": "External demand depends on industrial production and travel activity.",
                    "source": "Provider context snapshot",
                },
            }
        ),
        as_of=datetime.date(2026, 6, 20),
    )

    assert validate_company_research_profile(profile) == []
    industry = _field_lookup(profile, "industry")
    assert industry["macroeconomic_factors"]["available"] is True
    assert industry["macroeconomic_factors"]["source"] == "Provider context snapshot"
    assert industry["geopolitical_risks"]["available"] is True
    assert industry["regulation"]["available"] is True
    assert industry["raw_material_exposure"]["available"] is True
    assert industry["customer_demand"]["available"] is True
    assert industry["industry_size"]["available"] is False
    assert industry["industry_size"]["value"] == INFORMATION_NOT_AVAILABLE
    assert industry["market_share"]["available"] is False
