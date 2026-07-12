"""Canonical company research profile contract helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

INFORMATION_NOT_AVAILABLE = "Information not available"

COMPANY_RESEARCH_CHECKLIST: dict[str, tuple[tuple[str, str], ...]] = {
    "financial": (
        ("revenue", "Revenue"),
        ("gross_profit", "Gross profit"),
        ("operating_profit", "Operating profit"),
        ("net_profit", "Net profit"),
        ("profit_margins", "Profit margins"),
        ("eps", "Earnings per share, EPS"),
        ("cash_flow", "Cash flow"),
        ("debt_level", "Debt level"),
        ("cash_position", "Cash position"),
        ("assets", "Assets"),
        ("liabilities", "Liabilities"),
        ("return_ratios", "Return ratios"),
        ("capital_expenditure", "Capital expenditure"),
        ("dividends_buybacks", "Dividends and buybacks"),
        ("financial_trends", "Financial trends"),
        ("accounting_notes", "Accounting notes"),
    ),
    "business": (
        ("business_model", "Business model"),
        ("main_products_services", "Main products or services"),
        ("revenue_segments", "Revenue segments"),
        ("profit_segments", "Profit segments"),
        ("customers", "Customers"),
        ("customer_concentration", "Customer concentration"),
        ("suppliers", "Suppliers"),
        ("supplier_concentration", "Supplier concentration"),
        ("pricing_power", "Pricing power"),
        ("sales_channels", "Sales channels"),
        ("geographic_exposure", "Geographic exposure"),
        ("production_capacity", "Production capacity"),
        ("supply_chain", "Supply chain"),
        ("key_operating_metrics", "Key operating metrics"),
        ("competitive_advantage", "Competitive advantage"),
        ("management_strategy_team", "Management strategy and team"),
    ),
    "industry": (
        ("industry_size", "Industry size"),
        ("industry_growth_rate", "Industry growth rate"),
        ("industry_trends", "Industry trends"),
        ("market_share", "Market share"),
        ("competition", "Competition"),
        ("barriers_to_entry", "Barriers to entry"),
        ("industry_profitability", "Industry profitability"),
        ("business_cycle", "Business cycle"),
        ("regulation", "Regulation"),
        ("technology_changes", "Technology changes"),
        ("supply_and_demand", "Supply and demand"),
        ("raw_material_exposure", "Raw material exposure"),
        ("customer_demand", "Customer demand"),
        ("substitution_risk", "Substitution risk"),
        ("macroeconomic_factors", "Macroeconomic factors"),
        ("geopolitical_risks", "Geopolitical risks"),
    ),
    "valuation": (
        ("share_price", "Share price"),
        ("market_capitalization", "Market capitalization"),
        ("enterprise_value", "Enterprise value"),
        ("pe_ratio", "PE ratio"),
        ("forward_pe", "Forward PE"),
        ("pb_ratio", "PB ratio"),
        ("ps_ratio", "PS ratio"),
        ("ev_ebitda", "EV/EBITDA"),
        ("free_cash_flow_yield", "Free cash flow yield"),
        ("dividend_yield", "Dividend yield"),
        ("peg_ratio", "PEG ratio"),
        ("historical_valuation", "Historical valuation"),
        ("peer_comparison", "Peer comparison"),
        ("growth_assumptions", "Growth assumptions"),
        ("profit_assumptions", "Profit assumptions"),
        ("discounted_cash_flow", "Discounted cash flow, DCF"),
        ("bull_base_bear_scenarios", "Bull/base/bear scenarios"),
        ("margin_of_safety", "Margin of safety"),
    ),
}

COMPANY_RESEARCH_CATEGORIES: tuple[str, ...] = tuple(COMPANY_RESEARCH_CHECKLIST)
COMPANY_RESEARCH_FIELD_TOTAL = sum(len(fields) for fields in COMPANY_RESEARCH_CHECKLIST.values())


class CompanyResearchContractError(ValueError):
    """Raised when a company research profile does not match the contract."""


def field_label(category: str, key: str) -> str:
    """Return the canonical label for a field key."""

    for candidate_key, label in COMPANY_RESEARCH_CHECKLIST[category]:
        if candidate_key == key:
            return label
    return key.replace("_", " ").title()


def available_field(
    category: str,
    key: str,
    value: Any,
    *,
    source: str,
    as_of: str | None = None,
    evidence_refs: Sequence[Mapping[str, Any]] | None = None,
    confidence: float = 0.85,
    stale: bool = False,
    stale_reason: str | None = None,
    freshness_as_of: str | None = None,
    conflict: bool = False,
    conflict_reason: str | None = None,
    provider_values: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a canonical available field."""

    value_text = _join_value(value)
    if not _text_available(value_text):
        return unavailable_field(category, key)

    return {
        "key": key,
        "category": category,
        "label": field_label(category, key),
        "value": value_text,
        "available": True,
        "source": source,
        "as_of": as_of,
        "confidence": _bounded_confidence(confidence),
        "evidence_refs": _evidence_refs(evidence_refs, source=source),
        "stale": bool(stale),
        "stale_reason": stale_reason if stale else None,
        "freshness_as_of": freshness_as_of or as_of,
        "conflict": bool(conflict),
        "conflict_reason": conflict_reason if conflict else None,
        "provider_values": [dict(item) for item in provider_values or []],
        "reason": None,
    }


