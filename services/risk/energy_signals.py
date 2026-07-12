"""Energy-market stress signals derived from retained energy snapshots."""

from __future__ import annotations

import datetime
import decimal
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from services.evidence.graph import evidence_ref_for_provider_fact

_MISSING = object()


@dataclass(frozen=True)
class EnergyStressSignal:
    """One explainable energy stress signal."""

    name: str
    region: str
    commodity: str
    snapshot_date: datetime.date
    score: float
    direction: str
    value: float | None = None
    baseline_value: float | None = None
    change_pct: float | None = None
    evidence_refs: tuple[Mapping[str, str], ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in {"raises_risk", "lowers_risk"}:
            msg = "direction must be raises_risk or lowers_risk"
            raise ValueError(msg)
        object.__setattr__(self, "name", _compact(self.name))
        object.__setattr__(self, "region", str(self.region).strip() or "global")
        object.__setattr__(self, "commodity", _compact(self.commodity) or "energy")
        object.__setattr__(self, "snapshot_date", _date_value(self.snapshot_date))
        object.__setattr__(self, "score", round(_clamp(float(self.score)), 2))
        object.__setattr__(self, "evidence_refs", _dedupe_refs(self.evidence_refs))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))
        if not self.evidence_refs:
            msg = "energy stress signals require evidence refs"
            raise ValueError(msg)

    @property
    def driver_id(self) -> str:
        return f"energy:{self.region}:{self.commodity}:{self.name}"

    def as_payload(self) -> dict[str, Any]:
        return {
            "driver_id": self.driver_id,
            "name": self.name,
            "group": "energy",
            "region": self.region,
            "commodity": self.commodity,
            "snapshot_date": self.snapshot_date.isoformat(),
            "score": self.score,
            "weight": 1.0,
            "direction": self.direction,
            "value": self.value,
            "baseline_value": self.baseline_value,
            "change_pct": self.change_pct,
            "evidence_refs": list(self.evidence_refs),
            "metadata": _json_safe(self.metadata),
        }

    def as_risk_signal(self, *, weight: float = 1.0) -> dict[str, Any]:
        payload = self.as_payload()
        payload["weight"] = weight
        return payload


