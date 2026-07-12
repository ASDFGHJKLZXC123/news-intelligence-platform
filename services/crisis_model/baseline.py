"""Baseline probability model for standalone Track M predictions.

Phase 2 provides a transparent, deterministic baseline. It is intentionally simple:
normalize signal groups, map the stress score through a logistic curve, derive
monotonic horizon buckets, then validate the canonical prediction payload.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from db.models.enums import RiskType, risk_level_for_score
from services.crisis_model.contract import (
    risk_score_from_components,
    validate_prediction_payload,
)
from services.crisis_model.signals import CountrySignalPanel, SignalScore, score_signal_panel

BASELINE_MODEL_VERSION: Final[str] = "baseline-probability.v1"

RISK_TYPE_IMPACT_PRIOR: Final[Mapping[str, float]] = {
    RiskType.BANKING.value: 86.0,
    RiskType.CURRENCY.value: 78.0,
    RiskType.SOVEREIGN.value: 82.0,
    RiskType.RECESSION.value: 76.0,
    RiskType.MARKET_LIQUIDITY.value: 74.0,
    RiskType.GEOPOLITICAL_SUPPLY_CHAIN.value: 80.0,
    RiskType.COMPANY.value: 70.0,
}

RISK_TYPE_MULTIPLIER: Final[Mapping[str, float]] = {
    RiskType.BANKING.value: 1.10,
    RiskType.CURRENCY.value: 1.03,
    RiskType.SOVEREIGN.value: 1.06,
    RiskType.RECESSION.value: 0.96,
    RiskType.MARKET_LIQUIDITY.value: 1.00,
    RiskType.GEOPOLITICAL_SUPPLY_CHAIN.value: 1.02,
    RiskType.COMPANY.value: 0.92,
}


@dataclass(frozen=True)
class BaselinePrediction:
    """Standalone baseline prediction plus its canonical payload."""

    target_type: str
    target_id: str
    risk_type: str
    probability_0_6m: float
    probability_6_12m: float
    probability_12_18m: float
    probability_within_18m: float
    risk_score: float
    risk_level: str
    confidence_score: float
    top_drivers: tuple[dict[str, Any], ...]
    payload: Mapping[str, Any]


def _logistic(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _clamp_probability(value: float) -> float:
    return max(0.0, min(0.95, value))


def signal_group_scores(panel: CountrySignalPanel) -> dict[str, float]:
    """Return weighted 0-100 scores for each signal group in a panel."""
    grouped: dict[str, list[tuple[float, float]]] = {}
    for signal in panel.signals:
        grouped.setdefault(signal.group, []).append((signal.score, signal.weight))

    scores: dict[str, float] = {}
    for group, values in grouped.items():
        weight = sum(item_weight for _, item_weight in values)
        scores[group] = round(
            sum(score * item_weight for score, item_weight in values) / weight,
            2,
        )
    return scores


def horizon_bucket_probabilities(
    signal_score: SignalScore, group_scores: Mapping[str, float], risk_type: str
) -> tuple[float, float, float, float]:
    """Convert signal stress into canonical horizon bucket probabilities."""
    multiplier = RISK_TYPE_MULTIPLIER.get(risk_type, 1.0)
    fast_stress = max(
        group_scores.get("market_financial", 0.0),
        group_scores.get("news_event", 0.0),
    )
    base_probability = _logistic((signal_score.signal_score - 45.0) / 14.0)
    within_18m = round(_clamp_probability(base_probability * 0.65 * multiplier), 4)

    timing_frontload = 0.25 + 0.30 * (fast_stress / 100.0)
    within_6m = round(within_18m * timing_frontload, 4)
    within_12m = round(within_18m * min(0.88, 0.62 + 0.20 * (fast_stress / 100.0)), 4)
    if within_12m < within_6m:
        within_12m = within_6m

    probability_0_6m = within_6m
    probability_6_12m = round(within_12m - within_6m, 4)
    probability_12_18m = round(within_18m - probability_0_6m - probability_6_12m, 4)
    probability_within_18m = round(
        probability_0_6m + probability_6_12m + probability_12_18m, 4
    )
    return (
        probability_0_6m,
        probability_6_12m,
        probability_12_18m,
        probability_within_18m,
    )


def build_baseline_prediction(
    panel: CountrySignalPanel,
    *,
    risk_type: str,
    target_type: str = "country",
    target_id: str | None = None,
) -> BaselinePrediction:
    """Build and validate a canonical prediction from a country signal panel."""
    if risk_type not in {item.value for item in RiskType}:
        msg = f"unsupported risk_type: {risk_type}"
        raise ValueError(msg)

    signal_score = score_signal_panel(panel)
    group_scores = signal_group_scores(panel)
    p_0_6m, p_6_12m, p_12_18m, p_within_18m = horizon_bucket_probabilities(
        signal_score, group_scores, risk_type
    )

    urgency_score = 0.0 if p_within_18m == 0 else round((p_0_6m / p_within_18m) * 100.0, 2)
    impact_score = max(signal_score.signal_score, RISK_TYPE_IMPACT_PRIOR[risk_type])
    risk_score = risk_score_from_components(
        probability_score=p_within_18m * 100.0,
        impact_score=impact_score,
        urgency_score=urgency_score,
        evidence_strength=signal_score.confidence_score * 100.0,
        model_consensus=60.0,
    )
    risk_level = risk_level_for_score(risk_score).value

    drivers = tuple(
        {
            "name": signal.name,
            "group": signal.group,
            "score": signal.score,
            "contribution": signal.contribution,
        }
        for signal in signal_score.top_drivers
    )
    payload: dict[str, Any] = {
        "target_type": target_type,
        "target_id": target_id or panel.country,
        "risk_type": risk_type,
        "as_of_date": panel.date,
        "probability_0_6m": p_0_6m,
        "probability_6_12m": p_6_12m,
        "probability_12_18m": p_12_18m,
        "probability_within_18m": p_within_18m,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "confidence_score": signal_score.confidence_score,
        "model_versions": {
            "signal_foundation": "deterministic-signals.v1",
            "baseline_probability": BASELINE_MODEL_VERSION,
        },
        "top_drivers": list(drivers),
        "historical_analogies": [],
        "evidence_refs": list(signal_score.evidence_refs),
        "what_could_escalate": [],
        "what_could_reduce_risk": [],
    }
    validate_prediction_payload(payload)

    return BaselinePrediction(
        target_type=payload["target_type"],
        target_id=payload["target_id"],
        risk_type=risk_type,
        probability_0_6m=p_0_6m,
        probability_6_12m=p_6_12m,
        probability_12_18m=p_12_18m,
        probability_within_18m=p_within_18m,
        risk_score=risk_score,
        risk_level=risk_level,
        confidence_score=signal_score.confidence_score,
        top_drivers=drivers,
        payload=payload,
    )
