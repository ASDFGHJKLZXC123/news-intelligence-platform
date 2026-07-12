"""Build listed-company research profiles from persisted provider data.

The profile shape mirrors the frontend company fundamentals tab and the
``listed_company_research_checklist.md`` categories. The builder is deliberately
conservative: when a field cannot be supported by provider data already present in
the system, it returns ``Information not available`` instead of inventing a value.
"""

from __future__ import annotations

import datetime
import decimal
from collections.abc import Mapping, Sequence
from typing import Any

from services.company_research.contract import (
    COMPANY_RESEARCH_CHECKLIST,
    INFORMATION_NOT_AVAILABLE,
    available_field,
    coverage_for_checklist,
    evidence_refs_for_checklist,
    field_label,
    fields_for_category,
    missing_fields_for_checklist,
    stale_fields_for_checklist,
    unavailable_field,
)
from services.company_research.industry_context import industry_context_values
from services.company_research.peers import build_peer_group, peer_comparison_text

FINANCIAL_FACT_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_profit": ("OperatingIncomeLoss",),
    "net_profit": ("NetIncomeLoss", "ProfitLoss"),
    "eps": ("EarningsPerShareDiluted", "EarningsPerShareBasic"),
    "cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "cash_position": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "capital_expenditure": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "CapitalExpenditures",
    ),
    "dividends": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
    "buybacks": ("PaymentsForRepurchaseOfCommonStock",),
    "debt_current": ("ShortTermBorrowings", "ShortTermDebtCurrent", "LongTermDebtCurrent"),
    "debt_noncurrent": (
        "LongTermDebtNoncurrent",
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "LongTermDebt",
    ),
}

MARKET_DATA_MAX_AGE_DAYS = 7


def build_company_research_profile(
    company: Any | None,
    *,
    facts: Sequence[Any] | None = None,
    filings: Sequence[Any] | None = None,
    requested_ticker: str | None = None,
    requested_cik: str | None = None,
    requested_name: str | None = None,
    as_of: datetime.date | None = None,
) -> dict[str, Any]:
    """Return a frontend/API company research profile.

    ``company`` may be a SQLAlchemy model, a SimpleNamespace from tests, an entity
    profile, or ``None``. Missing identity/provider coverage is represented in the
    returned fields rather than as an exception.
    """

    fact_list = list(facts or [])
    filing_list = list(filings or [])
    identity = _company_identity(
        company,
        requested_ticker=requested_ticker,
        requested_cik=requested_cik,
        requested_name=requested_name,
    )
    company_name = identity["name"]
    ticker = identity["ticker"]
    cik = identity["cik"]
    metadata = _metadata(company)
    sic = _first_text(_getattr(company, "sic"), _metadata_lookup(metadata, "sic"))
    sic_description = _first_text(
        _getattr(company, "sic_description"),
        _metadata_lookup(metadata, "sic_description", "sicDescription"),
    )
    fiscal_year_end = _first_text(
        _getattr(company, "fiscal_year_end"),
        _metadata_lookup(metadata, "fiscal_year_end", "fiscalYearEnd"),
    )

    financial_fields = _financial_fields(fact_list, filing_list)
    business_fields = _business_fields(company_name, metadata, sic, sic_description, identity)
    industry_fields = _industry_fields(company_name, metadata, sic, sic_description)
    valuation_fields = _valuation_fields(metadata, fact_list, as_of=as_of)
    checklist = {
        "financial": financial_fields,
        "business": business_fields,
        "industry": industry_fields,
        "valuation": valuation_fields,
    }
    coverage = coverage_for_checklist(checklist)

    latest_period = _latest_period_end(fact_list)
    as_of_value = as_of or latest_period or _latest_filing_date(filing_list) or datetime.date.today()
    source_summary = _source_summary(company, fact_list, filing_list)

    return {
        "asOf": as_of_value.isoformat(),
        "lastUpdatedAt": as_of_value.isoformat(),
        "identity": {
            "name": company_name,
            "ticker": ticker,
            "cik": cik,
            "exchange": _first_text(_getattr(company, "exchange"), _metadata_lookup(metadata, "exchange")),
            "fiscal_year_end": fiscal_year_end,
            "sic": sic,
            "sic_description": sic_description,
        },
        "coverage": coverage,
        "sources": source_summary,
        "financial": _financial_card(company_name, financial_fields),
        "business": _business_card(company_name, business_fields, sic, sic_description),
        "industry": _industry_card(company_name, industry_fields, sic_description),
        "valuation": _valuation_card(ticker, valuation_fields),
        "researchChecklist": checklist,
        "missingFields": missing_fields_for_checklist(checklist),
        "staleFields": stale_fields_for_checklist(checklist),
        "evidenceRefs": evidence_refs_for_checklist(checklist),
    }


