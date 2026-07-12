"""Phase 4 historical analogy prior tests."""

from __future__ import annotations

from dataclasses import replace

from db.models.enums import RiskType
from services.crisis_model.contract import validate_evidence_refs
from services.crisis_model.historical import (
    CurrentSituation,
    HistoricalCase,
    historical_analogy_prior,
    retrieve_analogies,
    similarity_score,
)


def _current() -> CurrentSituation:
    return CurrentSituation(
        target_id="US",
        risk_type=RiskType.BANKING.value,
        event_type="banking_stress",
        mechanism="deposit_outflow",
        macro_context="tight_policy",
        market_regime="credit_stress",
        geography="US",
        institution_type="regional_bank",
    )


def _case(case_id: str, *, risk_type: str = RiskType.BANKING.value) -> HistoricalCase:
    return HistoricalCase(
        id=case_id,
        title="Historical regional bank stress",
        risk_type=risk_type,
        event_type="banking_stress",
        mechanism="deposit_outflow",
        macro_context="tight_policy",
        market_regime="credit_stress",
        geography="US",
        institution_type="regional_bank",
        outcome_crisis=True,
        severity_score=85.0,
        similarities=("deposit pressure", "tight policy"),
        differences=("different regulatory context",),
        limitations=("small fixture case",),
    )


def test_similarity_score_is_bounded_and_penalizes_risk_type_mismatch() -> None:
    current = _current()
    matching = similarity_score(current, _case("match"))
    mismatched = similarity_score(
        current,
        _case("mismatch", risk_type=RiskType.CURRENCY.value),
    )

    assert 0.0 <= mismatched < matching <= 1.0


def test_retrieve_analogies_ranks_matches_and_omits_cases_without_limitations() -> None:
    current = _current()
    unrelated = HistoricalCase(
        id="unrelated",
        title="Unrelated case",
        risk_type=RiskType.SOVEREIGN.value,
        event_type="default",
        mechanism="rollover_stress",
        macro_context="fiscal_stress",
        market_regime="calm",
        geography="EU",
        institution_type="sovereign",
        outcome_crisis=False,
        limitations=("not similar",),
    )
    no_limitations = replace(_case("no-limitations"), limitations=())

    analogies = retrieve_analogies(current, [_case("best"), unrelated, no_limitations], top_k=2)

    assert [analogy.case.id for analogy in analogies] == ["best"]
    assert analogies[0].why_relevant


def test_historical_analogy_prior_is_bounded_shrunk_and_evidenced() -> None:
    analogies = retrieve_analogies(_current(), [_case("case-1"), _case("case-2")])

    forecast = historical_analogy_prior(
        analogies,
        risk_type=RiskType.BANKING.value,
        base_rate=0.08,
        cap=0.30,
    )

    assert forecast.component == "historical_analogy"
    assert 0.08 < forecast.probability_within_18m <= 0.30
    assert forecast.buckets.probability_0_6m < forecast.buckets.probability_6_12m
    assert forecast.confidence_score > 0
    assert forecast.top_drivers
    validate_evidence_refs(forecast.evidence_refs)


def test_historical_analogy_prior_handles_no_analogies() -> None:
    forecast = historical_analogy_prior((), risk_type=RiskType.BANKING.value, base_rate=0.06)

    assert forecast.probability_within_18m == 0.06
    assert forecast.confidence_score == 0.0
    assert forecast.evidence_refs == ()
