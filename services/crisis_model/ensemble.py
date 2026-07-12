"""Ensemble helpers for standalone crisis-model component forecasts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import pstdev
from typing import Final

from services.crisis_model.evidence import dedupe_evidence_refs, validate_component_evidence
from services.crisis_model.types import ComponentForecast, HorizonBuckets

ENSEMBLE_MODEL_VERSION: Final[str] = "logit-ensemble.v1"


@dataclass(frozen=True)
class EnsembleWeights:
    """Static component weights for the MVP logit ensemble."""

    weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "baseline": 0.55,
            "event_shock": 0.25,
            "historical_analogy": 0.20,
        }
    )

    def weight_for(self, component: str) -> float:
        return max(0.0, float(self.weights.get(component, 0.0)))


def _clamp_probability(probability: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, probability))


def _logit(probability: float) -> float:
    p = _clamp_probability(probability)
    return math.log(p / (1.0 - p))


def _inverse_logit(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def component_disagreement(components: Sequence[ComponentForecast]) -> float:
    """Return population stddev of within-18m component probabilities."""
    if len(components) < 2:
        return 0.0
    return round(pstdev(component.probability_within_18m for component in components), 6)


def _combine_probability(values: Sequence[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in values)
    if total_weight <= 0:
        return 0.0
    combined_logit = sum(_logit(probability) * weight for probability, weight in values) / total_weight
    return round(_inverse_logit(combined_logit), 6)


def combine_component_forecasts(
    components: Sequence[ComponentForecast],
    weights: EnsembleWeights | None = None,
) -> ComponentForecast:
    """Combine component forecasts into one ensemble component."""
    if not components:
        msg = "at least one component forecast is required"
        raise ValueError(msg)
    risk_types = {component.risk_type for component in components}
    if len(risk_types) != 1:
        msg = "all component forecasts must share the same risk_type"
        raise ValueError(msg)
    weights = weights or EnsembleWeights()
    for component in components:
        validate_component_evidence(component)

    p_0_6m = _combine_probability(
        [(component.buckets.probability_0_6m, weights.weight_for(component.component)) for component in components]
    )
    p_within_12m = _combine_probability(
        [(component.buckets.cumulative_12m, weights.weight_for(component.component)) for component in components]
    )
    p_within_18m = _combine_probability(
        [(component.probability_within_18m, weights.weight_for(component.component)) for component in components]
    )
    p_within_12m = max(p_0_6m, min(p_within_12m, p_within_18m))
    buckets = HorizonBuckets(
        p_0_6m,
        round(p_within_12m - p_0_6m, 6),
        round(p_within_18m - p_within_12m, 6),
    )

    disagreement = component_disagreement(components)
    mean_confidence = sum(component.confidence_score for component in components) / len(components)
    confidence = round(max(0.0, min(1.0, mean_confidence * (1.0 - disagreement))), 4)
    evidence_strength = round(
        min(1.0, sum(component.evidence_strength for component in components) / len(components)),
        4,
    )
    evidence_refs = dedupe_evidence_refs(
        [ref for component in components for ref in component.evidence_refs]
    )
    top_drivers = tuple(
        driver for component in components for driver in component.top_drivers[:3]
    )
    versions = ", ".join(f"{component.component}:{component.model_version}" for component in components)

    return ComponentForecast(
        component="ensemble",
        risk_type=components[0].risk_type,
        buckets=buckets,
        confidence_score=confidence,
        evidence_strength=evidence_strength,
        model_version=ENSEMBLE_MODEL_VERSION,
        top_drivers=top_drivers,
        evidence_refs=evidence_refs,
        notes=(f"combined {len(components)} component(s)", f"component_versions={versions}"),
    )
