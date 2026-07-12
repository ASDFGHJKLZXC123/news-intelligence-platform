"""Shared value objects for standalone crisis-model components."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from services.crisis_model.contract import validate_probability_contract


@dataclass(frozen=True)
class HorizonBuckets:
    """Canonical probability buckets used by Track M prediction payloads."""

    probability_0_6m: float
    probability_6_12m: float
    probability_12_18m: float

    @property
    def probability_within_18m(self) -> float:
        return round(
            self.probability_0_6m + self.probability_6_12m + self.probability_12_18m,
            6,
        )

    @property
    def cumulative_12m(self) -> float:
        return round(self.probability_0_6m + self.probability_6_12m, 6)

    def as_payload(self) -> dict[str, float]:
        payload = {
            "probability_0_6m": round(self.probability_0_6m, 6),
            "probability_6_12m": round(self.probability_6_12m, 6),
            "probability_12_18m": round(self.probability_12_18m, 6),
            "probability_within_18m": self.probability_within_18m,
        }
        validate_probability_contract(payload, tolerance=1e-5)
        return payload


@dataclass(frozen=True)
class ComponentForecast:
    """A standalone forecast component before final ensemble/payload construction."""

    component: str
    risk_type: str
    buckets: HorizonBuckets
    confidence_score: float
    evidence_strength: float
    model_version: str
    top_drivers: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    evidence_refs: tuple[Mapping[str, str], ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def probability_within_18m(self) -> float:
        return self.buckets.probability_within_18m

    def as_component_payload(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "risk_type": self.risk_type,
            **self.buckets.as_payload(),
            "confidence_score": self.confidence_score,
            "evidence_strength": self.evidence_strength,
            "model_version": self.model_version,
            "top_drivers": list(self.top_drivers),
            "evidence_refs": list(self.evidence_refs),
            "notes": list(self.notes),
        }
