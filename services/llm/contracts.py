"""Typed LLM output contracts.

Single source of truth: `docs/specs/llm-contracts-reconciliation.md` (companion to
ADR 0008). Plan §16 is superseded where it conflicts with that spec.

Universal conventions enforced here:

- ``*_score`` fields are 0-100 JSON numbers, normalized to two decimals (ROUND_HALF_UP).
  Bounds are enforced on the value the provider sent, before rounding, so 100.004 is
  rejected rather than rounded into range. ``confidence``/``probability`` are 0.0-1.0
  floats.
- Impact direction is an enum (``positive|negative|mixed|unclear``), never a signed score.
- Scenario sets are MECE: probabilities sum to 1.0 +/- 0.01.
- ID whitelist: models never mint IDs. Every ID in an output must appear in the
  whitelist injected into the prompt, or validation rejects the payload. The whitelist is
  fail-closed at the public boundary: an omitted/``None`` ``allowed_ids`` is a
  configuration error (even for an ID-free payload), while an explicitly empty sequence is
  valid configuration that permits zero IDs.
- Abstain path: every array-shaped schema accepts ``[]`` plus a ``no_finding_reason``.
- Envelope: every payload carries schema_name, schema_version, prompt_template_version.
  All three are required and never defaulted, so a payload missing one is rejected.
- Derived fields (``severity``) are computed here and never model-emitted, which splits a
  validated contract into two serializations: :meth:`BaseLLMContract.to_normalized_payload`
  (auditable/downstream; includes them) and :meth:`BaseLLMContract.to_provider_payload`
  (contract input; excludes them, so it can be fed back through validation).
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    ValidationInfo,
    WithJsonSchema,
    computed_field,
    field_validator,
    model_validator,
)

from db.models.enums import Horizon, RiskLevel, risk_level_for_score

_ALLOWED_IDS_CONTEXT_KEY: Final[str] = "allowed_ids"

#: The one decision an ID-whitelisted contract may return that is not an ID: "none of these"
#: (ADR 0005 stage 3). It is a literal, never an ID, so it can never collide with one.
NIL_DECISION: Final[str] = "NIL"

_SCORE_MIN: Final[Decimal] = Decimal(0)
_SCORE_MAX: Final[Decimal] = Decimal(100)
_SCORE_QUANTUM: Final[Decimal] = Decimal("0.01")

_PROBABILITY_SUM: Final[Decimal] = Decimal("1.0")
_PROBABILITY_TOLERANCE: Final[Decimal] = Decimal("0.01")


def _probability_total(probabilities: Sequence[float]) -> Decimal:
    """Total the probabilities exactly, so the tolerance bound stays inclusive.

    Binary floats cannot represent the decimals providers emit, so summing them accrues
    error that a boundary total cannot absorb: 0.33 + 0.33 + 0.33 lands 1e-17 away from
    0.99, and the resulting distance from 1.0 (0.010000000000000009) exceeds a tolerance
    the spec defines as inclusive. Re-reading each float through its shortest round-trip
    repr recovers the decimal the provider sent and totals it without drift.
    """

    return sum((Decimal(str(value)) for value in probabilities), Decimal(0))


def _normalize_score(value: Any) -> float:
    """Validate one 0-100 score strictly, then round it half-up to two decimals.

    Strict on type: a bool, a numeric string, or any non-number is rejected rather than
    coerced, and NaN/Infinity never pass. Bounds are checked against the value the
    provider actually sent, so a near-miss like 100.004 is a validation failure and cannot
    round its way into range. Extra fractional digits inside the bounds are normalized,
    not rejected: providers routinely emit them and the run should still succeed.
    """

    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ValueError("score must be a JSON number, not a string, bool, or null")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("score must be a finite number")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("score must be a finite number")
    if not _SCORE_MIN <= Decimal(str(value)) <= _SCORE_MAX:
        raise ValueError("score must be between 0 and 100")
    return float(Decimal(str(value)).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP))


LLMScore = Annotated[
    float,
    PlainValidator(_normalize_score),
    WithJsonSchema({"type": "number", "minimum": 0, "maximum": 100}),
]
"""Every ``*_score`` contract field. Stays a plain provider-side JSON number: the bounds
travel in the schema, while the two-decimal normalization happens here, never as a
``multipleOf`` constraint a provider would have to satisfy on its own."""


def _allowed_ids(info: ValidationInfo) -> set[str] | None:
    """Return the injected whitelist, or None when a model is constructed without context.

    An empty set is a real whitelist that admits no IDs, so it is never conflated with
    None. The public boundary always injects a set; None only reaches here on direct
    ``model_validate`` calls (offline schema checks, fixtures).
    """

    raw = info.context.get(_ALLOWED_IDS_CONTEXT_KEY) if info.context else None
    if raw is None:
        return None
    return {str(item) for item in raw}


def _enforce_allowed_id(value: str, info: ValidationInfo, *, field_name: str) -> str:
    whitelist = _allowed_ids(info)
    if whitelist is not None and value not in whitelist:
        raise ValueError(f"{field_name} must be whitelisted: {value}")
    return value


def _enforce_allowed_ids(
    values: Sequence[str], info: ValidationInfo, *, field_name: str
) -> list[str]:
    whitelist = _allowed_ids(info)
    if whitelist is None or not values:
        return list(values)
    invalid = [value for value in values if value not in whitelist]
    if invalid:
        raise ValueError(f"{field_name} must be whitelisted: {', '.join(invalid)}")
    return list(values)


def _validate_findings_or_no_reason(
    *, items: Sequence[Any], no_finding_reason: str | None, label: str
) -> None:
    if not items and not no_finding_reason:
        raise ValueError(f"{label} must contain at least one item or provide no_finding_reason")
    if items and no_finding_reason:
        raise ValueError(f"{label} cannot be combined with no_finding_reason")


class LLMContractValidationError(ValueError):
    """Raised when contract registry lookup fails or payloads cannot be dispatched."""


class LLMContractLookupError(LLMContractValidationError):
    """Raised when a schema_name has no registered contract model."""


class LLMContractConfigurationError(LLMContractValidationError):
    """Raised when the caller never injected the ID whitelist the validator needs.

    A missing whitelist is a misconfiguration, not permission to stop policing IDs: it is
    rejected even when the payload happens to carry none. Callers that legitimately allow
    zero IDs must say so with an explicit empty sequence.
    """


class ImpactDirection(StrEnum):
    """Direction of an impact. Never a signed score (spec: universal conventions)."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    UNCLEAR = "unclear"


