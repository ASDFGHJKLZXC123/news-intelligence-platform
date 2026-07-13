"""SEC EDGAR provider client using stdlib urllib/json transport."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.request import Request
from urllib.request import urlopen as stdlib_urlopen

from packages.providers.base import (
    SECCompanyFact,
    SECCompanyTicker,
    SECCompanyTickerProvider,
    SECEdgarProvider,
    SECSubmission,
    normalize_sec_cik,
)

UrlOpenCallable = Callable[..., Any]

DEFAULT_SEC_DATA_BASE_URL = "https://data.sec.gov"
SEC_ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DEFAULT_TIMEOUT_SECONDS = 30.0


class SECEdgarClient(SECEdgarProvider, SECCompanyTickerProvider):
    """Small SEC EDGAR client with injectable transport.

    Covers both data.sec.gov (submissions/facts) and the www.sec.gov company_tickers.json
    identity seed; both are the same publisher under the same fair-access policy, so they
    share one descriptive User-Agent and one transport.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        urlopen: UrlOpenCallable | None = None,
        base_url: str = DEFAULT_SEC_DATA_BASE_URL,
        company_tickers_url: str = SEC_COMPANY_TICKERS_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not user_agent.strip():
            msg = "SEC user_agent is required"
            raise ValueError(msg)
        self._user_agent = user_agent
        self._urlopen = urlopen or stdlib_urlopen
        self._base_url = base_url.rstrip("/")
        self._company_tickers_url = company_tickers_url
        self._timeout = timeout

    def fetch_submissions(self, cik: str) -> list[SECSubmission]:
        normalized_cik = normalize_sec_cik(cik)
        url = f"{self._base_url}/submissions/CIK{normalized_cik}.json"
        payload = _read_json(self._urlopen, self._request(url), self._timeout)
        return _parse_submissions(normalized_cik, payload)

    def fetch_company_facts(self, cik: str) -> list[SECCompanyFact]:
        normalized_cik = normalize_sec_cik(cik)
        url = f"{self._base_url}/api/xbrl/companyfacts/CIK{normalized_cik}.json"
        payload = _read_json(self._urlopen, self._request(url), self._timeout)
        return _parse_company_facts(normalized_cik, payload)

    def fetch_company_tickers(self) -> list[SECCompanyTicker]:
        request = self._request(self._company_tickers_url)
        payload = _read_json(self._urlopen, request, self._timeout)
        return _parse_company_tickers(payload, self._company_tickers_url)

    def _request(self, url: str) -> Request:
        return Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": self._user_agent,
            },
        )


def _read_json(
    urlopen: UrlOpenCallable, target: str | Request, timeout: float
) -> Mapping[str, Any]:
    handle = urlopen(target, timeout=timeout)
    if hasattr(handle, "__enter__"):
        with handle as response:
            body = response.read()
    else:
        body = handle.read()
    if isinstance(body, bytes | bytearray):
        text = body.decode("utf-8")
    else:
        text = str(body)
    payload = json.loads(text)
    if not isinstance(payload, Mapping):
        msg = "SEC response must be a JSON object"
        raise ValueError(msg)
    return payload


def _parse_submissions(cik: str, payload: Mapping[str, Any]) -> list[SECSubmission]:
    company_name = _text(payload.get("name"))
    filings = payload.get("filings", {})
    if not isinstance(filings, Mapping):
        return []
    recent = filings.get("recent", {})
    if not isinstance(recent, Mapping):
        return []

    accessions = _sequence(recent.get("accessionNumber"))
    submissions: list[SECSubmission] = []
    for index, accession_number in enumerate(accessions):
        accession = _text(accession_number)
        if not accession:
            continue
        primary_document = _text(_at(recent, "primaryDocument", index))
        evidence_ref = _archive_url(cik, accession, primary_document)
        submissions.append(
            SECSubmission(
                cik=cik,
                accession_number=accession,
                form=_text(_at(recent, "form", index)),
                filed_at=_date_to_utc(_parse_date(_at(recent, "filingDate", index))),
                report_at=_optional_date_to_utc(_at(recent, "reportDate", index)),
                company_name=company_name,
                primary_document=primary_document,
                primary_doc_description=_text(_at(recent, "primaryDocDescription", index)),
                items=_text(_at(recent, "items", index)),
                source_refs=(f"sec:submissions:{cik}",),
                evidence_refs=(evidence_ref,),
                metadata=_row_metadata(recent, index),
            )
        )
    return submissions