def unavailable_field(category: str, key: str, *, reason: str | None = None) -> dict[str, Any]:
    """Return a canonical unavailable field."""

    return {
        "key": key,
        "category": category,
        "label": field_label(category, key),
        "value": INFORMATION_NOT_AVAILABLE,
        "available": False,
        "source": None,
        "as_of": None,
        "confidence": 0.0,
        "evidence_refs": [],
        "stale": False,
        "stale_reason": None,
        "freshness_as_of": None,
        "conflict": False,
        "conflict_reason": None,
        "provider_values": [],
        "reason": reason or "No supported provider data has been collected for this item.",
    }


def normalize_field(category: str, key: str, field: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Merge any field-like mapping onto the canonical field shape."""

    if field is None:
        return unavailable_field(category, key)

    raw_value = field.get("value")
    raw_available = bool(field.get("available"))
    available = raw_available and _text_available(raw_value)
    if not available:
        return unavailable_field(category, key, reason=_optional_text(field.get("reason")))

    source = _optional_text(field.get("source")) or "Profile data"
    return available_field(
        category,
        key,
        raw_value,
        source=source,
        as_of=_optional_text(field.get("as_of")),
        evidence_refs=field.get("evidence_refs") if isinstance(field.get("evidence_refs"), Sequence) else None,
        confidence=_confidence_value(field.get("confidence")),
        stale=bool(field.get("stale")),
        stale_reason=_optional_text(field.get("stale_reason")),
        freshness_as_of=_optional_text(field.get("freshness_as_of")),
        conflict=bool(field.get("conflict")),
        conflict_reason=_optional_text(field.get("conflict_reason")),
        provider_values=field.get("provider_values") if isinstance(field.get("provider_values"), Sequence) else None,
    )


def fields_for_category(category: str, values: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return all canonical fields for a category, preserving provided values."""

    return [normalize_field(category, key, values.get(key)) for key, _label in COMPANY_RESEARCH_CHECKLIST[category]]


def coverage_for_checklist(checklist: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Return available/missing counts for the canonical checklist."""

    total = 0
    available = 0
    by_category: dict[str, dict[str, int]] = {}
    for category in COMPANY_RESEARCH_CATEGORIES:
        fields = list(checklist.get(category, []))
        category_total = len(fields)
        category_available = sum(1 for field in fields if field.get("available"))
        by_category[category] = {
            "available": category_available,
            "total": category_total,
            "missing": category_total - category_available,
        }
        total += category_total
        available += category_available
    return {
        "available": available,
        "total": total,
        "missing": total - available,
        "coverage_pct": round((available / total) * 100, 1) if total else 0.0,
        "by_category": by_category,
    }


def missing_fields_for_checklist(checklist: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Return canonical descriptors for unavailable fields."""

    missing: list[dict[str, Any]] = []
    for category in COMPANY_RESEARCH_CATEGORIES:
        for field in checklist.get(category, []):
            if not field.get("available"):
                missing.append(
                    {
                        "category": category,
                        "key": field.get("key"),
                        "label": field.get("label"),
                        "reason": field.get("reason") or "No supported provider data has been collected for this item.",
                    }
                )
    return missing


def stale_fields_for_checklist(checklist: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Return canonical descriptors for stale fields."""

    stale_fields: list[dict[str, Any]] = []
    for category in COMPANY_RESEARCH_CATEGORIES:
        for field in checklist.get(category, []):
            if field.get("stale"):
                stale_fields.append(
                    {
                        "category": category,
                        "key": field.get("key"),
                        "label": field.get("label"),
                        "as_of": field.get("as_of"),
                        "reason": field.get("stale_reason") or "Field data is stale.",
                    }
                )
    return stale_fields


def evidence_refs_for_checklist(checklist: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Return de-duplicated evidence references from all fields."""

    refs: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for fields in checklist.values():
        for field in fields:
            for ref in field.get("evidence_refs") or []:
                if not isinstance(ref, Mapping):
                    continue
                source_type = _optional_text(ref.get("source_type"))
                source_id = _optional_text(ref.get("source_id"))
                marker = (source_type, source_id)
                if marker in seen:
                    continue
                seen.add(marker)
                refs.append({"source_type": source_type, "source_id": source_id})
    return refs


def validate_company_research_profile(profile: Mapping[str, Any]) -> list[str]:
    """Return contract validation errors for a profile."""

    errors: list[str] = []
    for key in (
        "asOf",
        "lastUpdatedAt",
        "identity",
        "coverage",
        "sources",
        "financial",
        "business",
        "industry",
        "valuation",
        "researchChecklist",
        "missingFields",
        "staleFields",
        "evidenceRefs",
    ):
        if key not in profile:
            errors.append(f"missing top-level key: {key}")

    checklist = profile.get("researchChecklist")
    if not isinstance(checklist, Mapping):
        errors.append("researchChecklist must be an object")
        return errors

    for category in COMPANY_RESEARCH_CATEGORIES:
        fields = checklist.get(category)
        if not isinstance(fields, Sequence) or isinstance(fields, str | bytes | bytearray):
            errors.append(f"researchChecklist.{category} must be a list")
            continue

        by_key = {field.get("key"): field for field in fields if isinstance(field, Mapping)}
        expected = COMPANY_RESEARCH_CHECKLIST[category]
        if len(fields) != len(expected):
            errors.append(f"researchChecklist.{category} expected {len(expected)} fields, got {len(fields)}")
        for key, label in expected:
            field = by_key.get(key)
            if field is None:
                errors.append(f"missing field: {category}.{key}")
                continue
            errors.extend(_validate_field(category, key, label, field))

    coverage = profile.get("coverage")
    if isinstance(coverage, Mapping):
        expected_coverage = coverage_for_checklist(checklist)
        for key in ("available", "total", "missing", "coverage_pct"):
            if coverage.get(key) != expected_coverage[key]:
                errors.append(f"coverage.{key} expected {expected_coverage[key]}, got {coverage.get(key)}")
    else:
        errors.append("coverage must be an object")

    if not isinstance(profile.get("missingFields"), Sequence):
        errors.append("missingFields must be a list")
    if not isinstance(profile.get("staleFields"), Sequence):
        errors.append("staleFields must be a list")
    if not isinstance(profile.get("evidenceRefs"), Sequence):
        errors.append("evidenceRefs must be a list")

    return errors


def assert_valid_company_research_profile(profile: Mapping[str, Any]) -> None:
    """Raise when a profile does not match the canonical contract."""

    errors = validate_company_research_profile(profile)
    if errors:
        raise CompanyResearchContractError("; ".join(errors))


def _validate_field(category: str, key: str, label: str, field: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    expected = {
        "key",
        "category",
        "label",
        "value",
        "available",
        "source",
        "as_of",
        "confidence",
        "evidence_refs",
        "stale",
        "stale_reason",
        "freshness_as_of",
        "conflict",
        "conflict_reason",
        "provider_values",
        "reason",
    }
    for required_key in expected:
        if required_key not in field:
            errors.append(f"{category}.{key} missing {required_key}")

    if field.get("category") != category:
        errors.append(f"{category}.{key} category mismatch")
    if field.get("label") != label:
        errors.append(f"{category}.{key} label mismatch")
    if not isinstance(field.get("available"), bool):
        errors.append(f"{category}.{key} available must be bool")

    value = field.get("value")
    if field.get("available") is True:
        if not _text_available(value):
            errors.append(f"{category}.{key} available field has unavailable value")
        if not _optional_text(field.get("source")):
            errors.append(f"{category}.{key} available field requires source")
        if not isinstance(field.get("evidence_refs"), Sequence):
            errors.append(f"{category}.{key} evidence_refs must be a list")
    elif field.get("available") is False:
        if value != INFORMATION_NOT_AVAILABLE:
            errors.append(f"{category}.{key} unavailable value must be Information not available")
        if field.get("source") is not None:
            errors.append(f"{category}.{key} unavailable source must be null")
        if field.get("confidence") != 0.0:
            errors.append(f"{category}.{key} unavailable confidence must be 0.0")

    confidence = field.get("confidence")
    if not isinstance(confidence, int | float) or confidence < 0 or confidence > 1:
        errors.append(f"{category}.{key} confidence must be between 0 and 1")
    if not isinstance(field.get("evidence_refs"), Sequence):
        errors.append(f"{category}.{key} evidence_refs must be a list")
    if not isinstance(field.get("stale"), bool):
        errors.append(f"{category}.{key} stale must be bool")
    if field.get("stale") and not _optional_text(field.get("stale_reason")):
        errors.append(f"{category}.{key} stale field requires stale_reason")
    if not isinstance(field.get("conflict"), bool):
        errors.append(f"{category}.{key} conflict must be bool")
    if field.get("conflict") and not isinstance(field.get("provider_values"), Sequence):
        errors.append(f"{category}.{key} conflict field requires provider_values")
    return errors


def _evidence_refs(evidence_refs: Sequence[Mapping[str, Any]] | None, *, source: str) -> list[dict[str, Any]]:
    refs = [dict(ref) for ref in evidence_refs or [] if isinstance(ref, Mapping)]
    if refs:
        return refs
    return [{"source_type": "provider_summary", "source_id": source}]


def _join_value(value: Any) -> str:
    if value in (None, ""):
        return INFORMATION_NOT_AVAILABLE
    if isinstance(value, Mapping):
        return "; ".join(f"{key}: {_join_value(item)}" for key, item in value.items()) or INFORMATION_NOT_AVAILABLE
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return "; ".join(_join_value(item) for item in value) or INFORMATION_NOT_AVAILABLE
    return str(value)


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _text_available(value: Any) -> bool:
    text = _optional_text(value)
    return text is not None and text != INFORMATION_NOT_AVAILABLE


def _confidence_value(value: Any) -> float:
    if isinstance(value, int | float):
        return _bounded_confidence(float(value))
    return 0.85


def _bounded_confidence(value: float) -> float:
    return max(0.0, min(1.0, round(float(value), 3)))
