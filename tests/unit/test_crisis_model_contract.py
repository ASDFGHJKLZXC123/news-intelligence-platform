"""Phase 0 executable contract checks for the standalone crisis model."""

from __future__ import annotations

import datetime

import pytest

from services.crisis_model import (
    ContractValidationError,
    cumulative_probabilities,
    risk_score_from_components,
    validate_evaluation_payload,
    validate_evidence_refs,
    validate_prediction_payload,
    validate_probability_contract,
)


def _valid_prediction() -> dict[str, object]:
    return {
        "target_type": "country",
        "target_id": "US",
        "risk_type": "banking",
        "as_of_date": datetime.date(2026, 6, 17),
        "probability_0_6m": 0.10,
        "probability_6_12m": 0.14,
        "probability_12_18m": 0.04,
        "probability_within_18m": 0.28,
        "risk_score": 67.0,
        "risk_level": "high",
        "confidence_score": 0.68,
        "model_versions": {"baseline": "phase0-fixture"},
        "evidence_refs": [
            {"kind": "event", "id": "event-1"},
            {"kind": "article", "id": "article-1"},
        ],
    }


def test_probability_contract_uses_buckets_and_monotonic_cumulative_values() -> None:
    prediction = _valid_prediction()

    assert validate_probability_contract(prediction) == pytest.approx((0.10, 0.24, 0.28))
    assert cumulative_probabilities(prediction) == pytest.approx((0.10, 0.24, 0.28))


def test_probability_contract_rejects_bucket_total_mismatch() -> None:
    prediction = _valid_prediction()
    prediction["probability_within_18m"] = 0.30

    with pytest.raises(ContractValidationError, match="sum of the three horizon buckets"):
        validate_probability_contract(prediction)


def test_probability_contract_rejects_invalid_probability_range() -> None:
    prediction = _valid_prediction()
    prediction["probability_6_12m"] = -0.01

    with pytest.raises(ContractValidationError, match=r"probability_6_12m must be in"):
        validate_probability_contract(prediction)


def test_evidence_refs_require_non_empty_kind_and_id_pairs() -> None:
    validate_evidence_refs([{"kind": "signal", "id": "country-daily-risk:US:2026-06-17"}])

    with pytest.raises(ContractValidationError, match="must not be empty"):
        validate_evidence_refs([])
    with pytest.raises(ContractValidationError, match=r"kind must be one of"):
        validate_evidence_refs([{"kind": "unsupported", "id": "x"}])
    with pytest.raises(ContractValidationError, match=r"id must be a non-empty string"):
        validate_evidence_refs([{"kind": "event", "id": ""}])


def test_risk_score_formula_matches_model_design_weights() -> None:
    score = risk_score_from_components(
        probability_score=28,
        impact_score=95,
        urgency_score=70,
        evidence_strength=85,
        model_consensus=55,
    )

    assert score == 62.8


def test_prediction_payload_contract_accepts_valid_minimum_shape() -> None:
    validate_prediction_payload(_valid_prediction())


def test_prediction_payload_rejects_risk_level_mismatch() -> None:
    prediction = _valid_prediction()
    prediction["risk_level"] = "medium"

    with pytest.raises(ContractValidationError, match="risk_level must match risk_score band"):
        validate_prediction_payload(prediction)


def test_prediction_payload_requires_model_versions_and_evidence() -> None:
    prediction = _valid_prediction()
    prediction["model_versions"] = {}

    with pytest.raises(ContractValidationError, match="model_versions must be a non-empty object"):
        validate_prediction_payload(prediction)

    prediction = _valid_prediction()
    prediction["evidence_refs"] = []

    with pytest.raises(ContractValidationError, match="evidence_refs must not be empty"):
        validate_prediction_payload(prediction)


def test_evaluation_payload_contract_accepts_required_shape_and_optional_metrics() -> None:
    validate_evaluation_payload(
        {
            "prediction_id": "prediction-1",
            "evaluation_date": "2026-12-17",
            "horizon": "0_6m",
            "actual_outcome": False,
            "brier_score": 0.09,
            "log_loss": 0.12,
            "lead_time_days": 0,
        }
    )


def test_evaluation_payload_rejects_invalid_horizon_and_metric_ranges() -> None:
    with pytest.raises(ContractValidationError, match="horizon must be one of"):
        validate_evaluation_payload(
            {
                "prediction_id": "prediction-1",
                "evaluation_date": "2026-12-17",
                "horizon": "12m",
            }
        )

    with pytest.raises(ContractValidationError, match=r"brier_score must be in"):
        validate_evaluation_payload(
            {
                "prediction_id": "prediction-1",
                "evaluation_date": "2026-12-17",
                "horizon": "0_6m",
                "brier_score": 1.1,
            }
        )
