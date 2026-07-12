"""Phase 1 deterministic signal foundation tests."""

from __future__ import annotations

import datetime

import pytest

from db.models.enums import RiskLevel
from services.crisis_model.signals import (
    SIGNAL_DEFINITIONS,
    build_country_signal_panel,
    normalize_signal_value,
    score_signal_panel,
)


def test_normalize_signal_value_respects_direction_and_scale() -> None:
    credit = SIGNAL_DEFINITIONS["credit_to_gdp_gap"]
    reserves = SIGNAL_DEFINITIONS["fx_reserve_change_3m"]

    assert normalize_signal_value(6.0, credit) == 50.0
    assert normalize_signal_value(12.0, credit) == 100.0
    assert normalize_signal_value(-5.0, reserves) == 50.0
    assert normalize_signal_value(5.0, reserves) == 0.0


def test_build_country_signal_panel_normalizes_available_values_only() -> None:
    panel = build_country_signal_panel(
        country="US",
        date="2026-06-17",
        values={
            "credit_to_gdp_gap": 6.0,
            "bank_equity_drawdown_30d": -12.5,
            "news_velocity_zscore": 1.5,
            "unknown_future_signal": 99,
        },
        evidence_refs=[{"kind": "signal", "id": "country-daily-risk:US:2026-06-17"}],
    )

    assert panel.country == "US"
    assert panel.date == datetime.date(2026, 6, 17)
    assert [signal.name for signal in panel.signals] == [
        "credit_to_gdp_gap",
        "bank_equity_drawdown_30d",
        "news_velocity_zscore",
    ]
    assert panel.coverage_ratio == pytest.approx(3 / len(SIGNAL_DEFINITIONS), abs=0.0001)


def test_build_country_signal_panel_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="country must be a non-empty string"):
        build_country_signal_panel(country="", date="2026-06-17", values={})

    with pytest.raises(ValueError, match="credit_to_gdp_gap must be numeric"):
        build_country_signal_panel(
            country="US",
            date="2026-06-17",
            values={"credit_to_gdp_gap": "not-a-number"},
        )


def test_score_signal_panel_computes_weighted_score_and_top_drivers() -> None:
    panel = build_country_signal_panel(
        country="US",
        date="2026-06-17",
        values={
            "credit_to_gdp_gap": 8.0,
            "credit_spread_zscore": 2.4,
            "banking_stress_news_score": 80.0,
            "deposit_outflow_news_score": 70.0,
            "source_diversity_score": 0.75,
        },
        evidence_refs=[
            {"kind": "signal", "id": "country-daily-risk:US:2026-06-17"},
            {"kind": "event", "id": "event-banking-stress"},
        ],
    )

    score = score_signal_panel(panel, top_n=3)

    assert score.country == "US"
    assert score.signal_score > 70
    assert score.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
    assert 0.0 < score.confidence_score <= 1.0
    assert len(score.top_drivers) == 3
    assert score.top_drivers[0].contribution >= score.top_drivers[1].contribution
    assert score.evidence_refs == panel.evidence_refs


def test_score_signal_panel_handles_empty_panel() -> None:
    panel = build_country_signal_panel(country="US", date="2026-06-17", values={})

    score = score_signal_panel(panel)

    assert score.signal_score == 0.0
    assert score.risk_level is RiskLevel.LOW
    assert score.confidence_score == 0.0
    assert score.top_drivers == ()
