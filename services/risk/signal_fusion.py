"""Fuse derived provider signals into rating-driver payloads."""

from __future__ import annotations

import datetime
import decimal
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from db.models.enums import RiskLevel, risk_level_for_score
from services.crisis_model.contract import validate_evidence_refs

_MISSING = object()


@dataclass(frozen=True)
class RiskSignalInput:
    """A normalized signal accepted by the rating-driver fusion layer."""

    name: str
    group: str
    score: float
    evidence_refs: tuple[Mapping[str, str], ...]
    weight: float = 1.0
    direction: str = "raises_risk"
    driver_id: str = ""
    value: Any = None
    rationale: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in {"raises_risk", "lowers_risk"}:
            msg = "direction must be raises_risk or lowers_risk"
            raise ValueError(msg)
        if not self.name.strip():
            msg = "signal name must be a non-empty string"
            raise ValueError(msg)
        if not self.group.strip():
            msg = "signal group must be a non-empty string"
            raise ValueError(msg)
        if self.weight <= 0 or not math.isfinite(self.weight):
            msg = "signal weight must be a positive finite number"
            raise ValueError(msg)
        score = _clamp(float(self.score))
        refs = _dedupe_refs(self.evidence_refs)
        if not refs:
            msg = "risk signals require evidence refs"
            raise ValueError(msg)
        validate_evidence_refs(refs)
        object.__setattr__(self, "name", _compact(self.name))
        object.__setattr__(self, "group", _compact(self.group))
        object.__setattr__(self, "score", round(score, 2))
        object.__setattr__(self, "weight", float(self.weight))
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "driver_id", self.driver_id or f"{self.group}:{self.name}")
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    @property
    def signed_score(self) -> float:
        return self.score if self.direction == "raises_risk" else -self.score


@dataclass(frozen=True)
class RatingDriver:
    """One evidence-backed contribution in a rating payload."""

    driver_id: str
    name: str
    group: str
    score: float
    weight: float
    contribution: float
    direction: str
    evidence_refs: tuple[Mapping[str, str], ...]
    value: Any = None
    rationale: str = ""
    previous_score: float | None = None
    changed_since_previous: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_refs:
            msg = "rating drivers require evidence refs"
            raise ValueError(msg)
        validate_evidence_refs(self.evidence_refs)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def as_payload(self) -> dict[str, Any]:
        return {
            "driver_id": self.driver_id,
            "name": self.name,
            "group": self.group,
            "score": self.score,
            "weight": self.weight,
            "contribution": self.contribution,
            "direction": self.direction,
            "value": _json_safe(self.value),
            "rationale": self.rationale,
            "previous_score": self.previous_score,
            "changed_since_previous": self.changed_since_previous,
            "evidence_refs": list(self.evidence_refs),
            "metadata": _json_safe(self.metadata),
        }


@dataclass(frozen=True)
class RatingPayload:
    """Explainable rating-driver payload for downstream APIs."""

    target_type: str
    target_id: str
    risk_type: str
    as_of_date: datetime.date
    risk_score: float
    risk_level: RiskLevel
    confidence_score: float
    top_drivers: tuple[RatingDriver, ...]
    evidence_refs: tuple[Mapping[str, str], ...]
    changed_since_previous: Mapping[str, Any] | None = None
    model_versions: Mapping[str, str] = field(
        default_factory=lambda: {"signal_fusion": "provider-signal-fusion.v1"}
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.target_type.strip():
            msg = "target_type must be a non-empty string"
            raise ValueError(msg)
        if not self.target_id.strip():
            msg = "target_id must be a non-empty string"
            raise ValueError(msg)
        if not self.risk_type.strip():
            msg = "risk_type must be a non-empty string"
            raise ValueError(msg)
        refs = _dedupe_refs(self.evidence_refs)
        if not refs:
            msg = "rating payloads require evidence refs"
            raise ValueError(msg)
        validate_evidence_refs(refs)
        object.__setattr__(self, "as_of_date", _date_value(self.as_of_date))
        object.__setattr__(self, "risk_score", round(_clamp(float(self.risk_score)), 2))
        object.__setattr__(self, "confidence_score", round(_clamp(float(self.confidence_score), 0.0, 1.0), 4))
        object.__setattr__(self, "top_drivers", tuple(self.top_drivers))
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "model_versions", _freeze_mapping(self.model_versions))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))
        if self.changed_since_previous is not None:
            object.__setattr__(
                self,
                "changed_since_previous",
                _freeze_mapping(self.changed_since_previous),
            )

    def as_payload(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "risk_type": self.risk_type,
            "as_of_date": self.as_of_date.isoformat(),
            "risk_score": self.risk_score,
            "risk_level": self.risk_level.value,
            "confidence_score": self.confidence_score,
            "model_versions": _json_safe(self.model_versions),
            "top_drivers": [driver.as_payload() for driver in self.top_drivers],
            "evidence_refs": list(self.evidence_refs),
            "changed_since_previous": _json_safe(self.changed_since_previous),
            "metadata": _json_safe(self.metadata),
        }


