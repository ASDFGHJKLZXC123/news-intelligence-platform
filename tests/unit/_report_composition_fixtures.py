"""Shared, DB-free builders and fakes for the Stage 6 composition unit tests.

Underscore-prefixed so pytest does not collect it. It provides hand-built
:class:`BriefInputs`/:class:`BriefContext`/:class:`BriefMaterial`, a narrow
:class:`FakeOrchestrator` that returns *real* validated :class:`ReportComposition` contracts,
and a :class:`Harness` around the production orchestrator with a scripted provider.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any

from services.llm.adapters import LLMInvocationMode, LLMInvocationRequest
from services.llm.contracts import validate_llm_contract_payload
from services.llm.orchestrator import LLMOrchestratorResult
from services.llm.policy import LLMTier
from services.llm.selection import RepresentativeArticleSelection
from services.reports.context import (
    AnalogyContext,
    BriefContext,
    ClaimContext,
    EventAnalogyContext,
    EventEvidenceContext,
    EventForecast,
    EvidenceArticle,
    EvidenceLink,
    ExcerptOrigin,
    ForecastScenarioRow,
    HistoricalOnset,
    HistoricalOutcomeContext,
    ParentEpisode,
    SourceExcerpt,
)
from services.reports.contracts import (
    AlertStateChange,
    BriefInputs,
    DataQualityNote,
    ExecutiveSummaryInputs,
    LinkedRisk,
    RiskKey,
    RiskMove,
    RiskProvenance,
    RiskRadar,
    RiskRadarEntry,
    SelectedEvent,
)
from services.reports.material import BriefMaterial, build_brief_material
from services.reports.prompts import (
    COMPOSITION_PROMPT_TEMPLATE_VERSION,
    COMPOSITION_SCHEMA,
    COMPOSITION_SCHEMA_VERSION,
)
from services.reports.window import window_for_date

UTC = datetime.UTC
BRIEF_DATE = datetime.date(2026, 7, 14)
WINDOW = window_for_date(BRIEF_DATE)
NOW = WINDOW.end - datetime.timedelta(hours=1)

EVENT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID_2 = uuid.UUID("22222222-2222-4222-8222-222222222222")
CLAIM_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CLAIM_ID_2 = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ARTICLE_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
EPISODE_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
PARENT_EPISODE_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
MINTED_ID = uuid.UUID("99999999-9999-4999-8999-999999999999")

RISK_KEY = RiskKey("country", "US", "banking")


# --------------------------------------------------------------------------------------
# Word helpers
# --------------------------------------------------------------------------------------


def words(count: int) -> str:
    """A string of exactly ``count`` whitespace-separated tokens (``count_words`` counts them)."""
    return " ".join(f"w{index}" for index in range(count))


# --------------------------------------------------------------------------------------
# Domain builders
# --------------------------------------------------------------------------------------


def excerpt(text: str = "A bounded excerpt from the source article.") -> SourceExcerpt:
    return SourceExcerpt(text=text, origin=ExcerptOrigin.SUMMARY, truncated=False)


def claim(
    claim_id: uuid.UUID = CLAIM_ID,
    *,
    text: str = "The central bank held rates steady amid disinflation.",
    confidence: float | None = 0.8,
    article_title: str = "Rates held steady",
    article_id: uuid.UUID = ARTICLE_ID,
    excerpt_text: str = "A bounded excerpt from the source article.",
) -> ClaimContext:
    link = EvidenceLink(
        article=EvidenceArticle(
            article_id=article_id,
            title=article_title,
            publisher="Wire",
            url="https://example.test/a",
            published_at=NOW,
            source_credibility=0.7,
            excerpt=excerpt(excerpt_text),
        ),
        support_type="supports",
        support_confidence=0.9,
    )
    return ClaimContext(
        claim_id=claim_id,
        claim_text=text,
        claim_type="fact",
        claim_confidence=confidence,
        links=(link,),
    )


def evidence_for(event_id: uuid.UUID, claims: tuple[ClaimContext, ...]) -> EventEvidenceContext:
    return EventEvidenceContext(event_id=event_id, claims=claims)


def selected_event(
    event_id: uuid.UUID = EVENT_ID, *, title: str = "Rate decision", rank: int = 1
) -> SelectedEvent:
    return SelectedEvent(
        rank=rank,
        event_id=event_id,
        title=title,
        hotness_score=72.0,
        max_linked_risk=LinkedRisk(score=55.0, provenance=RiskProvenance.EVENT_OBSERVATION),
        ranking_score=65.0,
        credibility_sum=1.4,
        developing=False,
    )


def analogy(
    event_id: uuid.UUID = EVENT_ID,
    *,
    is_counterexample: bool = True,
    with_parent: bool = True,
) -> AnalogyContext:
    parent = (
        ParentEpisode(
            episode_id=PARENT_EPISODE_ID,
            name="Parent arc",
            onset_summary="Parent onset context.",
        )
        if with_parent
        else None
    )
    return AnalogyContext(
        event_id=event_id,
        similarity=0.83,
        rationale="Same deposit-run mechanism and concentrated funding base.",
        limitations=("Different central-bank backstop.",),
        shared_causes=("Rate-driven securities losses.",),
        regime_caveats=("Pre-QE regime; no backstop.",),
        evidence_refs={"refs": ["curated"]},
        onset=HistoricalOnset(
            episode_id=EPISODE_ID,
            name="1998 LTCM stress",
            episode_type="banking_stress",
            onset_date=datetime.date(1998, 8, 1),
            onset_summary="A leveraged fund faced margin calls as spreads widened.",
            geography="United States",
            regime_tags=("pre_QE",),
            is_counterexample=is_counterexample,
            source_refs={"refs": ["curated"]},
            parent=parent,
        ),
        outcome=HistoricalOutcomeContext(
            outcome_summary="Resolved via a coordinated recapitalisation.",
            outcomes=("Recapitalisation",),
            resolution_mechanism="Private recapitalisation brokered by the Fed.",
            peak_date=datetime.date(1998, 9, 1),
            end_date=datetime.date(1998, 12, 1),
        ),
    )


def forecast(event_id: uuid.UUID = EVENT_ID) -> EventForecast:
    scenario_set_id = uuid.uuid4()
    return EventForecast(
        event_id=event_id,
        scenario_set_id=scenario_set_id,
        scenarios=(
            ForecastScenarioRow(
                event_id=event_id,
                scenario_set_id=scenario_set_id,
                scenario_name="base_case",
                probability=1.0,
                risk_score=60.0,
                severity="high",
                horizon="0_6m",
                confidence=0.7,
                evidence_refs=None,
                created_at=NOW,
            ),
        ),
        created_at=NOW,
    )


def radar_entry(score: float, level: str, *, when: datetime.datetime = NOW) -> RiskRadarEntry:
    return RiskRadarEntry(key=RISK_KEY, score=score, level=level, as_of=when)


def risk_move(*, prev_level: str, cur_level: str, prev: float, cur: float) -> RiskMove:
    return RiskMove(
        key=RISK_KEY,
        current=radar_entry(cur, cur_level),
        previous=radar_entry(prev, prev_level, when=NOW - datetime.timedelta(days=1)),
    )


def alert_change(*, is_all_clear: bool = False, severity: str = "critical") -> AlertStateChange:
    return AlertStateChange(
        alert_id=uuid.uuid4(),
        title="Bank-run risk",
        state="resolved" if is_all_clear else "open",
        severity="low" if is_all_clear else severity,
        peak_severity=severity if is_all_clear else None,
        changed_at=NOW,
        related_event_id=None,
    )


def brief_inputs(
    *,
    top_events: tuple[SelectedEvent, ...] = (),
    alert_changes: tuple[AlertStateChange, ...] = (),
    radar: RiskRadar | None = None,
    largest_move: RiskMove | None = None,
    prior_brief: Any = None,
    notes: tuple[DataQualityNote, ...] = (),
) -> BriefInputs:
    radar = radar or RiskRadar(current=(), previous=(), moves=())
    return BriefInputs(
        window=WINDOW,
        top_events=top_events,
        executive_summary=ExecutiveSummaryInputs(
            alert_state_changes=alert_changes,
            top_events=top_events[:2],
            largest_risk_move=largest_move,
        ),
        risk_radar=radar,
        prior_brief=prior_brief,
        data_quality_notes=notes,
    )


def brief_context(
    *,
    evidence: tuple[EventEvidenceContext, ...] = (),
    analogies: tuple[EventAnalogyContext, ...] = (),
    forecasts: tuple[Any, ...] = (),
) -> BriefContext:
    return BriefContext(evidence=evidence, analogies=analogies, forecasts=forecasts)


def single_event_brief(
    *,
    claims: tuple[ClaimContext, ...] = (),
    radar: RiskRadar | None = None,
    analogies_present: bool = False,
    forecasts_present: bool = False,
    alert_changes: tuple[AlertStateChange, ...] = (),
    largest_move: RiskMove | None = None,
) -> tuple[BriefInputs, BriefContext, BriefMaterial]:
    """One selected event, optionally with claims, an empty radar, no forecasts by default."""
    event = selected_event()
    inputs = brief_inputs(
        top_events=(event,), radar=radar, alert_changes=alert_changes, largest_move=largest_move
    )
    evidence = (evidence_for(EVENT_ID, claims),) if claims else ()
    analogy_ctx = (
        (EventAnalogyContext(event_id=EVENT_ID, analogies=(analogy(),)),)
        if analogies_present
        else ()
    )
    forecasts = (forecast(),) if forecasts_present else ()
    context = brief_context(evidence=evidence, analogies=analogy_ctx, forecasts=forecasts)
    # This fixture models the explicit diagnostic/open path; closed-boundary tests pass the
    # resulting contaminated material to the fail-closed composer adversarially.
    material = build_brief_material(
        inputs,
        context,
        prediction_backed_outputs_enabled=True,
    )
    return inputs, context, material


# --------------------------------------------------------------------------------------
# ReportComposition payloads
# --------------------------------------------------------------------------------------


def report_payload(
    blocks: list[tuple[str, list[str]]], *, no_finding_reason: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_name": COMPOSITION_SCHEMA,
        "schema_version": COMPOSITION_SCHEMA_VERSION,
        "prompt_template_version": COMPOSITION_PROMPT_TEMPLATE_VERSION,
        "blocks": [{"text": text, "claim_ids": claim_ids} for text, claim_ids in blocks],
    }
    if no_finding_reason is not None:
        payload["no_finding_reason"] = no_finding_reason
    return payload


def one_block(word_count: int, *, claim_id: uuid.UUID = CLAIM_ID) -> dict[str, Any]:
    return report_payload([(words(word_count), [str(claim_id)])])


def multi_block(counts: list[int], *, claim_id: uuid.UUID = CLAIM_ID) -> dict[str, Any]:
    return report_payload([(words(count), [str(claim_id)]) for count in counts])


def abstain(reason: str = "nothing citable to say") -> dict[str, Any]:
    return report_payload([], no_finding_reason=reason)


def script(payload: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Wrap a payload with telemetry overrides (tier, cache_hit, run_id, ...) for the fake."""
    return {"payload": payload, **overrides}


