"""Country indicator provider ingestion into country indicator tables."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from db.models import CountryIndicatorObservation, CountryIndicatorSeries
from packages.providers.base import CountryIndicator, CountryIndicatorProvider
from services.provider_data.common import (
    IngestionResult,
    add,
    find_one,
    json_safe,
    retain_raw_item,
)


def ingest_country_indicators(
    session: Any,
    provider: CountryIndicatorProvider,
    *,
    country_codes: Sequence[str],
    indicator_ids: Sequence[str],
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int | None = None,
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = "world-bank",
) -> IngestionResult:
    """Fetch country indicator metadata/observations and persist them idempotently."""

    fetched = 0
    inserted = 0
    skipped = 0
    series_inserted = 0
    raw_inserted = 0
    raw_skipped = 0

    for indicator_id in indicator_ids:
        indicator = provider.fetch_indicator_metadata(indicator_id)
        if indicator is None:
            indicator = CountryIndicator(indicator_id=indicator_id, name=indicator_id)
        series, created = _upsert_series(session, indicator, provider_name=provider_name)
        series_inserted += int(created)

        for country_code in country_codes:
            observations = provider.fetch_indicator_observations(
                country_code,
                indicator_id,
                start_year=start_year,
                end_year=end_year,
                limit=limit,
            )
            fetched += len(observations)
            for observation in observations:
                _, raw_created = retain_raw_item(
                    session,
                    provider=provider_name,
                    item_type="country_indicator_observation",
                    external_id=f"{observation.country_code}:{observation.indicator_id}:{observation.date}",
                    payload=observation,
                    observed_at=observation.observed_at,
                    provider_run_id=provider_run_id,
                    identity_parts=(observation.country_code, observation.indicator_id, observation.date),
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
            "raw_inserted": raw_inserted,
            "raw_skipped": raw_skipped,
            "series_inserted": series_inserted,
        },
    )


def _upsert_series(
    session: Any, indicator: CountryIndicator, *, provider_name: str
) -> tuple[CountryIndicatorSeries, bool]:
    existing = find_one(
        session,
        CountryIndicatorSeries,
        provider=provider_name,
        indicator_id=indicator.indicator_id,
    )
    values = {
        "title": indicator.name,
        "description": str(indicator.metadata.get("description") or "") or None,
        "unit": indicator.unit or None,
        "frequency": indicator.frequency or None,
        "topic": indicator.topics[0] if indicator.topics else None,
        "source": indicator.source or None,
        "series_metadata": json_safe(indicator),
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return existing, False
    row = CountryIndicatorSeries(
        id=uuid.uuid4(),
        provider=provider_name,
        indicator_id=indicator.indicator_id,
        **values,
    )
    add(session, row)
    return row, True


def _upsert_observation(session: Any, *, series: CountryIndicatorSeries, observation: Any) -> bool:
    existing = find_one(
        session,
        CountryIndicatorObservation,
        series_id=series.id,
        country_code=observation.country_code,
        observed_on=observation.date,
    )
    values = {
        "country_name": observation.country_name or None,
        "value": observation.value,
        "raw_value": None if observation.value is None else str(observation.value),
        "observation_metadata": {
            **json_safe(observation.metadata),
            "evidence_refs": json_safe(observation.evidence_refs),
            "schema_version": observation.schema_version,
        },
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return False
    add(
        session,
        CountryIndicatorObservation(
            id=uuid.uuid4(),
            series_id=series.id,
            country_code=observation.country_code,
            observed_on=observation.date,
            **values,
        ),
    )
    return True
