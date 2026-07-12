"""Phase 3 structured news/event shock model tests."""

from __future__ import annotations

from db.models.enums import RiskType
from services.crisis_model.contract import validate_evidence_refs
from services.crisis_model.event_shock import (
    RuleBasedNewsEventShockModel,
    aggregate_event_shocks,
    coerce_event_risk_input,
    news_event_bucket_probabilities,
    score_event_intensity,
)


def _event(event_id: str, *, severity: float, observed_at: str = "2026-06-16", future: bool = False):
    return {
        "event_id": event_id,
        "country": "US",
        "risk_type": RiskType.BANKING.value,
        "event_type": "banking_stress",
        "mechanism": "deposit_outflow",
        "severity_score": severity,
        "velocity_zscore": 2.4,
        "source_diversity_score": 0.8,
        "source_authority_score": 0.7,
        "official_confirmation": True,
        "rumor_risk_score": 10.0,
        "market_relevance_score": 85.0,
        "macro_relevance_score": 65.0,
        "observed_at": "2026-06-20" if future else observed_at,
        "evidence_article_ids": ["article-1", "article-2"],
    }


def test_score_event_intensity_increases_with_severity_and_velocity() -> None:
    low_event = _event("e-low", severity=25.0)
    high_event = _event("e-high", severity=90.0)

    assert score_event_intensity(coerce_event_risk_input(high_event)) > score_event_intensity(
        coerce_event_risk_input(low_event)
    )


def test_aggregate_event_shocks_filters_future_events_and_dedupes_evidence() -> None:
    features = aggregate_event_shocks(
        target_id="US",
        risk_type=RiskType.BANKING.value,
        as_of_date="2026-06-17",
        events=[
            _event("event-1", severity=80.0),
            _event("event-1", severity=80.0),
            _event("future", severity=100.0, future=True),
            {**_event("other-country", severity=100.0), "country": "CA"},
        ],
    )

    assert features.event_count == 2
    assert features.intensity_score > 0
    assert {"kind": "event", "id": "event-1"} in features.evidence_refs
    validate_evidence_refs(features.evidence_refs)
    assert {"kind": "event", "id": "future"} not in features.evidence_refs


def test_news_event_bucket_probabilities_are_short_horizon_weighted() -> None:
    features = aggregate_event_shocks(
        target_id="US",
        risk_type=RiskType.BANKING.value,
        as_of_date="2026-06-17",
        events=[_event("event-1", severity=85.0)],
    )
    buckets = news_event_bucket_probabilities(features)

    assert buckets.probability_0_6m > buckets.probability_6_12m
    assert buckets.probability_within_18m > 0
    assert sum(buckets.as_payload()[key] for key in [
        "probability_0_6m",
        "probability_6_12m",
        "probability_12_18m",
    ]) == buckets.as_payload()["probability_within_18m"]


def test_rule_based_news_event_shock_model_emits_component_forecast() -> None:
    forecast = RuleBasedNewsEventShockModel().predict(
        target_id="US",
        risk_type=RiskType.BANKING.value,
        as_of_date="2026-06-17",
        events=[_event("event-1", severity=85.0)],
    )

    assert forecast.component == "event_shock"
    assert forecast.probability_within_18m > 0
    assert forecast.confidence_score > 0
    assert forecast.evidence_strength > 0
    assert forecast.top_drivers
    validate_evidence_refs(forecast.evidence_refs)


def test_empty_event_shock_component_is_neutral() -> None:
    forecast = RuleBasedNewsEventShockModel().predict(
        target_id="US",
        risk_type=RiskType.BANKING.value,
        as_of_date="2026-06-17",
        events=[],
    )

    assert forecast.probability_within_18m == 0.0
    assert forecast.confidence_score == 0.0
    assert forecast.evidence_refs == ()
