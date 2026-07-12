"""Deterministic signal foundation for the standalone crisis model.

Phase 1 converts canonical daily country signals into normalized, explainable
0-100 stress features. It does not fetch provider data and does not write to the API.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from db.models.enums import RiskLevel, risk_level_for_score
from services.crisis_model.contract import validate_evidence_refs


@dataclass(frozen=True)
class SignalDefinition:
    """Normalization metadata for one canonical country-daily risk signal."""

    name: str
    group: str
    weight: float
    direction: int = 1
    scale: float = 1.0


@dataclass(frozen=True)
class NormalizedSignal:
    """A single normalized signal contribution."""

    name: str
    group: str
    raw_value: float
    score: float
    weight: float
    contribution: float


@dataclass(frozen=True)
class CountrySignalPanel:
    """Normalized daily signal panel for one country/date."""

    country: str
    date: datetime.date
    signals: tuple[NormalizedSignal, ...]
    evidence_refs: tuple[Mapping[str, str], ...] = field(default_factory=tuple)

    @property
    def coverage_ratio(self) -> float:
        return round(len(self.signals) / len(SIGNAL_DEFINITIONS), 4)


@dataclass(frozen=True)
class SignalScore:
    """Deterministic risk-like score produced from a normalized signal panel."""

    country: str
    date: datetime.date
    signal_score: float
    risk_level: RiskLevel
    confidence_score: float
    top_drivers: tuple[NormalizedSignal, ...]
    evidence_refs: tuple[Mapping[str, str], ...]


SIGNAL_DEFINITIONS: Final[Mapping[str, SignalDefinition]] = {
    # Macro / credit vulnerability.
    "credit_to_gdp_gap": SignalDefinition("credit_to_gdp_gap", "macro_credit", 0.065, scale=12.0),
    "debt_service_ratio": SignalDefinition("debt_service_ratio", "macro_credit", 0.055, scale=25.0),
    "house_price_gap": SignalDefinition("house_price_gap", "macro_credit", 0.040, scale=15.0),
    "current_account_balance_gdp": SignalDefinition(
        "current_account_balance_gdp", "macro_credit", 0.040, direction=-1, scale=8.0
    ),
    "fiscal_deficit_gdp": SignalDefinition("fiscal_deficit_gdp", "macro_credit", 0.045, scale=10.0),
    "fx_reserve_change_3m": SignalDefinition(
        "fx_reserve_change_3m", "macro_credit", 0.045, direction=-1, scale=10.0
    ),
    # Market / financial stress.
    "financial_stress_index": SignalDefinition(
        "financial_stress_index", "market_financial", 0.070, scale=2.5
    ),
    "credit_spread_zscore": SignalDefinition(
        "credit_spread_zscore", "market_financial", 0.065, scale=3.0
    ),
    "bank_equity_drawdown_30d": SignalDefinition(
        "bank_equity_drawdown_30d", "market_financial", 0.055, direction=-1, scale=25.0
    ),
    "volatility_zscore": SignalDefinition("volatility_zscore", "market_financial", 0.040, scale=3.0),
    "liquidity_stress_score": SignalDefinition(
        "liquidity_stress_score", "market_financial", 0.055, scale=100.0
    ),
    # News/event aggregates.
    "policy_uncertainty_news_zscore": SignalDefinition(
        "policy_uncertainty_news_zscore", "news_event", 0.050, scale=3.0
    ),
    "banking_stress_news_score": SignalDefinition(
        "banking_stress_news_score", "news_event", 0.055, scale=100.0
    ),
    "deposit_outflow_news_score": SignalDefinition(
        "deposit_outflow_news_score", "news_event", 0.055, scale=100.0
    ),
    "civil_unrest_score": SignalDefinition("civil_unrest_score", "news_event", 0.035, scale=100.0),
    "war_escalation_score": SignalDefinition(
        "war_escalation_score", "news_event", 0.045, scale=100.0
    ),
    "sanctions_risk_score": SignalDefinition(
        "sanctions_risk_score", "news_event", 0.035, scale=100.0
    ),
    "natural_disaster_score": SignalDefinition(
        "natural_disaster_score", "news_event", 0.030, scale=100.0
    ),
    "supply_chain_disruption_score": SignalDefinition(
        "supply_chain_disruption_score", "news_event", 0.040, scale=100.0
    ),
    "news_velocity_zscore": SignalDefinition("news_velocity_zscore", "news_event", 0.055, scale=3.0),
    "source_diversity_score": SignalDefinition("source_diversity_score", "news_event", 0.040),
    "official_confirmation_score": SignalDefinition(
        "official_confirmation_score", "news_event", 0.025
    ),
}


def _coerce_date(value: datetime.date | str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(value)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """Clamp a numeric value to a finite range."""
    if not math.isfinite(value):
        msg = "signal value must be finite"
        raise ValueError(msg)
    return max(low, min(high, value))


def normalize_signal_value(value: float, definition: SignalDefinition) -> float:
    """Map one raw signal to a 0-100 stress score."""
    directed = value * definition.direction
    if definition.scale == 1.0:
        return round(clamp(directed * 100.0), 2)
    return round(clamp((directed / definition.scale) * 100.0), 2)


def build_country_signal_panel(
    *,
    country: str,
    date: datetime.date | str,
    values: Mapping[str, Any],
    evidence_refs: Sequence[Mapping[str, str]] = (),
) -> CountrySignalPanel:
    """Normalize available canonical daily signals for one country/date."""
    if not country.strip():
        msg = "country must be a non-empty string"
        raise ValueError(msg)
    if evidence_refs:
        validate_evidence_refs(evidence_refs)

    normalized: list[NormalizedSignal] = []
    for name, definition in SIGNAL_DEFINITIONS.items():
        raw = values.get(name)
        if raw is None:
            continue
        try:
            raw_value = float(raw)
        except (TypeError, ValueError) as exc:
            msg = f"{name} must be numeric"
            raise ValueError(msg) from exc
        score = normalize_signal_value(raw_value, definition)
        normalized.append(
            NormalizedSignal(
                name=name,
                group=definition.group,
                raw_value=raw_value,
                score=score,
                weight=definition.weight,
                contribution=round(score * definition.weight, 4),
            )
        )

    return CountrySignalPanel(
        country=country,
        date=_coerce_date(date),
        signals=tuple(normalized),
        evidence_refs=tuple(evidence_refs),
    )


def score_signal_panel(panel: CountrySignalPanel, *, top_n: int = 5) -> SignalScore:
    """Create a deterministic daily country signal score from normalized features."""
    if not panel.signals:
        return SignalScore(
            country=panel.country,
            date=panel.date,
            signal_score=0.0,
            risk_level=RiskLevel.LOW,
            confidence_score=0.0,
            top_drivers=(),
            evidence_refs=panel.evidence_refs,
        )

    used_weight = sum(signal.weight for signal in panel.signals)
    weighted_score = sum(signal.contribution for signal in panel.signals)
    signal_score = round(clamp(weighted_score / used_weight), 2)
    confidence_score = round(min(1.0, 0.25 + panel.coverage_ratio + 0.1 * len(panel.evidence_refs)), 4)
    top_drivers = tuple(
        sorted(panel.signals, key=lambda signal: signal.contribution, reverse=True)[:top_n]
    )

    return SignalScore(
        country=panel.country,
        date=panel.date,
        signal_score=signal_score,
        risk_level=risk_level_for_score(signal_score),
        confidence_score=confidence_score,
        top_drivers=top_drivers,
        evidence_refs=panel.evidence_refs,
    )
