"""Provider-neutral industry context normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


INDUSTRY_CONTEXT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "industry_size": ("industry_size", "market_size"),
    "industry_growth_rate": ("industry_growth_rate", "growth_rate"),
    "industry_trends": ("industry_trends", "trends", "news_pressure"),
    "market_share": ("market_share",),
    "competition": ("competition", "competitors"),
    "barriers_to_entry": ("barriers_to_entry",),
    "industry_profitability": ("industry_profitability", "profitability"),
    "business_cycle": ("business_cycle", "cycle"),
    "regulation": ("regulation", "regulatory_pressure"),
    "technology_changes": ("technology_changes", "technology_shift"),
    "supply_and_demand": ("supply_and_demand", "demand_signal"),
    "raw_material_exposure": ("raw_material_exposure", "commodity_exposure"),
    "customer_demand": ("customer_demand", "external_demand"),
    "substitution_risk": ("substitution_risk",),
    "macroeconomic_factors": ("macroeconomic_factors", "macro_factors"),
    "geopolitical_risks": ("geopolitical_risks", "geopolitical_risk", "risk_factors"),
}


def industry_context_values(metadata: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return canonical industry context values explicitly supplied by providers."""

    context = _metadata_lookup(metadata, "industry_context", "industryContext", "external_risk_context")
    if not isinstance(context, Mapping):
        context = {}

    values: dict[str, dict[str, Any]] = {}
    for field_key, aliases in INDUSTRY_CONTEXT_FIELD_ALIASES.items():
        value = _metadata_lookup(context, *aliases)
        source = _metadata_lookup(context, f"{field_key}_source", "source")
        if value not in (None, ""):
            values[field_key] = {
                "value": value,
                "source": str(source or "Industry context metadata"),
            }
    return values


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
