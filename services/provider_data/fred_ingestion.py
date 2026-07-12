"""FRED provider ingestion into macro provider-data tables."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from db.models import MacroObservation, MacroSeries
from packages.providers.base import FREDObservation, FREDProvider
from services.provider_data.common import (
    IngestionResult,
    add,
    find_one,
    first_present,
    json_safe,
    retain_raw_item,
)


def ingest_fred_series(
    session: Any,
    provider: FREDProvider,
    *,
    series_ids: Sequence[str],
    observation_start: datetime.date | None = None,
    observation_end: datetime.date | None = None,
    limit: int | None = None,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = "fred",
    series_metadata_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> IngestionResult:
    """Fetch FRED observations and upsert macro series/observation rows."""

    fetched = 0
    inserted = 0
    skipped = 0
    series_inserted = 0
    raw_inserted = 0
    raw_skipped = 0

    for raw_series_id in series_ids:
        series_id = str(raw_series_id).strip()
        if not series_id:
            continue
        observations = provider.fetch_series_observations(
            series_id,
            observation_start=observation_start,
            observation_end=observation_end,
            limit=limit,
        )
        fetched += len(observations)

        metadata = dict(series_metadata_by_id.get(series_id, {})) if series_metadata_by_id else {}
        series, created = _upsert_series(
            session,
            provider_name=provider_name,
            series_id=series_id,
            observations=observations,
            metadata=metadata,
        )
        series_inserted += int(created)

        for observation in observations:
            _, raw_created = retain_raw_item(
                session,
                provider=provider_name,
                item_type="observation",
                external_id=_observation_external_id(observation),
                payload=observation,
                observed_at=observation.observed_at,
                provider_run_id=provider_run_id,
                identity_parts=_observation_identity(observation),
            )
            raw_inserted += int(raw_created)
            raw_skipped += int(not raw_created)

            was_inserted = _upsert_observation(session, series=series, observation=observation)
            inserted += int(was_inserted)
            skipped += int(not was_inserted)

    return IngestionResult(
        fetched=fetched,
        inserted=inserted,
        skipped=skipped,
        details={
            "series_inserted": series_inserted,
            "raw_inserted": raw_inserted,
            "raw_skipped": raw_skipped,
        },
    )


def _upsert_series(
    session: Any,
    *,
    provider_name: str,
    series_id: str,
    observations: Sequence[FREDObservation],
    metadata: Mapping[str, Any],
) -> tuple[MacroSeries, bool]:
    merged_metadata = _series_metadata(series_id, observations, metadata)
    title = str(
        first_present(
            merged_metadata.get("title"),
            merged_metadata.get("name"),
            merged_metadata.get("series_title"),
            series_id,
        )
    )
    series = find_one(session, MacroSeries, provider=provider_name, series_id=series_id)
    if series is None:
        series = MacroSeries(
            id=uuid.uuid4(),
            provider=provider_name,
            series_id=series_id,
            title=title,
            frequency=_optional_text(merged_metadata.get("frequency")),
            units=_optional_text(merged_metadata.get("units")),
            seasonal_adjustment=_optional_text(
                first_present(
                    merged_metadata.get("seasonal_adjustment"),
                    merged_metadata.get("seasonal_adjustment_short"),
                )
            ),
            country=_optional_text(merged_metadata.get("country")),
            source=_optional_text(first_present(merged_metadata.get("source"), "FRED")),
            series_metadata=json_safe(merged_metadata) or None,
        )
        add(session, series)
        return series, True

    series.title = title or series.title
    series.frequency = _optional_text(merged_metadata.get("frequency")) or series.frequency
    series.units = _optional_text(merged_metadata.get("units")) or series.units
    series.seasonal_adjustment = (
        _optional_text(
            first_present(
                merged_metadata.get("seasonal_adjustment"),
                merged_metadata.get("seasonal_adjustment_short"),
            )
        )
        or series.seasonal_adjustment
    )
    series.country = _optional_text(merged_metadata.get("country")) or series.country
    series.source = _optional_text(first_present(merged_metadata.get("source"), "FRED"))
    series.series_metadata = json_safe(merged_metadata) or series.series_metadata
    return series, False


def _upsert_observation(
    session: Any,
    *,
    series: MacroSeries,
    observation: FREDObservation,
) -> bool:
    existing = find_one(
        session,
        MacroObservation,
        series_uuid=series.id,
        observed_on=observation.date,
        realtime_start=observation.realtime_start,
        realtime_end=observation.realtime_end,
    )
    metadata = _observation_metadata(observation)
    if existing is not None:
        existing.value = observation.value
        existing.raw_value = _raw_observation_value(observation)
        existing.observation_metadata = metadata
        return False

    add(
        session,
        MacroObservation(
            id=uuid.uuid4(),
            series_uuid=series.id,
            observed_on=observation.date,
            value=observation.value,
            raw_value=_raw_observation_value(observation),
            realtime_start=observation.realtime_start,
            realtime_end=observation.realtime_end,
            observation_metadata=metadata,
        ),
    )
    return True


def _series_metadata(
    series_id: str,
    observations: Sequence[FREDObservation],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {"series_id": series_id}
    merged.update(json_safe(metadata))
    if observations:
        first_metadata = json_safe(observations[0].metadata)
        for key in (
            "title",
            "name",
            "series_title",
            "frequency",
            "units",
            "seasonal_adjustment",
            "seasonal_adjustment_short",
            "country",
            "source",
        ):
            if key in first_metadata and key not in merged:
                merged[key] = first_metadata[key]
    return merged


def _observation_metadata(observation: FREDObservation) -> dict[str, Any]:
    metadata = json_safe(observation.metadata)
    metadata.update(
        {
            "evidence_refs": json_safe(observation.evidence_refs),
            "provider_name": observation.provider_name,
            "schema_version": observation.schema_version,
            "source_refs": json_safe(observation.source_refs),
        }
    )
    return metadata


def _raw_observation_value(observation: FREDObservation) -> str | None:
    metadata = observation.metadata
    raw_value = metadata.get("value") if isinstance(metadata, Mapping) else None
    if raw_value is None and observation.value is not None:
        raw_value = observation.value
    return None if raw_value is None else str(raw_value)


def _observation_external_id(observation: FREDObservation) -> str:
    return ":".join(str(part) for part in _observation_identity(observation))


def _observation_identity(observation: FREDObservation) -> tuple[Any, ...]:
    return (
        observation.series_id,
        observation.date,
        observation.realtime_start,
        observation.realtime_end,
    )


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


ingest_fred_observations = ingest_fred_series
