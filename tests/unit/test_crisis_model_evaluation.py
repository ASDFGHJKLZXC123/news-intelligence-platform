"""Phase 5 evaluation metric tests."""

from __future__ import annotations

import datetime

from services.crisis_model.evaluation import (
    brier_score,
    evaluate_prediction,
    lead_time_days,
    log_loss,
)


def _prediction():
    return {
        "as_of_date": datetime.date(2026, 6, 17),
        "probability_0_6m": 0.60,
        "probability_6_12m": 0.15,
        "probability_12_18m": 0.05,
        "probability_within_18m": 0.80,
    }


def test_brier_score_and_log_loss_are_exact_on_simple_fixtures() -> None:
    assert brier_score(0.25, False) == 0.0625
    assert brier_score(0.75, True) == 0.0625
    assert log_loss(0.5, True) == 0.693147


def test_lead_time_days_requires_alert_and_actual_start() -> None:
    assert lead_time_days(
        prediction_date=datetime.date(2026, 1, 1),
        actual_start_date=datetime.date(2026, 2, 1),
        alerted=True,
    ) == 31
    assert lead_time_days(
        prediction_date=datetime.date(2026, 1, 1),
        actual_start_date=datetime.date(2026, 2, 1),
        alerted=False,
    ) is None


def test_evaluate_prediction_builds_phase0_valid_payload() -> None:
    payload = evaluate_prediction(
        _prediction(),
        prediction_id="prediction-1",
        horizon="0_6m",
        evaluation_date="2026-12-17",
        actual_outcome=False,
        threshold=0.50,
    )

    assert payload["brier_score"] == 0.36
    assert payload["false_positive"] is True
    assert payload["false_negative"] is False
