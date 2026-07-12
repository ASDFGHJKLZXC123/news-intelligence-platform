"""Company universe and identity resolution helpers."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from services.provider_data.common import normalize_cik, normalize_name


def build_company_universe(
    *,
    frontend_companies: Sequence[Any] | None = None,
    sec_companies: Sequence[Any] | None = None,
    entity_profiles: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """Return canonical company records from frontend, SEC, and entity inputs.

    This is an application-layer bridge for Phase 1. It does not require a
    migration, but it produces the canonical shape expected by later storage work.
    """

    frontend_list = list(frontend_companies or [])
    sec_list = list(sec_companies or [])
    entity_list = list(entity_profiles or [])

    sec_by_cik = {_clean_cik(_get(company, "cik")): company for company in sec_list if _clean_cik(_get(company, "cik"))}
    sec_by_ticker = _group_by_ticker(sec_list, "ticker")
    entity_by_cik = {
        _clean_cik(_get(profile, "primary_cik")): profile
        for profile in entity_list
        if _clean_cik(_get(profile, "primary_cik"))
    }
    entity_by_ticker = _group_by_ticker(entity_list, "primary_ticker")
    entity_by_name = {
        _clean_name(_get(profile, "canonical_name")): profile
        for profile in entity_list
        if _clean_name(_get(profile, "canonical_name"))
    }
    ticker_choices = _ticker_choices(frontend_list, sec_list, entity_list)

    records: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for company in frontend_list:
        record = resolve_company_identity(
            company,
            sec_by_cik=sec_by_cik,
            sec_by_ticker=sec_by_ticker,
            entity_by_cik=entity_by_cik,
            entity_by_ticker=entity_by_ticker,
            entity_by_name=entity_by_name,
            ticker_choices=ticker_choices,
        )
        records.append(record)
        seen_keys.add(record["canonical_company_id"])

    for company in sec_list:
        record = resolve_company_identity(
            company,
            source_type="sec_company",
            sec_by_cik=sec_by_cik,
            sec_by_ticker=sec_by_ticker,
            entity_by_cik=entity_by_cik,
            entity_by_ticker=entity_by_ticker,
            entity_by_name=entity_by_name,
            ticker_choices=ticker_choices,
        )
        if record["canonical_company_id"] not in seen_keys:
            records.append(record)
            seen_keys.add(record["canonical_company_id"])

    for profile in entity_list:
        record = resolve_company_identity(
            profile,
            source_type="entity_profile",
            sec_by_cik=sec_by_cik,
            sec_by_ticker=sec_by_ticker,
            entity_by_cik=entity_by_cik,
            entity_by_ticker=entity_by_ticker,
            entity_by_name=entity_by_name,
            ticker_choices=ticker_choices,
        )
        if record["canonical_company_id"] not in seen_keys:
            records.append(record)
            seen_keys.add(record["canonical_company_id"])

    return records


def resolve_company_identity(
    source: Any,
    *,
    source_type: str = "frontend_company",
    sec_by_cik: Mapping[str, Any] | None = None,
    sec_by_ticker: Mapping[str, Sequence[Any]] | None = None,
    entity_by_cik: Mapping[str, Any] | None = None,
    entity_by_ticker: Mapping[str, Sequence[Any]] | None = None,
    entity_by_name: Mapping[str, Any] | None = None,
    ticker_choices: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Resolve a single source object into the canonical company identity shape."""

    sec_by_cik = sec_by_cik or {}
    sec_by_ticker = sec_by_ticker or {}
    entity_by_cik = entity_by_cik or {}
    entity_by_ticker = entity_by_ticker or {}
    entity_by_name = entity_by_name or {}
    ticker_choices = ticker_choices or {}

    source_values = _source_values(source, source_type)
    ticker = source_values["ticker"]
    exchange = source_values["exchange"]
    cik = source_values["cik"]
    name = source_values["name"]
    normalized_name = _clean_name(name)

    sec_company = _match_sec_company(cik, ticker, exchange, sec_by_cik, sec_by_ticker)
    entity_profile = _match_entity_profile(cik, ticker, normalized_name, entity_by_cik, entity_by_ticker, entity_by_name)

    resolved = _merge_identity_values(source_values, sec_company, entity_profile)
    duplicate_choices = list(ticker_choices.get(resolved["ticker"] or "", []))
    duplicate_ticker = len(duplicate_choices) > 1
    ambiguous_ticker = duplicate_ticker and not resolved["exchange"] and sec_company is None and entity_profile is None
    canonical_company_id = _canonical_company_id(resolved, source_values, sec_company, entity_profile, ambiguous_ticker)

    return {
        "canonical_company_id": canonical_company_id,
        "display_name": resolved["name"],
        "normalized_name": _clean_name(resolved["name"]),
        "identifiers": {
            "frontend_company_id": source_values["frontend_company_id"],
            "ticker": resolved["ticker"],
            "cik": resolved["cik"],
            "lei": resolved["lei"],
        },
        "listing": {
            "ticker": resolved["ticker"],
            "exchange": resolved["exchange"],
            "country": resolved["country"],
            "status": "active" if resolved["ticker"] else "partial",
            "duplicate_ticker": duplicate_ticker,
            "duplicate_ticker_choices": duplicate_choices,
        },
        "classification": {
            "industry": resolved["industry"],
            "sector": resolved["sector"],
            "sic": resolved["sic"],
            "sic_description": resolved["sic_description"],
            "naics": resolved["naics"],
        },
        "resolution": {
            "status": "ambiguous_ticker" if ambiguous_ticker else "resolved",
            "method": _resolution_method(source_values, sec_company, entity_profile, ambiguous_ticker),
            "partial": sec_company is None and entity_profile is None,
        },
        "source_refs": _source_refs(source_values, sec_company, entity_profile),
    }


