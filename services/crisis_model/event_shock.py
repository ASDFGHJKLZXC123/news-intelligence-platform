"""Structured news/event shock component for the standalone crisis model."""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from db.models.enums import RiskType
from services.crisis_model.evidence import dedupe_evidence_refs, event_feature_evidence_refs
from services.crisis_model.types import ComponentForecast, HorizonBuckets

EVENT_SHOCK_MODEL_VERSION: Final[str] = "event-shock-rules.v1"


@dataclass(frozen=True)
class EventRiskInput:
    """DB-free shape mirroring the useful parts of `event_risk_features`."""

    event_id: str
    country: str | None = None
    risk_type: str | None = None
    event_type: str | None = None
    mechanism: str | None = None
    severity_score: float | None = None
    velocity_zscore: float | None = None
    source_diversity_score: float | None = None
    source_authority_score: float | None = None
    official_confirmation: bool | None = None
    rumor_risk_score: float | None = None
    market_relevance_score: float | None = None
    macro_relevance_score: float | None = None
    observed_at: datetime.datetime | datetime.date | str | None = None
    evidence_article_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventShockFeatures:
    """Aggregated event-shock features for a target/date/risk type."""

    target_id: str
    risk_type: str
    as_of_date: datetime.date
    event_count: int
    intensity_score: float
    confidence_score: float
    evidence_strength: float
    top_drivers: tuple[Mapping[str, Any], ...]
    evidence_refs: tuple[Mapping[str, str], ...]


