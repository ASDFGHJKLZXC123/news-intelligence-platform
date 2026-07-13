"""Unit tests for ADR 0006 item 1: alias normalization + SEC company_tickers seeding."""

from __future__ import annotations

import json
import uuid
from types import TracebackType
from typing import Any
from urllib.request import Request

import pytest

from db.models import EntityAlias, EntityIdentifier, EntityProfile, RawIngestionItem
from packages.providers.base import SECCompanyTicker, SECCompanyTickerProvider
from packages.providers.fakes import FakeSECCompanyTickerProvider
from packages.providers.sec_edgar import SEC_COMPANY_TICKERS_URL, SECEdgarClient
from services.provider_data import ingest_sec_company_tickers, normalize_alias
from tests.unit.test_provider_data_services import FakeSession

USER_AGENT = "news-intelligence-platform tests@example.com"

SEC_PAYLOAD = {
    "0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."},
    "1": {"cik_str": "1652044", "ticker": "GOOGL", "title": "Alphabet Inc."},
}


class BytesResponse:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> BytesResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class RecordingTransport:
    def __init__(self, *payloads: Any) -> None:
        self.payloads = list(payloads)
        self.requests: list[Request] = []
        self.timeouts: list[float | None] = []

    def __call__(self, target: str | Request, timeout: float | None = None) -> BytesResponse:
        assert isinstance(target, Request)
        self.requests.append(target)
        self.timeouts.append(timeout)
        return BytesResponse(self.payloads.pop(0))


# --- Canonical alias normalization ---------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Casefold + the full ADR 0006 legal-suffix list, one suffix each.
        ("Apple Inc.", "apple"),
        ("Acme Corp", "acme"),
        ("Acme Corporation", "acme"),
        ("Acme Ltd", "acme"),
        ("Acme Limited", "acme"),
        ("Acme LLC", "acme"),
        ("Acme LP", "acme"),
        ("Acme PLC", "acme"),
        ("Siemens AG", "siemens"),
        ("Totalenergies SA", "totalenergies"),
        ("Airbus NV", "airbus"),
        ("Acme Co", "acme"),
        ("Acme Holdings", "acme"),
        ("Acme Group", "acme"),
        # Repeated trailing suffixes strip until a non-suffix token remains.
        ("Acme Holdings Group Inc.", "acme"),
        ("Acme Co Ltd", "acme"),
        ("Acme Group Holdings Corporation Limited", "acme"),
        # Punctuation: dots/apostrophes vanish, other punctuation separates.
        ("U.S. Steel Corp", "us steel"),
        ("Macy's Inc.", "macys"),
        ("Coca-Cola Co", "coca cola"),
        ("Procter & Gamble Co", "procter gamble"),
        # Whitespace collapse.
        ("  Acme   Widgets \t Inc.  ", "acme widgets"),
        # A suffix-only alias keeps its last token rather than normalizing to "".
        ("Group", "group"),
        ("CO", "co"),
        ("Holdings Group", "holdings"),
        # Suffixes only strip from the end, never mid-name: a trailing parenthetical leaves
        # "inc" interior, so it stays.
        ("Group 1 Automotive", "group 1 automotive"),
        ("Inc Magazine", "inc magazine"),
        ("Berkshire Hathaway Inc. (Class B)", "berkshire hathaway inc class b"),
        # Non-ASCII letters survive.
        ("Nestlé S.A.", "nestlé"),
        # Empty input stays empty; callers skip it.
        ("   ", ""),
    ],
)
def test_normalize_alias_casefolds_depunctuates_and_strips_legal_suffixes(
    raw: str, expected: str
) -> None:
    assert normalize_alias(raw) == expected


def test_normalize_alias_is_idempotent() -> None:
    once = normalize_alias("Acme Holdings Group Inc.")
    assert normalize_alias(once) == once


