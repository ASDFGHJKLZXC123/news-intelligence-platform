"""Country context snapshot tests."""

from __future__ import annotations

import datetime

from packages.providers.base import (
    CountryIndicatorObservation,
    GeoIncident,
    HumanitarianReport,
)
from services.risk.country_context import (
    build_country_context_snapshot,
    country_context_risk_signals,
)

UTC = datetime.UTC


def _indicator(indicator_id: str, value: float, year: int = 2025) -> CountryIndicatorObservation:
    return CountryIndicatorObservation(
        country_code="US",
        indicator_id=indicator_id,
        observed_at=datetime.datetime(year, 12, 31, tzinfo=UTC),
        value=value,
        evidence_refs=(f"indicator:{indicator_id}:{year}",),
    )


def test_country_context_snapshot_classifies_latest_indicators_and_missing_data() -> None:
    snapshot = build_country_context_snapshot(
        country_code="us",
        snapshot_date=datetime.date(2026, 6, 17),
        indicator_observations=[
            _indicator("FP.CPI.TOTL.ZG", 5.0, 2024),
            _indicator("FP.CPI.TOTL.ZG", 8.0, 2025),
            _indicator("NY.GDP.MKTP.KD.ZG", -2.0, 2025),
            _indicator("GE.EST", -0.5, 2025),
        ],
    )

    assert snapshot.country_code == "US"
    assert snapshot.sovereign_context["inflation"]["value"] == 8.0
    assert snapshot.sovereign_context["gdp_growth"]["value"] == -2.0
    assert snapshot.governance_context["government_effectiveness"]["value"] == -0.5
    assert "DT.DOD.DECT.GN.ZS" in snapshot.metadata["missing_indicator_ids"]
    assert snapshot.risk_signals["inflation_pressure"]["score"] == 32.0
    assert snapshot.risk_signals["growth_pressure"]["score"] == 25.0
    assert all(ref["kind"] == "signal" for ref in snapshot.evidence_refs)


def test_country_context_includes_humanitarian_velocity_and_geo_severity() -> None:
    recent_report = HumanitarianReport(
        report_id="RW-1",
        title="Flood response update",
        url="https://example.test/rw-1",
        published_at=datetime.datetime(2026, 6, 12, tzinfo=UTC),
        country_codes=("US",),
        disaster_types=("Flood",),
        themes=("Shelter",),
    )
    previous_report = HumanitarianReport(
        report_id="RW-0",
        title="Earlier update",
        url="https://example.test/rw-0",
        published_at=datetime.datetime(2026, 5, 12, tzinfo=UTC),
        country_codes=("US",),
    )
    incident = GeoIncident(
        incident_id="USGS-1",
        incident_type="earthquake",
        title="M6.4 earthquake",
        occurred_at=datetime.datetime(2026, 6, 16, tzinfo=UTC),
        latitude=37.0,
        longitude=-122.0,
        magnitude=6.4,
        country_code="US",
        provider_name="usgs",
    )

    snapshot = build_country_context_snapshot(
        country_code="US",
        snapshot_date="2026-06-17",
        indicator_observations=[_indicator("NE.TRD.GNFS.ZS", 85.0)],
        humanitarian_reports=[recent_report, previous_report],
        geo_incidents=[incident],
    )

    assert snapshot.humanitarian_context["recent_report_count"] == 1
    assert snapshot.humanitarian_context["previous_window_report_count"] == 1
    assert snapshot.geo_context["recent_incident_count"] == 1
    assert snapshot.geo_context["max_severity_score"] == 80.0
    signals = {signal["name"]: signal for signal in country_context_risk_signals(snapshot)}
    assert signals["humanitarian_report_velocity"]["score"] == 15.0
    assert signals["disaster_incident_severity"]["score"] == 80.0
    assert signals["trade_exposure_concentration"]["score"] > 50.0
    assert snapshot.as_db_values()["snapshot_metadata"]["geo_context"]["recent_incident_count"] == 1
