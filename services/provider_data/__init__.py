"""Service-layer ingestion helpers for external provider data."""

from services.provider_data.common import IngestionResult, normalize_alias
from services.provider_data.country_indicator_ingestion import ingest_country_indicators
from services.provider_data.energy_ingestion import ingest_energy_series
from services.provider_data.entity_identity_ingestion import ingest_entity_identity_records
from services.provider_data.entity_redirects import (
    RedirectAmbiguousError,
    RedirectChainError,
    RedirectCycleError,
    RedirectTargetMissingError,
    RedirectTooDeepError,
    entity_redirect_history,
    redirect_chain,
    resolve_entity_redirect,
    upsert_entity_redirect,
)
from services.provider_data.fred_ingestion import (
    ingest_fred_observations,
    ingest_fred_series,
)
from services.provider_data.gdelt_ingestion import (
    ingest_gdelt,
    ingest_gdelt_raw_items,
)
from services.provider_data.geo_incident_ingestion import ingest_geo_incidents
from services.provider_data.humanitarian_ingestion import ingest_humanitarian_reports
from services.provider_data.sanctions_ingestion import ingest_sanctions_entities
from services.provider_data.sec_identity_ingestion import ingest_sec_company_tickers
from services.provider_data.sec_ingestion import (
    ingest_sec_companies,
    ingest_sec_company_data,
)
from services.provider_data.wikidata_identity_ingestion import ingest_wikidata_identities

__all__ = [
    "IngestionResult",
    "RedirectAmbiguousError",
    "RedirectChainError",
    "RedirectCycleError",
    "RedirectTargetMissingError",
    "RedirectTooDeepError",
    "entity_redirect_history",
    "ingest_country_indicators",
    "ingest_energy_series",
    "ingest_entity_identity_records",
    "ingest_fred_observations",
    "ingest_fred_series",
    "ingest_gdelt",
    "ingest_gdelt_raw_items",
    "ingest_geo_incidents",
    "ingest_humanitarian_reports",
    "ingest_sanctions_entities",
    "ingest_sec_companies",
    "ingest_sec_company_data",
    "ingest_sec_company_tickers",
    "ingest_wikidata_identities",
    "normalize_alias",
    "redirect_chain",
    "resolve_entity_redirect",
    "upsert_entity_redirect",
]
