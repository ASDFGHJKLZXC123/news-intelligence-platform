"""SEC EDGAR provider ingestion into SEC provider-data tables."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from db.models import SECCompany, SECFiling
from db.models import SECCompanyFact as SECCompanyFactModel
from packages.providers.base import (
    SECCompanyFact as SECCompanyFactDTO,
)
from packages.providers.base import (
    SECEdgarProvider,
    SECSubmission,
)
from services.provider_data.common import (
    IngestionResult,
    add,
    date_from_datetime,
    find_one,
    first_present,
    first_sequence_value,
    json_safe,
    normalize_cik,
    parse_optional_date,
    retain_raw_item,
    to_float_or_none,
)


def ingest_sec_companies(
    session: Any,
    provider: SECEdgarProvider,
    *,
    ciks: Sequence[str],
    provider_run_id: uuid.UUID | None = None,
    provider_name: str = "sec-edgar",
) -> IngestionResult:
    """Fetch SEC submissions/facts and upsert company, filing, and fact rows."""

    fetched = 0
    inserted = 0
    skipped = 0
    companies_inserted = 0
    raw_inserted = 0
    raw_skipped = 0

    for raw_cik in ciks:
        cik = normalize_cik(str(raw_cik))
        submissions = provider.fetch_submissions(cik)
        facts = provider.fetch_company_facts(cik)
        fetched += len(submissions) + len(facts)

        company, company_created = _upsert_company(session, cik=cik, submissions=submissions, facts=facts)
        companies_inserted += int(company_created)

        for submission in submissions:
            _, raw_created = retain_raw_item(
                session,
                provider=provider_name,
                item_type="submission",
                external_id=submission.accession_number,
                payload=submission,
                observed_at=submission.filed_at,
                provider_run_id=provider_run_id,
                identity_parts=(normalize_cik(submission.cik), submission.accession_number),
            )
            raw_inserted += int(raw_created)
            raw_skipped += int(not raw_created)

            was_inserted = _upsert_filing(session, company=company, submission=submission)
            inserted += int(was_inserted)
            skipped += int(not was_inserted)

        for fact in facts:
            _, raw_created = retain_raw_item(
                session,
                provider=provider_name,
                item_type="company_fact",
                external_id=_fact_external_id(fact),
                payload=fact,
                observed_at=fact.filed_at or fact.period_end_at,
                provider_run_id=provider_run_id,
                identity_parts=_fact_raw_identity(fact),
            )
            raw_inserted += int(raw_created)
            raw_skipped += int(not raw_created)

            was_inserted = _upsert_fact(session, company=company, fact=fact)
            inserted += int(was_inserted)
            skipped += int(not was_inserted)

    return IngestionResult(
        fetched=fetched,
        inserted=inserted,
        skipped=skipped,
        details={
            "companies_inserted": companies_inserted,
            "raw_inserted": raw_inserted,
            "raw_skipped": raw_skipped,
        },
    )


def _upsert_company(
    session: Any,
    *,
    cik: str,
    submissions: Sequence[SECSubmission],
    facts: Sequence[SECCompanyFactDTO],
) -> tuple[SECCompany, bool]:
    metadata = _company_metadata(cik, submissions, facts)
    name = str(
        first_present(
            *(submission.company_name for submission in submissions),
            metadata.get("name"),
            f"CIK {cik}",
        )
    )
    company = find_one(session, SECCompany, cik=cik)
    values = {
        "name": name,
        "ticker": _optional_text(first_sequence_value(first_present(metadata.get("ticker"), metadata.get("tickers")))),
        "exchange": _optional_text(
            first_sequence_value(first_present(metadata.get("exchange"), metadata.get("exchanges")))
        ),
        "sic": _optional_text(metadata.get("sic")),
        "sic_description": _optional_text(
            first_present(metadata.get("sic_description"), metadata.get("sicDescription"))
        ),
        "fiscal_year_end": _optional_text(
            first_present(metadata.get("fiscal_year_end"), metadata.get("fiscalYearEnd"))
        ),
        "entity_type": _optional_text(
            first_present(metadata.get("entity_type"), metadata.get("entityType"))
        ),
        "company_metadata": json_safe(metadata) or None,
    }

    if company is None:
        company = SECCompany(id=uuid.uuid4(), cik=cik, **values)
        add(session, company)
        return company, True

    company.name = values["name"] or company.name
    for field_name in (
        "ticker",
        "exchange",
        "sic",
        "sic_description",
        "fiscal_year_end",
        "entity_type",
    ):
        value = values[field_name]
        if value:
            setattr(company, field_name, value)
    company.company_metadata = values["company_metadata"] or company.company_metadata
    return company, False


def _upsert_filing(session: Any, *, company: SECCompany, submission: SECSubmission) -> bool:
    existing = find_one(session, SECFiling, accession_number=submission.accession_number)
    values = {
        "company_id": company.id,
        "form_type": submission.form,
        "filing_date": date_from_datetime(submission.filed_at),
        "report_date": date_from_datetime(submission.report_at),
        "primary_document_url": _primary_document_url(submission),
        "filing_detail_url": _filing_detail_url(submission),
        "filing_metadata": _filing_metadata(submission),
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return False

    add(
        session,
        SECFiling(
            id=uuid.uuid4(),
            accession_number=submission.accession_number,
            **values,
        ),
    )
    return True


def _upsert_fact(session: Any, *, company: SECCompany, fact: SECCompanyFactDTO) -> bool:
    period_end = date_from_datetime(fact.period_end_at)
    accession_number = fact.accession_number or None
    existing = find_one(
        session,
        SECCompanyFactModel,
        company_id=company.id,
        taxonomy=fact.taxonomy,
        concept=fact.concept,
        unit=fact.unit,
        period_end=period_end,
        accession_number=accession_number,
    )
    values = {
        "period_start": _fact_period_start(fact),
        "filed_at": date_from_datetime(fact.filed_at),
        "form_type": fact.form or None,
        "fiscal_year": fact.fiscal_year,
        "fiscal_period": fact.fiscal_period or None,
        "frame": fact.frame or None,
        "value": to_float_or_none(fact.value),
        "raw_value": None if fact.value is None else str(fact.value),
        "fact_metadata": _fact_metadata(fact),
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return False

    add(
        session,
        SECCompanyFactModel(
            id=uuid.uuid4(),
            company_id=company.id,
            taxonomy=fact.taxonomy,
            concept=fact.concept,
            unit=fact.unit,
            period_end=period_end,
            accession_number=accession_number,
            **values,
        ),
    )
    return True


def _company_metadata(
    cik: str,
    submissions: Sequence[SECSubmission],
    facts: Sequence[SECCompanyFactDTO],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"cik": cik}
    for item in [*submissions, *facts]:
        item_metadata = json_safe(item.metadata)
        if isinstance(item_metadata, Mapping):
            for key, value in item_metadata.items():
                metadata.setdefault(key, value)
    if submissions:
        metadata["source_refs"] = json_safe(submissions[0].source_refs)
        metadata["evidence_refs"] = json_safe(submissions[0].evidence_refs)
    elif facts:
        metadata["source_refs"] = json_safe(facts[0].source_refs)
        metadata["evidence_refs"] = json_safe(facts[0].evidence_refs)
    return metadata


def _filing_metadata(submission: SECSubmission) -> dict[str, Any]:
    metadata = json_safe(submission.metadata)
    metadata.update(
        {
            "company_name": submission.company_name,
            "evidence_refs": json_safe(submission.evidence_refs),
            "items": submission.items,
            "primary_doc_description": submission.primary_doc_description,
            "primary_document": submission.primary_document,
            "provider_name": submission.provider_name,
            "schema_version": submission.schema_version,
            "source_refs": json_safe(submission.source_refs),
        }
    )
    return metadata


def _fact_metadata(fact: SECCompanyFactDTO) -> dict[str, Any]:
    metadata = json_safe(fact.metadata)
    metadata.update(
        {
            "description": fact.description,
            "evidence_refs": json_safe(fact.evidence_refs),
            "label": fact.label,
            "provider_name": fact.provider_name,
            "schema_version": fact.schema_version,
            "source_refs": json_safe(fact.source_refs),
        }
    )
    return metadata


def _primary_document_url(submission: SECSubmission) -> str | None:
    return submission.evidence_refs[0] if submission.evidence_refs else None


def _filing_detail_url(submission: SECSubmission) -> str | None:
    metadata = submission.metadata
    if isinstance(metadata, Mapping) and metadata.get("filing_detail_url"):
        return str(metadata["filing_detail_url"])
    cik = normalize_cik(submission.cik).lstrip("0")
    accession = submission.accession_number.replace("-", "")
    if not cik or not accession:
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"


def _fact_period_start(fact: SECCompanyFactDTO) -> Any:
    metadata = fact.metadata
    if isinstance(metadata, Mapping):
        return parse_optional_date(metadata.get("start"))
    return None


def _fact_raw_identity(fact: SECCompanyFactDTO) -> tuple[Any, ...]:
    return (
        normalize_cik(fact.cik),
        fact.taxonomy,
        fact.concept,
        fact.unit,
        date_from_datetime(fact.period_end_at),
        fact.accession_number or None,
    )


def _fact_external_id(fact: SECCompanyFactDTO) -> str:
    return ":".join(
        str(part)
        for part in (
            normalize_cik(fact.cik),
            fact.taxonomy,
            fact.concept,
            fact.unit,
            date_from_datetime(fact.period_end_at),
            fact.accession_number,
        )
    )


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


ingest_sec_company_data = ingest_sec_companies