@dataclass(frozen=True)
class EnergyStressPanel:
    """Collection of energy stress signals for a snapshot date."""

    snapshot_date: datetime.date
    signals: tuple[EnergyStressSignal, ...]
    evidence_refs: tuple[Mapping[str, str], ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_date", _date_value(self.snapshot_date))
        object.__setattr__(self, "signals", tuple(sorted(self.signals, key=lambda signal: signal.driver_id)))
        refs = self.evidence_refs or tuple(ref for signal in self.signals for ref in signal.evidence_refs)
        object.__setattr__(self, "evidence_refs", _dedupe_refs(refs))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def as_payload(self) -> dict[str, Any]:
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            "signals": [signal.as_payload() for signal in self.signals],
            "evidence_refs": list(self.evidence_refs),
            "metadata": _json_safe(self.metadata),
        }

    def risk_signals(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(signal.as_risk_signal() for signal in self.signals)


def build_energy_stress_panel(
    *,
    snapshots: Sequence[Mapping[str, Any] | object],
    previous_snapshots: Sequence[Mapping[str, Any] | object] = (),
    snapshot_date: datetime.date | str | None = None,
) -> EnergyStressPanel:
    """Build energy stress signals from current and prior market snapshots."""
    if not snapshots:
        date_value = _date_value(snapshot_date or datetime.date.today())
        return EnergyStressPanel(snapshot_date=date_value, signals=(), metadata={"snapshot_count": 0})

    as_of = _date_value(snapshot_date) if snapshot_date is not None else max(
        _snapshot_date(snapshot) for snapshot in snapshots if _snapshot_date(snapshot) is not None
    )
    current_by_key = _latest_snapshots(snapshots, as_of=as_of, before=False)
    prior_pool = tuple(previous_snapshots) + tuple(snapshots)
    signals: list[EnergyStressSignal] = []

    for key in sorted(current_by_key):
        current = current_by_key[key]
        previous = _previous_snapshot(prior_pool, key=key, before_date=_snapshot_date(current))
        signals.extend(_signals_for_snapshot(current, previous))

    return EnergyStressPanel(
        snapshot_date=as_of,
        signals=tuple(signals),
        metadata={
            "snapshot_count": len(current_by_key),
            "previous_snapshot_count": len(tuple(previous_snapshots)),
        },
    )


def build_energy_stress_signals(
    snapshots: Sequence[Mapping[str, Any] | object],
    *,
    previous_snapshots: Sequence[Mapping[str, Any] | object] = (),
    snapshot_date: datetime.date | str | None = None,
) -> tuple[EnergyStressSignal, ...]:
    """Convenience wrapper returning only stress signals."""
    return build_energy_stress_panel(
        snapshots=snapshots,
        previous_snapshots=previous_snapshots,
        snapshot_date=snapshot_date,
    ).signals


def _signals_for_snapshot(
    current: Mapping[str, Any] | object,
    previous: Mapping[str, Any] | object | None,
) -> tuple[EnergyStressSignal, ...]:
    region = str(_get(current, "region", _get(current, "geography", "global")) or "global")
    commodity = _commodity(current)
    snapshot_date = _snapshot_date(current)
    refs = _evidence_refs(current)
    if snapshot_date is None:
        return ()

    signals: list[EnergyStressSignal] = []
    price = _number(current, "price")
    previous_price = _number(previous, "price") if previous is not None else None
    if price is not None and previous_price is not None and previous_price > 0:
        change = (price - previous_price) / previous_price
        signals.append(
            _signal(
                name=_price_signal_name(commodity),
                region=region,
                commodity=commodity,
                snapshot_date=snapshot_date,
                value=price,
                baseline_value=previous_price,
                change_pct=change,
                score=_clamp(abs(change) / 0.25 * 100.0),
                direction="raises_risk" if change >= 0 else "lowers_risk",
                evidence_refs=refs,
            )
        )

    inventory = _number(current, "inventory")
    previous_inventory = _number(previous, "inventory") if previous is not None else None
    if inventory is not None and previous_inventory is not None and previous_inventory > 0:
        change = (inventory - previous_inventory) / previous_inventory
        name = "gas_storage_stress" if "gas" in commodity else "inventory_drawdown"
        signals.append(
            _signal(
                name=name,
                region=region,
                commodity=commodity,
                snapshot_date=snapshot_date,
                value=inventory,
                baseline_value=previous_inventory,
                change_pct=change,
                score=_clamp(abs(change) / 0.20 * 100.0),
                direction="raises_risk" if change < 0 else "lowers_risk",
                evidence_refs=refs,
            )
        )

    production = _number(current, "production")
    previous_production = _number(previous, "production") if previous is not None else None
    if production is not None and previous_production is not None and previous_production > 0:
        change = (production - previous_production) / previous_production
        signals.append(
            _signal(
                name="production_disruption",
                region=region,
                commodity=commodity,
                snapshot_date=snapshot_date,
                value=production,
                baseline_value=previous_production,
                change_pct=change,
                score=_clamp(abs(change) / 0.15 * 100.0),
                direction="raises_risk" if change < 0 else "lowers_risk",
                evidence_refs=refs,
            )
        )

    imports = _number(current, "imports")
    consumption = _number(current, "consumption")
    if imports is not None and consumption is not None and consumption > 0:
        dependency = imports / consumption
        signals.append(
            _signal(
                name="energy_import_dependency",
                region=region,
                commodity=commodity,
                snapshot_date=snapshot_date,
                value=imports,
                baseline_value=consumption,
                change_pct=None,
                score=_clamp(dependency * 100.0),
                direction="raises_risk",
                evidence_refs=refs,
                metadata={"dependency_ratio": round(dependency, 4)},
            )
        )

    return tuple(signal for signal in signals if signal.score > 0.0)


def _signal(
    *,
    name: str,
    region: str,
    commodity: str,
    snapshot_date: datetime.date,
    value: float | None,
    baseline_value: float | None,
    change_pct: float | None,
    score: float,
    direction: str,
    evidence_refs: Sequence[Mapping[str, str]],
    metadata: Mapping[str, Any] | None = None,
) -> EnergyStressSignal:
    return EnergyStressSignal(
        name=name,
        region=region,
        commodity=commodity,
        snapshot_date=snapshot_date,
        score=score,
        direction=direction,
        value=None if value is None else round(value, 6),
        baseline_value=None if baseline_value is None else round(baseline_value, 6),
        change_pct=None if change_pct is None else round(change_pct, 6),
        evidence_refs=tuple(evidence_refs),
        metadata=metadata or {},
    )


def _latest_snapshots(
    snapshots: Sequence[Mapping[str, Any] | object],
    *,
    as_of: datetime.date,
    before: bool,
) -> dict[tuple[str, str], Mapping[str, Any] | object]:
    latest: dict[tuple[str, str], Mapping[str, Any] | object] = {}
    for snapshot in snapshots:
        date_value = _snapshot_date(snapshot)
        if date_value is None:
            continue
        if before and date_value >= as_of:
            continue
        if not before and date_value > as_of:
            continue
        key = _snapshot_key(snapshot)
        existing = latest.get(key)
        if existing is None or (_snapshot_date(existing) or datetime.date.min) < date_value:
            latest[key] = snapshot
    return latest


def _previous_snapshot(
    snapshots: Sequence[Mapping[str, Any] | object],
    *,
    key: tuple[str, str],
    before_date: datetime.date | None,
) -> Mapping[str, Any] | object | None:
    if before_date is None:
        return None
    latest = _latest_snapshots(
        [snapshot for snapshot in snapshots if _snapshot_key(snapshot) == key],
        as_of=before_date,
        before=True,
    )
    return latest.get(key)


def _snapshot_key(snapshot: Mapping[str, Any] | object) -> tuple[str, str]:
    region = str(_get(snapshot, "region", _get(snapshot, "geography", "global")) or "global")
    return (region, _commodity(snapshot))


def _commodity(snapshot: Mapping[str, Any] | object) -> str:
    value = _get(snapshot, "commodity", "")
    if value:
        return _compact(value)
    metadata = _get(snapshot, "snapshot_metadata", _get(snapshot, "metadata", {}))
    if isinstance(metadata, Mapping):
        series = metadata.get("series") or metadata.get("observation") or {}
        if isinstance(series, Mapping):
            text = f"{series.get('series_id', '')} {series.get('name', '')}".casefold()
            if "gas" in text:
                return "natural_gas"
            if "electric" in text or "power" in text:
                return "electricity"
            if "coal" in text:
                return "coal"
    return "oil"


def _price_signal_name(commodity: str) -> str:
    if "electric" in commodity or "power" in commodity:
        return "electricity_price_stress"
    if "oil" in commodity or "petroleum" in commodity or "crude" in commodity:
        return "oil_price_shock"
    return "energy_price_stress"


def _number(snapshot: Mapping[str, Any] | object | None, name: str) -> float | None:
    if snapshot is None:
        return None
    value = _get(snapshot, name, _MISSING)
    if value in (_MISSING, None, ""):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _snapshot_date(snapshot: Mapping[str, Any] | object) -> datetime.date | None:
    value = _get(snapshot, "snapshot_date", _get(snapshot, "date", _get(snapshot, "observed_at", None)))
    if value in (None, ""):
        return None
    return _date_value(value)


def _date_value(value: datetime.date | str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def _evidence_refs(snapshot: Mapping[str, Any] | object) -> tuple[Mapping[str, str], ...]:
    try:
        return (evidence_ref_for_provider_fact(snapshot),)
    except (TypeError, ValueError):
        region, commodity = _snapshot_key(snapshot)
        date_value = _snapshot_date(snapshot)
        return ({"kind": "signal", "id": f"energy_snapshot:{region}:{commodity}:{date_value}"},)


def _dedupe_refs(refs: Sequence[Mapping[str, str]]) -> tuple[Mapping[str, str], ...]:
    seen: set[tuple[str, str]] = set()
    output: list[Mapping[str, str]] = []
    for ref in refs:
        key = (str(ref["kind"]), str(ref["id"]))
        if key in seen:
            continue
        seen.add(key)
        output.append({"kind": key[0], "id": key[1]})
    return tuple(output)


def _get(source: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _compact(value: object) -> str:
    return "_".join(str(value).casefold().strip().replace("/", "_").split())


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
