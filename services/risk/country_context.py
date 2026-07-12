"""Country context snapshot construction from provider-data facts."""

from __future__ import annotations

import datetime
import decimal
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from services.evidence.graph import evidence_ref_for_provider_fact

DEFAULT_REQUIRED_INDICATOR_IDS: Final[tuple[str, ...]] = (
    "NY.GDP.MKTP.KD.ZG",
    "FP.CPI.TOTL.ZG",
    "BN.CAB.XOKA.GD.ZS",
    "DT.DOD.DECT.GN.ZS",
    "FI.RES.TOTL.CD",
    "SL.UEM.TOTL.ZS",
    "NE.TRD.GNFS.ZS",
    "GE.EST",
)

_MISSING = object()


@dataclass(frozen=True)
class CountryIndicatorSpec:
    """Interpretation metadata for one country indicator."""

    indicator_id: str
    metric: str
    context: str
    risk_signal: str
    direction: int
    scale: float


@dataclass(frozen=True)
class CountryContextSnapshot:
    """DB-free country context panel used by risk explanation APIs."""

    country_code: str
    snapshot_date: datetime.date
    sovereign_context: Mapping[str, Any] = field(default_factory=dict)
    governance_context: Mapping[str, Any] = field(default_factory=dict)
    debt_context: Mapping[str, Any] = field(default_factory=dict)
    trade_context: Mapping[str, Any] = field(default_factory=dict)
    development_context: Mapping[str, Any] = field(default_factory=dict)
    humanitarian_context: Mapping[str, Any] = field(default_factory=dict)
    geo_context: Mapping[str, Any] = field(default_factory=dict)
    risk_signals: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[Mapping[str, str], ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        country = self.country_code.strip().upper()
        if not country:
            msg = "country_code must be a non-empty string"
            raise ValueError(msg)
        object.__setattr__(self, "country_code", country)
        object.__setattr__(self, "snapshot_date", _coerce_date(self.snapshot_date))
        for name in (
            "sovereign_context",
            "governance_context",
            "debt_context",
            "trade_context",
            "development_context",
            "humanitarian_context",
            "geo_context",
            "risk_signals",
            "metadata",
        ):
            object.__setattr__(self, name, _freeze_mapping(getattr(self, name)))
        object.__setattr__(self, "evidence_refs", _dedupe_refs(self.evidence_refs))

    def as_payload(self) -> dict[str, Any]:
        return {
            "country_code": self.country_code,
            "snapshot_date": self.snapshot_date.isoformat(),
            "sovereign_context": _json_safe(self.sovereign_context),
            "governance_context": _json_safe(self.governance_context),
            "debt_context": _json_safe(self.debt_context),
            "trade_context": _json_safe(self.trade_context),
            "development_context": _json_safe(self.development_context),
            "humanitarian_context": _json_safe(self.humanitarian_context),
            "geo_context": _json_safe(self.geo_context),
            "risk_signals": _json_safe(self.risk_signals),
            "evidence_refs": list(self.evidence_refs),
            "metadata": _json_safe(self.metadata),
        }

    def as_db_values(self) -> dict[str, Any]:
        """Return values aligned with ``country_context_snapshots`` columns."""
        return {
            "country_code": self.country_code,
            "snapshot_date": self.snapshot_date,
            "sovereign_context": _json_safe(self.sovereign_context),
            "governance_context": _json_safe(self.governance_context),
            "debt_context": _json_safe(self.debt_context),
            "trade_context": _json_safe(self.trade_context),
            "development_context": _json_safe(self.development_context),
            "snapshot_metadata": {
                **_json_safe(self.metadata),
                "humanitarian_context": _json_safe(self.humanitarian_context),
                "geo_context": _json_safe(self.geo_context),
                "risk_signals": _json_safe(self.risk_signals),
                "evidence_refs": list(self.evidence_refs),
            },
        }


INDICATOR_SPECS: Final[Mapping[str, CountryIndicatorSpec]] = {
    "NY.GDP.MKTP.KD.ZG": CountryIndicatorSpec(
        "NY.GDP.MKTP.KD.ZG", "gdp_growth", "sovereign_context", "growth_pressure", -1, 8.0
    ),
    "FP.CPI.TOTL.ZG": CountryIndicatorSpec(
        "FP.CPI.TOTL.ZG", "inflation", "sovereign_context", "inflation_pressure", 1, 25.0
    ),
    "BN.CAB.XOKA.GD.ZS": CountryIndicatorSpec(
        "BN.CAB.XOKA.GD.ZS",
        "current_account_balance_gdp",
        "trade_context",
        "external_balance_pressure",
        -1,
        10.0,
    ),
    "DT.DOD.DECT.GN.ZS": CountryIndicatorSpec(
        "DT.DOD.DECT.GN.ZS",
        "external_debt_gni",
        "debt_context",
        "external_debt_pressure",
        1,
        100.0,
    ),
    "FI.RES.TOTL.CD": CountryIndicatorSpec(
        "FI.RES.TOTL.CD",
        "fx_reserves",
        "sovereign_context",
        "fx_reserve_pressure",
        -1,
        1_000_000_000.0,
    ),
    "SL.UEM.TOTL.ZS": CountryIndicatorSpec(
        "SL.UEM.TOTL.ZS",
        "unemployment",
        "development_context",
        "labor_market_stress",
        1,
        25.0,
    ),
    "NE.TRD.GNFS.ZS": CountryIndicatorSpec(
        "NE.TRD.GNFS.ZS",
        "trade_openness",
        "trade_context",
        "trade_exposure_concentration",
        1,
        150.0,
    ),
    "GE.EST": CountryIndicatorSpec(
        "GE.EST",
        "government_effectiveness",
        "governance_context",
        "governance_deterioration",
        -1,
        2.5,
    ),
}


def build_country_context_snapshot(
    *,
    country_code: str,
    snapshot_date: datetime.date | str,
    indicator_observations: Sequence[Mapping[str, Any] | object],
    humanitarian_reports: Sequence[Mapping[str, Any] | object] = (),
    geo_incidents: Sequence[Mapping[str, Any] | object] = (),
    required_indicator_ids: Sequence[str] = DEFAULT_REQUIRED_INDICATOR_IDS,
    recent_window_days: int = 30,
) -> CountryContextSnapshot:
    """Build a country context snapshot from already-ingested provider facts."""
    country = country_code.strip().upper()
    if not country:
        msg = "country_code must be a non-empty string"
        raise ValueError(msg)
    date_value = _coerce_date(snapshot_date)
    latest = _latest_indicator_observations(country, date_value, indicator_observations)

    contexts: dict[str, dict[str, Any]] = {
        "sovereign_context": {},
        "governance_context": {},
        "debt_context": {},
        "trade_context": {},
        "development_context": {},
    }
    risk_signals: dict[str, Any] = {}
    evidence_refs: list[Mapping[str, str]] = []

    for indicator_id, observation in sorted(latest.items()):
        spec = _spec_for_indicator(indicator_id, observation)
        refs = _evidence_refs(observation)
        evidence_refs.extend(refs)
        metric = _metric_payload(indicator_id, spec, observation, refs, date_value)
        contexts[spec.context][spec.metric] = metric
        risk_signals[spec.risk_signal] = _risk_signal_payload(
            name=spec.risk_signal,
            group=spec.context,
            score=_indicator_stress_score(metric["value"], spec),
            evidence_refs=refs,
            value=metric["value"],
            observed_on=metric["observed_on"],
        )

    missing_indicators = tuple(
        indicator_id for indicator_id in required_indicator_ids if indicator_id not in latest
    )
    humanitarian_context, humanitarian_signal = _humanitarian_context(
        country=country,
        snapshot_date=date_value,
        reports=humanitarian_reports,
        recent_window_days=recent_window_days,
    )
    geo_context, geo_signal = _geo_context(
        country=country,
        snapshot_date=date_value,
        incidents=geo_incidents,
        recent_window_days=recent_window_days,
    )
    if humanitarian_signal is not None:
        risk_signals[humanitarian_signal["name"]] = humanitarian_signal
        evidence_refs.extend(humanitarian_signal["evidence_refs"])
    if geo_signal is not None:
        risk_signals[geo_signal["name"]] = geo_signal
        evidence_refs.extend(geo_signal["evidence_refs"])

    return CountryContextSnapshot(
        country_code=country,
        snapshot_date=date_value,
        sovereign_context=contexts["sovereign_context"],
        governance_context=contexts["governance_context"],
        debt_context=contexts["debt_context"],
        trade_context=contexts["trade_context"],
        development_context=contexts["development_context"],
        humanitarian_context=humanitarian_context,
        geo_context=geo_context,
        risk_signals=risk_signals,
        evidence_refs=tuple(evidence_refs),
        metadata={
            "missing_indicator_ids": missing_indicators,
            "indicator_count": len(latest),
            "required_indicator_count": len(tuple(required_indicator_ids)),
            "coverage_ratio": round(
                len(latest) / len(tuple(required_indicator_ids)) if required_indicator_ids else 1.0,
                4,
            ),
            "recent_window_days": recent_window_days,
        },
    )


def country_context_risk_signals(snapshot: CountryContextSnapshot) -> tuple[Mapping[str, Any], ...]:
    """Return snapshot risk signals in deterministic order for fusion."""
    return tuple(snapshot.risk_signals[key] for key in sorted(snapshot.risk_signals))


def _latest_indicator_observations(
    country: str,
    snapshot_date: datetime.date,
    observations: Sequence[Mapping[str, Any] | object],
) -> dict[str, Mapping[str, Any] | object]:
    latest: dict[str, Mapping[str, Any] | object] = {}
    for observation in observations:
        if _country_code(observation) != country:
            continue
        observed_on = _observed_on(observation)
        if observed_on is None or observed_on > snapshot_date:
            continue
        indicator_id = _indicator_id(observation)
        if not indicator_id:
            continue
        existing = latest.get(indicator_id)
        if existing is None or (_observed_on(existing) or datetime.date.min) < observed_on:
            latest[indicator_id] = observation
    return latest


def _metric_payload(
    indicator_id: str,
    spec: CountryIndicatorSpec,
    observation: Mapping[str, Any] | object,
    refs: tuple[Mapping[str, str], ...],
    snapshot_date: datetime.date,
) -> dict[str, Any]:
    observed_on = _observed_on(observation)
    value = _float_or_none(_get(observation, "value", _MISSING))
    return {
        "indicator_id": indicator_id,
        "label": _indicator_label(observation, spec),
        "value": value,
        "unit": str(_get(observation, "unit", _get(observation, "units", "")) or ""),
        "observed_on": observed_on.isoformat() if observed_on else None,
        "freshness_days": (snapshot_date - observed_on).days if observed_on else None,
        "status": "available",
        "evidence_refs": list(refs),
    }


def _humanitarian_context(
    *,
    country: str,
    snapshot_date: datetime.date,
    reports: Sequence[Mapping[str, Any] | object],
    recent_window_days: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
    recent_start = snapshot_date - datetime.timedelta(days=recent_window_days)
    previous_start = recent_start - datetime.timedelta(days=recent_window_days)
    recent: list[Mapping[str, Any] | object] = []
    previous_count = 0

    for report in reports:
        published = _date_value(_get(report, "published_at", _get(report, "updated_at", None)))
        if published is None or published > snapshot_date or not _matches_country(report, country):
            continue
        if published >= recent_start:
            recent.append(report)
        elif published >= previous_start:
            previous_count += 1

    recent.sort(key=lambda item: (_date_value(_get(item, "published_at", None)) or datetime.date.min), reverse=True)
    refs = _dedupe_refs(ref for report in recent for ref in _evidence_refs(report))
    themes = sorted({str(item) for report in recent for item in _as_sequence(_get(report, "themes", ()))})
    disaster_types = sorted(
        {str(item) for report in recent for item in _as_sequence(_get(report, "disaster_types", ()))}
    )
    context = {
        "recent_report_count": len(recent),
        "previous_window_report_count": previous_count,
        "themes": themes,
        "disaster_types": disaster_types,
        "reports": [_report_payload(report) for report in recent[:5]],
        "evidence_refs": list(refs),
    }
    if not recent:
        return context, None

    score = _clamp(len(recent) * 15.0 + max(0, len(recent) - previous_count) * 20.0)
    return context, _risk_signal_payload(
        name="humanitarian_report_velocity",
        group="humanitarian_context",
        score=score,
        evidence_refs=refs,
        value=len(recent),
        observed_on=snapshot_date.isoformat(),
    )


def _geo_context(
    *,
    country: str,
    snapshot_date: datetime.date,
    incidents: Sequence[Mapping[str, Any] | object],
    recent_window_days: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
    recent_start = snapshot_date - datetime.timedelta(days=recent_window_days)
    recent: list[Mapping[str, Any] | object] = []
    for incident in incidents:
        observed = _date_value(_get(incident, "observed_at", _get(incident, "occurred_at", None)))
        if observed is None or observed > snapshot_date or observed < recent_start:
            continue
        if _country_code(incident) == country:
            recent.append(incident)

    recent.sort(key=lambda item: (_date_value(_get(item, "observed_at", _get(item, "occurred_at", None))) or datetime.date.min), reverse=True)
    severity_scores = tuple(_incident_severity_score(incident) for incident in recent)
    max_severity = max(severity_scores, default=0.0)
    refs = _dedupe_refs(ref for incident in recent for ref in _evidence_refs(incident))
    context = {
        "recent_incident_count": len(recent),
        "max_severity_score": max_severity,
        "incident_types": sorted({str(_get(incident, "incident_type", "unknown")) for incident in recent}),
        "incidents": [_incident_payload(incident) for incident in recent[:5]],
        "evidence_refs": list(refs),
    }
    if not recent:
        return context, None

    return context, _risk_signal_payload(
        name="disaster_incident_severity",
        group="geo_context",
        score=max_severity,
        evidence_refs=refs,
        value=len(recent),
        observed_on=snapshot_date.isoformat(),
    )


def _report_payload(report: Mapping[str, Any] | object) -> dict[str, Any]:
    published = _date_value(_get(report, "published_at", None))
    return {
        "id": str(_get(report, "report_id", _get(report, "external_id", ""))),
        "title": str(_get(report, "title", "")),
        "published_at": published.isoformat() if published else None,
        "url": str(_get(report, "url", "") or ""),
        "evidence_refs": list(_evidence_refs(report)),
    }


def _incident_payload(incident: Mapping[str, Any] | object) -> dict[str, Any]:
    observed = _date_value(_get(incident, "observed_at", _get(incident, "occurred_at", None)))
    return {
        "id": str(_get(incident, "incident_id", _get(incident, "external_id", ""))),
        "title": str(_get(incident, "title", "")),
        "incident_type": str(_get(incident, "incident_type", "unknown")),
        "observed_at": observed.isoformat() if observed else None,
        "severity_score": _incident_severity_score(incident),
        "evidence_refs": list(_evidence_refs(incident)),
    }


def _risk_signal_payload(
    *,
    name: str,
    group: str,
    score: float,
    evidence_refs: Sequence[Mapping[str, str]],
    value: Any,
    observed_on: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "group": group,
        "score": round(_clamp(score), 2),
        "weight": 1.0,
        "direction": "raises_risk",
        "value": _json_safe(value),
        "observed_on": _json_safe(observed_on),
        "evidence_refs": list(_dedupe_refs(evidence_refs)),
    }


def _indicator_stress_score(value: float | None, spec: CountryIndicatorSpec) -> float:
    if value is None:
        return 0.0
    if spec.risk_signal == "governance_deterioration":
        return _clamp(((2.5 - value) / 5.0) * 100.0)
    if spec.risk_signal == "fx_reserve_pressure":
        return 0.0 if value > 0 else 100.0
    directed = value * spec.direction
    return _clamp((directed / spec.scale) * 100.0)


def _spec_for_indicator(indicator_id: str, observation: Mapping[str, Any] | object) -> CountryIndicatorSpec:
    if indicator_id in INDICATOR_SPECS:
        return INDICATOR_SPECS[indicator_id]
    text = f"{indicator_id} {_indicator_label(observation, None)}".casefold()
    if "governance" in text or "government" in text or "rule of law" in text:
        return CountryIndicatorSpec(
            indicator_id, _metric_name(indicator_id), "governance_context", "governance_deterioration", -1, 2.5
        )
    if "debt" in text:
        return CountryIndicatorSpec(
            indicator_id, _metric_name(indicator_id), "debt_context", "external_debt_pressure", 1, 100.0
        )
    if "trade" in text or "current account" in text or "import" in text or "export" in text:
        return CountryIndicatorSpec(
            indicator_id, _metric_name(indicator_id), "trade_context", "trade_exposure_concentration", 1, 150.0
        )
    if "unemployment" in text or "poverty" in text or "population" in text:
        return CountryIndicatorSpec(
            indicator_id, _metric_name(indicator_id), "development_context", "development_stress", 1, 100.0
        )
    return CountryIndicatorSpec(
        indicator_id, _metric_name(indicator_id), "sovereign_context", "sovereign_macro_pressure", 1, 100.0
    )


def _indicator_id(observation: Mapping[str, Any] | object) -> str:
    value = _get(observation, "indicator_id", _MISSING)
    if value is not _MISSING and value not in (None, ""):
        return str(value)
    metadata = _metadata(observation)
    for key in ("indicator_id", "series_id"):
        value = metadata.get(key)
        if value not in (None, ""):
            return str(value)
    nested = metadata.get("observation")
    if isinstance(nested, Mapping):
        value = nested.get("indicator_id") or nested.get("series_id")
        if value not in (None, ""):
            return str(value)
    return ""


def _indicator_label(
    observation: Mapping[str, Any] | object,
    spec: CountryIndicatorSpec | None,
) -> str:
    for key in ("name", "title", "label", "indicator_name"):
        value = _get(observation, key, _MISSING)
        if value is not _MISSING and value not in (None, ""):
            return str(value)
    metadata = _metadata(observation)
    for key in ("name", "title", "label"):
        value = metadata.get(key)
        if value not in (None, ""):
            return str(value)
    return spec.metric if spec else _indicator_id(observation)


def _metric_name(indicator_id: str) -> str:
    return indicator_id.casefold().replace(".", "_").replace("-", "_")


def _observed_on(observation: Mapping[str, Any] | object) -> datetime.date | None:
    return _date_value(_get(observation, "observed_on", _get(observation, "date", _get(observation, "observed_at", None))))


def _matches_country(record: Mapping[str, Any] | object, country: str) -> bool:
    countries = _get(record, "country_codes", _get(record, "countries", ()))
    if countries:
        return country in {str(item).upper() for item in _as_sequence(countries)}
    return _country_code(record) == country


def _country_code(record: Mapping[str, Any] | object) -> str:
    value = _get(record, "country_code", _get(record, "country", ""))
    return str(value or "").strip().upper()


def _incident_severity_score(incident: Mapping[str, Any] | object) -> float:
    severity = str(_get(incident, "severity", "") or "").casefold()
    if severity in {"critical", "severe", "high"}:
        return 85.0
    if severity in {"moderate", "medium"}:
        return 55.0
    if severity in {"minor", "low"}:
        return 25.0
    magnitude = _float_or_none(_get(incident, "magnitude", None))
    if magnitude is not None:
        return round(_clamp((magnitude / 8.0) * 100.0), 2)
    return 35.0


def _evidence_refs(record: Mapping[str, Any] | object) -> tuple[Mapping[str, str], ...]:
    try:
        return (evidence_ref_for_provider_fact(record),)
    except (TypeError, ValueError):
        fallback = _get(record, "id", _get(record, "external_id", _get(record, "report_id", "unknown")))
        return ({"kind": "signal", "id": f"country-context:{fallback}"},)


def _dedupe_refs(refs: Sequence[Mapping[str, str]] | Any) -> tuple[Mapping[str, str], ...]:
    seen: set[tuple[str, str]] = set()
    output: list[Mapping[str, str]] = []
    for ref in refs:
        kind = str(ref["kind"])
        id_value = str(ref["id"])
        key = (kind, id_value)
        if key in seen:
            continue
        seen.add(key)
        output.append({"kind": kind, "id": id_value})
    return tuple(output)


def _metadata(record: Mapping[str, Any] | object) -> Mapping[str, Any]:
    for name in ("metadata", "observation_metadata", "snapshot_metadata"):
        value = _get(record, name, _MISSING)
        if isinstance(value, Mapping):
            return value
    return {}


def _get(source: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str | bytes | bytearray):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(value)
    if isinstance(value, set | frozenset):
        return tuple(sorted(value, key=lambda item: str(item)))
    return (value,)


def _date_value(value: Any) -> datetime.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def _coerce_date(value: datetime.date | str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def _float_or_none(value: Any) -> float | None:
    if value in (None, "", "."):
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        return None
    return parsed


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _json_safe(item) for key, item in sorted(value.items())})


def _json_safe(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in sorted(value.items())}
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_json_safe(item) for item in value), key=lambda item: str(item))
    return value
