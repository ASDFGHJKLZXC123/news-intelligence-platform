"""Stage 2 LLM contract and registry validation.

Asserts `docs/specs/llm-contracts-reconciliation.md` (companion to ADR 0008), including
the §16.1 amendments: structured `when` + whitelisted evidence article IDs, CompanyImpact
`assertion_status`, HistoricalAnalogy `historical_episode_id` + `regime_caveats`, the
horizon on forecasts/warnings, RiskWarning `risk_score` with derived `severity`, the
Critique accept/revise/reject verdict, and claim-tagged ReportComposition blocks.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from db.models.enums import Horizon, RiskLevel, risk_level_for_score
from services.llm.contracts import (
    NIL_DECISION,
    AssertionStatus,
    BaseLLMContract,
    CompanyImpact,
    Critique,
    CritiqueVerdict,
    EntityLinkAdjudication,
    EventExtraction,
    ForecastScenarios,
    HistoricalAnalogy,
    ImpactDirection,
    IndustryImpact,
    LLMContractConfigurationError,
    LLMContractLookupError,
    LLMContractValidationError,
    ReportComposition,
    RiskWarning,
    WhenPrecision,
    list_contract_schemas,
    llm_contract_json_schema,
    resolve_llm_contract,
    validate_llm_contract_payload,
    validate_llm_payload,
)

ENVELOPE_MEMBERS = ("schema_name", "schema_version", "prompt_template_version")

# Every ID a model may reference must be injected into the prompt; the validator rejects
# anything outside this set (spec: ID whitelist rule).
ALLOWED_IDS = [
    "event-1",
    "article-1",
    "article-2",
    "industry-energy",
    "company-acme",
    "episode-1973-oil-shock",
    "claim-1",
    "claim-2",
]


def _payloads() -> dict[str, dict[str, Any]]:
    envelope = {"schema_version": "1.0", "prompt_template_version": "v1"}
    return {
        "EventExtraction": {
            **envelope,
            "schema_name": "EventExtraction",
            "events": [
                {
                    "event_id": "event-1",
                    "summary": "Central bank raises rates unexpectedly.",
                    "key_facts": ["50bp hike", "No forward guidance given"],
                    "why_it_matters": "Repricing of the front end of the curve.",
                    "when": {"date": "2026-07-01", "precision": "day"},
                    "evidence_article_ids": ["article-1", "article-2"],
                    "impact_direction": ImpactDirection.NEGATIVE,
                    "confidence": 0.81,
                }
            ],
        },
        "IndustryImpact": {
            **envelope,
            "schema_name": "IndustryImpact",
            "impacts": [
                {
                    "industry_id": "industry-energy",
                    "industry_name": "Energy",
                    "rationale": "Input costs are rising quickly.",
                    "impact_score": 62.0,
                    "impact_direction": ImpactDirection.NEGATIVE,
                    "confidence": 0.71,
                }
            ],
        },
        "CompanyImpact": {
            **envelope,
            "schema_name": "CompanyImpact",
            "impacts": [
                {
                    "company_id": "company-acme",
                    "company_name": "Acme Energy",
                    "rationale": "Leads cost inflation in the supply chain.",
                    "impact_score": 54.0,
                    "impact_direction": ImpactDirection.NEGATIVE,
                    "assertion_status": AssertionStatus.ASSERTED,
                    "confidence": 0.62,
                }
            ],
        },
        "HistoricalAnalogy": {
            **envelope,
            "schema_name": "HistoricalAnalogy",
            "analogies": [
                {
                    "historical_episode_id": "episode-1973-oil-shock",
                    "explanation": "Comparable supply shock and policy response.",
                    "regime_caveats": ["Floating FX regime today", "Deeper futures market"],
                    "similarity_score": 49.0,
                    "confidence": 0.68,
                }
            ],
        },
        "ForecastScenarios": {
            **envelope,
            "schema_name": "ForecastScenarios",
            "scenarios": [
                {
                    "scenario_name": "upside_case",
                    "narrative": "Demand accelerates and prices decline.",
                    "probability": 0.33,
                    "risk_score": 24.0,
                    "horizon": Horizon.H_0_6M,
                    "impact_direction": ImpactDirection.POSITIVE,
                    "confidence": 0.61,
                },
                {
                    "scenario_name": "base_case",
                    "narrative": "Demand remains in line with consensus.",
                    "probability": 0.34,
                    "risk_score": 52.0,
                    "horizon": Horizon.H_6_12M,
                    "impact_direction": ImpactDirection.MIXED,
                    "confidence": 0.58,
                },
                {
                    "scenario_name": "downside_case",
                    "narrative": "The shock triggers a severe drawdown.",
                    "probability": 0.33,
                    "risk_score": 81.0,
                    "horizon": Horizon.H_12_18M,
                    "impact_direction": ImpactDirection.NEGATIVE,
                    "confidence": 0.56,
                },
            ],
        },
        "Critique": {
            **envelope,
            "schema_name": "Critique",
            "verdict": CritiqueVerdict.REVISE,
            "challenged_claims": [
                {
                    "claim_id": "claim-1",
                    "issue": "Scenario language does not state a time horizon.",
                    "confidence": 0.55,
                }
            ],
            "suggested_probability_changes": [
                {
                    "scenario_label": "Bear",
                    "current_probability": 0.33,
                    "suggested_probability": 0.45,
                    "rationale": "Funding stress is worse than the base case assumes.",
                }
            ],
            "revision_round": 0,
        },
        "RiskWarning": {
            **envelope,
            "schema_name": "RiskWarning",
            "warnings": [
                {
                    "title": "Liquidity compression",
                    "detail": "Liquidity compression in near-term funding markets.",
                    "risk_score": 82.0,
                    "probability": 0.72,
                    "horizon": Horizon.WITHIN_18M,
                    "confidence": 0.83,
                }
            ],
        },
        "ReportComposition": {
            **envelope,
            "schema_name": "ReportComposition",
            "blocks": [
                {
                    "text": "Energy exposures increased due to logistics congestion.",
                    "claim_ids": ["claim-1", "claim-2"],
                }
            ],
        },
        "EntityLinkAdjudication": {
            **envelope,
            "schema_name": "EntityLinkAdjudication",
            "decision": "company-acme",
        },
    }


def _payload(schema_name: str) -> dict[str, Any]:
    return _payloads()[schema_name]


# --- Enums are exactly what the spec names -----------------------------------------


def test_enums_match_the_spec_exactly() -> None:
    assert [d.value for d in ImpactDirection] == ["positive", "negative", "mixed", "unclear"]
    assert [p.value for p in WhenPrecision] == ["day", "week", "month", "quarter"]
    assert [s.value for s in AssertionStatus] == ["asserted", "denied", "speculative"]
    assert [v.value for v in CritiqueVerdict] == ["accept", "revise", "reject"]


# --- Registry, envelope, extra-forbid ------------------------------------------------


def test_registry_and_contract_names_are_discoverable() -> None:
    assert set(list_contract_schemas()) == {
        "EventExtraction",
        "IndustryImpact",
        "CompanyImpact",
        "HistoricalAnalogy",
        "ForecastScenarios",
        "Critique",
        "RiskWarning",
        "ReportComposition",
        "EntityLinkAdjudication",
    }


@pytest.mark.parametrize("schema_name", list_contract_schemas())
def test_each_contract_loads_valid_contract(schema_name: str) -> None:
    contract = validate_llm_payload(_payload(schema_name), allowed_ids=ALLOWED_IDS)

    assert isinstance(contract, BaseLLMContract)
    assert contract.schema_name == schema_name
    assert contract.schema_version == "1.0"
    assert contract.prompt_template_version == "v1"


@pytest.mark.parametrize("schema_name", list_contract_schemas())
@pytest.mark.parametrize("member", ENVELOPE_MEMBERS)
def test_dispatch_rejects_a_payload_missing_an_envelope_member(
    schema_name: str, member: str
) -> None:
    """The envelope is required on the wire: nothing is defaulted in for the model."""

    payload = _payload(schema_name)
    del payload[member]

    if member == "schema_name":
        # Dispatch cannot even pick a contract without it.
        with pytest.raises(LLMContractValidationError, match="payload must include schema_name"):
            validate_llm_payload(payload, allowed_ids=ALLOWED_IDS)
    else:
        with pytest.raises(ValidationError, match=f"{member}\n  Field required"):
            validate_llm_payload(payload, allowed_ids=ALLOWED_IDS)


@pytest.mark.parametrize("schema_name", list_contract_schemas())
@pytest.mark.parametrize("member", ENVELOPE_MEMBERS)
def test_direct_contract_validation_rejects_a_missing_envelope_member(
    schema_name: str, member: str
) -> None:
    """No subclass supplies its own schema_name, so per-contract validation demands it too."""

    payload = _payload(schema_name)
    del payload[member]

    with pytest.raises(ValidationError, match=f"{member}\n  Field required"):
        resolve_llm_contract(schema_name).model_validate(payload)


@pytest.mark.parametrize("schema_name", list_contract_schemas())
def test_provider_json_schema_requires_the_whole_envelope(schema_name: str) -> None:
    """Provider-native structured output must ask for all three envelope members."""

    schema = llm_contract_json_schema(schema_name)

    assert set(ENVELOPE_MEMBERS) <= set(schema["required"])
    assert schema["properties"]["schema_name"]["const"] == schema_name
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert schema["properties"]["prompt_template_version"]["minLength"] == 1


@pytest.mark.parametrize("bad_version", ["1", "1.1", "2.0", "v1"])
def test_schema_version_is_the_literal_string_1_0(bad_version: str) -> None:
    payload = _payload("EventExtraction")
    payload["schema_version"] = bad_version

    with pytest.raises(ValidationError, match="schema_version"):
        EventExtraction.model_validate(payload)


def test_prompt_template_version_must_be_a_non_empty_string() -> None:
    payload = _payload("EventExtraction")
    payload["prompt_template_version"] = ""

    with pytest.raises(ValidationError, match="at least 1 character"):
        EventExtraction.model_validate(payload)

    # Any non-empty template version is accepted: it tracks prompt revisions, not schema.
    payload["prompt_template_version"] = "v7"
    assert EventExtraction.model_validate(payload).prompt_template_version == "v7"


@pytest.mark.parametrize("schema_name", list_contract_schemas())
def test_every_contract_forbids_extra_fields(schema_name: str) -> None:
    payload = _payload(schema_name)
    payload["hallucinated_field"] = "nope"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validate_llm_contract_payload(
            schema_name=schema_name, payload=payload, allowed_ids=ALLOWED_IDS
        )


def test_findings_forbid_extra_fields_too() -> None:
    payload = _payload("IndustryImpact")
    payload["impacts"][0]["signed_score"] = -40.0

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IndustryImpact.model_validate(payload)


def test_lookup_error_for_unknown_schema_name() -> None:
    with pytest.raises(LLMContractLookupError, match="Unknown schema_name"):
        validate_llm_contract_payload(
            schema_name="MissingSchema", payload={"schema_name": "MissingSchema"}
        )


# --- ID whitelist: models never mint IDs ---------------------------------------------


@pytest.mark.parametrize(
    ("schema_name", "field_name", "mutate"),
    [
        (
            "EventExtraction",
            "event_id",
            lambda p: p["events"][0].__setitem__("event_id", "event-invented"),
        ),
        (
            "EventExtraction",
            "evidence_article_ids",
            lambda p: p["events"][0].__setitem__(
                "evidence_article_ids", ["article-1", "article-invented"]
            ),
        ),
        (
            "IndustryImpact",
            "industry_id",
            lambda p: p["impacts"][0].__setitem__("industry_id", "industry-invented"),
        ),
        (
            "CompanyImpact",
            "company_id",
            lambda p: p["impacts"][0].__setitem__("company_id", "company-invented"),
        ),
        (
            "HistoricalAnalogy",
            "historical_episode_id",
            lambda p: p["analogies"][0].__setitem__(
                "historical_episode_id", "episode-invented"
            ),
        ),
        (
            "Critique",
            "claim_id",
            lambda p: p["challenged_claims"][0].__setitem__("claim_id", "claim-invented"),
        ),
        (
            "ReportComposition",
            "claim_ids",
            lambda p: p["blocks"][0].__setitem__("claim_ids", ["claim-1", "claim-invented"]),
        ),
    ],
)
def test_whitelist_rejects_ids_the_model_minted(
    schema_name: str, field_name: str, mutate: Any
) -> None:
    payload = _payload(schema_name)
    mutate(payload)

    with pytest.raises(ValidationError, match=f"{field_name} must be whitelisted"):
        validate_llm_contract_payload(
            schema_name=schema_name, payload=payload, allowed_ids=ALLOWED_IDS
        )


def test_whitelisted_ids_pass() -> None:
    contract = validate_llm_contract_payload(
        schema_name="EventExtraction",
        payload=_payload("EventExtraction"),
        allowed_ids=ALLOWED_IDS,
    )

    assert contract.events[0].event_id == "event-1"
    assert contract.events[0].evidence_article_ids == ["article-1", "article-2"]


def _abstaining_payload() -> dict[str, Any]:
    """An EventExtraction payload that references no IDs at all."""

    return {
        "schema_name": "EventExtraction",
        "schema_version": "1.0",
        "prompt_template_version": "v1",
        "events": [],
        "no_finding_reason": "Nothing cleared the quality bar.",
    }


@pytest.mark.parametrize(
    "payload_factory", [lambda: _payload("EventExtraction"), _abstaining_payload]
)
def test_a_missing_whitelist_is_a_configuration_error_not_a_free_pass(
    payload_factory: Any,
) -> None:
    """Fail closed: no whitelist means misconfigured orchestration, even with zero IDs."""

    payload = payload_factory()

    with pytest.raises(LLMContractConfigurationError, match="allowed_ids whitelist is required"):
        validate_llm_contract_payload(schema_name="EventExtraction", payload=payload)

    with pytest.raises(LLMContractConfigurationError, match="allowed_ids whitelist is required"):
        validate_llm_contract_payload(
            schema_name="EventExtraction", payload=payload, allowed_ids=None
        )

    with pytest.raises(LLMContractConfigurationError, match="allowed_ids whitelist is required"):
        validate_llm_payload(payload)

    with pytest.raises(LLMContractConfigurationError, match="allowed_ids whitelist is required"):
        validate_llm_payload(payload, allowed_ids=None)


def test_a_configuration_error_is_an_llm_contract_validation_error() -> None:
    assert issubclass(LLMContractConfigurationError, LLMContractValidationError)


def test_an_explicitly_empty_whitelist_permits_an_id_free_abstention() -> None:
    """`[]` is configuration, not absence: a payload citing no IDs validates against it."""

    contract = validate_llm_contract_payload(
        schema_name="EventExtraction", payload=_abstaining_payload(), allowed_ids=[]
    )

    assert contract.events == []
    assert contract.no_finding_reason == "Nothing cleared the quality bar."


def test_an_explicitly_empty_whitelist_rejects_every_emitted_id() -> None:
    with pytest.raises(ValidationError, match="event_id must be whitelisted"):
        validate_llm_contract_payload(
            schema_name="EventExtraction", payload=_payload("EventExtraction"), allowed_ids=[]
        )


# --- Abstain path --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema_name", "array_field"),
    [
        ("EventExtraction", "events"),
        ("IndustryImpact", "impacts"),
        ("CompanyImpact", "impacts"),
        ("HistoricalAnalogy", "analogies"),
        ("ForecastScenarios", "scenarios"),
        ("Critique", "challenged_claims"),
        ("RiskWarning", "warnings"),
        ("ReportComposition", "blocks"),
    ],
)
def test_empty_array_requires_no_finding_reason(schema_name: str, array_field: str) -> None:
    payload = _payload(schema_name)
    payload[array_field] = []
    if schema_name == "Critique":
        payload["verdict"] = CritiqueVerdict.ACCEPT
        payload["suggested_probability_changes"] = []

    with pytest.raises(ValidationError, match="no_finding_reason"):
        validate_llm_contract_payload(
            schema_name=schema_name, payload=payload, allowed_ids=ALLOWED_IDS
        )

    payload["no_finding_reason"] = "Nothing survived the quality bar."
    contract = validate_llm_contract_payload(
        schema_name=schema_name, payload=payload, allowed_ids=ALLOWED_IDS
    )
    assert contract.no_finding_reason == "Nothing survived the quality bar."


def test_no_finding_reason_cannot_be_combined_with_findings() -> None:
    payload = _payload("EventExtraction")
    payload["no_finding_reason"] = "Abstaining."

    with pytest.raises(ValidationError, match="cannot be combined with no_finding_reason"):
        EventExtraction.model_validate(payload)


# --- Numeric bounds ------------------------------------------------------------------


@pytest.mark.parametrize("bad_score", [-0.1, 100.1])
def test_score_fields_are_bounded_0_to_100(bad_score: float) -> None:
    payload = _payload("IndustryImpact")
    payload["impacts"][0]["impact_score"] = bad_score

    with pytest.raises(ValidationError):
        IndustryImpact.model_validate(payload)


@pytest.mark.parametrize("bad_unit", [-0.01, 1.01])
def test_confidence_and_probability_are_bounded_0_to_1(bad_unit: float) -> None:
    company = _payload("CompanyImpact")
    company["impacts"][0]["confidence"] = bad_unit
    with pytest.raises(ValidationError):
        CompanyImpact.model_validate(company)

    risk = _payload("RiskWarning")
    risk["warnings"][0]["probability"] = bad_unit
    with pytest.raises(ValidationError):
        RiskWarning.model_validate(risk)


# --- Every *_score field: strict number, 0-100, normalized to two decimals ------------

# (schema_name, findings key, $defs name, score field) for every contract field ending
# in `_score`. One reusable type backs them all, so each rule is asserted on each field.
SCORE_FIELDS = [
    ("IndustryImpact", "impacts", "IndustryImpactFinding", "impact_score"),
    ("CompanyImpact", "impacts", "CompanyImpactFinding", "impact_score"),
    ("HistoricalAnalogy", "analogies", "HistoricalAnalogyFinding", "similarity_score"),
    ("ForecastScenarios", "scenarios", "ForecastScenario", "risk_score"),
    ("RiskWarning", "warnings", "RiskWarningFinding", "risk_score"),
]


def _score_payload(schema_name: str, findings_key: str, field: str, value: Any) -> dict[str, Any]:
    payload = _payload(schema_name)
    payload[findings_key][0][field] = value
    return payload


def _validate_score(
    schema_name: str, findings_key: str, field: str, value: Any
) -> Any:
    payload = _score_payload(schema_name, findings_key, field, value)
    contract = validate_llm_contract_payload(
        schema_name=schema_name, payload=payload, allowed_ids=ALLOWED_IDS
    )
    return getattr(getattr(contract, findings_key)[0], field)


@pytest.mark.parametrize(("schema_name", "findings_key", "_def", "field"), SCORE_FIELDS)
@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (62.005, 62.01),  # ROUND_HALF_UP, not banker's rounding (which would give 62.00)
        (62.015, 62.02),
        (62.004, 62.0),
        (0.125, 0.13),
        (99.999, 100.0),  # in bounds before rounding, so rounding up to the ceiling is fine
        (0.001, 0.0),
        (49.0, 49.0),  # already two decimals: unchanged
        (62, 62.0),  # a JSON integer is a JSON number
        (100, 100.0),
        (0, 0.0),
    ],
)
def test_score_fields_are_normalized_to_two_decimals_half_up(
    schema_name: str,
    findings_key: str,
    _def: str,
    field: str,
    raw: float,
    normalized: float,
) -> None:
    """Extra fractional digits are normalized, never a validation failure."""

    assert _validate_score(schema_name, findings_key, field, raw) == normalized


@pytest.mark.parametrize(("schema_name", "findings_key", "_def", "field"), SCORE_FIELDS)
@pytest.mark.parametrize(
    "bad_value",
    [
        "62.0",  # a numeric string is not a number: never coerced
        "not-a-number",
        True,  # bool is an int subclass in Python; the contract still rejects it
        False,
        None,
        [62.0],
        {"value": 62.0},
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_score_fields_reject_non_numeric_and_non_finite_input(
    schema_name: str, findings_key: str, _def: str, field: str, bad_value: Any
) -> None:
    with pytest.raises(ValidationError):
        validate_llm_contract_payload(
            schema_name=schema_name,
            payload=_score_payload(schema_name, findings_key, field, bad_value),
            allowed_ids=ALLOWED_IDS,
        )


@pytest.mark.parametrize(("schema_name", "findings_key", "_def", "field"), SCORE_FIELDS)
@pytest.mark.parametrize("out_of_range", [100.004, 100.001, -0.004, -0.1, 100.1, 101.0])
def test_score_bounds_are_applied_before_rounding(
    schema_name: str, findings_key: str, _def: str, field: str, out_of_range: float
) -> None:
    """100.004 must not round its way into range: bounds bite on the original value."""

    with pytest.raises(ValidationError, match="between 0 and 100"):
        validate_llm_contract_payload(
            schema_name=schema_name,
            payload=_score_payload(schema_name, findings_key, field, out_of_range),
            allowed_ids=ALLOWED_IDS,
        )


@pytest.mark.parametrize(("schema_name", "findings_key", "def_name", "field"), SCORE_FIELDS)
def test_score_json_schema_stays_a_bounded_number_without_multiple_of(
    schema_name: str, findings_key: str, def_name: str, field: str
) -> None:
    """Providers enforce type and bounds; two-decimal normalization is ours, not theirs."""

    schema = llm_contract_json_schema(schema_name)["$defs"][def_name]["properties"][field]

    assert schema["type"] == "number"
    assert schema["minimum"] == 0
    assert schema["maximum"] == 100
    assert "multipleOf" not in schema


# --- EventExtraction (§16.1 amendment) -----------------------------------------------


def test_event_extraction_carries_amended_fields() -> None:
    contract = validate_llm_contract_payload(
        schema_name="EventExtraction",
        payload=_payload("EventExtraction"),
        allowed_ids=ALLOWED_IDS,
    )
    event = contract.events[0]

    assert event.key_facts == ["50bp hike", "No forward guidance given"]
    assert event.why_it_matters
    assert event.when.date.isoformat() == "2026-07-01"
    assert event.when.precision is WhenPrecision.DAY
    assert event.impact_direction is ImpactDirection.NEGATIVE


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("key_facts", []), ("evidence_article_ids", [])],
)
def test_event_findings_require_facts_and_evidence(field_name: str, value: list[str]) -> None:
    payload = _payload("EventExtraction")
    payload["events"][0][field_name] = value

    with pytest.raises(ValidationError, match="at least 1 item"):
        EventExtraction.model_validate(payload)


def test_event_when_requires_a_valid_precision() -> None:
    payload = _payload("EventExtraction")
    payload["events"][0]["when"]["precision"] = "year"

    with pytest.raises(ValidationError):
        EventExtraction.model_validate(payload)


# --- CompanyImpact (§16.5 amendment) -------------------------------------------------


def test_company_impact_requires_assertion_status() -> None:
    payload = _payload("CompanyImpact")
    del payload["impacts"][0]["assertion_status"]

    with pytest.raises(ValidationError, match="assertion_status"):
        CompanyImpact.model_validate(payload)


def test_company_impact_rejects_unknown_assertion_status() -> None:
    payload = _payload("CompanyImpact")
    payload["impacts"][0]["assertion_status"] = "rumoured"

    with pytest.raises(ValidationError):
        CompanyImpact.model_validate(payload)


def test_company_impact_confidence_is_a_float_not_an_int_score() -> None:
    contract = validate_llm_contract_payload(
        schema_name="CompanyImpact", payload=_payload("CompanyImpact"), allowed_ids=ALLOWED_IDS
    )
    impact = contract.impacts[0]

    assert isinstance(impact.confidence, float)
    assert impact.confidence == 0.62
    assert impact.assertion_status is AssertionStatus.ASSERTED


# --- HistoricalAnalogy (§16.4 amendment) ---------------------------------------------


def test_historical_analogy_carries_episode_id_and_regime_caveats() -> None:
    contract = validate_llm_contract_payload(
        schema_name="HistoricalAnalogy",
        payload=_payload("HistoricalAnalogy"),
        allowed_ids=ALLOWED_IDS,
    )
    analogy = contract.analogies[0]

    assert analogy.historical_episode_id == "episode-1973-oil-shock"
    assert analogy.regime_caveats == ["Floating FX regime today", "Deeper futures market"]


def test_historical_analogy_regime_caveats_default_to_empty() -> None:
    payload = _payload("HistoricalAnalogy")
    del payload["analogies"][0]["regime_caveats"]

    contract = HistoricalAnalogy.model_validate(payload)
    assert contract.analogies[0].regime_caveats == []


# --- ForecastScenarios: MECE + horizon -----------------------------------------------


def test_forecast_scenarios_require_mece_probability_sum() -> None:
    payload = _payload("ForecastScenarios")
    for scenario in payload["scenarios"]:
        scenario["probability"] = 0.25

    with pytest.raises(ValidationError, match="sum to 1.0"):
        ForecastScenarios.model_validate(payload)

    payload["scenarios"][2]["probability"] = 0.50
    contract = ForecastScenarios.model_validate(payload)
    assert abs(sum(s.probability for s in contract.scenarios) - 1.0) <= 0.01


@pytest.mark.parametrize("probabilities", [(0.33, 0.34, 0.33), (0.325, 0.34, 0.34)])
def test_forecast_scenarios_accept_a_probability_sum_inside_the_tolerance(
    probabilities: tuple[float, float, float],
) -> None:
    payload = _payload("ForecastScenarios")
    for scenario, probability in zip(payload["scenarios"], probabilities, strict=True):
        scenario["probability"] = probability

    contract = ForecastScenarios.model_validate(payload)

    assert abs(sum(s.probability for s in contract.scenarios) - 1.0) <= 0.01


@pytest.mark.parametrize("probabilities", [(0.30, 0.34, 0.34), (0.35, 0.34, 0.34)])
def test_forecast_scenarios_reject_a_probability_sum_outside_the_tolerance(
    probabilities: tuple[float, float, float],
) -> None:
    payload = _payload("ForecastScenarios")
    for scenario, probability in zip(payload["scenarios"], probabilities, strict=True):
        scenario["probability"] = probability

    with pytest.raises(ValidationError, match="sum to 1.0"):
        ForecastScenarios.model_validate(payload)


@pytest.mark.parametrize(
    ("probabilities", "expected_total"),
    [
        ((0.33, 0.33, 0.33), Decimal("0.99")),
        ((0.33, 0.34, 0.34), Decimal("1.01")),
    ],
)
def test_forecast_scenarios_accept_the_inclusive_tolerance_boundaries(
    probabilities: tuple[float, float, float],
    expected_total: Decimal,
) -> None:
    """0.99 and 1.01 sit exactly on an inclusive bound, so both must validate.

    Summed as floats, each of these totals lands 0.010000000000000009 from 1.0 and would
    be rejected by a naive `abs(total - 1.0) > 0.01` check.
    """

    payload = _payload("ForecastScenarios")
    for scenario, probability in zip(payload["scenarios"], probabilities, strict=True):
        scenario["probability"] = probability

    contract = ForecastScenarios.model_validate(payload)

    total = sum((Decimal(str(s.probability)) for s in contract.scenarios), Decimal(0))
    assert total == expected_total
    assert abs(total - Decimal("1.0")) == Decimal("0.01")


@pytest.mark.parametrize(
    ("probabilities", "expected_total"),
    [
        ((0.33, 0.33, 0.329), Decimal("0.989")),
        ((0.33, 0.34, 0.341), Decimal("1.011")),
    ],
)
def test_forecast_scenarios_reject_totals_just_outside_the_tolerance(
    probabilities: tuple[float, float, float],
    expected_total: Decimal,
) -> None:
    """0.989 and 1.011 fall outside the bound by 0.001, so widening must not admit them."""

    assert sum((Decimal(str(p)) for p in probabilities), Decimal(0)) == expected_total

    payload = _payload("ForecastScenarios")
    for scenario, probability in zip(payload["scenarios"], probabilities, strict=True):
        scenario["probability"] = probability

    with pytest.raises(ValidationError, match="sum to 1.0"):
        ForecastScenarios.model_validate(payload)


def test_forecast_scenarios_keep_probabilities_as_provider_compatible_floats() -> None:
    """The decimal comparison is internal to validation; the field stays a JSON float."""

    payload = _payload("ForecastScenarios")
    for scenario in payload["scenarios"]:
        scenario["probability"] = 0.33

    contract = ForecastScenarios.model_validate(payload)

    assert [type(s.probability) for s in contract.scenarios] == [float, float, float]
    assert contract.to_provider_payload()["scenarios"][0]["probability"] == 0.33


def test_forecast_scenarios_reject_duplicate_names_even_when_probabilities_sum_to_one() -> None:
    """Mutual exclusivity: a repeated label is not a distinct branch of the world."""

    payload = _payload("ForecastScenarios")
    payload["scenarios"][2]["scenario_name"] = "base_case"

    with pytest.raises(ValidationError, match="mutually exclusive"):
        ForecastScenarios.model_validate(payload)


@pytest.mark.parametrize(
    "scenario_names",
    [
        ["base_case", "downside_case"],
        ["base_case", "upside_case", "tail_risk_case"],
        ["upside_case", "tail_risk_case"],
    ],
)
def test_forecast_scenarios_allow_a_distinct_partial_label_set(
    scenario_names: list[str],
) -> None:
    """The spec requires MECE, not that every allowed label appears in every set."""

    payload = _payload("ForecastScenarios")
    template = payload["scenarios"][0]
    payload["scenarios"] = [
        {**template, "scenario_name": name, "probability": 1.0 / len(scenario_names)}
        for name in scenario_names
    ]

    contract = ForecastScenarios.model_validate(payload)

    assert [s.scenario_name for s in contract.scenarios] == scenario_names


@pytest.mark.parametrize("scenario_name", ["Base_Case", "BASE_CASE", " base_case", "base_case "])
def test_forecast_scenario_rejects_case_and_whitespace_variants_of_a_label(
    scenario_name: str,
) -> None:
    """The Literal rejects variants outright, so set validation never has to normalize."""

    payload = _payload("ForecastScenarios")
    payload["scenarios"][0]["scenario_name"] = scenario_name

    with pytest.raises(ValidationError):
        ForecastScenarios.model_validate(payload)


def test_forecast_scenarios_abstain_without_tripping_the_mece_checks() -> None:
    payload = _payload("ForecastScenarios")
    payload["scenarios"] = []
    payload["no_finding_reason"] = "No forecastable branch survived the quality bar."

    contract = ForecastScenarios.model_validate(payload)

    assert contract.scenarios == []
    assert contract.no_finding_reason == "No forecastable branch survived the quality bar."


def test_forecast_scenarios_carry_the_horizon_the_probability_applies_to() -> None:
    contract = ForecastScenarios.model_validate(_payload("ForecastScenarios"))

    assert [s.horizon for s in contract.scenarios] == [
        Horizon.H_0_6M,
        Horizon.H_6_12M,
        Horizon.H_12_18M,
    ]


def test_forecast_scenario_rejects_an_unknown_horizon() -> None:
    payload = _payload("ForecastScenarios")
    payload["scenarios"][0]["horizon"] = "next_tuesday"

    with pytest.raises(ValidationError):
        ForecastScenarios.model_validate(payload)


@pytest.mark.parametrize(
    ("risk_score", "expected"),
    [(0.0, RiskLevel.LOW), (55.0, RiskLevel.MEDIUM), (75.01, RiskLevel.CRITICAL)],
)
def test_forecast_scenario_severity_is_derived_for_orm_persistence(
    risk_score: float, expected: RiskLevel
) -> None:
    payload = _payload("ForecastScenarios")
    payload["scenarios"][0]["risk_score"] = risk_score

    scenario = ForecastScenarios.model_validate(payload).scenarios[0]

    assert scenario.severity is expected
    assert scenario.scenario_name == "upside_case"
    assert scenario.narrative == "Demand accelerates and prices decline."
    assert scenario.to_orm_values() == {
        "scenario_name": "upside_case",
        "probability": 0.33,
        "risk_score": risk_score,
        "severity": expected.value,
        "horizon": "0_6m",
        "narrative": "Demand accelerates and prices decline.",
        "expected_impact": {"direction": "positive"},
        "confidence": 0.61,
    }


def test_forecast_scenario_rejects_model_emitted_severity() -> None:
    payload = _payload("ForecastScenarios")
    payload["scenarios"][0]["severity"] = "low"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ForecastScenarios.model_validate(payload)


def test_forecast_scenario_json_schema_does_not_request_severity() -> None:
    finding_schema = llm_contract_json_schema("ForecastScenarios")["$defs"][
        "ForecastScenario"
    ]

    assert "risk_score" in finding_schema["required"]
    assert "severity" not in finding_schema["properties"]


@pytest.mark.parametrize(
    ("schema_name", "findings_key"),
    [("ForecastScenarios", "scenarios"), ("RiskWarning", "warnings")],
)
def test_the_provider_payload_drops_derived_severity_so_it_can_be_revalidated(
    schema_name: str, findings_key: str
) -> None:
    """The replay representation must survive a round trip that `extra="forbid"` polices.

    A contract with a computed field has two serializations. The normalized one is for the
    audit row and everything downstream, so it carries `severity`. The provider one stands
    in for the model's own response — anything that reconstitutes a contract from a stored
    payload (the prompt cache) has to use it, because feeding `severity` back in is exactly
    what the contract forbids.
    """

    payload = _payload(schema_name)
    payload[findings_key][0]["risk_score"] = 75.005
    contract = validate_llm_contract_payload(
        schema_name=schema_name, payload=payload, allowed_ids=ALLOWED_IDS
    )

    normalized = contract.to_normalized_payload()[findings_key][0]
    replayable = contract.to_provider_payload()[findings_key][0]

    assert normalized["severity"] == RiskLevel.CRITICAL.value
    assert normalized["risk_score"] == 75.01
    assert "severity" not in replayable
    assert replayable["risk_score"] == 75.01  # normalization is kept, only the band is dropped

    # The whole point: it validates again, and the band is re-derived, not re-read.
    replayed = validate_llm_contract_payload(
        schema_name=schema_name,
        payload=contract.to_provider_payload(),
        allowed_ids=ALLOWED_IDS,
    )

    assert getattr(replayed, findings_key)[0].severity is RiskLevel.CRITICAL
    assert replayed.to_normalized_payload() == contract.to_normalized_payload()


# --- RiskWarning: risk_score + deterministic derived severity ------------------------


@pytest.mark.parametrize(
    ("risk_score", "expected"),
    [
        (0.0, RiskLevel.LOW),
        (30.0, RiskLevel.LOW),
        (30.01, RiskLevel.MEDIUM),
        (55.0, RiskLevel.MEDIUM),
        (55.01, RiskLevel.HIGH),
        (75.0, RiskLevel.HIGH),
        (75.01, RiskLevel.CRITICAL),
        (100.0, RiskLevel.CRITICAL),
    ],
)
def test_risk_warning_severity_is_derived_from_risk_score(
    risk_score: float, expected: RiskLevel
) -> None:
    payload = _payload("RiskWarning")
    payload["warnings"][0]["risk_score"] = risk_score

    warning = RiskWarning.model_validate(payload).warnings[0]

    assert warning.severity is expected
    assert warning.severity == risk_level_for_score(risk_score)
    assert warning.horizon is Horizon.WITHIN_18M


# Rounding decides the band at every boundary: .004 rounds down and stays in the lower
# band, .005 rounds half-up across it. Severity must follow the normalized score.
SEVERITY_AFTER_ROUNDING = [
    (30.004, 30.0, RiskLevel.LOW),
    (30.005, 30.01, RiskLevel.MEDIUM),
    (55.004, 55.0, RiskLevel.MEDIUM),
    (55.005, 55.01, RiskLevel.HIGH),
    (75.004, 75.0, RiskLevel.HIGH),
    (75.005, 75.01, RiskLevel.CRITICAL),
]


@pytest.mark.parametrize(("raw", "normalized", "expected"), SEVERITY_AFTER_ROUNDING)
def test_risk_warning_severity_is_derived_from_the_normalized_risk_score(
    raw: float, normalized: float, expected: RiskLevel
) -> None:
    payload = _payload("RiskWarning")
    payload["warnings"][0]["risk_score"] = raw

    warning = RiskWarning.model_validate(payload).warnings[0]

    assert warning.risk_score == normalized
    assert warning.severity is expected
    assert warning.severity == risk_level_for_score(normalized)


@pytest.mark.parametrize(("raw", "normalized", "expected"), SEVERITY_AFTER_ROUNDING)
def test_forecast_scenario_persists_the_normalized_score_and_its_derived_severity(
    raw: float, normalized: float, expected: RiskLevel
) -> None:
    payload = _payload("ForecastScenarios")
    payload["scenarios"][0]["risk_score"] = raw

    scenario = ForecastScenarios.model_validate(payload).scenarios[0]
    orm_values = scenario.to_orm_values()

    assert scenario.severity is expected
    assert orm_values["risk_score"] == normalized
    assert orm_values["severity"] == expected.value


def test_risk_warning_severity_cannot_be_model_emitted() -> None:
    payload = _payload("RiskWarning")
    payload["warnings"][0]["severity"] = RiskLevel.LOW

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RiskWarning.model_validate(payload)


def test_risk_warning_json_schema_never_asks_a_model_for_severity() -> None:
    finding_schema = llm_contract_json_schema("RiskWarning")["$defs"]["RiskWarningFinding"]

    assert "severity" not in finding_schema["properties"]
    assert "risk_score" in finding_schema["properties"]
    assert finding_schema["additionalProperties"] is False


# --- Critique: accept/revise/reject + termination rule --------------------------------


@pytest.mark.parametrize(
    "verdict", [CritiqueVerdict.ACCEPT, CritiqueVerdict.REVISE, CritiqueVerdict.REJECT]
)
def test_critique_accepts_each_verdict(verdict: CritiqueVerdict) -> None:
    payload = _payload("Critique")
    payload["verdict"] = verdict
    if verdict is CritiqueVerdict.ACCEPT:
        payload["suggested_probability_changes"] = []

    contract = validate_llm_contract_payload(
        schema_name="Critique", payload=payload, allowed_ids=ALLOWED_IDS
    )
    assert contract.verdict is verdict


def test_critique_rejects_an_unknown_verdict() -> None:
    payload = _payload("Critique")
    payload["verdict"] = "approve"

    with pytest.raises(ValidationError):
        Critique.model_validate(payload)


def test_critique_carries_challenged_claims_and_probability_changes() -> None:
    contract = validate_llm_contract_payload(
        schema_name="Critique", payload=_payload("Critique"), allowed_ids=ALLOWED_IDS
    )

    assert contract.challenged_claims[0].claim_id == "claim-1"
    change = contract.suggested_probability_changes[0]
    assert change.scenario_label == "Bear"
    assert (change.current_probability, change.suggested_probability) == (0.33, 0.45)


def test_critique_accept_cannot_also_suggest_probability_changes() -> None:
    payload = _payload("Critique")
    payload["verdict"] = CritiqueVerdict.ACCEPT

    with pytest.raises(ValidationError, match="not allowed when verdict is accept"):
        Critique.model_validate(payload)


def test_critique_termination_rule_allows_at_most_one_revision() -> None:
    payload = _payload("Critique")
    payload["verdict"] = CritiqueVerdict.REVISE
    payload["revision_round"] = 1

    with pytest.raises(ValidationError, match="at most one analyst revision"):
        Critique.model_validate(payload)

    # After the single revision the Critic may still reject; the Composer then receives
    # both positions and must note the unresolved disagreement (spec termination rule).
    payload["verdict"] = CritiqueVerdict.REJECT
    contract = Critique.model_validate(payload)
    assert contract.verdict is CritiqueVerdict.REJECT
    assert contract.revision_round == 1


# --- ReportComposition: claim-tagged blocks ------------------------------------------


def test_report_composition_blocks_are_text_plus_claim_ids() -> None:
    contract = validate_llm_contract_payload(
        schema_name="ReportComposition",
        payload=_payload("ReportComposition"),
        allowed_ids=ALLOWED_IDS,
    )
    block = contract.blocks[0]

    assert block.text.startswith("Energy exposures")
    assert block.claim_ids == ["claim-1", "claim-2"]


def test_report_composition_blocks_must_be_claim_tagged() -> None:
    payload = _payload("ReportComposition")
    payload["blocks"][0]["claim_ids"] = []

    with pytest.raises(ValidationError, match="at least 1 item"):
        ReportComposition.model_validate(payload)


# --- EntityLinkAdjudication: one whitelisted candidate id, or NIL (ADR 0005 stage 3) ---


def test_entity_link_adjudication_accepts_a_whitelisted_candidate_id() -> None:
    contract = validate_llm_contract_payload(
        schema_name="EntityLinkAdjudication",
        payload=_payload("EntityLinkAdjudication"),
        allowed_ids=ALLOWED_IDS,
    )

    assert isinstance(contract, EntityLinkAdjudication)
    assert contract.decision == "company-acme"
    assert contract.selected_id == "company-acme"


def test_entity_link_adjudication_accepts_nil_without_whitelisting_it() -> None:
    """NIL is the abstain path, so it passes a whitelist that does not contain it."""

    payload = _payload("EntityLinkAdjudication")
    payload["decision"] = NIL_DECISION

    contract = validate_llm_contract_payload(
        schema_name="EntityLinkAdjudication",
        payload=payload,
        allowed_ids=["company-acme"],
    )

    assert contract.decision == NIL_DECISION
    # NIL selects nothing: there is no id here for a caller to attach an entity by.
    assert contract.selected_id is None


@pytest.mark.parametrize(
    "minted",
    ["company-minted", "nil", "NIL ", "company-acme,company-other", ""],
)
def test_entity_link_adjudication_rejects_any_id_it_was_not_given(minted: str) -> None:
    """Only an injected id or the exact literal NIL: no minted id, no case variant, no list."""

    payload = _payload("EntityLinkAdjudication")
    payload["decision"] = minted

    with pytest.raises(ValidationError):
        validate_llm_contract_payload(
            schema_name="EntityLinkAdjudication",
            payload=payload,
            allowed_ids=["company-acme"],
        )


def test_entity_link_adjudication_permits_an_abstain_reason_only_with_nil() -> None:
    """The envelope's abstain reason is a reason for NIL, never prose attached to a selection."""

    payload = _payload("EntityLinkAdjudication")
    payload["no_finding_reason"] = "Neither candidate is this company."

    with pytest.raises(ValidationError, match="abstain reason"):
        validate_llm_contract_payload(
            schema_name="EntityLinkAdjudication",
            payload=payload,
            allowed_ids=ALLOWED_IDS,
        )

    payload["decision"] = NIL_DECISION
    contract = validate_llm_contract_payload(
        schema_name="EntityLinkAdjudication",
        payload=payload,
        allowed_ids=ALLOWED_IDS,
    )
    assert contract.no_finding_reason == "Neither candidate is this company."


def test_entity_link_adjudication_schema_asks_for_exactly_one_decision_field() -> None:
    """The provider-native schema has no field for prose, a runner-up, or a model confidence."""

    schema = llm_contract_json_schema("EntityLinkAdjudication")

    assert schema["additionalProperties"] is False
    assert "decision" in schema["required"]
    assert set(schema["properties"]) == {
        "schema_name",
        "schema_version",
        "prompt_template_version",
        "no_finding_reason",
        "decision",
    }
    assert NIL_DECISION in schema["properties"]["decision"]["description"]
