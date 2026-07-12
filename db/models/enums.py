"""Canonical enums shared by ORM models, services, and the crisis model.

These mirror `docs/contracts/canonical-schema.md` (the single source of truth). Columns
store the string values as TEXT; these StrEnums give the application typed constants.
"""

from __future__ import annotations

from enum import StrEnum


class SourceType(StrEnum):
    """Provenance channel for a piece of evidence (canonical `source_type`)."""

    RSS = "rss"
    NEWS_API = "news_api"
    OFFICIAL = "official"
    FILING = "filing"
    MACRO_INDICATOR = "macro_indicator"
    MARKET_DATA = "market_data"
    BANKING_DATA = "banking_data"
    HISTORICAL_CASE = "historical_case"


class RiskType(StrEnum):
    """Crisis families the platform models (canonical `risk_type`)."""

    BANKING = "banking"
    CURRENCY = "currency"
    SOVEREIGN = "sovereign"
    RECESSION = "recession"
    MARKET_LIQUIDITY = "market_liquidity"
    GEOPOLITICAL_SUPPLY_CHAIN = "geopolitical_supply_chain"
    COMPANY = "company"


class RiskLevel(StrEnum):
    """Risk-score band (canonical `risk_level`)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Horizon(StrEnum):
    """Forecast horizons (canonical `horizon`)."""

    H_0_6M = "0_6m"
    H_6_12M = "6_12m"
    H_12_18M = "12_18m"
    WITHIN_18M = "within_18m"


def risk_level_for_score(score: float) -> RiskLevel:
    """Map a 0-100 risk score to its canonical band (0-30/31-55/56-75/76-100)."""
    if score <= 30:
        return RiskLevel.LOW
    if score <= 55:
        return RiskLevel.MEDIUM
    if score <= 75:
        return RiskLevel.HIGH
    return RiskLevel.CRITICAL