def _company_identity(
    company: Any | None,
    *,
    requested_ticker: str | None,
    requested_cik: str | None,
    requested_name: str | None,
) -> dict[str, str | None]:
    metadata = _metadata(company)
    ticker = _first_text(
        requested_ticker,
        _getattr(company, "ticker"),
        _getattr(company, "primary_ticker"),
        _metadata_lookup(metadata, "ticker", "primary_ticker"),
        _first_sequence_value(_metadata_lookup(metadata, "tickers")),
    )
    cik = _first_text(
        requested_cik,
        _getattr(company, "cik"),
        _getattr(company, "primary_cik"),
        _metadata_lookup(metadata, "cik", "primary_cik"),
    )
    name = _first_text(
        requested_name,
        _getattr(company, "name"),
        _getattr(company, "canonical_name"),
        _metadata_lookup(metadata, "name", "entityName", "company_name"),
        ticker,
        cik,
        "Unknown company",
    )
    return {"name": name, "ticker": ticker, "cik": cik}


def _financial_fields(facts: Sequence[Any], filings: Sequence[Any]) -> list[dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for key in (
        "revenue",
        "gross_profit",
        "operating_profit",
        "net_profit",
        "eps",
        "cash_flow",
        "cash_position",
        "assets",
        "liabilities",
        "capital_expenditure",
    ):
        fact = _latest_fact(facts, FINANCIAL_FACT_CONCEPTS[key])
        if fact is not None:
            value = _format_fact_value(fact)
            if key == "cash_flow":
                cash_flow_text = _cash_flow_text(facts)
                value = cash_flow_text or value
            values[key] = _available_field(
                key,
                _label("financial", key),
                value,
                source=_fact_source(fact),
                as_of=_date_text(_fact_period_end(fact)),
            )

    debt_text = _debt_text(facts)
    if debt_text is not None:
        values["debt_level"] = _available_field(
            "debt_level",
            _label("financial", "debt_level"),
            debt_text,
            source="SEC company facts",
            as_of=_date_text(_latest_period_end(facts)),
        )

    margin_text = _margin_text(facts)
    if margin_text is not None:
        values["profit_margins"] = _available_field(
            "profit_margins",
            _label("financial", "profit_margins"),
            margin_text,
            source="Derived from SEC company facts",
            as_of=_date_text(_latest_period_end(facts)),
        )

    return_ratio_text = _return_ratio_text(facts)
    if return_ratio_text is not None:
        values["return_ratios"] = _available_field(
            "return_ratios",
            _label("financial", "return_ratios"),
            return_ratio_text,
            source="Derived from SEC company facts",
            as_of=_date_text(_latest_period_end(facts)),
        )

    dividends_buybacks = _dividends_buybacks_text(facts)
    if dividends_buybacks is not None:
        values["dividends_buybacks"] = _available_field(
            "dividends_buybacks",
            _label("financial", "dividends_buybacks"),
            dividends_buybacks,
            source="SEC company facts",
            as_of=_date_text(_latest_period_end(facts)),
        )

    trend_text = _financial_trend_text(facts)
    if trend_text is not None:
        values["financial_trends"] = _available_field(
            "financial_trends",
            _label("financial", "financial_trends"),
            trend_text,
            source="SEC company facts",
            as_of=_date_text(_latest_period_end(facts)),
        )

    latest_annual = _latest_filing(filings, {"10-K", "20-F", "40-F"})
    if latest_annual is not None:
        values["accounting_notes"] = _available_field(
            "accounting_notes",
            _label("financial", "accounting_notes"),
            f"Latest annual filing available: {_getattr(latest_annual, 'form_type', 'filing')}",
            source=_filing_source(latest_annual),
            as_of=_date_text(_filing_date(latest_annual)),
        )

    return _fields_for_category("financial", values)


def _business_fields(
    company_name: str,
    metadata: Mapping[str, Any],
    sic: str | None,
    sic_description: str | None,
    identity: Mapping[str, str | None],
) -> list[dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    description = _first_text(
        _metadata_lookup(metadata, "business_description", "businessDescription", "description"),
        _metadata_lookup(metadata, "summary"),
    )
    if description:
        values["business_model"] = _available_field(
            "business_model",
            _label("business", "business_model"),
            description,
            source=_metadata_field_source(metadata, "business_description_source", default="Company metadata"),
        )
    elif sic_description:
        values["business_model"] = _available_field(
            "business_model",
            _label("business", "business_model"),
            f"SEC SIC classification: {sic_description}",
            source=f"SEC SIC {sic}" if sic else "SEC SIC",
        )

    products = _metadata_lookup(metadata, "products", "main_products_services", "mainProducts")
    if products:
        values["main_products_services"] = _available_field(
            "main_products_services",
            _label("business", "main_products_services"),
            _join_value(products),
            source="Company metadata",
        )

    revenue_segments = _metadata_lookup(metadata, "revenue_segments", "segments", "revenueSegments")
    if revenue_segments:
        values["revenue_segments"] = _available_field(
            "revenue_segments",
            _label("business", "revenue_segments"),
            _join_value(revenue_segments),
            source="Company metadata",
        )

    country = _first_text(_metadata_lookup(metadata, "country"), identity.get("country"))
    if country:
        values["geographic_exposure"] = _available_field(
            "geographic_exposure",
            _label("business", "geographic_exposure"),
            country,
            source="Company metadata",
        )

    advantage = _metadata_lookup(metadata, "competitive_advantage", "advantages", "moat")
    if advantage:
        values["competitive_advantage"] = _available_field(
            "competitive_advantage",
            _label("business", "competitive_advantage"),
            _join_value(advantage),
            source="Company metadata",
        )

    strategy = _metadata_lookup(metadata, "strategy", "management_strategy", "management")
    if strategy:
        values["management_strategy_team"] = _available_field(
            "management_strategy_team",
            _label("business", "management_strategy_team"),
            _join_value(strategy),
            source=_metadata_field_source(metadata, "management_strategy_source", default="Company metadata"),
        )

    if not values and company_name:
        values["business_model"] = _unavailable_field(
            "business_model",
            _label("business", "business_model"),
            reason=f"No parsed business-model data for {company_name}.",
        )
    return _fields_for_category("business", values)


def _industry_fields(
    company_name: str,
    metadata: Mapping[str, Any],
    sic: str | None,
    sic_description: str | None,
) -> list[dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    if sic_description:
        values["industry_trends"] = _available_field(
            "industry_trends",
            _label("industry", "industry_trends"),
            f"Company mapped to SEC SIC industry: {sic_description}",
            source=f"SEC SIC {sic}" if sic else "SEC SIC",
        )

    for key, item in industry_context_values(metadata).items():
        values[key] = _available_field(
            key,
            _label("industry", key),
            _join_value(item["value"]),
            source=item["source"],
        )

    filing_risks = _metadata_lookup(metadata, "geopolitical_risks", "risk_factors", "risks")
    if filing_risks and "geopolitical_risks" not in values:
        values["geopolitical_risks"] = _available_field(
            "geopolitical_risks",
            _label("industry", "geopolitical_risks"),
            _join_value(filing_risks),
            source=_metadata_field_source(metadata, "risk_factors_source", default="Company metadata"),
        )

    if not values and company_name:
        values["industry_trends"] = _unavailable_field(
            "industry_trends",
            _label("industry", "industry_trends"),
            reason=f"No industry dataset is linked to {company_name}.",
        )
    return _fields_for_category("industry", values)


def _valuation_fields(
    metadata: Mapping[str, Any],
    facts: Sequence[Any],
    *,
    as_of: datetime.date | None,
) -> list[dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    market_data = _metadata_lookup(metadata, "market_data", "valuation")
    if isinstance(market_data, Mapping):
        field_sources = {
            "share_price": ("share_price", "price"),
            "market_capitalization": ("market_cap", "market_capitalization"),
            "enterprise_value": ("enterprise_value",),
            "pe_ratio": ("pe_ratio", "pe"),
            "forward_pe": ("forward_pe",),
            "pb_ratio": ("pb_ratio", "pb"),
            "ps_ratio": ("ps_ratio", "ps"),
            "ev_ebitda": ("ev_ebitda",),
            "free_cash_flow_yield": ("free_cash_flow_yield", "fcf_yield"),
            "dividend_yield": ("dividend_yield",),
            "peg_ratio": ("peg_ratio", "peg"),
            "historical_valuation": ("historical_valuation", "valuation_trend"),
            "peer_comparison": ("peer_comparison", "peer_valuation"),
        }
        market_as_of = _market_data_as_of(market_data)
        stale, stale_reason = _market_data_staleness(market_as_of, as_of=as_of)
        for key, aliases in field_sources.items():
            value = _metadata_lookup(market_data, *aliases)
            if value not in (None, ""):
                values[key] = _market_data_field(key, value, market_as_of, stale=stale, stale_reason=stale_reason)

        enterprise_value = _derived_enterprise_value(market_data, facts)
        if enterprise_value is not None and "enterprise_value" not in values:
            values["enterprise_value"] = _market_data_field(
                "enterprise_value",
                _format_money(enterprise_value),
                market_as_of,
                stale=stale,
                stale_reason=stale_reason,
                source="Derived from market data and SEC company facts",
            )

        fcf_yield = _derived_fcf_yield(market_data, facts)
        if fcf_yield is not None and "free_cash_flow_yield" not in values:
            values["free_cash_flow_yield"] = _market_data_field(
                "free_cash_flow_yield",
                _format_percent(fcf_yield),
                market_as_of,
                stale=stale,
                stale_reason=stale_reason,
                source="Derived from market data and SEC company facts",
            )

        peer_group = _peer_group_from_metadata(metadata)
        peer_text = peer_comparison_text(peer_group, _market_metric_values(market_data)) if peer_group else None
        if peer_text is not None:
            values["peer_comparison"] = _market_data_field(
                "peer_comparison",
                peer_text,
                market_as_of,
                stale=stale,
                stale_reason=stale_reason,
                source="Peer comparison metadata",
            )

    return _fields_for_category("valuation", values)


def _market_data_field(
    key: str,
    value: Any,
    market_as_of: datetime.date | None,
    *,
    stale: bool,
    stale_reason: str | None,
    source: str = "Market-data metadata",
) -> dict[str, Any]:
    return available_field(
        "valuation",
        key,
        _join_value(value),
        source=source,
        as_of=_date_text(market_as_of),
        confidence=0.8 if not stale else 0.45,
        stale=stale,
        stale_reason=stale_reason,
        freshness_as_of=_date_text(market_as_of),
        evidence_refs=(
            [
                {
                    "source_type": "market_data",
                    "source_id": f"{source}:{key}:{_date_text(market_as_of) or 'unknown'}",
                }
            ]
        ),
    )


def _peer_group_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    peer_data = _metadata_lookup(metadata, "peer_group", "peerGroup", "peers")
    if not isinstance(peer_data, Mapping):
        return None
    company = peer_data.get("company")
    candidates = peer_data.get("peers")
    if not isinstance(company, Mapping) or not isinstance(candidates, Sequence):
        return None
    overrides = peer_data.get("manual_overrides", peer_data.get("manualOverrides"))
    calculated_at = _to_date(_metadata_lookup(peer_data, "calculated_at", "calculatedAt"))
    return build_peer_group(
        company,
        [item for item in candidates if isinstance(item, Mapping)],
        manual_overrides=[item for item in overrides if isinstance(item, Mapping)] if isinstance(overrides, Sequence) else None,
        calculated_at=calculated_at,
    )


def _market_metric_values(market_data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pe_ratio": _metadata_lookup(market_data, "pe_ratio", "pe"),
        "ps_ratio": _metadata_lookup(market_data, "ps_ratio", "ps"),
        "pb_ratio": _metadata_lookup(market_data, "pb_ratio", "pb"),
        "ev_ebitda": _metadata_lookup(market_data, "ev_ebitda"),
        "free_cash_flow_yield": _metadata_lookup(market_data, "free_cash_flow_yield", "fcf_yield"),
    }


def _financial_card(company_name: str, fields: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    lookup = _field_lookup(fields)
    headline = (
        f"{company_name} has SEC financial facts available for this profile."
        if any(field["available"] for field in fields)
        else INFORMATION_NOT_AVAILABLE
    )
    return {
        "headline": headline,
        "metrics": [
            _metric("Revenue", lookup, "revenue", tone="positive"),
            _metric("Net profit", lookup, "net_profit", tone="positive"),
            _metric("Cash flow", lookup, "cash_flow", tone="positive"),
            _metric("Cash position", lookup, "cash_position", tone="positive"),
            _metric("Assets", lookup, "assets", tone="neutral"),
            _metric("Debt level", lookup, "debt_level", tone="warning"),
        ],
        "notes": [
            _availability_note(fields, "financial"),
            "Unsupported fields remain marked as Information not available.",
        ],
    }


def _business_card(
    company_name: str,
    fields: Sequence[Mapping[str, Any]],
    sic: str | None,
    sic_description: str | None,
) -> dict[str, Any]:
    lookup = _field_lookup(fields)
    description = lookup.get("business_model", {}).get("value") or INFORMATION_NOT_AVAILABLE
    return {
        "description": description,
        "segments": [
            {
                "label": "SEC industry",
                "value": sic or INFORMATION_NOT_AVAILABLE,
                "detail": sic_description or INFORMATION_NOT_AVAILABLE,
            }
        ],
        "advantages": [_value_or_unavailable(lookup, "competitive_advantage")],
        "watchItems": [
            "Business detail not collected"
            if not any(field["available"] for field in fields)
            else f"Review source coverage for {company_name}"
        ],
    }


def _industry_card(
    company_name: str,
    fields: Sequence[Mapping[str, Any]],
    sic_description: str | None,
) -> dict[str, Any]:
    lookup = _field_lookup(fields)
    return {
        "position": sic_description or INFORMATION_NOT_AVAILABLE,
        "growthDrivers": [_value_or_unavailable(lookup, "industry_growth_rate")],
        "risks": [_value_or_unavailable(lookup, "geopolitical_risks")],
        "outlook": (
            _value_or_unavailable(lookup, "industry_trends")
            if any(field["available"] for field in fields)
            else f"Industry dataset is not yet available for {company_name}."
        ),
    }


def _valuation_card(ticker: str | None, fields: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    lookup = _field_lookup(fields)
    return {
        "view": "Market-data unavailable" if not any(field["available"] for field in fields) else "Collected",
        "price": _value_or_unavailable(lookup, "share_price"),
        "marketCap": _value_or_unavailable(lookup, "market_capitalization"),
        "metrics": [
            _metric("PE ratio", lookup, "pe_ratio"),
            _metric("Forward PE", lookup, "forward_pe"),
            _metric("FCF yield", lookup, "free_cash_flow_yield"),
        ],
        "interpretation": (
            "Valuation cannot be judged until market price, share count, and peer-comparison data are collected."
            if not any(field["available"] for field in fields)
            else f"Valuation fields were collected for {ticker or 'this company'}."
        ),
    }


def _fields_for_category(category: str, values: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return fields_for_category(category, values)


def _available_field(
    key: str,
    label: str,
    value: Any,
    *,
    source: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    category = _category_for_field_key(key)
    field = available_field(
        category,
        key,
        value,
        source=source or "Profile data",
        as_of=as_of,
    )
    if label:
        field["label"] = label
    return field


def _unavailable_field(key: str, label: str, *, reason: str | None = None) -> dict[str, Any]:
    category = _category_for_field_key(key)
    field = unavailable_field(category, key, reason=reason)
    if label:
        field["label"] = label
    return field


def _label(category: str, key: str) -> str:
    return field_label(category, key)


def _category_for_field_key(key: str) -> str:
    for category, fields in COMPANY_RESEARCH_CHECKLIST.items():
        if any(candidate_key == key for candidate_key, _label in fields):
            return category
    return "financial"


def _metric(
    label: str,
    lookup: Mapping[str, Mapping[str, Any]],
    key: str,
    *,
    tone: str = "neutral",
) -> dict[str, str]:
    field = lookup.get(key)
    return {
        "label": label,
        "value": str(field.get("value") if field else INFORMATION_NOT_AVAILABLE),
        "tone": tone if field and field.get("available") else "neutral",
    }


def _coverage(checklist: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    return coverage_for_checklist(checklist)


def _source_summary(company: Any | None, facts: Sequence[Any], filings: Sequence[Any]) -> dict[str, Any]:
    return {
        "sec_company_available": company is not None,
        "sec_fact_count": len(facts),
        "sec_filing_count": len(filings),
        "providers": [
            provider
            for provider, available in (
                ("SEC EDGAR", company is not None or bool(facts) or bool(filings)),
                ("Market data", False),
                ("Industry data", False),
            )
            if available
        ],
    }


def _latest_fact(facts: Sequence[Any], concepts: Sequence[str]) -> Any | None:
    concept_set = {concept.casefold() for concept in concepts}
    candidates = [
        fact
        for fact in facts
        if str(_getattr(fact, "concept", "")).casefold() in concept_set
        and _number(_getattr(fact, "value")) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=_fact_sort_key)


def _facts_for_concepts(facts: Sequence[Any], concepts: Sequence[str]) -> list[Any]:
    concept_set = {concept.casefold() for concept in concepts}
    candidates = [
        fact
        for fact in facts
        if str(_getattr(fact, "concept", "")).casefold() in concept_set
        and _number(_getattr(fact, "value")) is not None
    ]
    return sorted(candidates, key=_fact_sort_key, reverse=True)


def _fact_sort_key(fact: Any) -> tuple[datetime.date, int, int]:
    period_end = _fact_period_end(fact) or datetime.date.min
    fiscal_year = _getattr(fact, "fiscal_year") or 0
    try:
        fiscal_year_int = int(fiscal_year)
    except (TypeError, ValueError):
        fiscal_year_int = 0
    fiscal_period = str(_getattr(fact, "fiscal_period", "") or "").upper()
    return (period_end, fiscal_year_int, 1 if fiscal_period == "FY" else 0)


def _fact_period_end(fact: Any) -> datetime.date | None:
    value = _first_present(_getattr(fact, "period_end"), _getattr(fact, "period_end_at"))
    return _to_date(value)


def _latest_period_end(facts: Sequence[Any]) -> datetime.date | None:
    dates = [_fact_period_end(fact) for fact in facts]
    available = [date for date in dates if date is not None]
    return max(available) if available else None


def _format_fact_value(fact: Any) -> str:
    value = _number(_getattr(fact, "value"))
    unit = str(_getattr(fact, "unit", "") or "")
    if value is None:
        return INFORMATION_NOT_AVAILABLE
    if "USD" in unit and "shares" not in unit.casefold():
        return _format_money(value)
    if "shares" in unit.casefold():
        return _format_plain(value)
    if "USD/shares" in unit or "USD/sh" in unit:
        return f"${value:,.2f}"
    return _format_plain(value)


def _fact_source(fact: Any) -> str:
    taxonomy = _getattr(fact, "taxonomy", "taxonomy")
    concept = _getattr(fact, "concept", "concept")
    return f"SEC company facts: {taxonomy}/{concept}"


def _debt_text(facts: Sequence[Any]) -> str | None:
    current = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["debt_current"]))
    noncurrent = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["debt_noncurrent"]))
    if current is None and noncurrent is None:
        return None
    total = (current or 0.0) + (noncurrent or 0.0)
    if current is not None and noncurrent is not None:
        return f"{_format_money(total)} total debt"
    return _format_money(total)


def _margin_text(facts: Sequence[Any]) -> str | None:
    revenue = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["revenue"]))
    if not revenue:
        return None
    parts = []
    for label, concepts in (
        ("gross", FINANCIAL_FACT_CONCEPTS["gross_profit"]),
        ("operating", FINANCIAL_FACT_CONCEPTS["operating_profit"]),
        ("net", FINANCIAL_FACT_CONCEPTS["net_profit"]),
    ):
        value = _fact_number(_latest_fact(facts, concepts))
        if value is not None:
            parts.append(f"{label} {_format_percent(value / revenue)}")
    return "; ".join(parts) if parts else None


def _return_ratio_text(facts: Sequence[Any]) -> str | None:
    net_income = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["net_profit"]))
    assets = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["assets"]))
    equity = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["equity"]))
    if net_income is None:
        return None
    parts = []
    if equity:
        parts.append(f"ROE {_format_percent(net_income / equity)}")
    if assets:
        parts.append(f"ROA {_format_percent(net_income / assets)}")
    return "; ".join(parts) if parts else None


