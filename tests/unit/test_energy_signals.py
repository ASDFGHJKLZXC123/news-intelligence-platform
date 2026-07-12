"""Energy stress signal tests."""

from __future__ import annotations

import datetime

from services.risk.energy_signals import build_energy_stress_panel, build_energy_stress_signals


def test_energy_stress_panel_compares_snapshots_and_emits_evidence_backed_signals() -> None:
    previous = {
        "provider": "eia",
        "region": "US",
        "commodity": "oil",
        "snapshot_date": datetime.date(2026, 6, 10),
        "price": 80.0,
        "inventory": 1_000.0,
        "production": 100.0,
    }
    current = {
        "provider": "eia",
        "region": "US",
        "commodity": "oil",
        "snapshot_date": datetime.date(2026, 6, 17),
        "price": 100.0,
        "inventory": 800.0,
        "production": 85.0,
        "imports": 60.0,
        "consumption": 100.0,
    }

    panel = build_energy_stress_panel(snapshots=[current], previous_snapshots=[previous])
    signals = {signal.name: signal for signal in panel.signals}

    assert set(signals) == {
        "oil_price_shock",
        "inventory_drawdown",
        "production_disruption",
        "energy_import_dependency",
    }
    assert signals["oil_price_shock"].score == 100.0
    assert signals["inventory_drawdown"].change_pct == -0.2
    assert signals["production_disruption"].direction == "raises_risk"
    assert signals["energy_import_dependency"].metadata["dependency_ratio"] == 0.6
    assert all(signal.evidence_refs for signal in panel.signals)
    assert panel.as_payload()["metadata"]["snapshot_count"] == 1


def test_energy_signals_represent_risk_reducing_moves() -> None:
    previous = {
        "provider": "eia",
        "region": "EU",
        "commodity": "natural_gas",
        "snapshot_date": "2026-06-10",
        "price": 50.0,
        "inventory": 100.0,
    }
    current = {
        "provider": "eia",
        "region": "EU",
        "commodity": "natural_gas",
        "snapshot_date": "2026-06-17",
        "price": 40.0,
        "inventory": 120.0,
    }

    signals = {
        signal.name: signal
        for signal in build_energy_stress_signals([current], previous_snapshots=[previous])
    }

    assert signals["energy_price_stress"].direction == "lowers_risk"
    assert signals["gas_storage_stress"].direction == "lowers_risk"
    assert signals["gas_storage_stress"].score == 100.0
