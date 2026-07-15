"""Read-only API endpoints for persisted external provider data."""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from db.base import get_session
from db.models import (
    CountryContextSnapshot,
    CountryIndicatorObservation,
    CountryIndicatorSeries,
    EnergyMarketSnapshot,
    EntityProfile,
    GeoIncident,
    HumanitarianReport,
    MacroObservation,
    MacroSeries,
    ProviderRun,
    RawIngestionItem,
    SanctionsAlias,
    SanctionsEntity,
    SanctionsIdentifier,
    SanctionsMatch,
    SECCompany,
    SECCompanyFact,
    SECFiling,
)
from services.company_research import (
    build_company_research_profile,
    validate_company_research_profile,
)

router = APIRouter(prefix="/api/v1/provider-data", tags=["provider-data"])
company_research_router = APIRouter(prefix="/api/v1/company-research", tags=["company-research"])


def _iso(value: datetime.date | datetime.datetime | None) -> str | None:
    """Render a date/datetime for the wire: UTC ISO 8601 with a trailing `Z`.

    Naive datetimes are stored as UTC, so they are stamped as UTC rather than emitted
    without an offset -- the adapter cannot guess a timezone (api-adapter-contract). A
    plain date keeps its date-only ISO form. Mirrors ``apps.api.intelligence._iso``.
    """
    if value is None:
        return None
    if not isinstance(value, datetime.datetime):
        return value.isoformat()
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.date | datetime.datetime):
        return _iso(value)
    return value