def _cash_flow_text(facts: Sequence[Any]) -> str | None:
    operating = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["cash_flow"]))
    capex = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["capital_expenditure"]))
    if operating is None:
        return None
    parts = [f"operating {_format_money(operating)}"]
    if capex is not None:
        free_cash_flow = operating - abs(capex)
        parts.append(f"free cash flow {_format_money(free_cash_flow)}")
    return "; ".join(parts)


def _derived_enterprise_value(market_data: Mapping[str, Any], facts: Sequence[Any]) -> float | None:
    market_cap = _number(_metadata_lookup(market_data, "market_cap", "market_capitalization"))
    if market_cap is None:
        return None
    current_debt = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["debt_current"])) or 0.0
    noncurrent_debt = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["debt_noncurrent"])) or 0.0
    cash = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["cash_position"])) or 0.0
    return market_cap + current_debt + noncurrent_debt - cash


def _derived_fcf_yield(market_data: Mapping[str, Any], facts: Sequence[Any]) -> float | None:
    market_cap = _number(_metadata_lookup(market_data, "market_cap", "market_capitalization"))
    operating = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["cash_flow"]))
    capex = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["capital_expenditure"]))
    if not market_cap or operating is None or capex is None:
        return None
    return (operating - abs(capex)) / market_cap