# --------------------------------------------------------------------------------------
# Fakes and harness
# --------------------------------------------------------------------------------------

_EMPTY_SELECTION = RepresentativeArticleSelection(
    selected_articles=(),
    used_token_budget=0,
    selected_count=0,
    dropped_count=0,
    truncated_by_budget=False,
)


class FakeOrchestrator:
    """Returns real, whitelist-validated :class:`ReportComposition` contracts on a script.

    Each script entry is either an exception (raised, to simulate a provider/orchestrator
    failure) or a dict with a ``payload`` plus optional telemetry overrides (``tier``,
    ``cache_hit``, ``route_degradation_reasons``, ``degraded_provider``, ``run_id``,
    ``trace_id``). The payload is validated with the request's own ``allowed_ids``, so a minted
    claim id fails exactly as it would in production.
    """

    def __init__(self, scripts: list[Any]) -> None:
        self.scripts = list(scripts)
        self.requests: list[Any] = []
        self.call_count = 0

    def run(self, request: Any) -> LLMOrchestratorResult:
        self.requests.append(request)
        index = self.call_count
        self.call_count += 1
        entry = self.scripts[index] if index < len(self.scripts) else self.scripts[-1]
        if isinstance(entry, BaseException):
            raise entry
        if "payload" in entry:
            payload, overrides = entry["payload"], entry
        else:  # a bare ReportComposition payload, no telemetry overrides
            payload, overrides = entry, {}
        contract = validate_llm_contract_payload(
            schema_name=COMPOSITION_SCHEMA,
            payload=payload,
            allowed_ids=list(request.allowed_ids),
        )
        run = SimpleNamespace(
            id=overrides.get("run_id", uuid.uuid4()),
            prompt_name=request.prompt_name,
            prompt_version=request.prompt_version,
            prompt_template_version=request.prompt_template_version,
        )
        return LLMOrchestratorResult(
            run=run,
            contract=contract,
            trace_id=overrides.get("trace_id", f"trace-{index}"),
            cache_hit=overrides.get("cache_hit", False),
            tier=overrides.get("tier", LLMTier.T2),
            mode=LLMInvocationMode.REALTIME,
            queue=overrides.get("queue", "essential"),
            degraded_provider=overrides.get("degraded_provider"),
            route_degradation_reasons=tuple(overrides.get("route_degradation_reasons", ())),
            selected_articles=_EMPTY_SELECTION,
        )

    def requests_for(self, section_kind: str) -> list[Any]:
        return [r for r in self.requests if r.context["section_kind"] == section_kind]