def _source_values(source: Any, source_type: str) -> dict[str, str | None]:
    metadata = _metadata(source)
    return {
        "source_type": source_type,
        "frontend_company_id": _optional_text(_get(source, "companyId")),
        "name": _optional_text(
            _first_present(
                _get(source, "name"),
                _get(source, "canonical_name"),
                _metadata_lookup(metadata, "name", "company_name", "entityName"),
            )
        )
        or "Unknown company",
        "ticker": _clean_ticker(_first_present(_get(source, "ticker"), _get(source, "primary_ticker"), metadata.get("ticker"))),
        "cik": _clean_cik(
            _first_present(
                _get(source, "cik"),
                _get(source, "secCik"),
                _get(source, "primaryCik"),
                _get(source, "primary_cik"),
                _metadata_lookup(metadata, "cik", "primary_cik"),
            )
        ),
        "lei": _optional_text(_first_present(_get(source, "primary_lei"), _metadata_lookup(metadata, "lei", "primary_lei"))),
        "exchange": _clean_exchange(
            _first_present(_get(source, "exchange"), _metadata_lookup(metadata, "exchange", "primary_exchange"))
        ),
        "country": _optional_text(_first_present(_get(source, "country"), _metadata_lookup(metadata, "country"))),
        "industry": _optional_text(_first_present(_get(source, "industry"), _metadata_lookup(metadata, "industry"))),
        "sector": _optional_text(_metadata_lookup(metadata, "sector")),
        "sic": _optional_text(_first_present(_get(source, "sic"), _metadata_lookup(metadata, "sic"))),
        "sic_description": _optional_text(
            _first_present(_get(source, "sic_description"), _metadata_lookup(metadata, "sic_description", "sicDescription"))
        ),
        "naics": _optional_text(_metadata_lookup(metadata, "naics", "naics_code")),
    }


def _merge_identity_values(source_values: Mapping[str, str | None], sec_company: Any | None, entity_profile: Any | None) -> dict[str, str | None]:
    sec_metadata = _metadata(sec_company)
    entity_metadata = _metadata(entity_profile)
    return {
        "name": _optional_text(
            _first_present(
                _get(entity_profile, "canonical_name"),
                _get(sec_company, "name"),
                source_values.get("name"),
            )
        )
        or "Unknown company",
        "ticker": _clean_ticker(
            _first_present(
                source_values.get("ticker"),
                _get(sec_company, "ticker"),
                _get(entity_profile, "primary_ticker"),
            )
        ),
        "cik": _clean_cik(_first_present(source_values.get("cik"), _get(sec_company, "cik"), _get(entity_profile, "primary_cik"))),
        "lei": _optional_text(_first_present(source_values.get("lei"), _get(entity_profile, "primary_lei"))),
        "exchange": _clean_exchange(_first_present(source_values.get("exchange"), _get(sec_company, "exchange"))),
        "country": _optional_text(_first_present(source_values.get("country"), _get(entity_profile, "country"))),
        "industry": _optional_text(_first_present(source_values.get("industry"), _metadata_lookup(sec_metadata, "industry"))),
        "sector": _optional_text(_first_present(source_values.get("sector"), _metadata_lookup(sec_metadata, "sector"))),
        "sic": _optional_text(_first_present(source_values.get("sic"), _get(sec_company, "sic"))),
        "sic_description": _optional_text(
            _first_present(source_values.get("sic_description"), _get(sec_company, "sic_description"))
        ),
        "naics": _optional_text(_first_present(source_values.get("naics"), _metadata_lookup(sec_metadata, "naics"), _metadata_lookup(entity_metadata, "naics"))),
    }


def _match_sec_company(
    cik: str | None,
    ticker: str | None,
    exchange: str | None,
    sec_by_cik: Mapping[str, Any],
    sec_by_ticker: Mapping[str, Sequence[Any]],
) -> Any | None:
    if cik and cik in sec_by_cik:
        return sec_by_cik[cik]
    matches = list(sec_by_ticker.get(ticker or "", []))
    if not matches:
        return None
    if exchange:
        for company in matches:
            if _clean_exchange(_get(company, "exchange")) == exchange:
                return company
    return matches[0] if len(matches) == 1 else None


