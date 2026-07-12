"""Forecast evaluation metrics for standalone Track M predictions."""

from __future__ import annotations

import datetime
import math
from collections.abc import Mapping
from typing import Any

from services.crisis_model.contract import validate_evaluation_payload


def brier_score(probability: float, actual: bool) -> float:
    """Single-observation Brier score."""
    return round((float(probability) - (1.0 if actual else 0.0)) ** 2, 6)


def log_loss(probability: float, actual: bool, *, eps: float = 1e-6) -> float:
    """Single-observation binary log loss."""
    p = max(eps, min(1.0 - eps, float(probability)))
    loss = -math.log(p if actual else 1.0 - p)
    return round(loss, 6)


def lead_time_days(
    *,
    prediction_date: datetime.date,
    actual_start_date: datetime.date | None,
    alerted: bool,
) -> int | None:
    """Days of warning lead time when an alert fired before an actual crisis."""
    if not alerted or actual_start_date is None:
        return None
    return max(0, (actual_start_date - prediction_date).days)


def probability_for_horizon(prediction: Mapping[str, Any], horizon: str) -> float:
    mapping = {
        "0_6m": "probability_0_6m",
        "6_12m": "probability_6_12m",
        "12_18m": "probability_12_18m",
        "within_18m": "probability_within_18m",
    }
    if horizon not in mapping:
        msg = f"unsupported horizon: {horizon}"
        raise ValueError(msg)
    return float(prediction[mapping[horizon]])


def evaluate_prediction(
    prediction: Mapping[str, Any],
    *,
    prediction_id: str,
    horizon: str,
    evaluation_date: datetime.date | str,
    actual_outcome: bool,
    actual_start_date: datetime.date | None = None,
    threshold: float = 0.50,
) -> dict[str, Any]:
    """Build and validate a `crisis_prediction_evaluations` payload."""
    probability = probability_for_horizon(prediction, horizon)
    alerted = probability >= threshold
    as_of = prediction["as_of_date"]
    prediction_date = as_of if isinstance(as_of, datetime.date) else datetime.date.fromisoformat(str(as_of))
    payload = {
        "prediction_id": prediction_id,
        "evaluation_date": evaluation_date,
        "horizon": horizon,
        "actual_outcome": actual_outcome,
        "actual_start_date": actual_start_date,
        "brier_score": brier_score(probability, actual_outcome),
        "log_loss": log_loss(probability, actual_outcome),
        "false_positive": alerted and not actual_outcome,
        "false_negative": (not alerted) and actual_outcome,
        "lead_time_days": lead_time_days(
            prediction_date=prediction_date,
            actual_start_date=actual_start_date,
            alerted=alerted,
        ),
    }
    validate_evaluation_payload(payload)
    return payload
