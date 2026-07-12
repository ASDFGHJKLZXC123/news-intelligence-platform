"""Rule-based critic for standalone crisis-model forecasts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.crisis_model.ensemble import component_disagreement
from services.crisis_model.types import ComponentForecast


@dataclass(frozen=True)
class CritiqueFinding:
    """One critic finding about a forecast or report."""

    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class CriticResult:
    """Collection of critic findings."""

    findings: tuple[CritiqueFinding, ...]

    @property
    def passed(self) -> bool:
        return not any(finding.severity in {"high", "critical"} for finding in self.findings)


class RuleBasedForecastCritic:
    """Deterministic critic for evidence, disagreement, and unsafe certainty."""

    def review(
        self,
        prediction: Mapping[str, Any],
        *,
        components: Sequence[ComponentForecast] = (),
    ) -> CriticResult:
        findings: list[CritiqueFinding] = []
        probability = float(prediction.get("probability_within_18m", 0.0))
        confidence = float(prediction.get("confidence_score", 0.0))
        evidence_refs = prediction.get("evidence_refs") or ()

        if probability >= 0.35 and (not evidence_refs or confidence < 0.35):
            findings.append(
                CritiqueFinding(
                    code="weak_evidence_high_probability",
                    severity="high",
                    message="High probability forecast requires stronger evidence and confidence.",
                )
            )
        if components and component_disagreement(components) > 0.18:
            findings.append(
                CritiqueFinding(
                    code="component_disagreement",
                    severity="medium",
                    message="Component forecasts disagree materially; show disagreement to users.",
                )
            )
        for analogy in prediction.get("historical_analogies") or ():
            if not analogy.get("limitations"):
                findings.append(
                    CritiqueFinding(
                        code="analogy_without_limitations",
                        severity="medium",
                        message="Historical analogy is missing limitations.",
                    )
                )
        for text in _prediction_text_fields(prediction):
            if _contains_certainty_language(text):
                findings.append(
                    CritiqueFinding(
                        code="certainty_language",
                        severity="high",
                        message="Forecast language must remain probabilistic.",
                    )
                )
        return CriticResult(tuple(findings))


def _prediction_text_fields(prediction: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("summary", "why_now", "narrative"):
        value = prediction.get(key)
        if isinstance(value, str):
            values.append(value)
    return tuple(values)


def _contains_certainty_language(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in ("will happen", "guaranteed", "certain to"))
