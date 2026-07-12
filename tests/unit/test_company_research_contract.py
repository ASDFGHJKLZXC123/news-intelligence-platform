"""Company research profile contract tests."""

from __future__ import annotations

import datetime
import decimal
import uuid
from types import SimpleNamespace

from services.company_research import (
    COMPANY_RESEARCH_FIELD_TOTAL,
    INFORMATION_NOT_AVAILABLE,
    build_company_research_profile,
    validate_company_research_profile,
)
from services.company_research.contract import (
    available_field,
    coverage_for_checklist,
    fields_for_category,
    unavailable_field,
)

AS_OF = datetime.date(2026, 6, 20)


def _company() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        cik="0000320193",
        name="Apple Inc.",
        ticker="AAPL",
        exchange="NASDAQ",
        sic="3571",
        sic_description="Electronic Computers",
        fiscal_year_end="0928",
        company_metadata={
            "business_description": "Designs and sells consumer technology products and services.",
            "products": ["iPhone", "Mac", "Services"],
            "revenue_segments": ["Products", "Services"],
            "country": "US",
            "market_data": {
                "share_price": "195.00",
                "market_cap": "3000000000000",
                "pe_ratio": "30.5",
                "as_of": "2026-06-20",
            },
        },
    )


def _fact(concept: str, value: str, unit: str = "USD", period_end: datetime.date = AS_OF) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        taxonomy="us-gaap",
        concept=concept,
        unit=unit,
        period_start=datetime.date(period_end.year - 1, 1, 1),
        period_end=period_end,
        filed_at=period_end,
        accession_number="0000320193-26-000001",
        form_type="10-K",
        fiscal_year=period_end.year,
        fiscal_period="FY",
        frame=None,
        value=decimal.Decimal(value),
        raw_value=value,
        fact_metadata={"source": "test"},
    )


def _financial_fact_set(period_end: datetime.date, *, revenue: str, net_income: str, operating_cash: str) -> list[SimpleNamespace]:
    return [
        _fact("Revenues", revenue, period_end=period_end),
        _fact("GrossProfit", str(decimal.Decimal(revenue) * decimal.Decimal("0.45")), period_end=period_end),
        _fact("OperatingIncomeLoss", str(decimal.Decimal(revenue) * decimal.Decimal("0.30")), period_end=period_end),
        _fact("NetIncomeLoss", net_income, period_end=period_end),
        _fact("NetCashProvidedByUsedInOperatingActivities", operating_cash, period_end=period_end),
    ]


def _filing() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        accession_number="0000320193-26-000001",
        form_type="10-K",
        filing_date=AS_OF,
        filing_metadata={"source": "test"},
    )


def _field_lookup(profile: dict, category: str) -> dict[str, dict]:
    return {field["key"]: field for field in profile["researchChecklist"][category]}


def test_missing_company_profile_keeps_every_field_unavailable() -> None:
    profile = build_company_research_profile(None, requested_ticker="MISSING", as_of=AS_OF)

    assert validate_company_research_profile(profile) == []
    assert profile["coverage"] == {
        "available": 0,
        "total": COMPANY_RESEARCH_FIELD_TOTAL,
        "missing": COMPANY_RESEARCH_FIELD_TOTAL,
        "coverage_pct": 0.0,
        "by_category": {
            "financial": {"available": 0, "total": 16, "missing": 16},
            "business": {"available": 0, "total": 16, "missing": 16},
            "industry": {"available": 0, "total": 16, "missing": 16},
            "valuation": {"available": 0, "total": 18, "missing": 18},
        },
    }
    assert len(profile["missingFields"]) == COMPANY_RESEARCH_FIELD_TOTAL
    assert profile["staleFields"] == []
    assert profile["evidenceRefs"] == []

    revenue = _field_lookup(profile, "financial")["revenue"]
    assert revenue["category"] == "financial"
    assert revenue["value"] == INFORMATION_NOT_AVAILABLE
    assert revenue["available"] is False
    assert revenue["confidence"] == 0.0
    assert revenue["evidence_refs"] == []


