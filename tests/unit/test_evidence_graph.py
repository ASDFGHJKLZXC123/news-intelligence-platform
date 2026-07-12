"""Derived evidence graph tests."""

from __future__ import annotations

import datetime

from packages.providers.base import (
    CountryIndicatorObservation,
    HumanitarianReport,
    SanctionsEntity,
)
from services.evidence.graph import build_evidence_graph, canonical_fact_node_id

UTC = datetime.UTC


def test_evidence_graph_is_deterministic_and_links_provider_facts_to_subjects() -> None:
    sanctions = SanctionsEntity(
        entity_id="SDN-1",
        name="Example Energy Trading",
        programs=("CYBER2",),
        countries=("RU",),
        updated_at=datetime.datetime(2026, 6, 17, tzinfo=UTC),
        evidence_refs=("sanctions:SDN-1",),
    )
    indicator = CountryIndicatorObservation(
        country_code="US",
        indicator_id="FP.CPI.TOTL.ZG",
        observed_at=datetime.datetime(2025, 12, 31, tzinfo=UTC),
        value=7.5,
        unit="percent",
    )
    report = HumanitarianReport(
        report_id="RW-1",
        title="Flood response update",
        url="https://example.test/report",
        published_at=datetime.datetime(2026, 6, 10, tzinfo=UTC),
        country_codes=("US",),
        themes=("Floods",),
    )

    graph = build_evidence_graph([report, sanctions, indicator])
    reversed_graph = build_evidence_graph([indicator, sanctions, report])

    assert graph.as_payload() == reversed_graph.as_payload()
    assert graph.node("country:US") is not None
    assert graph.node("country:RU") is not None
    assert graph.node("indicator:FP.CPI.TOTL.ZG") is not None
    assert graph.node("entity:SDN-1") is not None

    indicator_node_id = canonical_fact_node_id(
        "country_indicator",
        "world-bank",
        "US:FP.CPI.TOTL.ZG:2025-12-31",
    )
    assert graph.node(indicator_node_id) is not None
    relationships = {edge.relationship for edge in graph.edges}
    assert {"measures_country", "measures_indicator", "sanctions_subject", "reports_country"} <= relationships
    assert all(node.evidence_refs for node in graph.nodes if node.provider != "internal")


def test_evidence_graph_accepts_explicit_provider_fact_mappings() -> None:
    graph = build_evidence_graph(
        [
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
    )

    assert graph.node("energy_snapshot:eia:US:oil:2026-06-17") is not None
    assert graph.node("region:US") is not None
    assert graph.node("commodity:oil") is not None
    assert {(edge.relationship, edge.target_id) for edge in graph.edges} == {
        ("tracks_region", "region:US"),
        ("tracks_commodity", "commodity:oil"),
    }