def _parse_company_facts(cik: str, payload: Mapping[str, Any]) -> list[SECCompanyFact]:
    facts = payload.get("facts", {})
    if not isinstance(facts, Mapping):
        return []

    results: list[SECCompanyFact] = []
    for taxonomy, concepts in facts.items():
        if not isinstance(concepts, Mapping):
            continue
        for concept, details in concepts.items():
            if not isinstance(details, Mapping):
                continue
            label = _text(details.get("label"))
            description = _text(details.get("description"))
            units = details.get("units", {})
            if not isinstance(units, Mapping):
                continue
            for unit, records in units.items():
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, Mapping):
                        continue
                    accession_number = _text(record.get("accn"))
                    results.append(
                        SECCompanyFact(
                            cik=cik,
                            taxonomy=_text(taxonomy),
                            concept=_text(concept),
                            unit=_text(unit),
                            value=record.get("val"),
                            accession_number=accession_number,
                            filed_at=_optional_date_to_utc(record.get("filed")),
                            period_end_at=_optional_date_to_utc(record.get("end")),
                            form=_text(record.get("form")),
                            fiscal_year=_optional_int(record.get("fy")),
                            fiscal_period=_text(record.get("fp")),
                            frame=_text(record.get("frame")),
                            label=label,
                            description=description,
                            source_refs=(f"sec:companyfacts:{cik}",),
                            evidence_refs=(
                                f"sec:fact:{cik}:{taxonomy}:{concept}:{unit}:{accession_number}",
                            ),
                            metadata=record,
                        )
                    )
    return results


def _parse_company_tickers(payload: Mapping[str, Any], url: str) -> list[SECCompanyTicker]:
    """Parse the ``{"0": {"cik_str": .., "ticker": .., "title": ..}, ...}`` JSON map.

    Rows are consumed in file order. A row missing a usable CIK or ticker is skipped rather
    than failing the whole seed.
    """

    tickers: list[SECCompanyTicker] = []
    for row_key, row in payload.items():
        if not isinstance(row, Mapping):
            continue
        raw_cik = _first_present(row.get("cik_str"), row.get("cik"))
        ticker = _text(row.get("ticker")).strip()
        if not ticker or not any(character.isdigit() for character in str(raw_cik)):
            continue
        cik = normalize_sec_cik(raw_cik)
        tickers.append(
            SECCompanyTicker(
                cik=cik,
                ticker=ticker,
                title=_text(row.get("title")),
                source_refs=(f"sec:company_tickers:{cik}",),
                evidence_refs=(url,),
                metadata={"row_key": str(row_key), "source_row": dict(row)},
            )
        )
    return tickers


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return ()


def _at(record: Mapping[str, Any], key: str, index: int) -> Any:
    values = _sequence(record.get(key))
    if index >= len(values):
        return None
    return values[index]


def _row_metadata(record: Mapping[str, Any], index: int) -> Mapping[str, Any]:
    row: dict[str, Any] = {}
    for key, values in record.items():
        sequence = _sequence(values)
        if index < len(sequence):
            row[str(key)] = sequence[index]
    return row


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_date_to_utc(value: Any) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    return _date_to_utc(_parse_date(value))


def _parse_date(value: Any) -> datetime.date:
    if value in (None, ""):
        msg = "date value is required"
        raise ValueError(msg)
    return datetime.date.fromisoformat(str(value))


def _date_to_utc(value: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(value, datetime.time(tzinfo=datetime.UTC))


def _archive_url(cik: str, accession_number: str, primary_document: str) -> str:
    compact_accession = accession_number.replace("-", "")
    compact_cik = str(int(cik))
    if not primary_document:
        return f"sec:accession:{cik}:{accession_number}"
    return f"{SEC_ARCHIVES_BASE_URL}/{compact_cik}/{compact_accession}/{primary_document}"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)