def test_partial_profile_has_full_contract_and_available_supported_facts() -> None:
    profile = build_company_research_profile(
        _company(),
        facts=[
            _fact("Revenues", "383300000000"),
            _fact("NetIncomeLoss", "96995000000"),
            _fact("Assets", "352583000000"),
            _fact("Liabilities", "290437000000"),
        ],
        filings=[_filing()],
        as_of=AS_OF,
    )

    assert validate_company_research_profile(profile) == []
    assert profile["coverage"]["total"] == COMPANY_RESEARCH_FIELD_TOTAL
    assert profile["coverage"]["available"] > 0

    financial = _field_lookup(profile, "financial")
    assert financial["revenue"]["available"] is True
    assert financial["revenue"]["category"] == "financial"
    assert financial["revenue"]["source"].startswith("SEC company facts")
    assert financial["revenue"]["evidence_refs"]
    assert financial["gross_profit"]["value"] == INFORMATION_NOT_AVAILABLE
    assert financial["gross_profit"]["available"] is False

    valuation = _field_lookup(profile, "valuation")
    assert valuation["share_price"]["available"] is True
    assert valuation["enterprise_value"]["available"] is True
    assert valuation["enterprise_value"]["source"] == "Derived from market data and SEC company facts"
    assert len(profile["missingFields"]) == COMPANY_RESEARCH_FIELD_TOTAL - profile["coverage"]["available"]


def test_capital_expenditure_fact_maps_to_canonical_field() -> None:
    profile = build_company_research_profile(
        _company(),
        facts=[_fact("PaymentsToAcquirePropertyPlantAndEquipment", "10959000000")],
        as_of=AS_OF,
    )

    assert validate_company_research_profile(profile) == []
    capex = _field_lookup(profile, "financial")["capital_expenditure"]
    assert capex["available"] is True
    assert capex["value"] == "$11.0B"


def test_sec_financial_profile_derives_cash_flow_ratios_and_trends() -> None:
    facts = [
        *_financial_fact_set(datetime.date(2025, 12, 31), revenue="100000000000", net_income="20000000000", operating_cash="25000000000"),
        *_financial_fact_set(datetime.date(2024, 12, 31), revenue="90000000000", net_income="18000000000", operating_cash="21000000000"),
        *_financial_fact_set(datetime.date(2023, 12, 31), revenue="80000000000", net_income="15000000000", operating_cash="19000000000"),
        _fact("CashAndCashEquivalentsAtCarryingValue", "12000000000"),
        _fact("Assets", "150000000000"),
        _fact("Liabilities", "70000000000"),
        _fact("StockholdersEquity", "80000000000"),
        _fact("LongTermDebtNoncurrent", "22000000000"),
        _fact("ShortTermDebtCurrent", "3000000000"),
        _fact("PaymentsToAcquirePropertyPlantAndEquipment", "9000000000"),
        _fact("PaymentsOfDividendsCommonStock", "5000000000"),
        _fact("PaymentsForRepurchaseOfCommonStock", "7000000000"),
        _fact("EarningsPerShareDiluted", "4.25", unit="USD/shares"),
    ]

    profile = build_company_research_profile(_company(), facts=facts, as_of=AS_OF)

    assert validate_company_research_profile(profile) == []
    financial = _field_lookup(profile, "financial")
    assert financial["cash_flow"]["available"] is True
    assert financial["cash_flow"]["value"] == "operating $25.0B; free cash flow $16.0B"
    assert financial["profit_margins"]["value"] == "gross 45.0%; operating 30.0%; net 20.0%"
    assert financial["return_ratios"]["value"] == "ROE 25.0%; ROA 13.3%"
    assert financial["debt_level"]["value"] == "$25.0B total debt"
    assert financial["dividends_buybacks"]["value"] == "dividends $5.0B; buybacks $7.0B"
    assert "Revenue trend points:" in financial["financial_trends"]["value"]
    assert "Net profit trend points:" in financial["financial_trends"]["value"]
    assert "Operating cash flow trend points:" in financial["financial_trends"]["value"]


