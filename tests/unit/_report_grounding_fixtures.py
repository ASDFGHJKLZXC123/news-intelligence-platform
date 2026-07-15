"""Shared, DB-free builders and fakes for the Stage 6 grounding-gate unit tests.

Underscore-prefixed so pytest does not collect it. It builds on the composition fixtures and adds:

* :class:`GateOrchestrator` -- one fake that dispatches by ``requested_schema``: composition calls
  return real, whitelist-validated :class:`ReportComposition` contracts (T2); grounding calls
  return real :class:`ClaimGrounding` contracts (T1). Both are validated with the request's own
  ``allowed_ids``, so an off-whitelist id fails exactly as it would in production.
* Grounding payload builders and a couple of scripted ``ground``/``compose`` callables.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from services.llm.adapters import LLMInvocationMode, LLMInvocationRequest
from services.llm.contracts import validate_llm_contract_payload
from services.llm.orchestrator import LLMOrchestratorResult
from services.llm.policy import LLMTier
from services.reports.grounding_prompts import (
    GROUNDING_PROMPT_TEMPLATE_VERSION,
    GROUNDING_SCHEMA,
    GROUNDING_SCHEMA_VERSION,
)
from services.reports.prompts import COMPOSITION_SCHEMA
from tests.unit._report_composition_fixtures import (  # noqa: F401 -- re-exported for tests
    _EMPTY_SELECTION,
    CLAIM_ID,
    CLAIM_ID_2,
    EPISODE_ID,
    EVENT_ID,
    MINTED_ID,
    Harness,
    alert_change,
    analogy,
    brief_context,
    brief_inputs,
    claim,
    evidence_for,
    report_payload,
    selected_event,
    single_event_brief,
    valid_provider_response,
    words,
)

#: The per-section target word counts a composition lands on, so a fake composition stays inside
#: each section's budget (report-generation spec) and is never degraded for a budget miss.
_SECTION_TARGETS: dict[str, int] = {
    "executive_summary": 108,
    "top_event": 150,
    "risk_radar": 60,
    "historical_parallels": 100,
}


# --------------------------------------------------------------------------------------
# Composition responses (ReportComposition)
# --------------------------------------------------------------------------------------


def per_claim_blocks(request: Any) -> dict[str, Any]:
    """One block per allowed claim, words distributed to hit the section's budget exactly.

    The default composition for grounding tests: it lets a test address individual blocks (one
    claim each) while staying in budget, so the draft is never degraded before the gate sees it.
    """

    kind = request.context["section_kind"]
    claim_ids = list(request.context["allowed_claim_ids"])
    target = _SECTION_TARGETS.get(kind, 100)
    n = len(claim_ids)
    if n == 0:
        return report_payload([], no_finding_reason="nothing citable")
    base = target // n
    remainder = target - base * n
    blocks = [
        (words(max(1, base + (remainder if index == 0 else 0))), [claim_id])
        for index, claim_id in enumerate(claim_ids)
    ]
    return report_payload(blocks)


# --------------------------------------------------------------------------------------
# Grounding responses (ClaimGrounding)
# --------------------------------------------------------------------------------------


def grounding_payload(
    verdicts: list[tuple[str, str]], *, no_finding_reason: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": GROUNDING_SCHEMA,
        "schema_version": GROUNDING_SCHEMA_VERSION,
        "prompt_template_version": GROUNDING_PROMPT_TEMPLATE_VERSION,
        "verdicts": [{"claim_id": claim_id, "verdict": verdict} for claim_id, verdict in verdicts],
    }
    if no_finding_reason is not None:
        payload["no_finding_reason"] = no_finding_reason
    return payload


def verdict_for_each(request: Any, verdict: str) -> dict[str, Any]:
    """A grounding reply giving ``verdict`` to every claim the block cited."""

    return grounding_payload([(cid, verdict) for cid in request.context["allowed_claim_ids"]])


def all_supported(request: Any) -> dict[str, Any]:
    return verdict_for_each(request, "supported")


def make_ground(
    *,
    unsupported: set[str] = frozenset(),
    unverifiable: set[str] = frozenset(),
    only_round: int | None = None,
) -> Any:
    """A ``ground`` callable: claims in ``unsupported``/``unverifiable`` get that verdict, else supported.

    ``only_round`` restricts the unsupported/unverifiable verdicts to one grounding round, so a test
    can model a regeneration that *fixes* the section (``only_round=1`` -> round 2 is all supported).
    """

    def ground(request: Any) -> dict[str, Any]:
        apply = only_round is None or request.context["grounding_round"] == only_round
        verdicts: list[tuple[str, str]] = []
        for cid in request.context["allowed_claim_ids"]:
            if apply and cid in unsupported:
                verdicts.append((cid, "unsupported"))
            elif apply and cid in unverifiable:
                verdicts.append((cid, "unverifiable"))
            else:
                verdicts.append((cid, "supported"))
        return grounding_payload(verdicts)

    return ground


# --------------------------------------------------------------------------------------
# The combined fake orchestrator
# --------------------------------------------------------------------------------------


class GateOrchestrator:
    """Dispatches by ``requested_schema``: composition -> ReportComposition (T2); grounding -> T1.

    ``compose`` and ``ground`` are callables ``(request) -> payload | Exception`` (an exception is
    raised, to simulate an orchestrator failure). Both default to healthy responses. Every request
    is recorded, split into ``composition_requests`` and ``grounding_requests`` for assertions.
    """

    def __init__(self, *, compose: Any = None, ground: Any = None) -> None:
        self._compose = compose or per_claim_blocks
        self._ground = ground or all_supported
        self.requests: list[Any] = []
        self.composition_requests: list[Any] = []
        self.grounding_requests: list[Any] = []
        self._counter = 0

    def run(self, request: Any) -> LLMOrchestratorResult:
        self.requests.append(request)
        schema = request.requested_schema
        if schema == COMPOSITION_SCHEMA:
            self.composition_requests.append(request)
            result = self._compose(request)
            tier = LLMTier.T2
        elif schema == GROUNDING_SCHEMA:
            self.grounding_requests.append(request)
            result = self._ground(request)
            tier = LLMTier.T1
        else:  # pragma: no cover - a mis-routed schema is a test bug, surfaced loudly
            raise AssertionError(f"unexpected requested_schema: {schema}")

        if isinstance(result, BaseException):
            raise result
        contract = validate_llm_contract_payload(
            schema_name=schema, payload=result, allowed_ids=list(request.allowed_ids)
        )
        index = self._counter
        self._counter += 1
        run = SimpleNamespace(
            id=uuid.uuid4(),
            prompt_name=request.prompt_name,
            prompt_version=request.prompt_version,
            prompt_template_version=request.prompt_template_version,
        )
        return LLMOrchestratorResult(
            run=run,
            contract=contract,
            trace_id=f"trace-{index}",
            cache_hit=False,
            tier=tier,
            mode=LLMInvocationMode.REALTIME,
            queue="essential",
            degraded_provider=None,
            route_degradation_reasons=(),
            selected_articles=_EMPTY_SELECTION,
        )

    def grounding_requests_for(self, section_kind: str) -> list[Any]:
        return [r for r in self.grounding_requests if r.context["section_kind"] == section_kind]

    def composition_requests_for(self, section_kind: str) -> list[Any]:
        return [r for r in self.composition_requests if r.context["section_kind"] == section_kind]

    def regenerations_for(self, section_kind: str) -> list[Any]:
        return [r for r in self.composition_requests_for(section_kind) if r.context["grounding_regeneration"]]


def gate_provider_response(request: LLMInvocationRequest) -> dict[str, Any]:
    """A production-orchestrator provider reply: valid composition or all-supported grounding."""

    if request.requested_schema == COMPOSITION_SCHEMA:
        return valid_provider_response(request)
    if request.requested_schema == GROUNDING_SCHEMA:
        allowed = list(request.context.get("allowed_claim_ids", []))
        return grounding_payload([(cid, "supported") for cid in allowed])
    raise AssertionError(f"unexpected requested_schema: {request.requested_schema}")  # pragma: no cover
