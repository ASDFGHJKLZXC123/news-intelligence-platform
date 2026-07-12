"""Executable contract for standalone Track M crisis-model payloads.

This module deliberately contains no model inference and no API wiring. It locks the
Phase 0 semantics that future model components must satisfy before integration.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

from db.models.enums import Horizon, RiskLevel, RiskType, risk_level_for_score

PROBABILITY_FIELDS: Final[tuple[str, ...]] = (
    "probability_0_6m",
    "probability_6_12m",
    "probability_12_18m",
    "probability_within_18m",
)

PREDICTION_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "target_type",
    "target_id",
    "risk_type",
    "as_of_date",
    *PROBABILITY_FIELDS,
    "risk_score",
    "risk_level",
    "confidence_score",
    "model_versions",
    "evidence_refs",
)

EVALUATION_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "prediction_id",
    "evaluation_date",
    "horizon",
)

TARGET_TYPES: Final[frozenset[str]] = frozenset({"country", "region", "industry", "company"})
EVIDENCE_REF_KINDS: Final[frozenset[str]] = frozenset(
    {"article", "event", "signal", "model_run", "historical_case"}
)

RISK_SCORE_WEIGHTS: Final[Mapping[str, float]] = {
    "probability_score": 0.35,
    "impact_score": 0.30,
    "urgency_score": 0.15,
    "evidence_strength": 0.10,
    "model_consensus": 0.10,
}


class ContractValidationError(ValueError):
    """Raised when a prediction/evaluation payload violates the canonical contract."""


def _require_fields(payload: Mapping[str, Any], fields: Sequence[str]) -> None:
    missing = [field for field in fields if field not in payload]
    if missing:
        msg = "missing required field(s): " + ", ".join(missing)
        raise ContractValidationError(msg)


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        msg = f"{field} must be numeric, not bool"
        raise ContractValidationError(msg)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        msg = f"{field} must be numeric"
        raise ContractValidationError(msg) from exc
    if not math.isfinite(parsed):
        msg = f"{field} must be finite"
        raise ContractValidationError(msg)
    return parsed


def _bounded(value: Any, field: str, low: float, high: float) -> float:
    parsed = _number(value, field)
    if parsed < low or parsed > high:
        msg = f"{field} must be in [{low}, {high}]"
        raise ContractValidationError(msg)
    return parsed


def _require_non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{field} must be a non-empty string"
        raise ContractValidationError(msg)
    return value


def cumulative_probabilities(payload: Mapping[str, Any]) -> tuple[float, float, float]:
    """Return cumulative 6m/12m/18m probabilities from canonical bucket fields."""
    _require_fields(payload, PROBABILITY_FIELDS)
    p_0_6m = _bounded(payload["probability_0_6m"], "probability_0_6m", 0.0, 1.0)
    p_6_12m = _bounded(payload["probability_6_12m"], "probability_6_12m", 0.0, 1.0)
    p_12_18m = _bounded(payload["probability_12_18m"], "probability_12_18m", 0.0, 1.0)
    _bounded(payload["probability_within_18m"], "probability_within_18m", 0.0, 1.0)
    return (p_0_6m, p_0_6m + p_6_12m, p_0_6m + p_6_12m + p_12_18m)


def validate_probability_contract(
    payload: Mapping[str, Any], *, tolerance: float = 1e-6
) -> tuple[float, float, float]:
    """Validate canonical probability buckets and return cumulative probabilities."""
    _require_fields(payload, PROBABILITY_FIELDS)
    p_0_6m = _bounded(payload["probability_0_6m"], "probability_0_6m", 0.0, 1.0)
    p_6_12m = _bounded(payload["probability_6_12m"], "probability_6_12m", 0.0, 1.0)
    p_12_18m = _bounded(payload["probability_12_18m"], "probability_12_18m", 0.0, 1.0)
    p_within_18m = _bounded(
        payload["probability_within_18m"], "probability_within_18m", 0.0, 1.0
    )

    bucket_total = p_0_6m + p_6_12m + p_12_18m
    if abs(bucket_total - p_within_18m) > tolerance:
        msg = "probability_within_18m must equal the sum of the three horizon buckets"
        raise ContractValidationError(msg)

    cumulative_6m = p_0_6m
    cumulative_12m = p_0_6m + p_6_12m
    if cumulative_6m - cumulative_12m > tolerance or cumulative_12m - p_within_18m > tolerance:
        msg = "cumulative crisis probabilities must be monotonic across horizons"
        raise ContractValidationError(msg)
    return (cumulative_6m, cumulative_12m, p_within_18m)


def validate_evidence_refs(evidence_refs: Any) -> None:
    """Validate the minimal evidence-ref JSON shape used by crisis predictions.

    The canonical schema stores refs as JSONB. Phase 0 locks a small v1 shape:
    ``{"kind": "...", "id": "..."}``.
    """
    if isinstance(evidence_refs, (str, bytes)) or not isinstance(evidence_refs, Sequence):
        msg = "evidence_refs must be a non-empty sequence"
        raise ContractValidationError(msg)
    if not evidence_refs:
        msg = "evidence_refs must not be empty"
        raise ContractValidationError(msg)

    for index, ref in enumerate(evidence_refs):
        if not isinstance(ref, Mapping):
            msg = f"evidence_refs[{index}] must be an object"
            raise ContractValidationError(msg)
        kind = ref.get("kind")
        if kind not in EVIDENCE_REF_KINDS:
            msg = f"evidence_refs[{index}].kind must be one of {sorted(EVIDENCE_REF_KINDS)}"
            raise ContractValidationError(msg)
        _require_non_empty_string(ref.get("id"), f"evidence_refs[{index}].id")


def risk_score_from_components(
    *,
    probability_score: float,
    impact_score: float,
    urgency_score: float,
    evidence_strength: float,
    model_consensus: float,
) -> float:
    """Compute the documented 0-100 risk score from normalized 0-100 components."""
    values = {
        "probability_score": probability_score,
        "impact_score": impact_score,
        "urgency_score": urgency_score,
        "evidence_strength": evidence_strength,
        "model_consensus": model_consensus,
    }
    weighted = 0.0
    for field, weight in RISK_SCORE_WEIGHTS.items():
        weighted += weight * _bounded(values[field], field, 0.0, 100.0)
    return round(weighted, 2)


def validate_prediction_payload(payload: Mapping[str, Any]) -> None:
    """Validate the minimum canonical payload every Track M prediction must carry."""
    _require_fields(payload, PREDICTION_REQUIRED_FIELDS)

    target_type = _require_non_empty_string(payload["target_type"], "target_type")
    if target_type not in TARGET_TYPES:
        msg = f"target_type must be one of {sorted(TARGET_TYPES)}"
        raise ContractValidationError(msg)
    _require_non_empty_string(payload["target_id"], "target_id")

    if payload["risk_type"] not in {risk_type.value for risk_type in RiskType}:
        msg = f"risk_type must be one of {[risk_type.value for risk_type in RiskType]}"
        raise ContractValidationError(msg)

    if not isinstance(payload["as_of_date"], (datetime.date, str)):
        msg = "as_of_date must be a date or ISO date string"
        raise ContractValidationError(msg)

    validate_probability_contract(payload)

    risk_score = _bounded(payload["risk_score"], "risk_score", 0.0, 100.0)
    risk_level = _require_non_empty_string(payload["risk_level"], "risk_level")
    if risk_level not in {level.value for level in RiskLevel}:
        msg = f"risk_level must be one of {[level.value for level in RiskLevel]}"
        raise ContractValidationError(msg)
    expected_level = risk_level_for_score(risk_score).value
    if risk_level != expected_level:
        msg = f"risk_level must match risk_score band: expected {expected_level}"
        raise ContractValidationError(msg)

    _bounded(payload["confidence_score"], "confidence_score", 0.0, 1.0)
    if not isinstance(payload["model_versions"], Mapping) or not payload["model_versions"]:
        msg = "model_versions must be a non-empty object"
        raise ContractValidationError(msg)
    validate_evidence_refs(payload["evidence_refs"])


def validate_evaluation_payload(payload: Mapping[str, Any]) -> None:
    """Validate the required post-horizon evaluation payload shape."""
    _require_fields(payload, EVALUATION_REQUIRED_FIELDS)
    _require_non_empty_string(str(payload["prediction_id"]), "prediction_id")
    if payload["horizon"] not in {horizon.value for horizon in Horizon}:
        msg = f"horizon must be one of {[horizon.value for horizon in Horizon]}"
        raise ContractValidationError(msg)
    if not isinstance(payload["evaluation_date"], (datetime.date, str)):
        msg = "evaluation_date must be a date or ISO date string"
        raise ContractValidationError(msg)

    if "actual_outcome" in payload and payload["actual_outcome"] is not None:
        if not isinstance(payload["actual_outcome"], bool):
            msg = "actual_outcome must be bool or null"
            raise ContractValidationError(msg)
    if "brier_score" in payload and payload["brier_score"] is not None:
        _bounded(payload["brier_score"], "brier_score", 0.0, 1.0)
    if "log_loss" in payload and payload["log_loss"] is not None:
        _bounded(payload["log_loss"], "log_loss", 0.0, math.inf)
    if "lead_time_days" in payload and payload["lead_time_days"] is not None:
        lead_time_days = _number(payload["lead_time_days"], "lead_time_days")
        if lead_time_days < 0 or not float(lead_time_days).is_integer():
            msg = "lead_time_days must be a non-negative integer"
            raise ContractValidationError(msg)
