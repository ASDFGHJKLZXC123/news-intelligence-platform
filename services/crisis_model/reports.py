"""Evidence-backed crisis-risk report composition."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.crisis_model.contract import validate_evidence_refs
from services.crisis_model.critic import CriticResult


@dataclass(frozen=True)
class ReportClaim:
    """One report claim with mandatory evidence refs."""

    text: str
    evidence_refs: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class ReportSection:
    """A report section containing evidence-backed claims."""

    title: str
    claims: tuple[ReportClaim, ...]


@dataclass(frozen=True)
class RiskReport:
    """Structured user-facing report for a crisis prediction."""

    title: str
    risk_level: str
    risk_score: float
    confidence_score: float
    sections: tuple[ReportSection, ...]
    critic: CriticResult | None = None


class ReportComposer:
    """Compose deterministic reports from validated prediction payloads."""

    def compose(
        self,
        prediction: Mapping[str, Any],
        *,
        critic: CriticResult | None = None,
    ) -> RiskReport:
        evidence_refs = tuple(prediction.get("evidence_refs") or ())
        sections = (
            ReportSection(
                title="Forecast",
                claims=(
                    ReportClaim(
                        text=(
                            f"{prediction['risk_type']} risk is assessed as "
                            f"{prediction['risk_level']} with "
                            f"{prediction['probability_within_18m']:.1%} probability within 18 months."
                        ),
                        evidence_refs=evidence_refs,
                    ),
                ),
            ),
            ReportSection(
                title="Drivers",
                claims=tuple(_driver_claim(driver, evidence_refs) for driver in prediction.get("top_drivers", [])[:5]),
            ),
        )
        report = RiskReport(
            title=f"{prediction['target_id']} {prediction['risk_type']} risk report",
            risk_level=prediction["risk_level"],
            risk_score=float(prediction["risk_score"]),
            confidence_score=float(prediction["confidence_score"]),
            sections=sections,
            critic=critic,
        )
        validate_report_evidence(report)
        return report


def _driver_claim(driver: Mapping[str, Any], fallback_refs: Sequence[Mapping[str, str]]) -> ReportClaim:
    refs = tuple(driver.get("evidence_refs") or fallback_refs)
    label = driver.get("signal") or driver.get("name") or driver.get("component") or "driver"
    score = driver.get("score") or driver.get("contribution") or driver.get("similarity_score")
    text = f"Driver {label} contributed to the assessment"
    if score is not None:
        text += f" with score {score}."
    else:
        text += "."
    return ReportClaim(text=text, evidence_refs=refs)


def validate_report_evidence(report: RiskReport) -> None:
    """Ensure every report claim carries evidence and avoids certainty language."""
    for section in report.sections:
        for claim in section.claims:
            if not claim.evidence_refs:
                msg = f"claim in section {section.title!r} has no evidence refs"
                raise ValueError(msg)
            validate_evidence_refs(claim.evidence_refs)
            lowered = claim.text.lower()
            if any(phrase in lowered for phrase in ("will happen", "guaranteed", "certain to")):
                msg = "report claim uses forbidden certainty language"
                raise ValueError(msg)