def _serialize_provider_run(run: ProviderRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "run_key": run.run_key,
        "provider": run.provider,
        "run_type": run.run_type,
        "status": run.status,
        "parameters": run.parameters,
        "stats": run.stats,
        "error": run.error,
        "item_count": run.item_count,
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


def _serialize_macro_series(series: MacroSeries) -> dict[str, Any]:
    return {
        "id": str(series.id),
        "provider": series.provider,
        "series_id": series.series_id,
        "title": series.title,
        "frequency": series.frequency,
        "units": series.units,
        "seasonal_adjustment": series.seasonal_adjustment,
        "country": series.country,
        "source": series.source,
        "metadata": series.series_metadata,
    }


def _serialize_macro_observation(observation: MacroObservation) -> dict[str, Any]:
    return {
        "id": str(observation.id),
        "observed_on": _iso(observation.observed_on),
        "value": _json_value(observation.value),
        "raw_value": observation.raw_value,
        "realtime_start": _iso(observation.realtime_start),
        "realtime_end": _iso(observation.realtime_end),
        "metadata": observation.observation_metadata,
    }


def _serialize_sec_company(company: SECCompany) -> dict[str, Any]:
    return {
        "id": str(company.id),
        "cik": company.cik,
        "name": company.name,
        "ticker": company.ticker,
        "exchange": company.exchange,
        "sic": company.sic,
        "sic_description": company.sic_description,
        "fiscal_year_end": company.fiscal_year_end,
        "entity_type": company.entity_type,
        "metadata": company.company_metadata,
    }


def _serialize_sec_filing(filing: SECFiling, company: SECCompany | None = None) -> dict[str, Any]:
    return {
        "id": str(filing.id),
        "company_id": str(filing.company_id),
        "cik": company.cik if company is not None else None,
        "company_name": company.name if company is not None else None,
        "ticker": company.ticker if company is not None else None,
        "accession_number": filing.accession_number,
        "form_type": filing.form_type,
        "filing_date": _iso(filing.filing_date),
        "report_date": _iso(filing.report_date),
        "primary_document_url": filing.primary_document_url,
        "filing_detail_url": filing.filing_detail_url,
        "metadata": filing.filing_metadata,
        "created_at": _iso(filing.created_at),
    }


def _serialize_sanctions_entity(entity: SanctionsEntity) -> dict[str, Any]:
    return {
        "id": str(entity.id),
        "provider": entity.provider,
        "list_code": entity.list_code,
        "entity_uid": entity.entity_uid,
        "entity_type": entity.entity_type,
        "primary_name": entity.primary_name,
        "normalized_name": entity.normalized_name,
        "country": entity.country,
        "programs": entity.programs,
        "remarks": entity.remarks,
        "first_seen_at": _iso(entity.first_seen_at),
        "last_seen_at": _iso(entity.last_seen_at),
        "created_at": _iso(entity.created_at),
        "updated_at": _iso(entity.updated_at),
    }


def _serialize_sanctions_alias(alias: SanctionsAlias) -> dict[str, Any]:
    return {
        "id": str(alias.id),
        "alias_name": alias.alias_name,
        "normalized_alias": alias.normalized_alias,
        "alias_type": alias.alias_type,
        "quality": alias.quality,
    }


def _serialize_sanctions_identifier(identifier: SanctionsIdentifier) -> dict[str, Any]:
    return {
        "id": str(identifier.id),
        "identifier_type": identifier.identifier_type,
        "identifier_value": identifier.identifier_value,
        "country": identifier.country,
        "issue_date": _iso(identifier.issue_date),
        "expiry_date": _iso(identifier.expiry_date),
    }


def _serialize_sanctions_match(match: SanctionsMatch) -> dict[str, Any]:
    return {
        "id": str(match.id),
        "target_type": match.target_type,
        "target_id": match.target_id,
        "sanctions_entity_id": str(match.sanctions_entity_id),
        "match_method": match.match_method,
        "match_score": _json_value(match.match_score),
        "matched_name": match.matched_name,
        "explanation": match.explanation,
        "review_status": match.review_status,
        "created_at": _iso(match.created_at),
    }


def _serialize_sanctions_change(entity: SanctionsEntity) -> dict[str, Any]:
    return {
        "change_type": "current_record",
        "changed_at": _iso(entity.last_seen_at or entity.updated_at),
        "entity": _serialize_sanctions_entity(entity),
    }


def _serialize_entity_profile(profile: EntityProfile) -> dict[str, Any]:
    return {
        "id": str(profile.id),
        "canonical_name": profile.canonical_name,
        "normalized_name": profile.normalized_name,
        "entity_type": profile.entity_type,
        "country": profile.country,
        "primary_ticker": profile.primary_ticker,
        "primary_cik": profile.primary_cik,
        "primary_lei": profile.primary_lei,
        "website": profile.website,
        "metadata": profile.profile_metadata,
        "created_at": _iso(profile.created_at),
        "updated_at": _iso(profile.updated_at),
    }


def _serialize_lei_record(profile: EntityProfile) -> dict[str, Any]:
    return {
        "entity_profile_id": str(profile.id),
        "lei": profile.primary_lei,
        "legal_name": profile.canonical_name,
        "entity_status": profile.profile_metadata.get("entity_status")
        if isinstance(profile.profile_metadata, Mapping)
        else None,
        "registration_status": profile.profile_metadata.get("registration_status")
        if isinstance(profile.profile_metadata, Mapping)
        else None,
        "country_code": profile.country,
        "jurisdiction": profile.profile_metadata.get("jurisdiction")
        if isinstance(profile.profile_metadata, Mapping)
        else None,
        "profile": _serialize_entity_profile(profile),
    }


def _serialize_country_indicator_series(series: CountryIndicatorSeries) -> dict[str, Any]:
    return {
        "id": str(series.id),
        "provider": series.provider,
        "indicator_id": series.indicator_id,
        "title": series.title,
        "description": series.description,
        "unit": series.unit,
        "frequency": series.frequency,
        "topic": series.topic,
        "source": series.source,
        "metadata": series.series_metadata,
    }


def _serialize_country_indicator_observation(
    observation: CountryIndicatorObservation,
    series: CountryIndicatorSeries | None = None,
) -> dict[str, Any]:
    item = {
        "id": str(observation.id),
        "series_id": str(observation.series_id),
        "country_code": observation.country_code,
        "country_name": observation.country_name,
        "observed_on": _iso(observation.observed_on),
        "value": _json_value(observation.value),
        "raw_value": observation.raw_value,
        "metadata": observation.observation_metadata,
    }
    if series is not None:
        item["series"] = _serialize_country_indicator_series(series)
    return item


def _serialize_country_context_snapshot(snapshot: CountryContextSnapshot) -> dict[str, Any]:
    return {
        "id": str(snapshot.id),
        "country_code": snapshot.country_code,
        "snapshot_date": _iso(snapshot.snapshot_date),
        "sovereign_context": snapshot.sovereign_context,
        "governance_context": snapshot.governance_context,
        "debt_context": snapshot.debt_context,
        "trade_context": snapshot.trade_context,
        "development_context": snapshot.development_context,
        "metadata": snapshot.snapshot_metadata,
        "created_at": _iso(snapshot.created_at),
    }


def _serialize_humanitarian_report(report: HumanitarianReport) -> dict[str, Any]:
    return {
        "id": str(report.id),
        "provider": report.provider,
        "external_id": report.external_id,
        "title": report.title,
        "url": report.url,
        "published_at": _iso(report.published_at),
        "country_codes": report.country_codes,
        "disaster_types": report.disaster_types,
        "organizations": report.organizations,
        "themes": report.themes,
        "summary": report.summary,
        "body_excerpt": report.body_excerpt,
        "created_at": _iso(report.created_at),
    }


def _serialize_geo_incident(incident: GeoIncident) -> dict[str, Any]:
    return {
        "id": str(incident.id),
        "provider": incident.provider,
        "external_id": incident.external_id,
        "incident_type": incident.incident_type,
        "title": incident.title,
        "country_code": incident.country_code,
        "region": incident.region,
        "latitude": _json_value(incident.latitude),
        "longitude": _json_value(incident.longitude),
        "magnitude": _json_value(incident.magnitude),
        "severity": incident.severity,
        "observed_at": _iso(incident.observed_at),
        "updated_at": _iso(incident.updated_at),
        "source_url": incident.source_url,
        "created_at": _iso(incident.created_at),
    }


def _serialize_energy_market_snapshot(snapshot: EnergyMarketSnapshot) -> dict[str, Any]:
    return {
        "id": str(snapshot.id),
        "provider": snapshot.provider,
        "region": snapshot.region,
        "commodity": snapshot.commodity,
        "snapshot_date": _iso(snapshot.snapshot_date),
        "price": _json_value(snapshot.price),
        "inventory": _json_value(snapshot.inventory),
        "production": _json_value(snapshot.production),
        "consumption": _json_value(snapshot.consumption),
        "imports": _json_value(snapshot.imports),
        "exports": _json_value(snapshot.exports),
        "metadata": snapshot.snapshot_metadata,
        "created_at": _iso(snapshot.created_at),
    }


def _serialize_energy_series(snapshot: EnergyMarketSnapshot) -> dict[str, Any]:
    return {
        "series_key": f"{snapshot.provider}:{snapshot.region}:{snapshot.commodity}",
        "provider": snapshot.provider,
        "region": snapshot.region,
        "commodity": snapshot.commodity,
        "latest_snapshot_date": _iso(snapshot.snapshot_date),
        "latest_snapshot": _serialize_energy_market_snapshot(snapshot),
    }


def _serialize_summary_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider": row["provider"],
        "item_type": row["item_type"],
        "count": row["count"],
        "latest_created_at": _iso(row["latest_created_at"]),
    }