def build_rating_payload(
    *,
    target_type: str,
    target_id: str,
    risk_type: str,
    as_of_date: datetime.date | str,
    signals: Sequence[RiskSignalInput | Mapping[str, Any] | object],
    previous_payload: RatingPayload | Mapping[str, Any] | None = None,
    top_n: int = 8,
    model_version: str = "provider-signal-fusion.v1",
) -> RatingPayload:
    """Build an evidence-backed rating payload from derived risk signals."""
    date_value = _date_value(as_of_date)
    normalized = [_coerce_signal(signal) for signal in signals]
    if not normalized:
        msg = "at least one risk signal is required"
        raise ValueError(msg)

    previous_scores = _previous_driver_scores(previous_payload)
    used_ids: dict[str, int] = {}
    drivers = tuple(
        _driver_from_signal(signal, _deduped_driver_id(signal.driver_id, used_ids), previous_scores)
        for signal in normalized
    )
    total_weight = sum(driver.weight for driver in drivers)
    weighted_score = sum(driver.contribution for driver in drivers) / total_weight
    risk_score = round(_clamp(weighted_score), 2)
    top_drivers = tuple(
        sorted(drivers, key=lambda driver: abs(driver.contribution), reverse=True)[:top_n]
    )
    evidence_refs = _dedupe_refs(ref for driver in drivers for ref in driver.evidence_refs)
    confidence_score = min(1.0, 0.25 + 0.06 * len(drivers) + 0.04 * len(evidence_refs))
    previous_score = _previous_risk_score(previous_payload)
    changed = None
    if previous_score is not None:
        changed = {
            "previous_risk_score": previous_score,
            "risk_score_delta": round(risk_score - previous_score, 2),
            "changed_driver_count": sum(
                1 for driver in drivers if driver.changed_since_previous not in (None, 0.0)
            ),
        }

    return RatingPayload(
        target_type=target_type,
        target_id=target_id,
        risk_type=risk_type,
        as_of_date=date_value,
        risk_score=risk_score,
        risk_level=risk_level_for_score(risk_score),
        confidence_score=confidence_score,
        top_drivers=top_drivers,
        evidence_refs=evidence_refs,
        changed_since_previous=changed,
        model_versions={"signal_fusion": model_version},
        metadata={
            "input_signal_count": len(normalized),
            "driver_count": len(drivers),
            "opposing_driver_count": sum(1 for driver in drivers if driver.direction == "lowers_risk"),
        },
    )


def build_rating_driver_payload(
    *,
    target_type: str,
    target_id: str,
    risk_type: str,
    as_of_date: datetime.date | str,
    signals: Sequence[RiskSignalInput | Mapping[str, Any] | object],
    previous_payload: RatingPayload | Mapping[str, Any] | None = None,
    top_n: int = 8,
) -> dict[str, Any]:
    """Return the serializable rating-driver payload directly."""
    return build_rating_payload(
        target_type=target_type,
        target_id=target_id,
        risk_type=risk_type,
        as_of_date=as_of_date,
        signals=signals,
        previous_payload=previous_payload,
        top_n=top_n,
    ).as_payload()


def signals_from_country_context(snapshot: Mapping[str, Any] | object) -> tuple[RiskSignalInput, ...]:
    """Convert a country context snapshot or payload into fusion inputs."""
    raw_signals = _get(snapshot, "risk_signals", {})
    if isinstance(raw_signals, Mapping):
        values = tuple(raw_signals[key] for key in sorted(raw_signals))
    else:
        values = tuple(raw_signals)
    return tuple(_coerce_signal(value) for value in values)


