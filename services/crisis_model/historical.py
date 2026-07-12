"""Historical analogy prior for the standalone crisis model."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from db.models.enums import RiskType
from services.crisis_model.evidence import dedupe_evidence_refs, evidence_ref
from services.crisis_model.types import ComponentForecast, HorizonBuckets

HISTORICAL_MODEL_VERSION: Final[str] = "historical-analogy-prior.v1"

SIMILARITY_WEIGHTS: Final[dict[str, float]] = {
    "semantic": 0.25,
    "event_type": 0.20,
    "mechanism": 0.15,
    "macro_context": 0.15,
    "market_regime": 0.10,
    "geography": 0.10,
    "institution_type": 0.05,
}


@dataclass(frozen=True)
class CurrentSituation:
    """Current target state used for analogy retrieval."""

    target_id: str
    risk_type: str
    event_type: str | None = None
    mechanism: str | None = None
    macro_context: str | None = None
    market_regime: str | None = None
    geography: str | None = None
    institution_type: str | None = None


@dataclass(frozen=True)
class HistoricalCase:
    """Curated historical case used by the analogy prior."""

    id: str
    title: str
    risk_type: str
    event_type: str | None
    mechanism: str | None
    macro_context: str | None
    market_regime: str | None
    geography: str | None
    institution_type: str | None
    outcome_crisis: bool
    severity_score: float = 50.0
    semantic_similarity: float | None = None
    similarities: tuple[str, ...] = ()
    differences: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoricalAnalogy:
    """Ranked analogy with explainability metadata."""

    case: HistoricalCase
    similarity_score: float
    weighted_outcome: float
    why_relevant: str


def _field_match(left: str | None, right: str | None) -> float | None:
    if left is None or right is None:
        return None
    return 1.0 if left == right else 0.0


def similarity_score(current: CurrentSituation, case: HistoricalCase) -> float:
    """Compute deterministic hybrid similarity with semantic-weight renormalization."""
    components: dict[str, float | None] = {
        "semantic": case.semantic_similarity,
        "event_type": _field_match(current.event_type, case.event_type),
        "mechanism": _field_match(current.mechanism, case.mechanism),
        "macro_context": _field_match(current.macro_context, case.macro_context),
        "market_regime": _field_match(current.market_regime, case.market_regime),
        "geography": _field_match(current.geography, case.geography),
        "institution_type": _field_match(current.institution_type, case.institution_type),
    }
    available = {
        name: max(0.0, min(1.0, value))
        for name, value in components.items()
        if value is not None
    }
    if not available:
        return 0.0
    total_weight = sum(SIMILARITY_WEIGHTS[name] for name in available)
    score = sum(available[name] * SIMILARITY_WEIGHTS[name] for name in available) / total_weight
    if current.risk_type != case.risk_type:
        score *= 0.55
    return round(score, 4)


def retrieve_analogies(
    current: CurrentSituation,
    cases: Sequence[HistoricalCase],
    *,
    top_k: int = 5,
    min_similarity: float = 0.15,
) -> tuple[HistoricalAnalogy, ...]:
    """Rank usable analogies and omit cases without limitations."""
    analogies: list[HistoricalAnalogy] = []
    for case in cases:
        if not case.limitations:
            continue
        score = similarity_score(current, case)
        if score < min_similarity:
            continue
        severity = max(0.0, min(1.0, case.severity_score / 100.0))
        weighted_outcome = score * severity * (1.0 if case.outcome_crisis else 0.0)
        analogies.append(
            HistoricalAnalogy(
                case=case,
                similarity_score=score,
                weighted_outcome=round(weighted_outcome, 4),
                why_relevant="; ".join(case.similarities[:2]) or "Matched structured context",
            )
        )
    return tuple(
        sorted(analogies, key=lambda analogy: analogy.similarity_score, reverse=True)[:top_k]
    )


def historical_analogy_prior(
    analogies: Sequence[HistoricalAnalogy],
    *,
    risk_type: str,
    base_rate: float = 0.08,
    cap: float = 0.35,
) -> ComponentForecast:
    """Convert ranked analogies into a shrunk, capped component forecast."""
    if risk_type not in {item.value for item in RiskType}:
        msg = f"unsupported risk_type: {risk_type}"
        raise ValueError(msg)
    if not analogies:
        buckets = HorizonBuckets(base_rate * 0.15, base_rate * 0.35, base_rate * 0.50)
        return ComponentForecast(
            component="historical_analogy",
            risk_type=risk_type,
            buckets=buckets,
            confidence_score=0.0,
            evidence_strength=0.0,
            model_version=HISTORICAL_MODEL_VERSION,
            notes=("No usable historical analogies found",),
        )

    denominator = sum(analogy.similarity_score for analogy in analogies)
    crisis_frequency = sum(analogy.weighted_outcome for analogy in analogies) / denominator
    within_18m = round(min(cap, 0.55 * base_rate + 0.45 * crisis_frequency), 6)
    buckets = HorizonBuckets(
        round(within_18m * 0.18, 6),
        round(within_18m * 0.42, 6),
        round(within_18m * 0.40, 6),
    )
    evidence_refs = dedupe_evidence_refs(
        [evidence_ref("historical_case", analogy.case.id) for analogy in analogies]
    )
    drivers = tuple(
        {
            "component": "historical_analogy",
            "case_id": analogy.case.id,
            "title": analogy.case.title,
            "similarity_score": analogy.similarity_score,
            "outcome_crisis": analogy.case.outcome_crisis,
            "why_relevant": analogy.why_relevant,
            "limitations": list(analogy.case.limitations),
            "evidence_refs": [evidence_ref("historical_case", analogy.case.id)],
        }
        for analogy in analogies
    )
    return ComponentForecast(
        component="historical_analogy",
        risk_type=risk_type,
        buckets=buckets,
        confidence_score=round(min(1.0, 0.20 + len(analogies) * 0.12), 4),
        evidence_strength=round(min(1.0, 0.20 + len(evidence_refs) * 0.12), 4),
        model_version=HISTORICAL_MODEL_VERSION,
        top_drivers=drivers,
        evidence_refs=evidence_refs,
    )