class ProviderDataRepository:
    """SQLAlchemy-backed read model for provider-data endpoints."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_provider_runs(
        self,
        *,
        provider: str | None,
        status: str | None,
        limit: int,
    ) -> list[ProviderRun]:
        stmt = select(ProviderRun).order_by(ProviderRun.created_at.desc()).limit(limit)
        if provider:
            stmt = stmt.where(ProviderRun.provider == provider)
        if status:
            stmt = stmt.where(ProviderRun.status == status)
        return list(self.session.execute(stmt).scalars().all())

    def list_macro_series(
        self,
        *,
        provider: str | None,
        country: str | None,
        q: str | None,
        limit: int,
    ) -> list[MacroSeries]:
        stmt = select(MacroSeries).order_by(MacroSeries.provider, MacroSeries.series_id).limit(limit)
        if provider:
            stmt = stmt.where(MacroSeries.provider == provider)
        if country:
            stmt = stmt.where(MacroSeries.country == country)
        if q:
            stmt = stmt.where(MacroSeries.title.ilike(f"%{q}%"))
        return list(self.session.execute(stmt).scalars().all())

    def get_macro_observations(
        self,
        *,
        series_id: str,
        provider: str | None,
        limit: int,
    ) -> tuple[MacroSeries | None, list[MacroObservation]]:
        series_stmt = select(MacroSeries).where(MacroSeries.series_id == series_id).limit(1)
        if provider:
            series_stmt = series_stmt.where(MacroSeries.provider == provider)
        series = self.session.execute(series_stmt).scalars().first()
        if series is None:
            return None, []

        observations_stmt = (
            select(MacroObservation)
            .where(MacroObservation.series_uuid == series.id)
            .order_by(MacroObservation.observed_on.desc())
            .limit(limit)
        )
        observations = list(self.session.execute(observations_stmt).scalars().all())
        return series, observations

    def _filtered_sec_companies(
        self, *, cik: str | None, ticker: str | None, q: str | None
    ) -> Select[tuple[SECCompany]]:
        """The SEC-company relation with only the WHERE filters applied (no order/limit/offset).

        Shared by :meth:`list_sec_companies` and :meth:`count_sec_companies` so the page and its
        ``total`` filter on identical predicates.
        """
        stmt = select(SECCompany)
        if cik:
            stmt = stmt.where(SECCompany.cik == cik.zfill(10))
        if ticker:
            stmt = stmt.where(func.upper(SECCompany.ticker) == ticker.upper())
        if q:
            stmt = stmt.where(SECCompany.name.ilike(f"%{q}%"))
        return stmt

    def list_sec_companies(
        self,
        *,
        cik: str | None,
        ticker: str | None,
        q: str | None,
        limit: int,
        offset: int = 0,
    ) -> list[SECCompany]:
        # `id` is a unique tie-breaker so equal names page deterministically under offset.
        stmt = (
            self._filtered_sec_companies(cik=cik, ticker=ticker, q=q)
            .order_by(SECCompany.name, SECCompany.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def count_sec_companies(self, *, cik: str | None, ticker: str | None, q: str | None) -> int:
        """COUNT of SEC companies matching the filters, before LIMIT/OFFSET."""
        stmt = select(func.count()).select_from(
            self._filtered_sec_companies(cik=cik, ticker=ticker, q=q).subquery()
        )
        return int(self.session.execute(stmt).scalar_one())

    def get_sec_company_by_id(self, company_id: uuid.UUID) -> SECCompany | None:
        return self.session.execute(select(SECCompany).where(SECCompany.id == company_id)).scalars().first()

    def list_sec_filings(
        self,
        *,
        cik: str | None,
        ticker: str | None,
        form_type: str | None,
        limit: int,
    ) -> list[tuple[SECFiling, SECCompany]]:
        stmt = (
            select(SECFiling, SECCompany)
            .join(SECCompany, SECFiling.company_id == SECCompany.id)
            .order_by(SECFiling.filing_date.desc().nullslast(), SECFiling.created_at.desc())
            .limit(limit)
        )
        if cik:
            stmt = stmt.where(SECCompany.cik == cik.zfill(10))
        if ticker:
            stmt = stmt.where(func.upper(SECCompany.ticker) == ticker.upper())
        if form_type:
            stmt = stmt.where(SECFiling.form_type == form_type.upper())
        return [(filing, company) for filing, company in self.session.execute(stmt).all()]

    def list_sec_company_facts(
        self,
        *,
        cik: str | None,
        ticker: str | None,
        concepts: Sequence[str] | None,
        limit: int,
    ) -> list[SECCompanyFact]:
        stmt = (
            select(SECCompanyFact, SECCompany)
            .join(SECCompany, SECCompanyFact.company_id == SECCompany.id)
            .order_by(
                SECCompanyFact.period_end.desc().nullslast(),
                SECCompanyFact.filed_at.desc().nullslast(),
                SECCompanyFact.concept,
            )
            .limit(limit)
        )
        if cik:
            stmt = stmt.where(SECCompany.cik == cik.zfill(10))
        if ticker:
            stmt = stmt.where(func.upper(SECCompany.ticker) == ticker.upper())
        if concepts:
            stmt = stmt.where(SECCompanyFact.concept.in_(list(concepts)))
        return [fact for fact, _company in self.session.execute(stmt).all()]

    def list_sanctions_entities(
        self,
        *,
        provider: str | None,
        list_code: str | None,
        country: str | None,
        q: str | None,
        limit: int,
    ) -> list[SanctionsEntity]:
        stmt = (
            select(SanctionsEntity)
            .order_by(SanctionsEntity.last_seen_at.desc().nullslast(), SanctionsEntity.primary_name)
            .limit(limit)
        )
        if provider:
            stmt = stmt.where(SanctionsEntity.provider == provider)
        if list_code:
            stmt = stmt.where(SanctionsEntity.list_code == list_code)
        if country:
            stmt = stmt.where(func.upper(SanctionsEntity.country) == country.upper())
        if q:
            like_q = f"%{q}%"
            stmt = stmt.where(
                or_(
                    SanctionsEntity.primary_name.ilike(like_q),
                    SanctionsEntity.normalized_name.ilike(like_q),
                )
            )
        return list(self.session.execute(stmt).scalars().all())

    def get_sanctions_entity(
        self, *, entity_id: uuid.UUID
    ) -> tuple[
        SanctionsEntity | None,
        list[SanctionsAlias],
        list[SanctionsIdentifier],
        list[SanctionsMatch],
    ]:
        entity = (
            self.session.execute(select(SanctionsEntity).where(SanctionsEntity.id == entity_id))
            .scalars()
            .first()
        )
        if entity is None:
            return None, [], [], []

        aliases = list(
            self.session.execute(
                select(SanctionsAlias)
                .where(SanctionsAlias.sanctions_entity_id == entity_id)
                .order_by(SanctionsAlias.alias_name)
            )
            .scalars()
            .all()
        )
        identifiers = list(
            self.session.execute(
                select(SanctionsIdentifier)
                .where(SanctionsIdentifier.sanctions_entity_id == entity_id)
                .order_by(SanctionsIdentifier.identifier_type, SanctionsIdentifier.identifier_value)
            )
            .scalars()
            .all()
        )
        matches = list(
            self.session.execute(
                select(SanctionsMatch)
                .where(SanctionsMatch.sanctions_entity_id == entity_id)
                .order_by(SanctionsMatch.created_at.desc())
            )
            .scalars()
            .all()
        )
        return entity, aliases, identifiers, matches

    def list_recent_sanctions_changes(
        self,
        *,
        provider: str | None,
        since: datetime.datetime | None,
        limit: int,
    ) -> list[SanctionsEntity]:
        stmt = (
            select(SanctionsEntity)
            .order_by(SanctionsEntity.last_seen_at.desc().nullslast(), SanctionsEntity.primary_name)
            .limit(limit)
        )
        if provider:
            stmt = stmt.where(SanctionsEntity.provider == provider)
        if since is not None:
            stmt = stmt.where(SanctionsEntity.last_seen_at >= since)
        return list(self.session.execute(stmt).scalars().all())

    def list_entity_profiles(
        self,
        *,
        country: str | None,
        entity_type: str | None,
        q: str | None,
        limit: int,
    ) -> list[EntityProfile]:
        stmt = select(EntityProfile).order_by(EntityProfile.canonical_name).limit(limit)
        if country:
            stmt = stmt.where(func.upper(EntityProfile.country) == country.upper())
        if entity_type:
            stmt = stmt.where(EntityProfile.entity_type == entity_type)
        if q:
            like_q = f"%{q}%"
            stmt = stmt.where(
                or_(
                    EntityProfile.canonical_name.ilike(like_q),
                    EntityProfile.normalized_name.ilike(like_q),
                    EntityProfile.primary_ticker.ilike(like_q),
                    EntityProfile.primary_lei.ilike(like_q),
                )
            )
        return list(self.session.execute(stmt).scalars().all())

    def list_lei_records(
        self,
        *,
        country: str | None,
        q: str | None,
        limit: int,
    ) -> list[EntityProfile]:
        stmt = (
            select(EntityProfile)
            .where(EntityProfile.primary_lei.is_not(None))
            .order_by(EntityProfile.canonical_name)
            .limit(limit)
        )
        if country:
            stmt = stmt.where(func.upper(EntityProfile.country) == country.upper())
        if q:
            like_q = f"%{q}%"
            stmt = stmt.where(
                or_(
                    EntityProfile.canonical_name.ilike(like_q),
                    EntityProfile.normalized_name.ilike(like_q),
                    EntityProfile.primary_lei.ilike(like_q),
                )
            )
        return list(self.session.execute(stmt).scalars().all())

    def list_country_indicator_series(
        self,
        *,
        provider: str | None,
        topic: str | None,
        q: str | None,
        limit: int,
    ) -> list[CountryIndicatorSeries]:
        stmt = (
            select(CountryIndicatorSeries)
            .order_by(CountryIndicatorSeries.provider, CountryIndicatorSeries.indicator_id)
            .limit(limit)
        )
        if provider:
            stmt = stmt.where(CountryIndicatorSeries.provider == provider)
        if topic:
            stmt = stmt.where(CountryIndicatorSeries.topic == topic)
        if q:
            like_q = f"%{q}%"
            stmt = stmt.where(
                or_(
                    CountryIndicatorSeries.indicator_id.ilike(like_q),
                    CountryIndicatorSeries.title.ilike(like_q),
                    CountryIndicatorSeries.description.ilike(like_q),
                )
            )
        return list(self.session.execute(stmt).scalars().all())

    def list_country_indicators(
        self,
        *,
        country_code: str,
        provider: str | None,
        indicator_id: str | None,
        limit: int,
    ) -> tuple[list[tuple[CountryIndicatorObservation, CountryIndicatorSeries]], list[CountryContextSnapshot]]:
        country = country_code.upper()
        stmt = (
            select(CountryIndicatorObservation, CountryIndicatorSeries)
            .join(
                CountryIndicatorSeries,
                CountryIndicatorObservation.series_id == CountryIndicatorSeries.id,
            )
            .where(func.upper(CountryIndicatorObservation.country_code) == country)
            .order_by(CountryIndicatorObservation.observed_on.desc())
            .limit(limit)
        )
        if provider:
            stmt = stmt.where(CountryIndicatorSeries.provider == provider)
        if indicator_id:
            stmt = stmt.where(CountryIndicatorSeries.indicator_id == indicator_id)

        snapshot_stmt = (
            select(CountryContextSnapshot)
            .where(func.upper(CountryContextSnapshot.country_code) == country)
            .order_by(CountryContextSnapshot.snapshot_date.desc())
            .limit(5)
        )
        return (
            [(observation, series) for observation, series in self.session.execute(stmt).all()],
            list(self.session.execute(snapshot_stmt).scalars().all()),
        )

    def list_humanitarian_reports(
        self,
        *,
        provider: str | None,
        country_code: str | None,
        q: str | None,
        limit: int,
    ) -> list[HumanitarianReport]:
        stmt = (
            select(HumanitarianReport)
            .order_by(
                HumanitarianReport.published_at.desc().nullslast(),
                HumanitarianReport.created_at.desc(),
            )
            .limit(limit)
        )
        if provider:
            stmt = stmt.where(HumanitarianReport.provider == provider)
        if country_code:
            stmt = stmt.where(HumanitarianReport.country_codes.contains([country_code.upper()]))
        if q:
            like_q = f"%{q}%"
            stmt = stmt.where(
                or_(HumanitarianReport.title.ilike(like_q), HumanitarianReport.summary.ilike(like_q))
            )
        return list(self.session.execute(stmt).scalars().all())

    def list_geo_incidents(
        self,
        *,
        provider: str | None,
        incident_type: str | None,
        country_code: str | None,
        region: str | None,
        limit: int,
    ) -> list[GeoIncident]:
        stmt = (
            select(GeoIncident)
            .order_by(GeoIncident.observed_at.desc().nullslast(), GeoIncident.created_at.desc())
            .limit(limit)
        )
        if provider:
            stmt = stmt.where(GeoIncident.provider == provider)
        if incident_type:
            stmt = stmt.where(GeoIncident.incident_type == incident_type)
        if country_code:
            stmt = stmt.where(func.upper(GeoIncident.country_code) == country_code.upper())
        if region:
            stmt = stmt.where(GeoIncident.region.ilike(f"%{region}%"))
        return list(self.session.execute(stmt).scalars().all())

    def list_energy_market_snapshots(
        self,
        *,
        provider: str | None,
        region: str | None,
        commodity: str | None,
        limit: int,
    ) -> list[EnergyMarketSnapshot]:
        stmt = (
            select(EnergyMarketSnapshot)
            .order_by(
                EnergyMarketSnapshot.snapshot_date.desc(),
                EnergyMarketSnapshot.region,
                EnergyMarketSnapshot.commodity,
            )
            .limit(limit)
        )
        if provider:
            stmt = stmt.where(EnergyMarketSnapshot.provider == provider)
        if region:
            stmt = stmt.where(EnergyMarketSnapshot.region.ilike(f"%{region}%"))
        if commodity:
            stmt = stmt.where(func.upper(EnergyMarketSnapshot.commodity) == commodity.upper())
        return list(self.session.execute(stmt).scalars().all())

    def list_energy_series(
        self,
        *,
        provider: str | None,
        region: str | None,
        commodity: str | None,
        limit: int,
    ) -> list[EnergyMarketSnapshot]:
        return self.list_energy_market_snapshots(
            provider=provider,
            region=region,
            commodity=commodity,
            limit=limit,
        )

    def raw_items_summary(self, *, provider: str | None) -> list[Mapping[str, Any]]:
        stmt = (
            select(
                RawIngestionItem.provider.label("provider"),
                RawIngestionItem.item_type.label("item_type"),
                func.count(RawIngestionItem.id).label("count"),
                func.max(RawIngestionItem.created_at).label("latest_created_at"),
            )
            .group_by(RawIngestionItem.provider, RawIngestionItem.item_type)
            .order_by(RawIngestionItem.provider, RawIngestionItem.item_type)
        )
        if provider:
            stmt = stmt.where(RawIngestionItem.provider == provider)
        return list(self.session.execute(stmt).mappings().all())


def get_provider_data_repository(
    session: Annotated[Session, Depends(get_session)],
) -> ProviderDataRepository:
    return ProviderDataRepository(session)


RepositoryDep = Annotated[ProviderDataRepository, Depends(get_provider_data_repository)]


def _csv_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _profile_target(company: Any | None, *, ticker: str | None = None, cik: str | None = None) -> dict[str, str | None]:
    return {
        "ticker": ticker or getattr(company, "ticker", None) or getattr(company, "primary_ticker", None),
        "cik": cik or getattr(company, "cik", None) or getattr(company, "primary_cik", None),
    }


def _company_research_profile_for_target(
    repo: ProviderDataRepository,
    *,
    company: Any | None,
    ticker: str | None = None,
    cik: str | None = None,
) -> dict[str, Any]:
    target = _profile_target(company, ticker=ticker, cik=cik)
    facts = repo.list_sec_company_facts(
        cik=target["cik"],
        ticker=target["ticker"],
        concepts=None,
        limit=500,
    )
    filing_rows = repo.list_sec_filings(
        cik=target["cik"],
        ticker=target["ticker"],
        form_type=None,
        limit=20,
    )
    filings = [row[0] for row in filing_rows]
    profile = build_company_research_profile(
        company,
        facts=facts,
        filings=filings,
        requested_ticker=target["ticker"],
        requested_cik=target["cik"],
    )
    _ensure_company_research_contract(profile)
    return profile


def _ensure_company_research_contract(profile: Mapping[str, Any]) -> None:
    errors = validate_company_research_profile(profile)
    if errors:
        raise HTTPException(status_code=500, detail={"message": "company research profile contract violation", "errors": errors})


def _ambiguous_company_research_profile(*, ticker: str, companies: Sequence[Any]) -> dict[str, Any]:
    profile = build_company_research_profile(
        None,
        requested_ticker=ticker,
        requested_name=ticker,
    )
    choices = [
        {
            "name": getattr(company, "name", None),
            "ticker": getattr(company, "ticker", None),
            "cik": getattr(company, "cik", None),
            "exchange": getattr(company, "exchange", None),
        }
        for company in companies
    ]
    profile["identity"]["resolution_status"] = "ambiguous_ticker"
    profile["identity"]["resolution_reason"] = "Multiple SEC company records share this ticker; use CIK or exchange-qualified identity."
    profile["identity"]["ticker_choices"] = choices
    profile["sources"]["ambiguous_ticker"] = True
    _ensure_company_research_contract(profile)
    return profile


def _company_research_profile_for_company_id(repo: ProviderDataRepository, company_id: str) -> dict[str, Any]:
    company: Any | None = None
    try:
        company_uuid = uuid.UUID(str(company_id))
    except (TypeError, ValueError):
        company_uuid = None
    if company_uuid is not None and hasattr(repo, "get_sec_company_by_id"):
        company = repo.get_sec_company_by_id(company_uuid)  # type: ignore[attr-defined]
    if company is None and str(company_id).isdigit():
        companies = repo.list_sec_companies(cik=company_id, ticker=None, q=None, limit=1)
        company = companies[0] if companies else None
    if company is None:
        companies = repo.list_sec_companies(cik=None, ticker=str(company_id), q=None, limit=2)
        if len(companies) > 1:
            return _ambiguous_company_research_profile(ticker=str(company_id), companies=companies)
        company = companies[0] if companies else None
    if company is None:
        profile = build_company_research_profile(None, requested_name=str(company_id))
        _ensure_company_research_contract(profile)
        return profile
    return _company_research_profile_for_target(repo, company=company)


@router.get("/provider-runs")
def list_provider_runs(
    repo: RepositoryDep,
    provider: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    runs = repo.list_provider_runs(provider=provider, status=status, limit=limit)
    return {"items": [_serialize_provider_run(run) for run in runs], "count": len(runs)}


@router.get("/macro/series")
def list_macro_series(
    repo: RepositoryDep,
    provider: str | None = None,
    country: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    series = repo.list_macro_series(provider=provider, country=country, q=q, limit=limit)
    return {"items": [_serialize_macro_series(item) for item in series], "count": len(series)}


@router.get("/macro/series/{series_id}/observations")
def get_macro_observations(
    series_id: str,
    repo: RepositoryDep,
    provider: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    series, observations = repo.get_macro_observations(
        series_id=series_id,
        provider=provider,
        limit=limit,
    )
    if series is None:
        raise HTTPException(status_code=404, detail="macro series not found")
    return {
        "series": _serialize_macro_series(series),
        "observations": [_serialize_macro_observation(item) for item in observations],
        "count": len(observations),
    }


@router.get("/sec/companies")
def list_sec_companies(
    repo: RepositoryDep,
    cik: str | None = None,
    ticker: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    companies = repo.list_sec_companies(cik=cik, ticker=ticker, q=q, limit=limit)
    return {"items": [_serialize_sec_company(item) for item in companies], "count": len(companies)}


@router.get("/sec/filings")
def list_sec_filings(
    repo: RepositoryDep,
    cik: str | None = None,
    ticker: str | None = None,
    form_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    rows = repo.list_sec_filings(cik=cik, ticker=ticker, form_type=form_type, limit=limit)
    return {
        "items": [_serialize_sec_filing(filing, company) for filing, company in rows],
        "count": len(rows),
    }


@company_research_router.get("/profiles")
@router.get("/company-research/profiles")
def list_company_research_profiles(
    repo: RepositoryDep,
    tickers: str | None = None,
    ciks: str | None = None,
    company_ids: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Return checklist-shaped company research profiles with exact pagination metadata.

    The endpoint assembles the requested profiles from persisted provider data. It
    intentionally returns unavailable checklist fields instead of failing when a
    company, fact, filing, market-data, or industry-data input is missing.

    Two modes, both reporting a real ``total`` and honouring ``limit``/``offset``:

    * Explicit selectors (``tickers``/``ciks``/``company_ids``) are a bounded lookup: every
      requested identifier is resolved so no category is silently dropped, ``total`` is the
      full requested count, then the page is sliced. (The ``data.js`` batching-index bug that
      mis-slices unrelated-length selector lists is owned by a later item, not fixed here.)
    * Query mode reports the real filtered SEC-company total and pages the matching companies.
    """

    ticker_values = _csv_values(tickers)
    cik_values = _csv_values(ciks)
    company_id_values = _csv_values(company_ids)

    if ticker_values or cik_values or company_id_values:
        assembled: list[dict[str, Any]] = []
        for company_id in company_id_values:
            assembled.append(_company_research_profile_for_company_id(repo, company_id))
        for ticker in ticker_values:
            companies = repo.list_sec_companies(cik=None, ticker=ticker, q=None, limit=2)
            if len(companies) > 1:
                assembled.append(_ambiguous_company_research_profile(ticker=ticker, companies=companies))
            else:
                company = companies[0] if companies else None
                assembled.append(_company_research_profile_for_target(repo, company=company, ticker=ticker))
        for cik in cik_values:
            companies = repo.list_sec_companies(cik=cik, ticker=None, q=None, limit=1)
            company = companies[0] if companies else None
            assembled.append(_company_research_profile_for_target(repo, company=company, cik=cik))
        total = len(assembled)
        items = assembled[offset : offset + limit]
    else:
        total = repo.count_sec_companies(cik=None, ticker=None, q=q)
        companies = repo.list_sec_companies(cik=None, ticker=None, q=q, limit=limit, offset=offset)
        items = [_company_research_profile_for_target(repo, company=company) for company in companies]

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@company_research_router.get("/profiles/{company_id}")
def get_company_research_profile(company_id: str, repo: RepositoryDep) -> dict[str, Any]:
    return _company_research_profile_for_company_id(repo, company_id)


