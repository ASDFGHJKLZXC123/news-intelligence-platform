"""Derived evidence and rating-intelligence API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from services.evidence.graph import build_evidence_graph
from services.risk.country_context import build_country_context_snapshot
from services.risk.energy_signals import build_energy_stress_panel
from services.risk.signal_fusion import build_rating_driver_payload

router = APIRouter(tags=["risk-intelligence"])


class EvidenceGraphRequest(BaseModel):
    facts: list[dict[str, Any]] = Field(default_factory=list)


class CountryRiskPanelRequest(BaseModel):
    country_code: str = Field(min_length=1)
    snapshot_date: str
    indicator_observations: list[dict[str, Any]] = Field(default_factory=list)
    humanitarian_reports: list[dict[str, Any]] = Field(default_factory=list)
    geo_incidents: list[dict[str, Any]] = Field(default_factory=list)
    required_indicator_ids: list[str] | None = None
    recent_window_days: int = Field(default=30, ge=1, le=365)


class EnergyStressRequest(BaseModel):
    snapshots: list[dict[str, Any]] = Field(default_factory=list)
    previous_snapshots: list[dict[str, Any]] = Field(default_factory=list)
    snapshot_date: str | None = None


class RatingDriversRequest(BaseModel):
    target_type: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    risk_type: str = Field(min_length=1)
    as_of_date: str
    signals: list[dict[str, Any]]
    previous_payload: dict[str, Any] | None = None
    top_n: int = Field(default=8, ge=1, le=25)


@router.post("/api/v1/signals/evidence-graph")
def build_evidence_graph_endpoint(request: EvidenceGraphRequest) -> dict[str, Any]:
    return build_evidence_graph(request.facts).as_payload()


@router.post("/api/v1/countries/{country_code}/risk-panel")
def build_country_risk_panel_endpoint(
    country_code: str,
    request: CountryRiskPanelRequest,
) -> dict[str, Any]:
    required_ids = request.required_indicator_ids
    snapshot = build_country_context_snapshot(
        country_code=country_code or request.country_code,
        snapshot_date=request.snapshot_date,
        indicator_observations=request.indicator_observations,
        humanitarian_reports=request.humanitarian_reports,
        geo_incidents=request.geo_incidents,
        required_indicator_ids=required_ids if required_ids is not None else (),
        recent_window_days=request.recent_window_days,
    )
    return snapshot.as_payload()


@router.post("/api/v1/signals/energy-stress")
def build_energy_stress_endpoint(request: EnergyStressRequest) -> dict[str, Any]:
    return build_energy_stress_panel(
        snapshots=request.snapshots,
        previous_snapshots=request.previous_snapshots,
        snapshot_date=request.snapshot_date,
    ).as_payload()


@router.post("/api/v1/ratings/{target_type}/{target_id}/drivers")
def build_rating_drivers_endpoint(
    target_type: str,
    target_id: str,
    request: RatingDriversRequest,
) -> dict[str, Any]:
    return build_rating_driver_payload(
        target_type=target_type or request.target_type,
        target_id=target_id or request.target_id,
        risk_type=request.risk_type,
        as_of_date=request.as_of_date,
        signals=request.signals,
        previous_payload=request.previous_payload,
        top_n=request.top_n,
    )
