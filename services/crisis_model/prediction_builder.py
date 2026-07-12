"""Final canonical prediction payload builder for Track M."""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any, Final

from db.models.enums import risk_level_for_score
from services.crisis_model.baseline import RISK_TYPE_IMPACT_PRIOR
from services.crisis_model.contract import risk_score_from_components, validate_prediction_payload
from services.crisis_model.evidence import dedupe_evidence_refs
from services.crisis_model.types import ComponentForecast

PREDICTION_BUILDER_VERSION: Final[str] = "prediction-builder.v1"


def build_prediction_payload(
    *,
    target_type: str,
    target_id: str,
    risk_type: str,
    as_of_date: datetime.date | str,
    ensemble: ComponentForecast,
    components: Sequence[ComponentForecast] = (),
    extra_model_versions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build and validate a final `crisis_predictions` payload."""
    if ensemble.risk_type != risk_type:
        msg = "ensemble risk_type must match prediction risk_type"
        raise ValueError(msg)
    buckets = ensemble.buckets.as_payload()
    impact_score = max(RISK_TYPE_IMPACT_PRIOR.get(risk_type, 70.0), ensemble.probability_within_18m * 100.0)
    urgency_score = 0.0
    if ensemble.probability_within_18m:
        urgency_score = round((ensemble.buckets.probability_0_6m / ensemble.probability_within_18m) * 100, 2)
    risk_score = risk_score_from_components(
        probability_score=ensemble.probability_within_18m * 100.0,
        impact_score=impact_score,
        urgency_score=urgency_score,
        evidence_strength=ensemble.evidence_strength * 100.0,
        model_consensus=ensemble.confidence_score * 100.0,
    )
    model_versions = {
        "ensemble": ensemble.model_version,
        "prediction_builder": PREDICTION_BUILDER_VERSION,
    }
    for component in components:
        model_versions[component.component] = component.model_version
    if extra_model_versions:
        model_versions.update(extra_model_versions)
    evidence_refs = dedupe_evidence_refs(
        [*ensemble.evidence_refs, *(ref for component in components for ref in component.evidence_refs)]
    )
    payload: dict[str, Any] = {
        "target_type": target_type,
        "target_id": target_id,
        "risk_type": risk_type,
        "as_of_date": as_of_date,
        **buckets,
        "risk_score": risk_score,
        "risk_level": risk_level_for_score(risk_score).value,
        "confidence_score": ensemble.confidence_score,
        "model_versions": model_versions,
        "top_drivers": list(ensemble.top_drivers),
        "historical_analogies": [
            driver for driver in ensemble.top_drivers if driver.get("component") == "historical_analogy"
        ],
        "evidence_refs": list(evidence_refs),
        "what_could_escalate": [],
        "what_could_reduce_risk": [],
    }
    validate_prediction_payload(payload)
    return payload