def _dividends_buybacks_text(facts: Sequence[Any]) -> str | None:
    dividends = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["dividends"]))
    buybacks = _fact_number(_latest_fact(facts, FINANCIAL_FACT_CONCEPTS["buybacks"]))
    parts = []
    if dividends is not None:
        parts.append(f"dividends {_format_money(abs(dividends))}")
    if buybacks is not None:
        parts.append(f"buybacks {_format_money(abs(buybacks))}")
    return "; ".join(parts) if parts else None


def _financial_trend_text(facts: Sequence[Any]) -> str | None:
    trend_groups = (
        ("Revenue", FINANCIAL_FACT_CONCEPTS["revenue"]),
        ("Net profit", FINANCIAL_FACT_CONCEPTS["net_profit"]),
        ("Operating cash flow", FINANCIAL_FACT_CONCEPTS["cash_flow"]),
    )
    group_texts: list[str] = []
    for label, concepts in trend_groups:
        text = _trend_points_text(label, _facts_for_concepts(facts, concepts))
        if text is not None:
            group_texts.append(text)
    return " | ".join(group_texts) if group_texts else None


def _trend_points_text(label: str, facts: Sequence[Any]) -> str | None:
    by_period: dict[datetime.date, Any] = {}
    for fact in facts:
        period = _fact_period_end(fact)
        if period is not None and period not in by_period:
            by_period[period] = fact
        if len(by_period) >= 5:
            break
    if len(by_period) < 2:
        return None
    parts = [
        f"{period.isoformat()} {_format_fact_value(fact)}"
        for period, fact in sorted(by_period.items(), reverse=True)
    ]
    return f"{label} trend points: " + "; ".join(parts)


