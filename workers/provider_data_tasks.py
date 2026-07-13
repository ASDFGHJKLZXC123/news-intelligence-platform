"""Celery tasks for external provider-data ingestion runs.

Tasks record deterministic provider-run lifecycle metadata, then invoke the service
layer through provider factories. Tests monkeypatch the factories with deterministic
fakes, so the unit suite never makes live provider calls.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from celery import shared_task

from db.base import SessionLocal
from db.models import ProviderRun
from packages.config import metrics
from packages.config.logging import get_logger, set_job_id
from packages.config.settings import get_settings
from packages.jobs import Stage1Job
from packages.providers.eia import EIAClient
from packages.providers.fred import FREDClient
from packages.providers.gdelt import GDELTClient
from packages.providers.gleif import GLEIFClient
from packages.providers.nasa_firms import NASAFIRMSClient
from packages.providers.ofac import OFACClient
from packages.providers.reliefweb import ReliefWebClient
from packages.providers.sec_edgar import SECEdgarClient
from packages.providers.usgs import USGSEarthquakeClient
from packages.providers.wikidata import WikidataClient
from packages.providers.world_bank import WorldBankClient
from services.provider_data import (
    IngestionResult,
    ingest_country_indicators,
    ingest_energy_series,
    ingest_entity_identity_records,
    ingest_fred_series,
    ingest_gdelt_raw_items,
    ingest_geo_incidents,
    ingest_humanitarian_reports,
    ingest_sanctions_entities,
    ingest_sec_companies,
    ingest_sec_company_tickers,
    ingest_wikidata_identities,
)
from workers.celery_app import Stage1Task

logger = get_logger("workers.provider_data_tasks")


@dataclass(frozen=True)
class ProviderBinding:
    """Resolved provider instance plus task-execution metadata."""

    provider: Any | None
    mode: str
    network_called: bool = False
    unavailable_reason: str | None = None


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _canonical_parameters(parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    encoded = json.dumps(parameters or {}, sort_keys=True, separators=(",", ":"), default=str)
    return json.loads(encoded)


def _stable_run_key(provider: str, run_type: str, parameters: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"parameters": parameters, "provider": provider, "run_type": run_type},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"{provider}:{run_type}:{digest}"


def _run_id_for_key(run_key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"news-intelligence-platform:provider-run:{run_key}")


def _sorted_strings(values: Sequence[str] | None) -> list[str]:
    return sorted(str(value) for value in (values or []))


def _weekly_period(now: datetime.datetime) -> str:
    """The ISO week a refresh belongs to, e.g. ``2026-W28``."""
    year, week, _ = now.isocalendar()
    return f"{year:04d}-W{week:02d}"


def _monthly_period(now: datetime.datetime) -> str:
    """The calendar month a refresh belongs to, e.g. ``2026-07``."""
    return f"{now.year:04d}-{now.month:02d}"


def _task_result(run: ProviderRun, job: Stage1Job, *, idempotent: bool) -> dict[str, Any]:
    stats = run.stats or {}
    result = stats.get("result") if isinstance(stats.get("result"), Mapping) else {}
    return {
        "status": "ok",
        "state": run.status,
        "provider": run.provider,
        "run_type": run.run_type,
        "run_id": str(run.id),
        "run_key": run.run_key,
        "job_id": job.job_id,
        "job_key": job.job_key,
        "item_count": run.item_count,
        "idempotent": idempotent,
        "client_available": stats.get("client_available", False),
        "mode": stats.get("mode"),
        "network_called": bool(stats.get("network_called", False)),
        "fetched": result.get("fetched", 0),
        "inserted": result.get("inserted", 0),
        "skipped": result.get("skipped", 0),
        "details": result.get("details", {}),
        "unavailable_reason": stats.get("unavailable_reason"),
    }


def _record_provider_run(
    *,
    provider: str,
    run_type: str,
    parameters: Mapping[str, Any] | None,
    provider_binding: ProviderBinding,
    ingest: Any | None,
) -> dict[str, Any]:
    normalized_parameters = _canonical_parameters(parameters)
    run_key = _stable_run_key(provider, run_type, normalized_parameters)
    run_id = _run_id_for_key(run_key)
    job = Stage1Job.create(
        "workers.provider_data_tasks.record_provider_run",
        {"provider": provider, "run_type": run_type, "parameters": normalized_parameters},
    ).mark_running()
    set_job_id(job.job_id)
    metrics.increment(metrics.JOB_STARTS)

    session = SessionLocal()
    try:
        run = session.get(ProviderRun, run_id)
        if run is not None and run.status == "succeeded":
            metrics.increment(metrics.JOB_SUCCESSES)
            completed = job.mark_succeeded()
            return _task_result(run, completed, idempotent=True)

        started_at = _utc_now()
        if run is None:
            run = ProviderRun(
                id=run_id,
                run_key=run_key,
                provider=provider,
                run_type=run_type,
                status="running",
                parameters=normalized_parameters,
                item_count=0,
                started_at=started_at,
            )
            session.add(run)
        else:
            run.status = "running"
            run.parameters = normalized_parameters
            run.stats = None
            run.error = None
            run.item_count = 0
            run.started_at = started_at
            run.completed_at = None

        session.flush()
        if provider_binding.provider is None or ingest is None:
            result = IngestionResult(fetched=0, inserted=0, skipped=0)
            run.status = "succeeded"
            run.item_count = 0
            run.completed_at = _utc_now()
            run.stats = {
                "client_available": provider_binding.provider is not None,
                "mode": provider_binding.mode,
                "network_called": False,
                "unavailable_reason": provider_binding.unavailable_reason,
                "result": result.as_dict(),
            }
        else:
            result = ingest(session, run.id, provider_binding.provider)
            run.status = "succeeded"
            run.item_count = result.inserted
            run.completed_at = _utc_now()
            run.stats = {
                "client_available": True,
                "mode": provider_binding.mode,
                "network_called": provider_binding.network_called,
                "result": result.as_dict(),
            }
        session.commit()

        metrics.increment(metrics.JOB_SUCCESSES)
        completed = job.mark_succeeded()
        logger.info(
            "provider data lifecycle stub completed",
            extra={"provider": provider, "run_type": run_type, "run_id": str(run.id)},
        )
        return _task_result(run, completed, idempotent=False)
    except Exception as error:
        session.rollback()
        _mark_run_failed(
            session,
            run_id=run_id,
            run_key=run_key,
            provider=provider,
            run_type=run_type,
            parameters=normalized_parameters,
            error=error,
        )
        metrics.increment(metrics.JOB_FAILURES)
        logger.exception("provider data lifecycle stub failed")
        raise
    finally:
        session.close()
        set_job_id(None)


def _mark_run_failed(
    session: Any,
    *,
    run_id: uuid.UUID,
    run_key: str,
    provider: str,
    run_type: str,
    parameters: Mapping[str, Any],
    error: BaseException,
) -> None:
    """Record the terminal state of a failed run on a clean transaction.

    The rollback above discards the in-flight row, so without this a failed run leaves no
    trace at all and the retry cannot tell a first attempt from a fifth. The run is re-read
    rather than reused because rollback detaches it. This must never raise: the provider
    error is the one worth propagating, and Stage1Task retries on it.
    """

    try:
        run = session.get(ProviderRun, run_id)
        if run is None:
            run = ProviderRun(
                id=run_id,
                run_key=run_key,
                provider=provider,
                run_type=run_type,
                status="failed",
                parameters=dict(parameters),
                item_count=0,
                started_at=_utc_now(),
            )
            session.add(run)
        run.status = "failed"
        run.error = {"message": str(error), "type": type(error).__name__}
        run.completed_at = _utc_now()
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "could not record provider run failure",
            extra={"provider": provider, "run_type": run_type, "run_id": str(run_id)},
        )


@shared_task(name="workers.provider_data_tasks.run_fred_macro_ingestion", base=Stage1Task)
def run_fred_macro_ingestion(
    series_ids: Sequence[str] | None = None,
    observation_start: str | None = None,
    observation_end: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run FRED macro ingestion when ``FRED_API_KEY`` is configured."""
    series = _sorted_strings(series_ids)
    start_date = _parse_date(observation_start)
    end_date = _parse_date(observation_end)

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_fred_series(
            session,
            provider,
            series_ids=series,
            observation_start=start_date,
            observation_end=end_date,
            limit=limit,
            provider_run_id=run_id,
        )

    return _record_provider_run(
        provider="fred",
        run_type="macro_observations",
        parameters={
            "limit": limit,
            "observation_end": observation_end,
            "observation_start": observation_start,
            "series_ids": series,
        },
        provider_binding=_fred_provider_binding(),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_sec_company_ingestion", base=Stage1Task)
def run_sec_company_ingestion(ciks: Sequence[str] | None = None) -> dict[str, Any]:
    """Run SEC EDGAR company ingestion when a real User-Agent is configured."""
    normalized_ciks = _sorted_strings(ciks)

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_sec_companies(
            session,
            provider,
            ciks=normalized_ciks,
            provider_run_id=run_id,
        )

    return _record_provider_run(
        provider="sec-edgar",
        run_type="company_filings",
        parameters={"ciks": normalized_ciks},
        provider_binding=_sec_provider_binding(),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_sec_identity_refresh", base=Stage1Task)
def run_sec_identity_refresh(period: str | None = None) -> dict[str, Any]:
    """Weekly SEC ``company_tickers.json`` identity seed (ADR 0006 source 1, precedence 1).

    The seed is the whole file, so this task takes no candidate list: it is the source that
    *creates* the bounded candidate set the GLEIF and Wikidata refreshes later read.
    """

    refresh_period = period or _weekly_period(_utc_now())

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_sec_company_tickers(session, provider, provider_run_id=run_id)

    return _record_provider_run(
        provider="sec-edgar",
        run_type="company_identity",
        # The period is what lets a *scheduled* refresh actually refresh. The run key is
        # derived from the parameters, and a succeeded run short-circuits as idempotent, so
        # a constant parameter set would make the second week a no-op forever. Keyed by week,
        # a retry inside the week resumes that week's run and the next week opens its own.
        parameters={"period": refresh_period},
        provider_binding=_sec_identity_provider_binding(),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_gdelt_raw_ingestion", base=Stage1Task)
def run_gdelt_raw_ingestion(
    query: str | None = None,
    lookback_days: int = 1,
    max_records: int = 50,
) -> dict[str, Any]:
    """Run GDELT raw article/event ingestion."""
    normalized_query = query or ""
    days = int(lookback_days)
    end_at = _utc_now()
    start_at = end_at - datetime.timedelta(days=days)

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_gdelt_raw_items(
            session,
            provider,
            query=normalized_query,
            start_at=start_at,
            end_at=end_at,
            max_records=max_records,
            provider_run_id=run_id,
        )

    return _record_provider_run(
        provider="gdelt",
        run_type="raw_items",
        parameters={
            "lookback_days": days,
            "max_records": max_records,
            "query": normalized_query,
            "window_end": end_at.isoformat(timespec="seconds"),
            "window_start": start_at.isoformat(timespec="seconds"),
        },
        provider_binding=_gdelt_provider_binding(),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_sanctions_ingestion", base=Stage1Task)
def run_sanctions_ingestion(
    program: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run sanctions ingestion from the configured sanctions provider."""

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_sanctions_entities(
            session,
            provider,
            program=program,
            limit=limit,
            provider_run_id=run_id,
        )

    return _record_provider_run(
        provider="ofac",
        run_type="sanctions_entities",
        parameters={"limit": limit, "program": program},
        provider_binding=_ofac_provider_binding(),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_entity_identity_ingestion", base=Stage1Task)
def run_entity_identity_ingestion(
    curated_watchlist: Sequence[str] | None = None,
    country_code: str | None = None,
    limit: int | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """Monthly GLEIF enrichment of SEC-seeded profiles plus the curated watchlist (ADR 0006).

    Bounded on both sides: the service searches only the configured watchlist names and the
    profiles a previous identity run already seeded, so an empty watchlist and an empty store
    mean no provider call at all.
    """

    settings = get_settings()
    # An omitted watchlist means "use the configured one"; an explicit empty list means
    # "SEC-seeded profiles only", so an ad-hoc call can narrow a run without editing config.
    watchlist = _sorted_strings(
        settings.identity_watchlist_names if curated_watchlist is None else curated_watchlist
    )
    search_limit = settings.gleif_search_limit if limit is None else int(limit)
    refresh_period = period or _monthly_period(_utc_now())

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_entity_identity_records(
            session,
            provider,
            curated_watchlist=watchlist,
            country_code=country_code,
            limit=search_limit,
            provider_run_id=run_id,
        )

    return _record_provider_run(
        provider="gleif",
        run_type="entity_identity",
        parameters={
            "country_code": country_code,
            "curated_watchlist": watchlist,
            "limit": search_limit,
            "period": refresh_period,
        },
        provider_binding=_gleif_provider_binding(),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_wikidata_identity_ingestion", base=Stage1Task)
def run_wikidata_identity_ingestion(
    curated_qids: Sequence[str] | None = None,
    batch_size: int | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """Monthly Wikidata enrichment at precedence 3 (ADR 0006 source 3).

    Bounded by construction: the provider only ever sees explicitly configured QIDs and the
    QIDs that CIK/LEI values already seeded on EntityProfiles resolve to. There is no
    free-text lookup and no crawl, so this never widens the entity set on its own.
    """

    settings = get_settings()
    qids = _sorted_strings(
        settings.wikidata_qid_seed_list if curated_qids is None else curated_qids
    )
    size = settings.wikidata_batch_size if batch_size is None else int(batch_size)
    refresh_period = period or _monthly_period(_utc_now())

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_wikidata_identities(
            session,
            provider,
            curated_qids=qids,
            batch_size=size,
            provider_run_id=run_id,
        )

    return _record_provider_run(
        provider="wikidata",
        run_type="entity_identity",
        parameters={
            "batch_size": size,
            "curated_qids": qids,
            "period": refresh_period,
        },
        provider_binding=_wikidata_provider_binding(),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_country_indicator_ingestion", base=Stage1Task)
def run_country_indicator_ingestion(
    country_codes: Sequence[str] | None = None,
    indicator_ids: Sequence[str] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run World Bank country indicator ingestion."""
    countries = _sorted_strings(country_codes)
    indicators = _sorted_strings(indicator_ids)

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_country_indicators(
            session,
            provider,
            country_codes=countries,
            indicator_ids=indicators,
            start_year=start_year,
            end_year=end_year,
            limit=limit,
            provider_run_id=run_id,
        )

    return _record_provider_run(
        provider="world-bank",
        run_type="country_indicators",
        parameters={
            "country_codes": countries,
            "end_year": end_year,
            "indicator_ids": indicators,
            "limit": limit,
            "start_year": start_year,
        },
        provider_binding=_world_bank_provider_binding(),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_humanitarian_report_ingestion", base=Stage1Task)
def run_humanitarian_report_ingestion(
    query: str | None = None,
    country_code: str | None = None,
    disaster_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Run ReliefWeb humanitarian report ingestion."""
    normalized_query = query or ""

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_humanitarian_reports(
            session,
            provider,
            query=normalized_query,
            country_code=country_code,
            disaster_type=disaster_type,
            limit=limit,
            provider_run_id=run_id,
        )

    return _record_provider_run(
        provider="reliefweb",
        run_type="humanitarian_reports",
        parameters={
            "country_code": country_code,
            "disaster_type": disaster_type,
            "limit": limit,
            "query": normalized_query,
        },
        provider_binding=_reliefweb_provider_binding(),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_geo_incident_ingestion", base=Stage1Task)
def run_geo_incident_ingestion(
    source: str = "usgs",
    region: str | None = None,
    lookback_days: int = 1,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run geospatial incident ingestion from USGS or NASA FIRMS."""
    normalized_source = source.strip().casefold()
    end_at = _utc_now()
    start_at = end_at - datetime.timedelta(days=int(lookback_days))

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_geo_incidents(
            session,
            provider,
            start_at=start_at,
            end_at=end_at,
            region=region,
            limit=limit,
            provider_run_id=run_id,
            provider_name=normalized_source,
        )

    return _record_provider_run(
        provider=normalized_source,
        run_type="geo_incidents",
        parameters={
            "limit": limit,
            "lookback_days": int(lookback_days),
            "region": region,
            "source": normalized_source,
            "window_end": end_at.isoformat(timespec="seconds"),
            "window_start": start_at.isoformat(timespec="seconds"),
        },
        provider_binding=_geo_provider_binding(normalized_source),
        ingest=ingest,
    )


@shared_task(name="workers.provider_data_tasks.run_energy_series_ingestion", base=Stage1Task)
def run_energy_series_ingestion(
    series_ids: Sequence[str] | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run EIA energy time-series ingestion when ``EIA_API_KEY`` is configured."""
    series = _sorted_strings(series_ids)
    start_dt = _parse_datetime(start_at)
    end_dt = _parse_datetime(end_at)

    def ingest(session: Any, run_id: uuid.UUID, provider: Any) -> IngestionResult:
        return ingest_energy_series(
            session,
            provider,
            series_ids=series,
            start_at=start_dt,
            end_at=end_dt,
            limit=limit,
            provider_run_id=run_id,
        )

    return _record_provider_run(
        provider="eia",
        run_type="energy_series",
        parameters={
            "end_at": end_at,
            "limit": limit,
            "series_ids": series,
            "start_at": start_at,
        },
        provider_binding=_eia_provider_binding(),
        ingest=ingest,
    )


def _fred_provider_binding() -> ProviderBinding:
    settings = get_settings()
    if not settings.fred_api_key:
        return ProviderBinding(
            provider=None,
            mode="configuration_skipped",
            unavailable_reason="FRED_API_KEY is not configured",
        )
    return ProviderBinding(
        provider=FREDClient(api_key=settings.fred_api_key),
        mode="provider_ingestion",
        network_called=True,
    )


def _is_placeholder_user_agent(value: str, variable: str) -> bool:
    """True while a fair-access User-Agent is still the placeholder the repo ships with."""

    return not value.strip() or f"configure {variable}" in value


def _sec_provider_binding() -> ProviderBinding:
    settings = get_settings()
    if _is_placeholder_user_agent(settings.sec_user_agent, "SEC_USER_AGENT"):
        return ProviderBinding(
            provider=None,
            mode="configuration_skipped",
            unavailable_reason="SEC_USER_AGENT is not configured",
        )
    return ProviderBinding(
        provider=SECEdgarClient(
            user_agent=settings.sec_user_agent,
            timeout=settings.sec_timeout_seconds,
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _sec_identity_provider_binding() -> ProviderBinding:
    """Bind the SEC client for the company_tickers identity seed (ADR 0006 source 1)."""

    settings = get_settings()
    # SEC fair access requires a descriptive, contactable User-Agent; the shipped placeholder
    # would get the scheduled job blocked, so an unconfigured deployment records a skipped run.
    if _is_placeholder_user_agent(settings.sec_user_agent, "SEC_USER_AGENT"):
        return ProviderBinding(
            provider=None,
            mode="configuration_skipped",
            unavailable_reason="SEC_USER_AGENT is not configured",
        )
    return ProviderBinding(
        provider=SECEdgarClient(
            user_agent=settings.sec_user_agent,
            company_tickers_url=settings.sec_company_tickers_url,
            timeout=settings.sec_timeout_seconds,
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _wikidata_provider_binding() -> ProviderBinding:
    """Bind the bounded WDQS client for the monthly enrichment (ADR 0006 source 3)."""

    settings = get_settings()
    # WDQS enforces a User-Agent policy of its own; a generic agent is rate-limited or blocked.
    if _is_placeholder_user_agent(settings.wikidata_user_agent, "WIKIDATA_USER_AGENT"):
        return ProviderBinding(
            provider=None,
            mode="configuration_skipped",
            unavailable_reason="WIKIDATA_USER_AGENT is not configured",
        )
    return ProviderBinding(
        provider=WikidataClient(
            user_agent=settings.wikidata_user_agent,
            endpoint=settings.wikidata_sparql_endpoint,
            timeout=settings.wikidata_timeout_seconds,
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _gdelt_provider_binding() -> ProviderBinding:
    settings = get_settings()
    base_url = settings.gdelt_base_url.rstrip("/")
    return ProviderBinding(
        provider=GDELTClient(
            doc_endpoint=f"{base_url}/doc/doc",
            events_endpoint=f"{base_url}/events/search",
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _ofac_provider_binding() -> ProviderBinding:
    settings = get_settings()
    base_url = settings.ofac_base_url.rstrip("/")
    return ProviderBinding(
        provider=OFACClient(
            entities_endpoint=f"{base_url}/api/PublicationPreview/exports/SDN.JSON",
            deltas_endpoint=f"{base_url}/api/PublicationPreview/exports/SDN_DELTA.JSON",
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _gleif_provider_binding() -> ProviderBinding:
    settings = get_settings()
    base_url = settings.gleif_base_url.rstrip("/")
    return ProviderBinding(
        provider=GLEIFClient(
            records_endpoint=f"{base_url}/lei-records",
            relationships_endpoint=f"{base_url}/relationship-records",
            user_agent=settings.gleif_user_agent,
            timeout=settings.gleif_timeout_seconds,
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _world_bank_provider_binding() -> ProviderBinding:
    settings = get_settings()
    base_url = settings.world_bank_base_url.rstrip("/")
    return ProviderBinding(
        provider=WorldBankClient(
            indicator_endpoint=f"{base_url}/indicator/{{indicator_id}}",
            observations_endpoint=f"{base_url}/country/{{country_code}}/indicator/{{indicator_id}}",
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _reliefweb_provider_binding() -> ProviderBinding:
    settings = get_settings()
    base_url = settings.reliefweb_base_url.rstrip("/")
    if not settings.reliefweb_app_name.strip():
        return ProviderBinding(
            provider=None,
            mode="configuration_skipped",
            unavailable_reason="RELIEFWEB_APP_NAME is not configured",
        )
    return ProviderBinding(
        provider=ReliefWebClient(
            app_name=settings.reliefweb_app_name,
            reports_endpoint=f"{base_url}/reports",
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _geo_provider_binding(source: str) -> ProviderBinding:
    settings = get_settings()
    if source in {"nasa", "nasa-firms", "firms"}:
        if not settings.nasa_firms_map_key:
            return ProviderBinding(
                provider=None,
                mode="configuration_skipped",
                unavailable_reason="NASA_FIRMS_MAP_KEY is not configured",
            )
        return ProviderBinding(
            provider=NASAFIRMSClient(
                map_key=settings.nasa_firms_map_key,
                fires_endpoint=f"{settings.nasa_firms_base_url.rstrip('/')}/area/json",
            ),
            mode="provider_ingestion",
            network_called=True,
        )
    return ProviderBinding(
        provider=USGSEarthquakeClient(
            earthquake_endpoint=f"{settings.usgs_earthquake_base_url.rstrip('/')}/query",
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _eia_provider_binding() -> ProviderBinding:
    settings = get_settings()
    if not settings.eia_api_key:
        return ProviderBinding(
            provider=None,
            mode="configuration_skipped",
            unavailable_reason="EIA_API_KEY is not configured",
        )
    return ProviderBinding(
        provider=EIAClient(
            api_key=settings.eia_api_key,
            series_endpoint=f"{settings.eia_base_url.rstrip('/')}/seriesid/{{series_id}}",
        ),
        mode="provider_ingestion",
        network_called=True,
    )


def _parse_date(value: str | None) -> datetime.date | None:
    if value in (None, ""):
        return None
    return datetime.date.fromisoformat(str(value))


def _parse_datetime(value: str | None) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)
