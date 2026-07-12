"""Company research identity/universe tests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from services.company_research import build_company_universe, resolve_company_identity
from services.provider_data.common import normalize_name


def _sec_company(
    name: str,
    *,
    cik: str,
    ticker: str | None,
    exchange: str | None = "NASDAQ",
    sic: str | None = "3571",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        cik=cik,
        name=name,
        ticker=ticker,
        exchange=exchange,
        sic=sic,
        sic_description="Electronic Computers",
        company_metadata={"sector": "Technology", "industry": "Semiconductors"},
    )


def _entity_profile(
    name: str,
    *,
    primary_cik: str | None = None,
    primary_ticker: str | None = None,
    primary_lei: str | None = None,
    country: str | None = "US",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        canonical_name=name,
        normalized_name=normalize_name(name),
        entity_type="company",
        country=country,
        primary_ticker=primary_ticker,
        primary_cik=primary_cik,
        primary_lei=primary_lei,
        profile_metadata={"source": "test"},
    )


def test_frontend_company_resolves_to_entity_profile_and_sec_company_by_cik() -> None:
    sec = _sec_company("NVIDIA Corporation", cik="0001045810", ticker="NVDA", exchange="NASDAQ")
    profile = _entity_profile(
        "NVIDIA Corporation",
        primary_cik="0001045810",
        primary_ticker="NVDA",
        primary_lei="549300S4KLFTLO7GSQ80",
    )
    frontend = {
        "companyId": "nvda",
        "name": "NVIDIA",
        "ticker": "NVDA",
        "cik": "1045810",
        "exchange": "NASDAQ",
        "industry": "Semiconductors",
        "country": "US",
    }

    universe = build_company_universe(
        frontend_companies=[frontend],
        sec_companies=[sec],
        entity_profiles=[profile],
    )

    assert len(universe) == 1
    record = universe[0]
    assert record["canonical_company_id"] == f"entity:{profile.id}"
    assert record["display_name"] == "NVIDIA Corporation"
    assert record["identifiers"]["ticker"] == "NVDA"
    assert record["identifiers"]["cik"] == "0001045810"
    assert record["identifiers"]["lei"] == "549300S4KLFTLO7GSQ80"
    assert record["listing"]["status"] == "active"
    assert record["classification"]["sic"] == "3571"
    assert record["resolution"]["method"] == "entity_profile:primary_cik"
    assert {ref["source_type"] for ref in record["source_refs"]} == {
        "frontend_company",
        "sec_company",
        "entity_profile",
    }


def test_frontend_company_without_cik_becomes_partial_canonical_record() -> None:
    frontend = {
        "companyId": "private-ai-co",
        "name": "Private AI Co",
        "ticker": "",
        "exchange": "",
        "industry": "AI Infrastructure",
        "country": "US",
    }

    record = build_company_universe(frontend_companies=[frontend])[0]

    assert record["canonical_company_id"] == "frontend:private-ai-co"
    assert record["display_name"] == "Private AI Co"
    assert record["identifiers"]["cik"] is None
    assert record["listing"]["status"] == "partial"
    assert record["resolution"] == {
        "status": "resolved",
        "method": "partial:name",
        "partial": True,
    }


def test_duplicate_ticker_across_exchanges_requires_explicit_exchange_match() -> None:
    nasdaq = _sec_company("Example US", cik="0000000001", ticker="ABC", exchange="NASDAQ")
    toronto = _sec_company("Example Canada", cik="0000000002", ticker="ABC", exchange="TSX")
    frontend_with_exchange = {
        "companyId": "abc-ca",
        "name": "Example Canada",
        "ticker": "ABC",
        "exchange": "TSX",
    }
    frontend_without_exchange = {
        "companyId": "abc-unknown",
        "name": "Example Unknown",
        "ticker": "ABC",
    }

    universe = build_company_universe(
        frontend_companies=[frontend_with_exchange, frontend_without_exchange],
        sec_companies=[nasdaq, toronto],
    )
    by_frontend_id = {
        record["identifiers"]["frontend_company_id"]: record
        for record in universe
        if record["identifiers"]["frontend_company_id"]
    }

    matched = by_frontend_id["abc-ca"]
    ambiguous = by_frontend_id["abc-unknown"]
    assert matched["canonical_company_id"] == "sec:0000000002"
    assert matched["resolution"]["status"] == "resolved"
    assert matched["listing"]["duplicate_ticker"] is True
    assert len(matched["listing"]["duplicate_ticker_choices"]) == 3

    assert ambiguous["canonical_company_id"] == "frontend:abc-unknown"
    assert ambiguous["resolution"]["status"] == "ambiguous_ticker"
    assert ambiguous["resolution"]["method"] == "ticker:ambiguous"
    assert ambiguous["listing"]["duplicate_ticker"] is True


def test_resolve_company_identity_matches_entity_by_name_when_identifiers_are_missing() -> None:
    profile = _entity_profile("Example Energy Co.", primary_lei="549300TEST0000000001")

    record = resolve_company_identity(
        {"companyId": "energy", "name": "Example Energy Co."},
        entity_by_name={normalize_name("Example Energy Co."): profile},
    )

    assert record["canonical_company_id"] == f"entity:{profile.id}"
    assert record["identifiers"]["lei"] == "549300TEST0000000001"
    assert record["resolution"]["method"] == "entity_profile:identifier"