def _latest_filing(filings: Sequence[Any], form_types: set[str] | None = None) -> Any | None:
    candidates = []
    for filing in filings:
        form_type = str(_getattr(filing, "form_type", "") or "").upper()
        if form_types is None or form_type in form_types:
            candidates.append(filing)
    if not candidates:
        return None
    return max(candidates, key=lambda filing: _filing_date(filing) or datetime.date.min)


def _market_data_as_of(market_data: Mapping[str, Any]) -> datetime.date | None:
    value = _metadata_lookup(market_data, "as_of", "asOf", "date", "priced_at", "pricedAt", "last_updated_at", "lastUpdatedAt")
    return _to_date(value)


def _market_data_staleness(
    market_as_of: datetime.date | None,
    *,
    as_of: datetime.date | None,
) -> tuple[bool, str | None]:
    if market_as_of is None:
        return True, "Market-data freshness date is unavailable."
    reference_date = as_of or datetime.date.today()
    age_days = (reference_date - market_as_of).days
    if age_days > MARKET_DATA_MAX_AGE_DAYS:
        return True, f"Market data is {age_days} days old; maximum freshness window is {MARKET_DATA_MAX_AGE_DAYS} days."
    return False, None


def _latest_filing_date(filings: Sequence[Any]) -> datetime.date | None:
    latest = _latest_filing(filings)
    return _filing_date(latest) if latest is not None else None


