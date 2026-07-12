"""Phase 6 critic and report composer tests."""

from __future__ import annotations

from db.models.enums import RiskType
from services.crisis_model.critic import RuleBasedForecastCritic
from services.crisis_model.reports import (
    ReportClaim,
    ReportComposer,
    ReportSection,
    RiskReport,
    validate_report_evidence,
)


def _prediction():
    return {
        "target_id": "US",
        "risk_type": RiskType.BANKING.value,
        "probability_within_18m": 0.42,
        "risk_score": 64.0,
        "risk_level": "high",
        "confidence_score": 0.72,
        "evidence_refs": [{"kind": "signal", "id": "country-daily-risk:US:2026-06-17"}],
        "top_drivers": [
            {
                "signal": "credit_spread_zscore",
                "score": 82.0,
                "evidence_refs": [{"kind": "signal", "id": "credit_spread:US"}],
            }
        ],
        "historical_analogies": [
            {
                "case_id": "case-1",
                "limitations": ["Different monetary policy regime"],
            }
        ],
    }


def test_rule_based_critic_flags_high_probability_with_weak_evidence() -> None:
    prediction = {**_prediction(), "confidence_score": 0.20, "evidence_refs": []}

    result = RuleBasedForecastCritic().review(prediction)

    assert not result.passed
    assert result.findings[0].code == "weak_evidence_high_probability"


def test_rule_based_critic_flags_certainty_language() -> None:
    prediction = {**_prediction(), "summary": "A banking crisis will happen soon."}

    result = RuleBasedForecastCritic().review(prediction)

    assert any(finding.code == "certainty_language" for finding in result.findings)


def test_report_composer_creates_evidence_backed_report() -> None:
    critic = RuleBasedForecastCritic().review(_prediction())
    report = ReportComposer().compose(_prediction(), critic=critic)

    assert report.title == "US banking risk report"
    assert report.sections
    validate_report_evidence(report)


def test_report_evidence_validation_rejects_unsupported_claims_and_certainty() -> None:
    unsupported = RiskReport(
        title="Bad report",
        risk_level="high",
        risk_score=80.0,
        confidence_score=0.5,
        sections=(ReportSection("Bad", (ReportClaim("Unsupported claim.", ()),)),),
    )
    try:
        validate_report_evidence(unsupported)
    except ValueError as exc:
        assert "no evidence refs" in str(exc)
    else:
        raise AssertionError("expected unsupported report claim to fail")

    certain = RiskReport(
        title="Bad report",
        risk_level="high",
        risk_score=80.0,
        confidence_score=0.5,
        sections=(
            ReportSection(
                "Bad",
                (
                    ReportClaim(
                        "This crisis is guaranteed.",
                        ({"kind": "signal", "id": "x"},),
                    ),
                ),
            ),
        ),
    )
    try:
        validate_report_evidence(certain)
    except ValueError as exc:
        assert "certainty language" in str(exc)
    else:
        raise AssertionError("expected certainty language to fail")