class CritiqueVerdict(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"


class WhenPrecision(StrEnum):
    """Granularity of an extracted event date, so it can feed event_timeline_items."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"


class AssertionStatus(StrEnum):
    """Claim status flowing from ADR 0005."""

    ASSERTED = "asserted"
    DENIED = "denied"
    SPECULATIVE = "speculative"


class BaseLLMContract(BaseModel):
    """Shared envelope. Carries no scores: those live on the findings themselves.

    All three envelope members are required on every contract and none of them is
    defaulted: a payload that omits one is rejected rather than silently repaired, and the
    generated provider JSON Schema lists all three as `required`.
    """

    schema_name: str
    schema_version: Literal["1.0"]
    prompt_template_version: str = Field(min_length=1)
    no_finding_reason: str | None = None

    model_config = ConfigDict(extra="forbid")

    def to_normalized_payload(self) -> dict[str, Any]:
        """Auditable/downstream shape: normalized scores plus every derived field."""

        return self.model_dump(mode="json")

    def to_provider_payload(self) -> dict[str, Any]:
        """Contract-input shape: only what a provider may legally emit, so it revalidates.

        Derived fields (`severity`) are excluded, because a payload carrying one is rejected
        by ``extra="forbid"`` — that rule is what keeps the band un-spoofable, so a replayed
        payload must not smuggle one back in. Everything the provider did send survives,
        already normalized, and `severity` is re-derived on revalidation.
        """

        return self.model_dump(mode="json", exclude_computed_fields=True)


class _Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventWhen(_Finding):
    """Structured event timing: `{date, precision}` (spec: EventExtraction amendment)."""

    date: datetime.date
    precision: WhenPrecision


class EventFinding(_Finding):
    event_id: str
    summary: str
    key_facts: list[str] = Field(min_length=1)
    why_it_matters: str
    when: EventWhen
    evidence_article_ids: list[str] = Field(min_length=1)
    impact_direction: ImpactDirection
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("event_id")
    @classmethod
    def _validate_event_id(cls, value: str, info: ValidationInfo) -> str:
        return _enforce_allowed_id(value, info, field_name="event_id")

    @field_validator("evidence_article_ids")
    @classmethod
    def _validate_evidence_article_ids(
        cls, values: list[str], info: ValidationInfo
    ) -> list[str]:
        return _enforce_allowed_ids(values, info, field_name="evidence_article_ids")


class IndustryImpactFinding(_Finding):
    industry_id: str
    industry_name: str
    rationale: str
    impact_score: LLMScore
    impact_direction: ImpactDirection
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("industry_id")
    @classmethod
    def _validate_industry_id(cls, value: str, info: ValidationInfo) -> str:
        return _enforce_allowed_id(value, info, field_name="industry_id")


class CompanyImpactFinding(_Finding):
    company_id: str
    company_name: str
    rationale: str
    impact_score: LLMScore
    impact_direction: ImpactDirection
    assertion_status: AssertionStatus
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("company_id")
    @classmethod
    def _validate_company_id(cls, value: str, info: ValidationInfo) -> str:
        return _enforce_allowed_id(value, info, field_name="company_id")


class HistoricalAnalogyFinding(_Finding):
    historical_episode_id: str
    explanation: str
    regime_caveats: list[str] = Field(default_factory=list)
    similarity_score: LLMScore
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("historical_episode_id")
    @classmethod
    def _validate_episode_id(cls, value: str, info: ValidationInfo) -> str:
        return _enforce_allowed_id(value, info, field_name="historical_episode_id")


class ForecastScenario(_Finding):
    """A scenario in a MECE set, aligned with the forecast_scenarios ORM columns."""

    scenario_name: Literal["base_case", "upside_case", "downside_case", "tail_risk_case"]
    narrative: str
    probability: float = Field(ge=0.0, le=1.0)
    risk_score: LLMScore
    horizon: Horizon
    impact_direction: ImpactDirection
    confidence: float = Field(ge=0.0, le=1.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def severity(self) -> RiskLevel:
        """Derive the persisted risk band from the normalized score; models never emit it."""

        return risk_level_for_score(self.risk_score)

    def to_orm_values(self) -> dict[str, Any]:
        """Return the contract fields in ``db.models.core.ForecastScenario`` shape."""

        return {
            "scenario_name": self.scenario_name,
            "probability": self.probability,
            "risk_score": self.risk_score,
            "severity": self.severity.value,
            "horizon": self.horizon.value,
            "narrative": self.narrative,
            "expected_impact": {"direction": self.impact_direction.value},
            "confidence": self.confidence,
        }


class ChallengedClaim(_Finding):
    claim_id: str
    issue: str
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("claim_id")
    @classmethod
    def _validate_claim_id(cls, value: str, info: ValidationInfo) -> str:
        return _enforce_allowed_id(value, info, field_name="claim_id")


class SuggestedProbabilityChange(_Finding):
    scenario_label: str
    current_probability: float = Field(ge=0.0, le=1.0)
    suggested_probability: float = Field(ge=0.0, le=1.0)
    rationale: str


class RiskWarningFinding(_Finding):
    """`severity` is derived from the normalized risk_score, never model-emitted (spec).

    It is a computed field, so a payload that tries to supply `severity` is rejected by
    ``extra="forbid"`` rather than silently overriding the derived band.
    """

    title: str
    detail: str
    risk_score: LLMScore
    probability: float = Field(ge=0.0, le=1.0)
    horizon: Horizon
    confidence: float = Field(ge=0.0, le=1.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def severity(self) -> RiskLevel:
        return risk_level_for_score(self.risk_score)


class ReportBlock(_Finding):
    """Claim-tagged prose block: `{text, claim_ids[]}` (spec: ReportComposition)."""

    text: str
    claim_ids: list[str] = Field(min_length=1)

    @field_validator("claim_ids")
    @classmethod
    def _validate_claim_ids(cls, values: list[str], info: ValidationInfo) -> list[str]:
        return _enforce_allowed_ids(values, info, field_name="claim_ids")


class EventExtraction(BaseLLMContract):
    SCHEMA_NAME: ClassVar[str] = "EventExtraction"
    schema_name: Literal["EventExtraction"]
    events: list[EventFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_events(self) -> EventExtraction:
        _validate_findings_or_no_reason(
            items=self.events,
            no_finding_reason=self.no_finding_reason,
            label="events",
        )
        return self


class IndustryImpact(BaseLLMContract):
    SCHEMA_NAME: ClassVar[str] = "IndustryImpact"
    schema_name: Literal["IndustryImpact"]
    impacts: list[IndustryImpactFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_impacts(self) -> IndustryImpact:
        _validate_findings_or_no_reason(
            items=self.impacts,
            no_finding_reason=self.no_finding_reason,
            label="impacts",
        )
        return self


class CompanyImpact(BaseLLMContract):
    SCHEMA_NAME: ClassVar[str] = "CompanyImpact"
    schema_name: Literal["CompanyImpact"]
    impacts: list[CompanyImpactFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_impacts(self) -> CompanyImpact:
        _validate_findings_or_no_reason(
            items=self.impacts,
            no_finding_reason=self.no_finding_reason,
            label="impacts",
        )
        return self


class HistoricalAnalogy(BaseLLMContract):
    SCHEMA_NAME: ClassVar[str] = "HistoricalAnalogy"
    schema_name: Literal["HistoricalAnalogy"]
    analogies: list[HistoricalAnalogyFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_analogies(self) -> HistoricalAnalogy:
        _validate_findings_or_no_reason(
            items=self.analogies,
            no_finding_reason=self.no_finding_reason,
            label="analogies",
        )
        return self


class ForecastScenarios(BaseLLMContract):
    SCHEMA_NAME: ClassVar[str] = "ForecastScenarios"
    schema_name: Literal["ForecastScenarios"]
    scenarios: list[ForecastScenario] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_scenarios(self) -> ForecastScenarios:
        _validate_findings_or_no_reason(
            items=self.scenarios,
            no_finding_reason=self.no_finding_reason,
            label="scenarios",
        )
        if self.scenarios:
            names = [item.scenario_name for item in self.scenarios]
            if len(set(names)) != len(names):
                duplicates = sorted({name for name in names if names.count(name) > 1})
                raise ValueError(
                    "scenario_name values must be mutually exclusive; "
                    f"duplicates: {', '.join(duplicates)}"
                )
            total = _probability_total([item.probability for item in self.scenarios])
            if abs(total - _PROBABILITY_SUM) > _PROBABILITY_TOLERANCE:
                raise ValueError("scenario probabilities must sum to 1.0 +/- 0.01")
        return self


class Critique(BaseLLMContract):
    """Critic output. Termination rule: at most one analyst revision (spec)."""

    SCHEMA_NAME: ClassVar[str] = "Critique"
    schema_name: Literal["Critique"]
    verdict: CritiqueVerdict
    challenged_claims: list[ChallengedClaim] = Field(default_factory=list)
    suggested_probability_changes: list[SuggestedProbabilityChange] = Field(
        default_factory=list
    )
    revision_round: int = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def _validate_critique(self) -> Critique:
        _validate_findings_or_no_reason(
            items=self.challenged_claims,
            no_finding_reason=self.no_finding_reason,
            label="challenged_claims",
        )
        if self.verdict == CritiqueVerdict.ACCEPT and self.suggested_probability_changes:
            raise ValueError(
                "suggested_probability_changes are not allowed when verdict is accept"
            )
        if self.verdict == CritiqueVerdict.REVISE and self.revision_round >= 1:
            raise ValueError(
                "termination rule: at most one analyst revision; "
                "the Composer must receive both positions instead"
            )
        return self


class RiskWarning(BaseLLMContract):
    SCHEMA_NAME: ClassVar[str] = "RiskWarning"
    schema_name: Literal["RiskWarning"]
    warnings: list[RiskWarningFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_warnings(self) -> RiskWarning:
        _validate_findings_or_no_reason(
            items=self.warnings,
            no_finding_reason=self.no_finding_reason,
            label="warnings",
        )
        return self


class ReportComposition(BaseLLMContract):
    SCHEMA_NAME: ClassVar[str] = "ReportComposition"
    schema_name: Literal["ReportComposition"]
    blocks: list[ReportBlock] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_blocks(self) -> ReportComposition:
        _validate_findings_or_no_reason(
            items=self.blocks,
            no_finding_reason=self.no_finding_reason,
            label="blocks",
        )
        return self


class EntityLinkAdjudication(BaseLLMContract):
    """ADR 0005 stage 3: which candidate an ambiguous news mention refers to, or ``NIL``.

    The decision space is closed by construction, which is the whole point of the contract:
    ``decision`` is one ID from the whitelist orchestration injected into the prompt, or the
    literal ``NIL``, and nothing else. A minted ID fails the whitelist rule every other contract
    obeys; ``NIL`` is permitted *explicitly* rather than by widening that rule, so abstaining
    stays possible without any invented ID becoming possible with it.

    There is no free-text field to argue in: ``extra="forbid"`` rejects one the model adds, and
    the envelope's ``no_finding_reason`` is only allowed alongside ``NIL`` -- where it is the
    universal abstain reason -- never as commentary attached to a selection.
    """

    SCHEMA_NAME: ClassVar[str] = "EntityLinkAdjudication"
    schema_name: Literal["EntityLinkAdjudication"]
    decision: str = Field(
        min_length=1,
        description=(
            "Exactly one candidate id from the injected candidate list, or the literal "
            f"'{NIL_DECISION}' when no candidate is the entity the mention refers to."
        ),
    )

    @field_validator("decision")
    @classmethod
    def _validate_decision(cls, value: str, info: ValidationInfo) -> str:
        if value == NIL_DECISION:
            return value
        return _enforce_allowed_id(value, info, field_name="decision")

    @model_validator(mode="after")
    def _validate_adjudication(self) -> EntityLinkAdjudication:
        if self.no_finding_reason and self.decision != NIL_DECISION:
            raise ValueError(
                "no_finding_reason is the abstain reason and belongs only with "
                f"decision='{NIL_DECISION}', never with a selected candidate"
            )
        return self

    @property
    def selected_id(self) -> str | None:
        """The chosen candidate ID, or ``None`` when the model abstained with ``NIL``."""

        return None if self.decision == NIL_DECISION else self.decision


LLM_CONTRACT_REGISTRY: dict[str, type[BaseLLMContract]] = {
    EventExtraction.SCHEMA_NAME: EventExtraction,
    IndustryImpact.SCHEMA_NAME: IndustryImpact,
    CompanyImpact.SCHEMA_NAME: CompanyImpact,
    HistoricalAnalogy.SCHEMA_NAME: HistoricalAnalogy,
    ForecastScenarios.SCHEMA_NAME: ForecastScenarios,
    Critique.SCHEMA_NAME: Critique,
    RiskWarning.SCHEMA_NAME: RiskWarning,
    ReportComposition.SCHEMA_NAME: ReportComposition,
    EntityLinkAdjudication.SCHEMA_NAME: EntityLinkAdjudication,
}


def list_contract_schemas() -> list[str]:
    """Return stable, sorted schema names."""

    return sorted(LLM_CONTRACT_REGISTRY)


def resolve_llm_contract(schema_name: str) -> type[BaseLLMContract]:
    """Return the registered contract model for ``schema_name``."""

    try:
        return LLM_CONTRACT_REGISTRY[schema_name]
    except KeyError as exc:
        available = ", ".join(list_contract_schemas())
        raise LLMContractLookupError(
            f"Unknown schema_name='{schema_name}'. Available: {available}"
        ) from exc


def llm_contract_json_schema(schema_name: str) -> dict[str, Any]:
    """Return the JSON Schema for provider-native structured-output enforcement."""

    return resolve_llm_contract(schema_name).model_json_schema()


def validate_llm_contract_payload(
    *,
    schema_name: str,
    payload: Mapping[str, Any],
    allowed_ids: Sequence[str] | None = None,
) -> BaseLLMContract:
    """Validate a payload using the registered contract schema.

    ``allowed_ids`` is the whitelist orchestration injects into the prompt. Omitting it or
    passing ``None`` is a configuration error, even for a payload that carries no IDs; an
    explicitly empty sequence is valid configuration and permits exactly zero IDs.
    """

    model = resolve_llm_contract(schema_name)
    if allowed_ids is None:
        raise LLMContractConfigurationError(
            f"allowed_ids whitelist is required to validate '{schema_name}': orchestration "
            "must inject the IDs the model may reference, or pass an explicit empty "
            "sequence to permit none"
        )
    context = {_ALLOWED_IDS_CONTEXT_KEY: {str(item) for item in allowed_ids}}
    return model.model_validate(payload, context=context)


def validate_llm_payload(
    payload: Mapping[str, Any], *, allowed_ids: Sequence[str] | None = None
) -> BaseLLMContract:
    """Validate a payload using its embedded schema_name field.

    ``allowed_ids`` carries the same fail-closed semantics as
    :func:`validate_llm_contract_payload`.
    """

    schema_name = payload.get("schema_name")
    if not isinstance(schema_name, str):
        raise LLMContractValidationError("payload must include schema_name")
    return validate_llm_contract_payload(
        schema_name=schema_name,
        payload=payload,
        allowed_ids=allowed_ids,
    )