def test_market_data_valuation_fields_and_derived_metrics_are_populated_when_fresh() -> None:
    company = _company()
    company.company_metadata["market_data"] = {
        "price": "100.00",
        "market_cap": "100000000000",
        "pe": "24.0",
        "forward_pe": "21.0",
        "pb_ratio": "8.5",
        "ps_ratio": "6.2",
        "ev_ebitda": "18.0",
        "dividend_yield": "0.6%",
        "peg_ratio": "1.7",
        "valuation_trend": "Near five-year median.",
        "peer_comparison": "Premium to peer median.",
        "as_of": "2026-06-20",
    }
    facts = [
        _fact("NetCashProvidedByUsedInOperatingActivities", "12000000000"),
        _fact("PaymentsToAcquirePropertyPlantAndEquipment", "2000000000"),
        _fact("CashAndCashEquivalentsAtCarryingValue", "5000000000"),
        _fact("LongTermDebtNoncurrent", "7000000000"),
        _fact("ShortTermDebtCurrent", "1000000000"),
    ]

    profile = build_company_research_profile(company, facts=facts, as_of=AS_OF)

    assert validate_company_research_profile(profile) == []
    valuation = _field_lookup(profile, "valuation")
    assert valuation["share_price"]["value"] == "100.00"
    assert valuation["market_capitalization"]["value"] == "100000000000"
    assert valuation["enterprise_value"]["value"] == "$103.0B"
    assert valuation["free_cash_flow_yield"]["value"] == "10.0%"
    assert valuation["pe_ratio"]["value"] == "24.0"
    assert valuation["historical_valuation"]["available"] is True
    assert valuation["peer_comparison"]["available"] is True
    assert valuation["share_price"]["stale"] is False
    assert valuation["share_price"]["evidence_refs"][0]["source_type"] == "market_data"


def test_stale_market_data_is_marked_stale_not_current() -> None:
    company = _company()
    company.company_metadata["market_data"] = {
        "price": "100.00",
        "market_cap": "100000000000",
        "as_of": "2026-05-01",
    }

    profile = build_company_research_profile(company, as_of=AS_OF)

    assert validate_company_research_profile(profile) == []
    valuation = _field_lookup(profile, "valuation")
    assert valuation["share_price"]["available"] is True
    assert valuation["share_price"]["stale"] is True
    assert "maximum freshness window" in valuation["share_price"]["stale_reason"]
    assert profile["staleFields"][0]["key"] == "share_price"


def test_contract_helpers_represent_stale_and_conflicting_fields() -> None:
    checklist = {
        "financial": fields_for_category(
            "financial",
            {
                "revenue": available_field(
                    "financial",
                    "revenue",
                    "$10.0B",
                    source="SEC EDGAR company facts",
                    as_of="2024-12-31",
                    confidence=0.91,
                    stale=True,
                    stale_reason="Latest annual revenue is older than the freshness policy.",
                    provider_values=[
                        {"provider": "sec", "value": "$10.0B", "as_of": "2024-12-31"},
                    ],
                ),
                "net_profit": available_field(
                    "financial",
                    "net_profit",
                    "$1.0B",
                    source="Provider reconciliation",
                    as_of="2025-12-31",
                    conflict=True,
                    conflict_reason="SEC and vendor values differ materially.",
                    provider_values=[
                        {"provider": "sec", "value": "$1.0B"},
                        {"provider": "vendor", "value": "$1.2B"},
                    ],
                ),
            },
        ),
        "business": fields_for_category("business", {}),
        "industry": fields_for_category("industry", {}),
        "valuation": fields_for_category("valuation", {}),
    }

    coverage = coverage_for_checklist(checklist)
    assert coverage["available"] == 2
    revenue = {field["key"]: field for field in checklist["financial"]}["revenue"]
    net_profit = {field["key"]: field for field in checklist["financial"]}["net_profit"]
    assert revenue["stale"] is True
    assert revenue["stale_reason"]
    assert net_profit["conflict"] is True
    assert len(net_profit["provider_values"]) == 2


def test_contract_validator_catches_missing_and_malformed_fields() -> None:
    profile = build_company_research_profile(_company(), as_of=AS_OF)
    profile["researchChecklist"]["financial"] = profile["researchChecklist"]["financial"][1:]
    profile["researchChecklist"]["business"][0] = unavailable_field("business", "business_model")
    profile["researchChecklist"]["business"][0]["source"] = "bad"

    errors = validate_company_research_profile(profile)
    assert "missing field: financial.revenue" in errors
    assert any("business.business_model unavailable source must be null" in error for error in errors)
