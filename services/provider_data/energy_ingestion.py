"""Energy provider ingestion into energy market snapshot storage."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from typing import Any

from db.models import EnergyMarketSnapshot
from packages.providers.base import EnergyProvider
from services.provider_data.common import (
    IngestionResult,
    add,
    find_one,
    json_safe,
    retain_raw_item,
)


def ingest_energy_series(
    session: Any,
    provider: EnergyProvider,
    *,
    series_ids: Sequence[str],
    start_at: datetime.datetime | None = None,
    end_at: datetime.datetime | None = None,
    limit: int | None = None,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = "eia",
) -> IngestionResult:
    """Fetch EIA-style energy observations and persist market snapshots."""

    fetched = 0
    inserted = 0
    skipped = 0
    raw_inserted = 0
    raw_skipped = 0

    for raw_series_id in series_ids:
        series_id = str(raw_series_id).strip()
        if not series_id:
            continue
        series = provider.fetch_series(series_id)
        observations = provider.fetch_series_observations(
            series_id,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )
        fetched += len(observations)
        for observation in observations:
            _, raw_created = retain_raw_item(
                session,
                provider=provider_name,
                item_type="energy_observation",
                external_id=f"{observation.series_id}:{observation.date}",
                payload=observation,
                observed_at=observation.observed_at,
                provider_run_id=provider_run_id,
                identity_parts=(observation.series_id, observation.date),
            )
            raw_inserted += int(raw_created)
            raw_skipped += int(not raw_created)
            was_inserted = _upsert_snapshot(session, series=series, observation=observation)
            inserted += int(was_inserted)
            skipped += int(not was_inserted)

    return IngestionResult(
        fetched=fetched,
        inserted=inserted,
        skipped=skipped,
        details={"raw_inserted": raw_inserted, "raw_skipped": raw_skipped},
    )


def _upsert_snapshot(session: Any, *, series: Any, observation: Any) -> bool:
    region = observation.geography or series.geography or "global"
    commodity = _commodity(series.series_id, series.name)
    existing = find_one(
        session,
        EnergyMarketSnapshot,
        provider=observation.provider_name,
        region=region,
        commodity=commodity,
        snapshot_date=observation.date,
    )
    values = {
        "price": observation.value if _looks_like_price(series, observation) else None,
        "inventory": None if _looks_like_price(series, observation) else observation.value,
        "snapshot_metadata": {
            "observation": json_safe(observation),
            "series": json_safe(series),
            "units": observation.units or series.units,
        },
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return False
    add(
        session,
        EnergyMarketSnapshot(
            id=uuid.uuid4(),
            provider=observation.provider_name,
            region=region,
            commodity=commodity,
            snapshot_date=observation.date,
            **values,
        ),
    )
    return True


def _commodity(series_id: str, name: str) -> str:
    text = f"{series_id} {name}".casefold()
    if "gas" in text:
        return "natural_gas"
    if "electric" in text or "power" in text:
        return "electricity"
    if "coal" in text:
        return "coal"
    return "oil"


def _looks_like_price(series: Any, observation: Any) -> bool:
    text = f"{series.name} {series.units} {observation.units}".casefold()
    return "price" in text or "$" in text or "dollar" in text