# --- SEC company_tickers client ------------------------------------------------------
def test_fetch_company_tickers_sends_descriptive_headers_and_parses_json_map() -> None:
    transport = RecordingTransport(SEC_PAYLOAD)
    client = SECEdgarClient(user_agent=USER_AGENT, urlopen=transport)

    tickers = client.fetch_company_tickers()

    request = transport.requests[0]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert request.full_url == SEC_COMPANY_TICKERS_URL
    assert headers["user-agent"] == USER_AGENT
    assert headers["accept"] == "application/json"

    assert [(item.cik, item.ticker, item.title) for item in tickers] == [
        ("0000320193", "AAPL", "Apple Inc."),
        ("0001652044", "GOOGL", "Alphabet Inc."),
    ]
    assert tickers[0].source_refs == ("sec:company_tickers:0000320193",)
    assert tickers[0].evidence_refs == (SEC_COMPANY_TICKERS_URL,)
    assert tickers[0].metadata["source_row"]["ticker"] == "aapl"
    assert tickers[0].schema_version == "sec-company-ticker.v1"


def test_fetch_company_tickers_skips_malformed_rows() -> None:
    payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1234, "ticker": "", "title": "No Ticker Inc."},
        "2": {"cik_str": "N/A", "ticker": "BAD", "title": "No Digits Inc."},
        "3": {"ticker": "MISSING", "title": "No CIK Inc."},
        "4": "not-an-object",
    }
    client = SECEdgarClient(user_agent=USER_AGENT, urlopen=RecordingTransport(payload))

    assert [item.ticker for item in client.fetch_company_tickers()] == ["AAPL"]


def test_fetch_company_tickers_rejects_non_object_payload() -> None:
    client = SECEdgarClient(user_agent=USER_AGENT, urlopen=RecordingTransport([1, 2, 3]))

    with pytest.raises(ValueError, match="must be a JSON object"):
        client.fetch_company_tickers()


def test_sec_client_requires_a_descriptive_user_agent() -> None:
    with pytest.raises(ValueError, match="user_agent is required"):
        SECEdgarClient(user_agent="   ", urlopen=RecordingTransport())


def test_company_ticker_dto_normalizes_and_is_immutable() -> None:
    record = SECCompanyTicker(cik="320193", ticker=" aapl ", title="  Apple Inc.  ")

    assert (record.cik, record.ticker, record.title) == ("0000320193", "AAPL", "Apple Inc.")
    with pytest.raises(AttributeError):
        record.ticker = "MSFT"  # type: ignore[misc]
    with pytest.raises(ValueError, match="CIK must contain digits"):
        SECCompanyTicker(cik="none", ticker="X", title="X")


def test_client_and_fake_satisfy_the_company_ticker_protocol() -> None:
    assert isinstance(FakeSECCompanyTickerProvider(), SECCompanyTickerProvider)
    assert isinstance(
        SECEdgarClient(user_agent=USER_AGENT, urlopen=RecordingTransport()),
        SECCompanyTickerProvider,
    )
    assert FakeSECCompanyTickerProvider().fetch_company_tickers() == (
        FakeSECCompanyTickerProvider().fetch_company_tickers()
    )