@router.get("/sanctions/entities")
def list_sanctions_entities(
    repo: RepositoryDep,
    provider: str | None = None,
    list_code: str | None = None,
    country: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    entities = repo.list_sanctions_entities(
        provider=provider,
        list_code=list_code,
        country=country,
        q=q,
        limit=limit,
    )
    return {"items": [_serialize_sanctions_entity(item) for item in entities], "count": len(entities)}


@router.get("/sanctions/entities/{entity_id}")
def get_sanctions_entity(entity_id: uuid.UUID, repo: RepositoryDep) -> dict[str, Any]:
    entity, aliases, identifiers, matches = repo.get_sanctions_entity(entity_id=entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="sanctions entity not found")
    return {
        "entity": _serialize_sanctions_entity(entity),
        "aliases": [_serialize_sanctions_alias(item) for item in aliases],
        "identifiers": [_serialize_sanctions_identifier(item) for item in identifiers],
        "matches": [_serialize_sanctions_match(item) for item in matches],
    }


@router.get("/sanctions/recent-changes")
def list_recent_sanctions_changes(
    repo: RepositoryDep,
    provider: str | None = None,
    since: datetime.datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    changes = repo.list_recent_sanctions_changes(provider=provider, since=since, limit=limit)
    return {"items": [_serialize_sanctions_change(item) for item in changes], "count": len(changes)}


@router.get("/entity-identity/lei-records")
def list_lei_records(
    repo: RepositoryDep,
    country: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    records = repo.list_lei_records(country=country, q=q, limit=limit)
    return {"items": [_serialize_lei_record(item) for item in records], "count": len(records)}


@router.get("/entity-identity/profiles")
def list_entity_profiles(
    repo: RepositoryDep,
    country: str | None = None,
    entity_type: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    profiles = repo.list_entity_profiles(
        country=country,
        entity_type=entity_type,
        q=q,
        limit=limit,
    )
    return {"items": [_serialize_entity_profile(item) for item in profiles], "count": len(profiles)}


@router.get("/country-indicators/series")
def list_country_indicator_series(
    repo: RepositoryDep,
    provider: str | None = None,
    topic: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    series = repo.list_country_indicator_series(
        provider=provider,
        topic=topic,
        q=q,
        limit=limit,
    )
    return {
        "items": [_serialize_country_indicator_series(item) for item in series],
        "count": len(series),
    }


@router.get("/country-indicators/{country_code}")
def list_country_indicators(
    country_code: str,
    repo: RepositoryDep,
    provider: str | None = None,
    indicator_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    rows, snapshots = repo.list_country_indicators(
        country_code=country_code,
        provider=provider,
        indicator_id=indicator_id,
        limit=limit,
    )
    return {
        "country_code": country_code.upper(),
        "observations": [
            _serialize_country_indicator_observation(observation, series)
            for observation, series in rows
        ],
        "context_snapshots": [_serialize_country_context_snapshot(item) for item in snapshots],
        "count": len(rows),
    }


@router.get("/humanitarian/reports")
def list_humanitarian_reports(
    repo: RepositoryDep,
    provider: str | None = None,
    country_code: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    reports = repo.list_humanitarian_reports(
        provider=provider,
        country_code=country_code,
        q=q,
        limit=limit,
    )
    return {"items": [_serialize_humanitarian_report(item) for item in reports], "count": len(reports)}


@router.get("/geo/incidents")
def list_geo_incidents(
    repo: RepositoryDep,
    provider: str | None = None,
    incident_type: str | None = None,
    country_code: str | None = None,
    region: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    incidents = repo.list_geo_incidents(
        provider=provider,
        incident_type=incident_type,
        country_code=country_code,
        region=region,
        limit=limit,
    )
    return {"items": [_serialize_geo_incident(item) for item in incidents], "count": len(incidents)}


@router.get("/energy/market-snapshots")
def list_energy_market_snapshots(
    repo: RepositoryDep,
    provider: str | None = None,
    region: str | None = None,
    commodity: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    snapshots = repo.list_energy_market_snapshots(
        provider=provider,
        region=region,
        commodity=commodity,
        limit=limit,
    )
    return {
        "items": [_serialize_energy_market_snapshot(item) for item in snapshots],
        "count": len(snapshots),
    }


@router.get("/energy/series")
def list_energy_series(
    repo: RepositoryDep,
    provider: str | None = None,
    region: str | None = None,
    commodity: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    series = repo.list_energy_series(
        provider=provider,
        region=region,
        commodity=commodity,
        limit=limit,
    )
    return {"items": [_serialize_energy_series(item) for item in series], "count": len(series)}


@router.get("/raw-items/summary")
def raw_items_summary(repo: RepositoryDep, provider: str | None = None) -> dict[str, Any]:
    rows = repo.raw_items_summary(provider=provider)
    items = [_serialize_summary_row(row) for row in rows]
    return {"items": items, "count": len(items), "total": sum(item["count"] for item in items)}
