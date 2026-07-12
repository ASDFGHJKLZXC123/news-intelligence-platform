"""Phase 5 ensemble and final prediction builder tests."""

from __future__ import annotations

import datetime

from db.models.enums import RiskType
from services.crisis_model.contract import validate_prediction_payload
from services.crisis_model.ensemble import (
    EnsembleWeights,
    combine_component_forecasts,
    component_disagreement,
)
from services.crisis_model.prediction_builder import build_prediction_payload
from services.crisis_model.types import ComponentForecast, HorizonBuckets


def _component(name: str, within: float, confidence: float = 0.8) -> ComponentForecast:
    return ComponentForecast(
        component=name,
        risk_type=RiskType.BANKING.value,
        buckets=HorizonBuckets(within * 0.45, within * 0.35, within * 0.20),
        confidence_score=confidence,
        evidence_strength=0.75,
        model_version=f"{name}.v1",
        top_drivers=(
            {
                "component": name,
                "signal": f"{name}_driver",
                "score": within * 100,
                "evidence_refs": [{"kind": "signal", "id": f"{name}:signal"}],
            },
        ),
        evidence_refs=({"kind": "signal", "id": f"{name}:signal"},),
    )


def test_component_disagreement_measures_probability_spread() -> None:
    assert component_disagreement([_component("baseline", 0.10), _component("event_shock", 0.50)]) > 0
    assert component_disagreement([_component("baseline", 0.10)]) == 0.0


def test_combine_component_forecasts_produces_contract_valid_buckets_and_evidence() -> None:
    forecast = combine_component_forecasts(
        [
            _component("baseline", 0.22),
            _component("event_shock", 0.30),
            _component("historical_analogy", 0.16),
        ],
        EnsembleWeights(),
    )

    payload = forecast.buckets.as_payload()
    assert forecast.component == "ensemble"
    assert payload["probability_0_6m"] > 0
    assert payload["probability_within_18m"] > payload["probability_0_6m"]
    assert forecast.confidence_score > 0
    assert len(forecast.evidence_refs) == 3
    assert forecast.top_drivers


def test_build_prediction_payload_validates_against_phase0_contract() -> None:
    components = [
        _component("baseline", 0.22),
        _component("event_shock", 0.30),
        _component("historical_analogy", 0.16),
    ]
    ensemble = combine_component_forecasts(components)

    payload = build_prediction_payload(
        target_type="country",
        target_id="US",
        risk_type=RiskType.BANKING.value,
        as_of_date=datetime.date(2026, 6, 17),
        ensemble=ensemble,
        components=components,
    )

    validate_prediction_payload(payload)
    assert payload["model_versions"]["ensemble"]
    assert payload["model_versions"]["baseline"]
    assert payload["evidence_refs"]
    assert payload["risk_level"] in {"low", "medium", "high", "critical"}
