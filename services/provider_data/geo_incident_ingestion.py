"""Geospatial incident ingestion into provider-data storage."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from db.models import GeoIncident as GeoIncidentModel
from packages.providers.base import GeoIncidentProvider
from services.provider_data.common import (
    IngestionResult,
    add,
    find_one,
    json_safe,
    retain_raw_item,
)


def ingest_geo_incidents(
    session: Any,
    provider: GeoIncidentProvider,
    *,
    start_at: datetime.datetime | None = None,
    end_at: datetime.datetime | None = None,
    region: str | None = None,
    limit: int | None = None,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = "geo",
) -> IngestionResult:
    """Fetch and persist normalized geospatial incidents."""

    incidents = provider.search_incidents(
        start_at=start_at,
        end_at=end_at,
        region=region,
        limit=limit,
    )
    inserted = 0
    skipped = 0
    raw_inserted = 0
    raw_skipped = 0

    for incident in incidents:
        provider_key = incident.provider_name if incident.provider_name != "unknown" else provider_name
        _, raw_created = retain_raw_item(
            session,
            provider=provider_key,
            item_type="geo_incident",
            external_id=incident.incident_id,
            payload=incident,
            observed_at=incident.occurred_at,
            provider_run_id=provider_run_id,
            identity_parts=(provider_key, incident.incident_id),
        )
        raw_inserted += int(raw_created)
        raw_skipped += int(not raw_created)
        was_inserted = _upsert_incident(session, incident, provider_name=provider_key)
        inserted += int(was_inserted)
        skipped += int(not was_inserted)

    return IngestionResult(
        fetched=len(incidents),
        inserted=inserted,
        skipped=skipped,
        details={"raw_inserted": raw_inserted, "raw_skipped": raw_skipped},
    )


def _upsert_incident(session: Any, incident: Any, *, provider_name: str) -> bool:
    existing = find_one(
        session,
        GeoIncidentModel,
        provider=provider_name,
        external_id=incident.incident_id,
    )
    values = {
        "incident_type": incident.incident_type,
        "title": incident.title,
        "country_code": incident.country_code or None,
        "region": incident.place or None,
        "latitude": incident.latitude,
        "longitude": incident.longitude,
        "magnitude": incident.magnitude,
        "severity": incident.severity or None,
        "observed_at": incident.occurred_at,
        "updated_at": incident.occurred_at,
        "source_url": incident.url or None,
        "source_payload": json_safe(incident),
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return False
    add(
        session,
        GeoIncidentModel(
            id=uuid.uuid4(),
            provider=provider_name,
            external_id=incident.incident_id,
            **values,
        ),
    )
    return True
