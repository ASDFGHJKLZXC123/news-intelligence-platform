"""Signal fusion/rating-driver tests."""

from __future__ import annotations

import datetime

import pytest

from services.risk.signal_fusion import (
    RiskSignalInput,
    build_rating_driver_payload,
    build_rating_payload,
    signals_from_country_context,
)


def _ref(id_value: str) -> dict[str, str]:
    return {"kind": "signal", "id": id_value}


def test_rating_payload_preserves_opposing_drivers_and_changed_since_previous() -> None:
    previous = {
        "risk_score": 40.0,
        "top_drivers": [
            {"driver_id": "macro:inflation_pressure", "score": 40.0},
            {"driver_id": "energy:energy_price_stress", "score": 30.0},
        ],
    }

    payload = build_rating_payload(
        target_type="country",
        target_id="US",
        risk_type="sovereign",
        as_of_date=datetime.date(2026, 6, 17),
        previous_payload=previous,
        signals=[
            RiskSignalInput(
                name="inflation_pressure",
                group="macro",
                score=80.0,
                weight=2.0,
                evidence_refs=(_ref("indicator:inflation"),),
            ),
            {
                "name": "energy_price_stress",
                "group": "energy",
                "score": 30.0,
                "direction": "lowers_risk",
                "evidence_refs": [_ref("energy:price")],
            },
        ],
    )

    drivers = {driver.driver_id: driver for driver in payload.top_drivers}
    assert payload.risk_score == 43.33
    assert payload.risk_level == "medium"
    assert payload.changed_since_previous["previous_risk_score"] == 40.0
    assert payload.changed_since_previous["risk_score_delta"] == 3.33
    assert drivers["macro:inflation_pressure"].changed_since_previous == 40.0
    assert drivers["energy:energy_price_stress"].contribution == -30.0
    assert drivers["energy:energy_price_stress"].direction == "lowers_risk"
    assert len(payload.evidence_refs) == 2


def test_rating_payload_keeps_duplicate_signal_names_as_separate_drivers() -> None:
    payload = build_rating_driver_payload(
        target_type="country",
        target_id="US",
        risk_type="geopolitical_supply_chain",
        as_of_date="2026-06-17",
        signals=[
            {
                "name": "energy_price_stress",
                "group": "energy",
                "score": 70.0,
                "evidence_refs": [_ref("energy:oil")],
            },
            {
                "name": "energy_price_stress",
                "group": "energy",
                "score": 20.0,
                "direction": "lowers_risk",
                "evidence_refs": [_ref("energy:gas")],
            },
        ],
    )

    driver_ids = [driver["driver_id"] for driver in payload["top_drivers"]]
    assert driver_ids == ["energy:energy_price_stress", "energy:energy_price_stress:2"]
    assert payload["top_drivers"][1]["contribution"] == -20.0


def test_rating_payload_rejects_signals_without_evidence() -> None:
    with pytest.raises(ValueError, match="risk signals require evidence refs"):
        build_rating_payload(
            target_type="country",
            target_id="US",
            risk_type="sovereign",
            as_of_date="2026-06-17",
            signals=[{"name": "inflation_pressure", "group": "macro", "score": 80.0}],
        )


def test_signals_from_country_context_payload() -> None:
    signals = signals_from_country_context(
        {
            "risk_signals": {
                "inflation_pressure": {
                    "name": "inflation_pressure",
                    "group": "sovereign_context",
                    "score": 32.0,
                    "evidence_refs": [_ref("indicator:inflation")],
                }
            }
        }
    )

    assert len(signals) == 1
    assert signals[0].driver_id == "sovereign_context:inflation_pressure"