# --- Identity ingestion --------------------------------------------------------------
def test_sec_ticker_ingestion_seeds_profiles_identifiers_aliases_and_raw_payloads() -> None:
    session = FakeSession()
    run_id = uuid.uuid4()

    result = ingest_sec_company_tickers(
        session, FakeSECCompanyTickerProvider(), provider_run_id=run_id
    )

    # 3 seed rows -> 2 companies; the second Alphabet class hits the existing profile.
    assert result.fetched == 3
    assert result.inserted == 2
    assert result.skipped == 1
    assert result["details"]["identifiers_inserted"] == 5
    assert result["details"]["aliases_inserted"] == 5
    assert result["details"]["raw_inserted"] == 3

    profiles = {profile.primary_cik: profile for profile in session.all_of(EntityProfile)}
    assert set(profiles) == {"0000320193", "0001652044"}

    apple = profiles["0000320193"]
    assert apple.canonical_name == "Apple Inc."
    assert apple.normalized_name == "apple inc."
    assert apple.primary_ticker == "AAPL"
    assert apple.entity_type == "company"

    # Seed order decides the primary ticker; both classes are still recorded.
    alphabet = profiles["0001652044"]
    assert alphabet.primary_ticker == "GOOGL"
    assert alphabet.profile_metadata["sec_tickers"] == ["GOOG", "GOOGL"]

    # SEC precedence/source is recorded so lower-precedence sources cannot overwrite it later.
    metadata = apple.profile_metadata
    assert metadata["source"] == {
        "dataset": "company_tickers.json",
        "evidence_refs": ["https://www.sec.gov/files/company_tickers.json"],
        "precedence": 1,
        "provider": "sec-edgar",
        "schema_version": "sec-company-ticker.v1",
        "source_refs": ["sec:company_tickers:0000320193"],
    }
    assert metadata["identity_sources"] == {
        "canonical_name": "sec-edgar",
        "primary_cik": "sec-edgar",
        "primary_ticker": "sec-edgar",
    }

    identifiers = session.all_of(EntityIdentifier)
    assert {(item.identifier_type, item.identifier_value) for item in identifiers} == {
        ("cik", "0000320193"),
        ("cik", "0001652044"),
        ("ticker", "AAPL"),
        ("ticker", "GOOGL"),
        ("ticker", "GOOG"),
    }
    assert all(item.provider == "sec-edgar" for item in identifiers)
    assert all(float(item.confidence_score) == 1.0 for item in identifiers)

    # Raw alias is retained next to the normalized key.
    aliases = session.all_of(EntityAlias)
    assert {(item.alias, item.normalized_alias, item.alias_type) for item in aliases} == {
        ("Apple Inc.", "apple", "legal_name"),
        ("AAPL", "aapl", "ticker"),
        ("Alphabet Inc.", "alphabet", "legal_name"),
        ("GOOGL", "googl", "ticker"),
        ("GOOG", "goog", "ticker"),
    }
    assert all(item.source == "sec-edgar" for item in aliases)

    raw_items = session.all_of(RawIngestionItem)
    assert len(raw_items) == 3
    assert {item.item_type for item in raw_items} == {"company_ticker"}
    assert all(item.provider_run_id == run_id for item in raw_items)
    assert raw_items[0].payload["ticker"] == "AAPL"


def test_sec_ticker_ingestion_is_idempotent_on_repeat() -> None:
    session = FakeSession()
    provider = FakeSECCompanyTickerProvider()

    ingest_sec_company_tickers(session, provider)
    second = ingest_sec_company_tickers(session, provider)

    assert second.fetched == 3
    assert second.inserted == 0
    assert second.skipped == 3
    assert second["details"] == {
        "aliases_inserted": 0,
        "identifiers_inserted": 0,
        "raw_inserted": 0,
        "raw_skipped": 3,
    }
    assert len(session.all_of(EntityProfile)) == 2
    assert len(session.all_of(EntityIdentifier)) == 5
    assert len(session.all_of(EntityAlias)) == 5
    assert len(session.all_of(RawIngestionItem)) == 3


def test_sec_ingestion_claims_authority_without_clobbering_other_source_fields() -> None:
    session = FakeSession()
    existing = EntityProfile(
        id=uuid.uuid4(),
        canonical_name="APPLE INC",
        normalized_name="apple inc",
        primary_cik="0000320193",
        primary_lei="HWUPKR0MPOU8FGXBT394",
        country="US",
        profile_metadata={"identity_sources": {"primary_lei": "gleif"}, "gleif": {"kept": True}},
    )
    session.add(existing)

    ingest_sec_company_tickers(
        session,
        FakeSECCompanyTickerProvider(
            [SECCompanyTicker(cik="320193", ticker="AAPL", title="Apple Inc.")]
        ),
    )

    # SEC outranks GLEIF on the fields it owns, and leaves the rest of the profile alone.
    assert existing.canonical_name == "Apple Inc."
    assert existing.primary_ticker == "AAPL"
    assert existing.primary_lei == "HWUPKR0MPOU8FGXBT394"
    assert existing.country == "US"
    assert existing.profile_metadata["gleif"] == {"kept": True}
    assert existing.profile_metadata["identity_sources"] == {
        "canonical_name": "sec-edgar",
        "primary_cik": "sec-edgar",
        "primary_lei": "gleif",
        "primary_ticker": "sec-edgar",
    }
    assert len(session.all_of(EntityProfile)) == 1