def signals_from_energy_panel(panel: Mapping[str, Any] | object) -> tuple[RiskSignalInput, ...]:
    """Convert an energy stress panel or payload into fusion inputs."""
    raw_signals = _get(panel, "signals", _get(panel, "risk_signals", ()))
    if callable(raw_signals):
        raw_signals = raw_signals()
    return tuple(_coerce_signal(signal) for signal in raw_signals)


def _driver_from_signal(
    signal: RiskSignalInput,
    driver_id: str,
    previous_scores: Mapping[str, float],
) -> RatingDriver:
    previous_score = previous_scores.get(driver_id)
    if previous_score is None:
        previous_score = previous_scores.get(signal.driver_id)
    changed = None if previous_score is None else round(signal.score - previous_score, 2)
    return RatingDriver(
        driver_id=driver_id,
        name=signal.name,
        group=signal.group,
        score=signal.score,
        weight=signal.weight,
        contribution=round(signal.signed_score * signal.weight, 4),
        direction=signal.direction,
        evidence_refs=signal.evidence_refs,
        value=signal.value,
        rationale=signal.rationale,
        previous_score=previous_score,
        changed_since_previous=changed,
        metadata=signal.metadata,
    )


def _coerce_signal(signal: RiskSignalInput | Mapping[str, Any] | object) -> RiskSignalInput:
    if isinstance(signal, RiskSignalInput):
        return signal
    as_risk_signal = getattr(signal, "as_risk_signal", None)
    if callable(as_risk_signal):
        signal = as_risk_signal()
    refs = _get(signal, "evidence_refs", ())
    if callable(refs):
        refs = refs()
    return RiskSignalInput(
        name=str(_get(signal, "name", _get(signal, "signal", ""))),
        group=str(_get(signal, "group", _get(signal, "source", "provider"))),
        score=float(_get(signal, "score", 0.0)),
        weight=float(_get(signal, "weight", 1.0)),
        direction=str(_get(signal, "direction", "raises_risk") or "raises_risk"),
        driver_id=str(_get(signal, "driver_id", "") or ""),
        value=_get(signal, "value", None),
        rationale=str(_get(signal, "rationale", "") or ""),
        evidence_refs=tuple(_coerce_ref(ref) for ref in refs),
        metadata=_get(signal, "metadata", {}) or {},
    )


def _coerce_ref(ref: Mapping[str, Any] | str) -> Mapping[str, str]:
    if isinstance(ref, str):
        return {"kind": "signal", "id": ref}
    kind = str(ref.get("kind", "signal") or "signal")
    id_value = str(ref.get("id", "") or "").strip()
    if not id_value:
        msg = "evidence ref id must be a non-empty string"
        raise ValueError(msg)
    return {"kind": kind, "id": id_value}


def _deduped_driver_id(driver_id: str, used_ids: dict[str, int]) -> str:
    count = used_ids.get(driver_id, 0) + 1
    used_ids[driver_id] = count
    if count == 1:
        return driver_id
    return f"{driver_id}:{count}"


def _previous_driver_scores(payload: RatingPayload | Mapping[str, Any] | None) -> Mapping[str, float]:
    if payload is None:
        return {}
    drivers = _get(payload, "top_drivers", ())
    scores: dict[str, float] = {}
    for driver in drivers:
        driver_id = str(_get(driver, "driver_id", _get(driver, "name", "")) or "")
        score = _get(driver, "score", _MISSING)
        if driver_id and score is not _MISSING:
            scores[driver_id] = float(score)
    return scores


def _previous_risk_score(payload: RatingPayload | Mapping[str, Any] | None) -> float | None:
    if payload is None:
        return None
    value = _get(payload, "risk_score", _MISSING)
    if value is _MISSING:
        return None
    return float(value)


def _get(source: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _date_value(value: datetime.date | str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def _dedupe_refs(refs: Sequence[Mapping[str, str]] | Any) -> tuple[Mapping[str, str], ...]:
    seen: set[tuple[str, str]] = set()
    output: list[Mapping[str, str]] = []
    for ref in refs:
        key = (str(ref["kind"]), str(ref["id"]))
        if key in seen:
            continue
        seen.add(key)
        output.append({"kind": key[0], "id": key[1]})
    return tuple(output)


def _compact(value: object) -> str:
    return "_".join(str(value).casefold().strip().replace("/", "_").split())


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _json_safe(item) for key, item in sorted(value.items())})


def _json_safe(value: Any) -> Any:
    if isinstance(value, RiskLevel):
        return value.value
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