class Harness:
    """The production orchestrator, a scripted provider, and a record of every request/prompt."""

    def __init__(self, respond: Any) -> None:
        from packages.config.settings import Settings
        from services.llm.cache import InMemoryLLMPromptCache
        from services.llm.fake_providers import CallableLLMProvider
        from services.llm.orchestrator import LLMOrchestrator
        from services.llm.repository import InMemoryLLMRuntimeRepository

        self.repository = InMemoryLLMRuntimeRepository()
        self.invocations: list[LLMInvocationRequest] = []
        self.requests: list[Any] = []
        self._respond = respond
        self.provider = CallableLLMProvider(self._capture)
        self._orchestrator = LLMOrchestrator(
            settings=Settings(),
            repository=self.repository,
            providers_by_tier={
                "T1": (self.provider,),
                "T2": (self.provider,),
                "T3": (self.provider,),
            },
            cache=InMemoryLLMPromptCache(),
        )

    def _capture(self, request: LLMInvocationRequest) -> Any:
        self.invocations.append(request)
        return self._respond(request)

    def run(self, request: Any) -> Any:
        self.requests.append(request)
        return self._orchestrator.run(request)


def valid_provider_response(request: LLMInvocationRequest) -> dict[str, Any]:
    """A scripted provider reply that cites an allowed claim and lands in the section's budget."""
    allowed = list(request.context.get("allowed_claim_ids", []))
    claim_id = allowed[0] if allowed else str(CLAIM_ID)
    targets = {
        "executive_summary": 108,
        "top_event": 150,
        "risk_radar": 60,
        "historical_parallels": 100,
    }
    count = targets.get(request.context.get("section_kind", ""), 100)
    return report_payload([(words(count), [claim_id])])
