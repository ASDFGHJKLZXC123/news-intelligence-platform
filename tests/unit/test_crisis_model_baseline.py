"""Phase 2 baseline probability model tests."""

from __future__ import annotations

import pytest

from db.models.enums import RiskType
from services.crisis_model import validate_prediction_payload
from services.crisis_model.baseline import (
    build_baseline_prediction,
    horizon_bucket_probabilities,
    signal_group_scores,
)
from services.crisis_model.signals import build_country_signal_panel, score_signal_panel


def _panel(stress: float = 80.0):
    return build_country_signal_panel(
        country="US",
        date="2026-06-17",
        values={
            "credit_to_gdp_gap": 9.0,
            "credit_spread_zscore": 2.4,
            "bank_equity_drawdown_30d": -18.0,
            "banking_stress_news_score": stress,
            "deposit_outflow_news_score": max(0.0, stress - 10),
            "news_velocity_zscore": 2.1,
            "source_diversity_score": 0.8,
        },
        evidence_refs=[
            {"kind": "signal", "id": "country-daily-risk:US:2026-06-17"},
            {"kind": "event", "id": "event-banking-stress"},
        ],
    )


def test_signal_group_scores_are_weighted_by_group() -> None:
    scores = signal_group_scores(_panel())

    assert set(scores) == {"macro_credit", "market_financial", "news_event"}
    assert scores["news_event"] > 70


def test_horizon_bucket_probabilities_are_canonical_and_monotonic() -> None:
    panel = _panel()
    signal_score = score_signal_panel(panel)
    groups = signal_group_scores(panel)

    probabilities = horizon_bucket_probabilities(signal_score, groups, RiskType.BANKING.value)

    assert len(probabilities) == 4
    assert probabilities[0] > 0
    assert sum(probabilities[:3]) == pytest.approx(probabilities[3])
    assert probabilities[0] <= probabilities[0] + probabilities[1] <= probabilities[3]


def test_baseline_prediction_payload_validates_against_phase0_contract() -> None:
    prediction = build_baseline_prediction(_panel(), risk_type=RiskType.BANKING.value)

    validate_prediction_payload(prediction.payload)
    assert prediction.target_type == "country"
    assert prediction.target_id == "US"
    assert prediction.probability_within_18m > prediction.probability_0_6m
    assert prediction.payload["model_versions"]["baseline_probability"]
    assert prediction.payload["top_drivers"]
    assert prediction.payload["evidence_refs"]


def test_baseline_prediction_increases_with_signal_stress() -> None:
    low = build_baseline_prediction(_panel(stress=20.0), risk_type=RiskType.BANKING.value)
    high = build_baseline_prediction(_panel(stress=90.0), risk_type=RiskType.BANKING.value)

    assert high.probability_within_18m > low.probability_within_18m
    assert high.risk_score > low.risk_score


def test_baseline_prediction_rejects_unknown_risk_type() -> None:
    with pytest.raises(ValueError, match="unsupported risk_type"):
        build_baseline_prediction(_panel(), risk_type="financial_crisis")
