"""Derived risk-intelligence API tests."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from apps.api.main import app


def _post(path: str, payload: dict[str, Any]):
    return TestClient(app, client=("127.0.0.1", 5000)).post(path, json=payload)


def test_evidence_graph_endpoint_builds_nodes_and_edges() -> None:
    resp = _post(
        "/api/v1/signals/evidence-graph",
        {
            "facts": [
                {
                    "kind": "energy_snapshot",
                    "provider": "eia",
                    "fact_id": "US:oil:2026-06-17",
                    "label": "US oil snapshot",
                    "region": "US",
                    "commodity": "oil",
                    "snapshot_date": "2026-06-17",
                    "evidence_refs": [{"kind": "signal", "id": "energy:US:oil:2026-06-17"}],
                }
            ]
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert {node["id"] for node in body["nodes"]} >= {
        "energy_snapshot:eia:US:oil:2026-06-17",
        "region:US",
        "commodity:oil",
    }
    assert {edge["relationship"] for edge in body["edges"]} == {
        "tracks_region",
        "tracks_commodity",
    }


def test_country_risk_panel_endpoint_builds_context_and_signals() -> None:
    resp = _post(
        "/api/v1/countries/US/risk-panel",
        {
            "country_code": "US",
            "snapshot_date": "2026-06-17",
            "indicator_observations": [
                {
                    "provider": "world-bank",
                    "country_code": "US",
                    "indicator_id": "FP.CPI.TOTL.ZG",
                    "observed_at": "2025-12-31T00:00:00+00:00",
                    "value": 8.0,
                    "evidence_refs": [{"kind": "signal", "id": "indicator:inflation"}],
                }
            ],
            "required_indicator_ids": ["FP.CPI.TOTL.ZG", "GE.EST"],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["country_code"] == "US"
    assert body["sovereign_context"]["inflation"]["value"] == 8.0
    assert body["risk_signals"]["inflation_pressure"]["score"] == 32.0
    assert "GE.EST" in body["metadata"]["missing_indicator_ids"]


def test_energy_stress_and_rating_driver_endpoints() -> None:
    energy_resp = _post(
        "/api/v1/signals/energy-stress",
        {
            "previous_snapshots": [
                {
                    "provider": "eia",
                    "region": "US",
                    "commodity": "oil",
                    "snapshot_date": "2026-06-10",
                    "price": 80.0,
                    "inventory": 1000.0,
                }
            ],
            "snapshots": [
                {
                    "provider": "eia",
                    "region": "US",
                    "commodity": "oil",
                    "snapshot_date": "2026-06-17",
                    "price": 100.0,
                    "inventory": 800.0,
                }
            ],
        },
    )

    assert energy_resp.status_code == 200
    energy_body = energy_resp.json()
    signal_names = {signal["name"] for signal in energy_body["signals"]}
    assert {"oil_price_shock", "inventory_drawdown"} <= signal_names

    rating_resp = _post(
        "/api/v1/ratings/country/US/drivers",
        {
            "target_type": "country",
            "target_id": "US",
            "risk_type": "sovereign",
            "as_of_date": "2026-06-17",
            "previous_payload": {"risk_score": 40.0, "top_drivers": []},
            "signals": [
                {
                    "name": "inflation_pressure",
                    "group": "macro",
                    "score": 80.0,
                    "weight": 2.0,
                    "evidence_refs": [{"kind": "signal", "id": "indicator:inflation"}],
                },
                {
                    "name": "energy_price_stress",
                    "group": "energy",
                    "score": 30.0,
                    "direction": "lowers_risk",
                    "evidence_refs": [{"kind": "signal", "id": "energy:price"}],
                },
            ],
        },
    )

    assert rating_resp.status_code == 200
    rating = rating_resp.json()
    assert rating["risk_score"] == 43.33
    assert rating["changed_since_previous"]["risk_score_delta"] == 3.33
    assert any(driver["direction"] == "lowers_risk" for driver in rating["top_drivers"])