def _match_entity_profile(
    cik: str | None,
    ticker: str | None,
    normalized_name: str,
    entity_by_cik: Mapping[str, Any],
    entity_by_ticker: Mapping[str, Sequence[Any]],
    entity_by_name: Mapping[str, Any],
) -> Any | None:
    if cik and cik in entity_by_cik:
        return entity_by_cik[cik]
    ticker_matches = list(entity_by_ticker.get(ticker or "", []))
    if len(ticker_matches) == 1:
        return ticker_matches[0]
    return entity_by_name.get(normalized_name)


def _canonical_company_id(
    resolved: Mapping[str, str | None],
    source_values: Mapping[str, str | None],
    sec_company: Any | None,
    entity_profile: Any | None,
    ambiguous_ticker: bool,
) -> str:
    if entity_profile is not None and _get(entity_profile, "id"):
        return f"entity:{_get(entity_profile, 'id')}"
    if sec_company is not None and resolved.get("cik"):
        return f"sec:{resolved['cik']}"
    if resolved.get("cik"):
        return f"cik:{resolved['cik']}"
    if resolved.get("ticker") and resolved.get("exchange") and not ambiguous_ticker:
        return f"listing:{resolved['ticker']}:{resolved['exchange']}"
    if source_values.get("frontend_company_id"):
        return f"frontend:{source_values['frontend_company_id']}"
    return f"unresolved:{_stable_uuid(resolved.get('name') or 'unknown')}"


def _resolution_method(
    source_values: Mapping[str, str | None],
    sec_company: Any | None,
    entity_profile: Any | None,
    ambiguous_ticker: bool,
) -> str:
    if ambiguous_ticker:
        return "ticker:ambiguous"
    if entity_profile is not None and source_values.get("cik"):
        return "entity_profile:primary_cik"
    if sec_company is not None and source_values.get("cik"):
        return "sec_company:cik"
    if entity_profile is not None:
        return "entity_profile:identifier"
    if sec_company is not None:
        return "sec_company:identifier"
    if source_values.get("cik"):
        return "partial:cik"
    if source_values.get("ticker"):
        return "partial:ticker"
    return "partial:name"


def _source_refs(source_values: Mapping[str, str | None], sec_company: Any | None, entity_profile: Any | None) -> list[dict[str, str]]:
    refs = [{"source_type": source_values["source_type"] or "source", "source_id": source_values.get("frontend_company_id") or source_values["name"] or "unknown"}]
    if sec_company is not None:
        refs.append({"source_type": "sec_company", "source_id": _get(sec_company, "cik") or str(_get(sec_company, "id"))})
    if entity_profile is not None:
        refs.append({"source_type": "entity_profile", "source_id": str(_get(entity_profile, "id"))})
    return refs


def _ticker_choices(*groups: Sequence[Any]) -> dict[str, list[dict[str, str | None]]]:
    by_ticker: dict[str, dict[tuple[str | None, str], dict[str, str | None]]] = defaultdict(dict)
    for group in groups:
        for item in group:
            values = _source_values(item, "source")
            ticker = values["ticker"]
            if not ticker:
                continue
            choice = {
                "name": values["name"],
                "ticker": ticker,
                "exchange": values["exchange"],
                "cik": values["cik"],
            }
            marker = (values["exchange"], _clean_name(values["name"]))
            existing = by_ticker[ticker].get(marker)
            if existing is None or (not existing.get("cik") and choice.get("cik")):
                by_ticker[ticker][marker] = choice
    return {ticker: list(choices.values()) for ticker, choices in by_ticker.items()}


def _group_by_ticker(items: Sequence[Any], attr: str) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        ticker = _clean_ticker(_get(item, attr))
        if ticker:
            grouped[ticker].append(item)
    return dict(grouped)


def _metadata(obj: Any | None) -> Mapping[str, Any]:
    if obj is None:
        return {}
    for name in ("company_metadata", "profile_metadata", "metadata"):
        value = _get(obj, name)
        if isinstance(value, Mapping):
            return value
    return {}


def _metadata_lookup(metadata: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in metadata and metadata[key] not in (None, ""):
            return metadata[key]
    normalized = {str(key).casefold(): value for key, value in metadata.items()}
    for key in keys:
        value = normalized.get(key.casefold())
        if value not in (None, ""):
            return value
    return None


def _get(obj: Any | None, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _clean_name(value: Any) -> str:
    text = _optional_text(value)
    return normalize_name(text) if text else ""


def _clean_ticker(value: Any) -> str | None:
    text = _optional_text(value)
    return text.upper() if text else None


def _clean_exchange(value: Any) -> str | None:
    text = _optional_text(value)
    return text.upper() if text else None


def _clean_cik(value: Any) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    try:
        return normalize_cik(text)
    except ValueError:
        return None


def _stable_uuid(value: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"signal-company:{value}")