def _filing_date(filing: Any) -> datetime.date | None:
    return _to_date(_getattr(filing, "filing_date"))


def _filing_source(filing: Any) -> str:
    accession = _getattr(filing, "accession_number", None)
    if accession:
        return f"SEC filing {accession}"
    return "SEC filing"


def _field_lookup(fields: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(field["key"]): field for field in fields}


def _value_or_unavailable(lookup: Mapping[str, Mapping[str, Any]], key: str) -> str:
    field = lookup.get(key)
    if not field or not field.get("available"):
        return INFORMATION_NOT_AVAILABLE
    return str(field.get("value") or INFORMATION_NOT_AVAILABLE)


def _availability_note(fields: Sequence[Mapping[str, Any]], category: str) -> str:
    available = sum(1 for field in fields if field.get("available"))
    return f"{available} of {len(fields)} {category} checklist fields collected."


def _metadata(obj: Any | None) -> Mapping[str, Any]:
    if obj is None:
        return {}
    for name in ("company_metadata", "profile_metadata", "metadata"):
        value = _getattr(obj, name)
        if isinstance(value, Mapping):
            return value
    return {}


def _metadata_lookup(metadata: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in metadata and metadata[key] not in (None, ""):
            return metadata[key]
    normalized = {str(key).casefold(): value for key, value in metadata.items()}
    for key in keys:
        value = normalized.get(key.casefold())
        if value not in (None, ""):
            return value
    return None


def _metadata_field_source(metadata: Mapping[str, Any], key: str, *, default: str) -> str:
    value = metadata.get(key)
    if isinstance(value, Mapping):
        section = value.get("section")
        accession = value.get("accession_number")
        if section and accession:
            return f"SEC filing section {section} ({accession})"
        if section:
            return f"SEC filing section {section}"
    return default


def _getattr(obj: Any | None, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    return getattr(obj, name, default)


def _first_text(*values: Any) -> str | None:
    value = _first_present(*values)
    if value is None:
        return None
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        value = value[0] if value else None
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _first_sequence_value(value: Any) -> Any:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value[0] if value else None
    return value


def _join_value(value: Any) -> str:
    if value in (None, ""):
        return INFORMATION_NOT_AVAILABLE
    if isinstance(value, Mapping):
        return "; ".join(f"{key}: {_join_value(item)}" for key, item in value.items()) or INFORMATION_NOT_AVAILABLE
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return "; ".join(_join_value(item) for item in value) or INFORMATION_NOT_AVAILABLE
    return str(value)


def _fact_number(fact: Any | None) -> float | None:
    if fact is None:
        return None
    return _number(_getattr(fact, "value"))


def _number(value: Any) -> float | None:
    if value in (None, "", "."):
        return None
    if isinstance(value, decimal.Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    for threshold, suffix in ((1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M")):
        if magnitude >= threshold:
            return f"{sign}${magnitude / threshold:,.1f}{suffix}"
    return f"{sign}${magnitude:,.0f}"


def _format_percent(value: float) -> str:
    return f"{value * 100:,.1f}%"


def _format_plain(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _to_date(value: Any) -> datetime.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        return None


def _date_text(value: datetime.date | None) -> str | None:
    return value.isoformat() if value is not None else None