def _get(source: Mapping[str, Any] | object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _coerce_date(value: datetime.datetime | datetime.date | str | None) -> datetime.date | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _score_0_100(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return max(0.0, min(100.0, float(value)))


def _zscore_to_score(value: Any) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(100.0, (float(value) / 3.0) * 100.0))


def coerce_event_risk_input(source: Mapping[str, Any] | object) -> EventRiskInput:
    """Convert a mapping/ORM-like object into an EventRiskInput."""
    event_id = _get(source, "event_id") or _get(source, "id")
    if not event_id:
        msg = "event input requires event_id or id"
        raise ValueError(msg)
    articles = _get(source, "evidence_article_ids") or ()
    return EventRiskInput(
        event_id=str(event_id),
        country=_get(source, "country"),
        risk_type=_get(source, "risk_type"),
        event_type=_get(source, "event_type"),
        mechanism=_get(source, "mechanism"),
        severity_score=_get(source, "severity_score"),
        velocity_zscore=_get(source, "velocity_zscore"),
        source_diversity_score=_get(source, "source_diversity_score"),
        source_authority_score=_get(source, "source_authority_score"),
        official_confirmation=_get(source, "official_confirmation"),
        rumor_risk_score=_get(source, "rumor_risk_score"),
        market_relevance_score=_get(source, "market_relevance_score"),
        macro_relevance_score=_get(source, "macro_relevance_score"),
        observed_at=_get(source, "observed_at"),
        evidence_article_ids=tuple(str(article_id) for article_id in articles),
    )


def score_event_intensity(event: EventRiskInput) -> float:
    """Compute a deterministic 0-100 event-shock intensity score."""
    severity = _score_0_100(event.severity_score)
    velocity = _zscore_to_score(event.velocity_zscore)
    diversity = _score_0_100(event.source_diversity_score) * (
        100.0 if event.source_diversity_score is not None and event.source_diversity_score <= 1 else 1
    )
    authority = _score_0_100(event.source_authority_score) * (
        100.0 if event.source_authority_score is not None and event.source_authority_score <= 1 else 1
    )
    relevance = max(
        _score_0_100(event.market_relevance_score),
        _score_0_100(event.macro_relevance_score),
    )
    rumor_penalty = _score_0_100(event.rumor_risk_score) * 0.16
    official_bonus = 8.0 if event.official_confirmation else 0.0
    intensity = (
        0.34 * severity
        + 0.20 * velocity
        + 0.12 * diversity
        + 0.10 * authority
        + 0.14 * relevance
        + official_bonus
        - rumor_penalty
    )
    return round(max(0.0, min(100.0, intensity)), 2)


def aggregate_event_shocks(
    *,
    target_id: str,
    risk_type: str,
    as_of_date: datetime.date | str,
    events: Sequence[Mapping[str, Any] | object],
) -> EventShockFeatures:
    """Aggregate event-risk features into one shock component input."""
    if risk_type not in {item.value for item in RiskType}:
        msg = f"unsupported risk_type: {risk_type}"
        raise ValueError(msg)
    as_of = _coerce_date(as_of_date)
    assert as_of is not None

    drivers: list[Mapping[str, Any]] = []
    evidence_refs: list[Mapping[str, str]] = []
    intensities: list[float] = []
    confidences: list[float] = []

    for raw_event in events:
        event = coerce_event_risk_input(raw_event)
        observed_date = _coerce_date(event.observed_at) or as_of
        if observed_date > as_of:
            continue
        if event.country and event.country != target_id:
            continue
        if event.risk_type and event.risk_type != risk_type:
            continue

        intensity = score_event_intensity(event)
        if intensity <= 0:
            continue
        age_days = max(0, (as_of - observed_date).days)
        recency_weight = max(0.25, 1.0 - age_days / 45.0)
        weighted_intensity = round(intensity * recency_weight, 2)
        refs = event_feature_evidence_refs(event)
        evidence_refs.extend(refs)
        intensities.append(weighted_intensity)
        confidence = max(
            0.0,
            min(
                1.0,
                0.25
                + 0.25 * min(1.0, len(event.evidence_article_ids) / 3.0)
                + 0.25 * (_score_0_100(event.source_diversity_score) / 100.0)
                + (0.15 if event.official_confirmation else 0.0)
                - 0.20 * (_score_0_100(event.rumor_risk_score) / 100.0),
            ),
        )
        confidences.append(round(confidence, 4))
        drivers.append(
            {
                "component": "event_shock",
                "event_id": event.event_id,
                "event_type": event.event_type,
                "mechanism": event.mechanism,
                "score": weighted_intensity,
                "observed_at": observed_date.isoformat(),
                "evidence_refs": list(refs),
            }
        )

    if not intensities:
        return EventShockFeatures(
            target_id=target_id,
            risk_type=risk_type,
            as_of_date=as_of,
            event_count=0,
            intensity_score=0.0,
            confidence_score=0.0,
            evidence_strength=0.0,
            top_drivers=(),
            evidence_refs=(),
        )

    sorted_drivers = tuple(sorted(drivers, key=lambda item: item["score"], reverse=True)[:5])
    intensity_score = round(min(100.0, max(intensities) * 0.55 + sum(intensities) * 0.18), 2)
    refs = dedupe_evidence_refs(evidence_refs)
    return EventShockFeatures(
        target_id=target_id,
        risk_type=risk_type,
        as_of_date=as_of,
        event_count=len(intensities),
        intensity_score=intensity_score,
        confidence_score=round(sum(confidences) / len(confidences), 4),
        evidence_strength=round(min(1.0, 0.25 + len(refs) / 8.0), 4),
        top_drivers=sorted_drivers,
        evidence_refs=refs,
    )


def news_event_bucket_probabilities(features: EventShockFeatures) -> HorizonBuckets:
    """Map aggregate event-shock intensity to canonical probability buckets."""
    within_18m = round(min(0.42, (features.intensity_score / 100.0) * 0.38), 6)
    p_0_6m = round(within_18m * 0.62, 6)
    p_6_12m = round(within_18m * 0.27, 6)
    p_12_18m = round(within_18m - p_0_6m - p_6_12m, 6)
    return HorizonBuckets(p_0_6m, p_6_12m, p_12_18m)


class RuleBasedNewsEventShockModel:
    """Deterministic event-shock model over structured event risk features."""

    model_version = EVENT_SHOCK_MODEL_VERSION

    def predict(
        self,
        *,
        target_id: str,
        risk_type: str,
        as_of_date: datetime.date | str,
        events: Sequence[Mapping[str, Any] | object],
    ) -> ComponentForecast:
        features = aggregate_event_shocks(
            target_id=target_id,
            risk_type=risk_type,
            as_of_date=as_of_date,
            events=events,
        )
        return ComponentForecast(
            component="event_shock",
            risk_type=risk_type,
            buckets=news_event_bucket_probabilities(features),
            confidence_score=features.confidence_score,
            evidence_strength=features.evidence_strength,
            model_version=self.model_version,
            top_drivers=features.top_drivers,
            evidence_refs=features.evidence_refs,
            notes=(f"{features.event_count} event(s) contributed",),
        )
