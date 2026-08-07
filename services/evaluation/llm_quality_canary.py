"""Cost-bounded, secret-safe live quality canary for the four production LLM workloads.

The canary deliberately evaluates one configured provider/model at a time.  It uses the real
production prompt builders, contracts, orchestrator validation retry, and composition word-budget
retry, but no database, Redis, broker, prompt cache, or provider fallback.  Fixtures are synthetic
and the public report never contains prompts, prose, raw payloads, exception messages, or settings.

Passing this small canary authorizes a larger shadow evaluation only.  It is not statistically
large enough to authorize a production promotion.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

from packages.config.settings import Settings
from services.analogies.contracts import EpisodeCandidate
from services.analogies.rerank import (
    RERANK_PROMPT_NAME,
    RERANK_PROMPT_TEMPLATE_VERSION,
    RERANK_SCHEMA,
    RERANK_TIER,
    RerankStatus,
    build_rerank_prompt,
    rerank_candidates,
)
from services.entities.adjudication import (
    ADJUDICATION_PROMPT_NAME,
    ADJUDICATION_PROMPT_TEMPLATE_VERSION,
    ADJUDICATION_SCHEMA,
    ADJUDICATION_TIER,
    AdjudicationDecision,
    MentionAdjudicator,
    build_adjudication_prompt,
)
from services.entities.news_linking import (
    AliasEvidence,
    LinkBand,
    LinkCandidate,
    MentionLinkResult,
    mention_run_key,
    mention_target_id,
)
from services.evaluation.llm_shadow_cost import ShadowCostGuard
from services.llm.adapters import (
    LLMInvocationMode,
    LLMInvocationRequest,
    LLMInvocationResponse,
    LLMProviderAdapter,
)
from services.llm.contracts import (
    ClaimGrounding,
    GroundingVerdict,
    llm_contract_json_schema,
)
from services.llm.http_providers import build_providers_by_tier
from services.llm.orchestrator import LLMOrchestrator, LLMOrchestratorRequest
from services.llm.policy import LLMTier
from services.llm.pricing import estimate_completion_cost_usd
from services.llm.repository import InMemoryLLMRuntimeRepository
from services.llm.selection import estimate_token_count
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import EntityLabel, EntityMention, SentenceContext
from services.reports.composition import compose_section, count_words
from services.reports.context import (
    BriefContext,
    ClaimContext,
    EventEvidenceContext,
    EvidenceArticle,
    EvidenceLink,
    ExcerptOrigin,
    SourceExcerpt,
)
from services.reports.contracts import (
    BriefInputs,
    ExecutiveSummaryInputs,
    LinkedRisk,
    RiskProvenance,
    RiskRadar,
    SelectedEvent,
)
from services.reports.copyright import SnippetSource, check_block_copyright
from services.reports.grounding import (
    GROUNDING_RISK_LEVEL,
    GROUNDING_TEMPERATURE,
    GROUNDING_TIER,
    grounding_job,
)
from services.reports.grounding_prompts import (
    GROUNDING_PROMPT_NAME,
    GROUNDING_PROMPT_TEMPLATE_VERSION,
    GROUNDING_PROMPT_VERSION,
    GROUNDING_SCHEMA,
    build_grounding_prompt,
)
from services.reports.material import SectionKind, SectionMaterial
from services.reports.prompts import (
    COMPOSITION_PROMPT_TEMPLATE_VERSION,
    COMPOSITION_SCHEMA,
    TOP_EVENT_PROMPT_NAME,
    build_top_event_prompt,
)
from services.reports.window import window_for_date

_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_CASES_PATH: Final = _REPOSITORY_ROOT / "evaluation" / "llm-canary" / "cases.v1.json"
CASES_SCHEMA: Final = "llm-quality-canary-cases.v1"
REPORT_SCHEMA: Final = "llm-quality-canary.v1"
DEFAULT_MAX_CALLS: Final = 20
DEFAULT_MAX_COST_USD: Final = 0.50
ACTIVE_ROUTE_REPORT_SCHEMA: Final = "llm-active-route-canary.v1"
ACTIVE_ROUTE_PROFILE: Final = "active-routes.v1"
ACTIVE_ROUTE_MAX_CALLS: Final = 6
ACTIVE_ROUTE_MAX_COST_USD: Final = 0.25
FROZEN_CANARY_FIXTURE_SHA256: Final = (
    "cbb8ea061d159d981e9ce612d1a98c57c3452ae1edcbdbd27b1f42ae7f01707c"
)
_CANARY_DATE: Final = datetime.date(2026, 7, 31)
_CANARY_TIME: Final = datetime.datetime(2026, 7, 30, 14, 0, tzinfo=datetime.UTC)
_FIXTURE_NAMESPACE: Final = uuid.UUID("5be499ab-2791-4967-982b-87305c26510d")
_PROVIDERS: Final = ("gemini", "deepseek")
_WORKLOAD_CALL_CEILINGS: Final[Mapping[str, int]] = {
    "entity_adjudication": 2,
    "claim_grounding": 2,
    "analogy_rerank": 2,
    "report_composition": 4,
}
_ACTIVE_ROUTE_CALL_CEILINGS: Final[Mapping[str, int]] = {
    # The two extraction-style T1 probes are intentionally one-shot. The more complex T2
    # workloads receive the remaining allowance, with composition retaining enough room for a
    # schema-validation correction and its separate word-budget retry.
    "entity_adjudication": 1,
    "claim_grounding": 1,
    "analogy_rerank": 1,
    "report_composition": 3,
}
_ACTIVE_ROUTE_PROVIDERS: Final[Mapping[LLMTier, str]] = {
    LLMTier.T1: "gemini",
    LLMTier.T2: "openai",
}


class LLMQualityCanaryError(RuntimeError):
    """The canary cannot safely start or its bounded execution failed."""


class CanaryCallLimitError(LLMQualityCanaryError):
    """A provider invocation was refused before the configured launch ceiling was exceeded."""


class CanaryAxis(StrEnum):
    """Lexicographic gate axes.  An earlier failure cannot be offset by a later success."""

    CONTRACT = "contract"
    SAFETY = "safety"
    SEMANTICS = "semantics"
    QUALITY = "quality"


_AXIS_ORDER: Final = (
    CanaryAxis.CONTRACT,
    CanaryAxis.SAFETY,
    CanaryAxis.SEMANTICS,
    CanaryAxis.QUALITY,
)


@dataclass(frozen=True)
class CanaryCheck:
    name: str
    axis: CanaryAxis
    passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "axis": self.axis.value, "passed": self.passed}


@dataclass(frozen=True)
class CanaryRoute:
    provider: str
    model: str
    tier: LLMTier
    role: str
    thinking_level: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "tier": self.tier.value,
            "role": self.role,
            "thinking_level": self.thinking_level,
        }


@dataclass(frozen=True)
class ScoredBlock:
    """Private scoring shape.  Text is evaluated in memory and never serialized."""

    text: str
    claim_ids: tuple[str, ...]


ProviderBuilder = Callable[..., dict[str, tuple[LLMProviderAdapter, ...]]]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LLMQualityCanaryError(message)


def _finite_nonnegative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def load_canary_cases(path: Path = DEFAULT_CASES_PATH) -> tuple[dict[str, Any], ...]:
    """Load and minimally validate the reviewable synthetic fixture file."""

    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LLMQualityCanaryError(
            "the quality-canary fixture file is unavailable or invalid"
        ) from exc
    _require(isinstance(document, dict), "the quality-canary fixture must be a JSON object")
    _require(document.get("schema") == CASES_SCHEMA, "unexpected quality-canary fixture schema")
    cases = document.get("cases")
    _require(isinstance(cases, list), "the quality-canary fixture needs a cases array")
    _require(len(cases) == 4, "the v1 quality canary requires exactly four workload cases")

    normalized: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    workloads: set[str] = set()
    for value in cases:
        _require(isinstance(value, dict), "every quality-canary case must be an object")
        case_id = value.get("case_id")
        workload = value.get("workload")
        tier = value.get("tier")
        _require(isinstance(case_id, str) and bool(case_id), "every case needs a case_id")
        _require(case_id not in identifiers, "quality-canary case IDs must be unique")
        _require(workload in _WORKLOAD_CALL_CEILINGS, "unknown quality-canary workload")
        expected_tier = "T1" if workload in {"entity_adjudication", "claim_grounding"} else "T2"
        _require(tier == expected_tier, "quality-canary workload has the wrong tier")
        identifiers.add(case_id)
        workloads.add(workload)
        normalized.append(dict(value))

    _require(
        workloads == set(_WORKLOAD_CALL_CEILINGS),
        "the v1 quality canary must cover every production LLM workload exactly once",
    )
    return tuple(normalized)


def fixture_sha256(path: Path = DEFAULT_CASES_PATH) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise LLMQualityCanaryError("the quality-canary fixture file is unavailable") from exc


def _resolve_cases_path(path: Path) -> Path:
    """Normalize caller-supplied paths while keeping path failures inside the safe error type."""

    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise LLMQualityCanaryError("the quality-canary fixture path is invalid") from exc


def _fixture_path_label(path: Path) -> str:
    """Return a useful repository-relative label without exposing arbitrary host paths."""

    try:
        return str(path.relative_to(_REPOSITORY_ROOT))
    except ValueError:
        return "<external-fixture>"


def _require_frozen_canary_fixture(path: Path) -> None:
    """Restrict the active-route profile to the reviewed repository fixture bytes."""

    canonical = _resolve_cases_path(DEFAULT_CASES_PATH)
    _require(
        path == canonical,
        "the active-route canary requires the canonical frozen fixture path",
    )
    _require(
        fixture_sha256(path) == FROZEN_CANARY_FIXTURE_SHA256,
        "the active-route canary fixture does not match the reviewed digest",
    )


def select_canary_cases(
    cases: Sequence[dict[str, Any]], case_ids: Sequence[str] | None = None
) -> tuple[dict[str, Any], ...]:
    if not case_ids:
        return tuple(cases)
    requested = tuple(dict.fromkeys(case_ids))
    by_id = {str(case["case_id"]): case for case in cases}
    missing = [case_id for case_id in requested if case_id not in by_id]
    _require(not missing, "one or more requested quality-canary cases do not exist")
    return tuple(by_id[case_id] for case_id in requested)


def resolve_canary_routes(
    settings: Settings, provider_names: Sequence[str] | None = None
) -> tuple[CanaryRoute, ...]:
    """Resolve Gemini/DeepSeek peers from the actual primary/fallback production routes."""

    requested = tuple(dict.fromkeys(provider_names or _PROVIDERS))
    _require(bool(requested), "at least one canary provider is required")
    _require(set(requested) <= set(_PROVIDERS), "the quality canary supports Gemini and DeepSeek")
    routes: list[CanaryRoute] = []
    for provider in requested:
        for tier in (LLMTier.T1, LLMTier.T2):
            primary = {
                "provider": settings.llm_tier_providers.get(tier.value),
                "model": settings.llm_models.get(tier.value),
                "role": "primary",
            }
            entries = [primary]
            for index, fallback in enumerate(settings.llm_tier_fallbacks.get(tier.value, ())):
                entries.append(
                    {
                        "provider": fallback.get("provider"),
                        "model": fallback.get("model"),
                        "role": f"fallback_{index + 1}",
                    }
                )
            matches = [entry for entry in entries if entry["provider"] == provider]
            _require(
                len(matches) == 1 and bool(matches[0]["model"]),
                f"{provider} must appear exactly once in the configured {tier.value} route",
            )
            routes.append(
                CanaryRoute(
                    provider=provider,
                    model=str(matches[0]["model"]),
                    tier=tier,
                    role=str(matches[0]["role"]),
                    thinking_level=(
                        settings.gemini_model_thinking_levels.get(str(matches[0]["model"]))
                        if provider == "gemini"
                        else None
                    ),
                )
            )
            if provider == "gemini":
                _require(
                    routes[-1].thinking_level is not None,
                    "every routed Gemini model needs an explicit thinking level",
                )
    return tuple(routes)


def resolve_active_canary_routes(settings: Settings) -> tuple[CanaryRoute, ...]:
    """Resolve the two active primary routes covered by the fixed mixed-provider profile."""

    routes: list[CanaryRoute] = []
    for tier in (LLMTier.T1, LLMTier.T2):
        expected_provider = _ACTIVE_ROUTE_PROVIDERS[tier]
        provider = settings.llm_tier_providers.get(tier.value)
        model = settings.llm_models.get(tier.value)
        _require(
            provider == expected_provider,
            f"the active-route canary requires {expected_provider} as the {tier.value} primary",
        )
        _require(isinstance(model, str) and bool(model), "an active canary route has no model")
        if tier is LLMTier.T2:
            _require(model == "gpt-4.1", "the active-route canary requires OpenAI GPT-4.1 at T2")
        thinking_level = (
            settings.gemini_model_thinking_levels.get(model) if provider == "gemini" else None
        )
        if provider == "gemini":
            _require(
                thinking_level is not None,
                "the active Gemini route needs an explicit thinking level",
            )
        routes.append(
            CanaryRoute(
                provider=provider,
                model=model,
                tier=tier,
                role="primary",
                thinking_level=thinking_level,
            )
        )
    return tuple(routes)


def _case_by_workload(cases: Sequence[dict[str, Any]], workload: str) -> dict[str, Any]:
    matches = [case for case in cases if case["workload"] == workload]
    _require(len(matches) == 1, f"expected exactly one {workload} canary case")
    return matches[0]


def _make_claim(raw: Mapping[str, Any], *, case_id: str) -> ClaimContext:
    claim_id = uuid.UUID(str(raw["id"]))
    article_id = uuid.uuid5(_FIXTURE_NAMESPACE, f"{case_id}:{claim_id}")
    excerpt = str(raw["excerpt"])
    _require(len(excerpt) <= 200, "canary evidence excerpts must stay within production bounds")
    return ClaimContext(
        claim_id=claim_id,
        claim_text=str(raw["text"]),
        claim_type="fact",
        claim_confidence=0.95,
        links=(
            EvidenceLink(
                article=EvidenceArticle(
                    article_id=article_id,
                    title="Synthetic quality-canary source",
                    publisher=str(raw["publisher"]),
                    url=str(raw["url"]),
                    published_at=_CANARY_TIME,
                    source_credibility=0.9,
                    excerpt=SourceExcerpt(
                        text=excerpt,
                        origin=ExcerptOrigin.SUMMARY,
                        truncated=False,
                    ),
                ),
                support_type="supports",
                support_confidence=0.95,
            ),
        ),
    )


def build_entity_fixture(case: Mapping[str, Any]) -> tuple[EntityMention, MentionLinkResult]:
    mention_data = case["mention"]
    sentence_text = str(mention_data["context"])
    surface = str(mention_data["surface"])
    start = sentence_text.index(surface)
    sentence = SentenceContext(
        index=0,
        text=sentence_text,
        start_char=0,
        end_char=len(sentence_text),
        previous_text=None,
        next_text=None,
    )
    mention = EntityMention(
        article_key="synthetic-quality-canary-entity",
        text=surface,
        label=EntityLabel.ORG,
        start_char=start,
        end_char=start + len(surface),
        sentence=sentence,
        assertion_status=AssertionStatus.ASSERTED,
    )
    candidates: list[LinkCandidate] = []
    for index, raw in enumerate(case["candidates"]):
        entity_id = uuid.UUID(str(raw["id"]))
        evidence = AliasEvidence(
            entity_id=entity_id,
            alias=surface,
            normalized_alias=surface.casefold(),
            alias_type="short_name",
            source="synthetic-quality-canary",
            prior_multiplier=1.0,
        )
        score = 0.03 - (index * 0.01)
        industries = tuple(part.strip() for part in str(raw["industry"]).split(";") if part.strip())
        candidates.append(
            LinkCandidate(
                entity_id=entity_id,
                canonical_name=str(raw["canonical_name"]),
                entity_type="company",
                country=str(raw["country"]),
                ticker=str(raw["ticker"]),
                industries=industries,
                evidence=evidence,
                alternate_evidence=(),
                redirected_from=(),
                redirect_paths=(),
                signals=(),
                prior_multiplier=1.0,
                signal_score=score,
                score=score,
                band=LinkBand.ADJUDICATE,
            )
        )
    result = MentionLinkResult(
        article_key=mention.article_key,
        surface=mention.text,
        normalized_surface=mention.text.casefold(),
        start_char=mention.start_char,
        end_char=mention.end_char,
        label=mention.label,
        assertion_status=mention.assertion_status,
        band=LinkBand.ADJUDICATE,
        matched_entity_id=None,
        confidence_score=candidates[0].score,
        reason="ambiguous synthetic quality-canary mention",
        run_key=mention_run_key(mention),
        target_id=mention_target_id(mention),
        explanation="band=adjudicate",
        candidates=tuple(candidates),
        rejected=(),
    )
    return mention, result


def build_grounding_fixture(case: Mapping[str, Any]) -> tuple[str, tuple[ClaimContext, ...]]:
    claims = tuple(_make_claim(raw, case_id=str(case["case_id"])) for raw in case["claims"])
    return str(case["block_text"]), claims


def build_analogy_fixture(
    case: Mapping[str, Any],
) -> tuple[SimpleNamespace, tuple[EpisodeCandidate, ...], tuple[str, ...]]:
    raw_event = case["event"]
    event = SimpleNamespace(
        id=uuid.UUID(str(raw_event["id"])),
        title=str(raw_event["title"]),
        summary=str(raw_event["summary"]),
        event_type=str(raw_event["event_type"]),
        country=str(raw_event["country"]),
        region=str(raw_event["region"]),
        first_seen_at=datetime.date.fromisoformat(str(raw_event["first_seen_at"])),
    )
    candidates: list[EpisodeCandidate] = []
    for raw in case["candidates"]:
        candidates.append(
            EpisodeCandidate(
                episode_id=uuid.UUID(str(raw["id"])),
                name=str(raw["name"]),
                episode_type=str(raw["episode_type"]),
                onset_date=datetime.date.fromisoformat(str(raw["onset_date"])),
                peak_date=None,
                end_date=None,
                onset_summary=str(raw["onset_summary"]),
                onset_indicators=dict(raw["onset_indicators"]),
                geography=str(raw["geography"]),
                affected_industries=tuple(str(item) for item in raw["affected_industries"]),
                regime_tags=tuple(str(item) for item in raw["regime_tags"]),
                is_counterexample=False,
                source_refs={"source": "synthetic-quality-canary"},
                parent_episode_id=None,
                similarity=float(raw["similarity"]),
                regime_caveats_required=False,
                regime_caveat_reasons=(),
            )
        )
    return event, tuple(candidates), tuple(str(item) for item in raw_event["regime_tags"])


def build_composition_fixture(
    case: Mapping[str, Any],
) -> tuple[
    SelectedEvent,
    tuple[ClaimContext, ...],
    BriefInputs,
    BriefContext,
    SectionMaterial,
]:
    raw_event = case["event"]
    event = SelectedEvent(
        rank=1,
        event_id=uuid.UUID(str(raw_event["id"])),
        title=str(raw_event["title"]),
        hotness_score=float(raw_event["hotness_score"]),
        max_linked_risk=LinkedRisk(
            score=float(raw_event["max_linked_risk_score"]),
            provenance=RiskProvenance.EVENT_OBSERVATION,
        ),
        ranking_score=float(raw_event["hotness_score"]),
        credibility_sum=1.8,
        developing=bool(raw_event["developing"]),
    )
    claims = tuple(_make_claim(raw, case_id=str(case["case_id"])) for raw in case["claims"])
    evidence = EventEvidenceContext(event_id=event.event_id, claims=claims)
    inputs = BriefInputs(
        window=window_for_date(_CANARY_DATE),
        top_events=(event,),
        executive_summary=ExecutiveSummaryInputs(
            alert_state_changes=(),
            top_events=(event,),
            largest_risk_move=None,
        ),
        risk_radar=RiskRadar(current=(), previous=(), moves=()),
        prior_brief=None,
        data_quality_notes=(),
    )
    context = BriefContext(evidence=(evidence,))
    material = SectionMaterial(
        kind=SectionKind.TOP_EVENT,
        order=1,
        title=event.title,
        claim_ids=tuple(claim.claim_id for claim in claims),
        event=event,
        evidence=evidence,
    )
    return event, claims, inputs, context, material


def _prompt_spec(case: Mapping[str, Any]) -> tuple[str, str, LLMTier, str, str]:
    workload = case["workload"]
    if workload == "entity_adjudication":
        mention, result = build_entity_fixture(case)
        return (
            build_adjudication_prompt(mention, result),
            ADJUDICATION_SCHEMA,
            ADJUDICATION_TIER,
            ADJUDICATION_PROMPT_NAME,
            ADJUDICATION_PROMPT_TEMPLATE_VERSION,
        )
    if workload == "claim_grounding":
        block_text, claims = build_grounding_fixture(case)
        return (
            build_grounding_prompt(block_text=block_text, claims=claims),
            GROUNDING_SCHEMA,
            GROUNDING_TIER,
            GROUNDING_PROMPT_NAME,
            GROUNDING_PROMPT_TEMPLATE_VERSION,
        )
    if workload == "analogy_rerank":
        event, candidates, regime_tags = build_analogy_fixture(case)
        return (
            build_rerank_prompt(event, candidates, current_regime_tags=regime_tags),
            RERANK_SCHEMA,
            RERANK_TIER,
            RERANK_PROMPT_NAME,
            RERANK_PROMPT_TEMPLATE_VERSION,
        )
    if workload == "report_composition":
        event, claims, _inputs, _context, _material = build_composition_fixture(case)
        budget = case["budget"]
        return (
            build_top_event_prompt(
                event=event,
                claims=claims,
                target=int(budget["target"]),
                minimum=int(budget["minimum"]),
                maximum=int(budget["maximum"]),
            ),
            COMPOSITION_SCHEMA,
            LLMTier.T2,
            TOP_EVENT_PROMPT_NAME,
            COMPOSITION_PROMPT_TEMPLATE_VERSION,
        )
    raise LLMQualityCanaryError("unknown quality-canary workload")


def _route_for(routes: Sequence[CanaryRoute], provider: str, tier: LLMTier) -> CanaryRoute:
    matches = [route for route in routes if route.provider == provider and route.tier is tier]
    _require(len(matches) == 1, "quality-canary route resolution is ambiguous")
    return matches[0]


def _active_route_for_tier(routes: Sequence[CanaryRoute], tier: LLMTier) -> CanaryRoute:
    matches = [route for route in routes if route.tier is tier]
    _require(len(matches) == 1, "active quality-canary route resolution is ambiguous")
    return matches[0]


def _validate_price(settings: Settings, route: CanaryRoute) -> None:
    price = settings.llm_provider_token_price_usd_per_1m.get(f"{route.provider}:{route.model}")
    _require(isinstance(price, Mapping), "a canary route has no configured token pricing")
    _require(
        _finite_nonnegative(price.get("input")) and _finite_nonnegative(price.get("output")),
        "a canary route has invalid token pricing",
    )


def preflight_reservation(
    settings: Settings,
    *,
    cases: Sequence[dict[str, Any]],
    routes: Sequence[CanaryRoute],
) -> dict[str, Any]:
    """Reserve every permitted call at production token limits before any client is built."""

    estimated_cost = 0.0
    max_calls = 0
    pairs: list[dict[str, Any]] = []
    providers = tuple(dict.fromkeys(route.provider for route in routes))
    for provider in providers:
        for case in cases:
            prompt, schema, tier, _prompt_name, _template = _prompt_spec(case)
            route = _route_for(routes, provider, tier)
            _validate_price(settings, route)
            schema_text = json.dumps(
                llm_contract_json_schema(schema), sort_keys=True, separators=(",", ":")
            )
            # Mirrors the orchestrator limiter reservation, plus room for one validation-feedback
            # message.  Gemini receives a 2x output reserve because billed thought tokens are
            # reported as output tokens even though they are not visible response prose.
            input_tokens = (
                estimate_token_count(prompt) + (2 * estimate_token_count(schema_text)) + 64 + 512
            )
            output_limit = int(settings.llm_tier_max_output_tokens.get(tier.value, 0))
            _require(output_limit > 0, "a canary tier has no positive output-token ceiling")
            output_tokens = output_limit * (2 if provider == "gemini" else 1)
            calls = _WORKLOAD_CALL_CEILINGS[str(case["workload"])]
            per_call = estimate_completion_cost_usd(
                settings,
                route.provider,
                route.model,
                input_tokens,
                output_tokens,
                LLMInvocationMode.REALTIME,
            )
            cost = per_call * calls
            estimated_cost += cost
            max_calls += calls
            pairs.append(
                {
                    "case_id": case["case_id"],
                    "workload": case["workload"],
                    "provider": provider,
                    "model": route.model,
                    "tier": tier.value,
                    "thinking_level": route.thinking_level,
                    "max_network_calls": calls,
                    "reserved_cost_usd": cost,
                }
            )
    return {
        "max_network_calls": max_calls,
        "reserved_cost_usd": estimated_cost,
        "cost_cap_kind": "conservative_preflight_launch_reservation",
        "pairs": pairs,
    }


def preflight_active_route_reservation(
    settings: Settings,
    *,
    cases: Sequence[dict[str, Any]],
    routes: Sequence[CanaryRoute],
) -> dict[str, Any]:
    """Reserve the fixed case-to-active-tier schedule without a provider cross-product."""

    estimated_cost = 0.0
    max_calls = 0
    pairs: list[dict[str, Any]] = []
    for case in cases:
        prompt, schema, tier, _prompt_name, _template = _prompt_spec(case)
        route = _active_route_for_tier(routes, tier)
        _validate_price(settings, route)
        schema_text = json.dumps(
            llm_contract_json_schema(schema), sort_keys=True, separators=(",", ":")
        )
        input_tokens = (
            estimate_token_count(prompt) + (2 * estimate_token_count(schema_text)) + 64 + 512
        )
        output_limit = int(settings.llm_tier_max_output_tokens.get(tier.value, 0))
        _require(output_limit > 0, "an active canary tier has no positive output-token ceiling")
        output_tokens = output_limit * (2 if route.provider == "gemini" else 1)
        calls = _ACTIVE_ROUTE_CALL_CEILINGS[str(case["workload"])]
        per_call = estimate_completion_cost_usd(
            settings,
            route.provider,
            route.model,
            input_tokens,
            output_tokens,
            LLMInvocationMode.REALTIME,
        )
        cost = per_call * calls
        estimated_cost += cost
        max_calls += calls
        pairs.append(
            {
                "case_id": case["case_id"],
                "workload": case["workload"],
                "provider": route.provider,
                "model": route.model,
                "tier": tier.value,
                "thinking_level": route.thinking_level,
                "max_network_calls": calls,
                "reserved_cost_usd": cost,
            }
        )
    return {
        "max_network_calls": max_calls,
        "reserved_cost_usd": estimated_cost,
        "cost_cap_kind": "preflight_estimate_plus_rolling_hard_cap",
        "pairs": pairs,
    }


def _check(name: str, axis: CanaryAxis, passed: object) -> CanaryCheck:
    return CanaryCheck(name=name, axis=axis, passed=bool(passed))


def check_decision(checks: Sequence[CanaryCheck], *, retried: bool = False) -> str:
    for axis in _AXIS_ORDER:
        if any(check.axis is axis and not check.passed for check in checks):
            return f"failed_{axis.value}"
    return "passed_with_retry" if retried else "passed"


def score_entity_case(
    *, selected_id: str | None, expected_id: str
) -> tuple[list[CanaryCheck], dict[str, Any]]:
    wrong_attachment = selected_id is not None and selected_id != expected_id
    checks = [
        _check("no_wrong_entity_attachment", CanaryAxis.SAFETY, not wrong_attachment),
        _check("expected_entity_selected", CanaryAxis.SEMANTICS, selected_id == expected_id),
    ]
    return checks, {"selected_id": selected_id}


def score_grounding_case(
    *, verdicts: Sequence[tuple[str, str]], expected: Mapping[str, str]
) -> tuple[list[CanaryCheck], dict[str, Any]]:
    returned_ids = [claim_id for claim_id, _verdict in verdicts]
    by_id = {claim_id: verdict for claim_id, verdict in verdicts}
    exact_coverage = len(returned_ids) == len(set(returned_ids)) and set(returned_ids) == set(
        expected
    )
    contradicted_ids = {
        claim_id
        for claim_id, verdict in expected.items()
        if verdict == GroundingVerdict.UNSUPPORTED
    }
    contradiction_safe = exact_coverage and all(
        by_id.get(claim_id) == GroundingVerdict.UNSUPPORTED for claim_id in contradicted_ids
    )
    exact_verdicts = exact_coverage and all(
        by_id.get(key) == value for key, value in expected.items()
    )
    checks = [
        _check("exact_unique_claim_coverage", CanaryAxis.CONTRACT, exact_coverage),
        _check("direct_contradictions_are_unsupported", CanaryAxis.SAFETY, contradiction_safe),
        _check("expected_grounding_verdicts", CanaryAxis.SEMANTICS, exact_verdicts),
    ]
    safe_verdicts = [
        {"claim_id": claim_id, "verdict": verdict} for claim_id, verdict in sorted(verdicts)
    ]
    return checks, {"verdicts": safe_verdicts}


def score_analogy_case(
    *,
    selected_ids: Sequence[str],
    explanations: Sequence[str],
    candidate_ids: Sequence[str],
    expected_ids: Sequence[str],
    matched: bool,
) -> tuple[list[CanaryCheck], dict[str, Any]]:
    combined_explanations = " ".join(explanations).casefold()
    hindsight_claim = bool(
        re.search(
            r"\b(eventually|ultimately|outcome|later collapsed|later failed|ended in|"
            r"resolved through|was bailed out|became insolvent)\b",
            combined_explanations,
        )
    )
    checks = [
        _check(
            "no_foreign_or_duplicate_episode_ids",
            CanaryAxis.CONTRACT,
            len(selected_ids) == len(set(selected_ids)) and set(selected_ids) <= set(candidate_ids),
        ),
        _check(
            "expected_structural_subset",
            CanaryAxis.SEMANTICS,
            list(selected_ids) == list(expected_ids),
        ),
        _check("non_abstaining_clear_match", CanaryAxis.SEMANTICS, matched),
        _check(
            "nonblank_structural_explanations",
            CanaryAxis.QUALITY,
            bool(explanations) and all(value.strip() for value in explanations),
        ),
        _check("no_hindsight_outcome_claims", CanaryAxis.SAFETY, not hindsight_claim),
    ]
    return checks, {"selected_ids": list(selected_ids)}


def _contains_rate_fact(text: str) -> bool:
    lowered = text.casefold()
    rate = "4.75" in lowered
    magnitude = any(
        token in lowered for token in ("25 basis", "quarter-point", "quarter point", "5.00")
    )
    return rate and magnitude


def _contains_inflation_easing(text: str) -> bool:
    lowered = text.casefold()
    return "inflation" in lowered and bool(
        re.search(r"\b(eas\w*|cool\w*|moder\w*|declin\w*|slow\w*)\b", lowered)
    )


def score_composition_case(
    *,
    blocks: Sequence[ScoredBlock],
    claims: Sequence[ClaimContext],
    required_claim_ids: Sequence[str],
    minimum: int,
    maximum: int,
) -> tuple[list[CanaryCheck], dict[str, Any]]:
    allowed_ids = {str(claim.claim_id) for claim in claims}
    required = set(required_claim_ids)
    cited = {claim_id for block in blocks for claim_id in block.claim_ids}
    combined = " ".join(block.text for block in blocks)
    lowered = combined.casefold()
    word_count = count_words(combined)

    rate_claim_id = str(claims[0].claim_id)
    policy_claim_id = str(claims[1].claim_id)
    citation_alignment = True
    copyright_clean = True
    for index, block in enumerate(blocks):
        block_lower = block.text.casefold()
        if any(token in block_lower for token in ("4.75", "5.00", "25 basis", "quarter")):
            citation_alignment = citation_alignment and rate_claim_id in block.claim_ids
        if any(
            token in block_lower
            for token in ("inflation", "restrict", "incoming data", "data-dependent")
        ):
            citation_alignment = citation_alignment and policy_claim_id in block.claim_ids
        snippets: list[SnippetSource] = []
        for claim in claims:
            if str(claim.claim_id) not in block.claim_ids:
                continue
            for link in claim.links:
                article = link.article
                snippets.append(
                    SnippetSource(
                        article_id=article.article_id,
                        publisher=article.publisher,
                        url=article.url,
                        text=article.excerpt.text,
                    )
                )
        copyright_clean = copyright_clean and not check_block_copyright(
            block_index=index,
            block_text=block.text,
            snippets=snippets,
        )

    policy_restrictive = "restrict" in lowered
    policy_conditional = "data" in lowered and bool(
        re.search(r"\b(depend\w*|conditional|incoming)\b", lowered)
    )
    forbidden_inflation_number = bool(re.search(r"\b(?:2\.0|3\.2)\s*(?:%|percent)", lowered))
    checks = [
        _check("only_whitelisted_claim_ids", CanaryAxis.CONTRACT, cited <= allowed_ids),
        _check("fact_to_claim_citations_align", CanaryAxis.SAFETY, citation_alignment),
        _check("copyright_clean", CanaryAxis.SAFETY, copyright_clean),
        _check(
            "no_unsupported_inflation_number", CanaryAxis.SAFETY, not forbidden_inflation_number
        ),
        _check("non_abstaining_composition", CanaryAxis.SEMANTICS, bool(blocks)),
        _check("all_required_claims_cited", CanaryAxis.SEMANTICS, required <= cited),
        _check("rate_decision_fact_present", CanaryAxis.SEMANTICS, _contains_rate_fact(combined)),
        _check(
            "inflation_easing_present", CanaryAxis.SEMANTICS, _contains_inflation_easing(combined)
        ),
        _check("restrictive_policy_present", CanaryAxis.SEMANTICS, policy_restrictive),
        _check("data_dependent_guidance_present", CanaryAxis.SEMANTICS, policy_conditional),
        _check("section_word_budget", CanaryAxis.QUALITY, minimum <= word_count <= maximum),
        _check(
            "nonblank_blocks",
            CanaryAxis.QUALITY,
            bool(blocks) and all(block.text.strip() for block in blocks),
        ),
    ]
    return checks, {"word_count": word_count, "cited_claim_ids": sorted(cited)}


def _execution_checks(
    runs: Sequence[Any],
    *,
    route: CanaryRoute,
    schema: str,
    prompt_name: str,
    prompt_template_version: str,
) -> list[CanaryCheck]:
    successful = [run for run in runs if run.status in {"succeeded", "cached"}]
    terminal = successful[-1] if successful else None
    envelope_matches = bool(
        terminal is not None
        and terminal.output_schema_name == schema
        and terminal.output_schema_version == "1.0"
        and terminal.output.get("prompt_template_version") == prompt_template_version
    )
    return [
        _check("terminal_contract_valid", CanaryAxis.CONTRACT, terminal is not None),
        _check("contract_envelope_matches", CanaryAxis.CONTRACT, envelope_matches),
        _check(
            "provider_model_attribution",
            CanaryAxis.CONTRACT,
            bool(runs)
            and all(run.provider == route.provider and run.model == route.model for run in runs),
        ),
        _check(
            "production_prompt_identity",
            CanaryAxis.CONTRACT,
            bool(runs)
            and all(
                run.prompt_name == prompt_name
                and run.prompt_template_version == prompt_template_version
                for run in runs
            ),
        ),
        _check(
            "no_cache_or_fallback",
            CanaryAxis.CONTRACT,
            bool(runs)
            and all(
                run.status != "cached" and run.model_params.get("degraded_provider") is None
                for run in runs
            ),
        ),
    ]


def _safe_error(exc: BaseException) -> dict[str, Any]:
    status: int | None = None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        candidate = getattr(response, "status_code", None)
        if isinstance(candidate, int):
            status = candidate
            break
        current = current.__cause__ if isinstance(current.__cause__, BaseException) else None
    return {"type": type(exc).__name__, "http_status": status}


def _run_metrics(
    runs: Sequence[Any], *, network_calls: int, workload_attempts: int
) -> dict[str, Any]:
    return {
        "network_calls": network_calls,
        "workload_attempts": workload_attempts,
        "validation_retries": sum(run.status == "validation_failed" for run in runs),
        "input_tokens": sum(int(run.input_tokens or 0) for run in runs),
        "output_tokens": sum(int(run.output_tokens or 0) for run in runs),
        "latency_ms": sum(int(run.latency_ms or 0) for run in runs),
        "estimated_cost_usd": sum(float(run.cost_usd or 0.0) for run in runs),
    }


class _CallGuard:
    """Shared launch ceiling.  It retains counts and case IDs, never prompts or responses."""

    def __init__(self, max_total: int) -> None:
        self.max_total = max_total
        self.total_calls = 0
        self._case_id: str | None = None
        self._case_limit = 0
        self._case_calls = 0

    def begin_case(self, case_id: str, limit: int) -> None:
        _require(self._case_id is None, "a canary call-budget scope is already active")
        self._case_id = case_id
        self._case_limit = limit
        self._case_calls = 0

    def end_case(self) -> int:
        calls = self._case_calls
        self._case_id = None
        self._case_limit = 0
        self._case_calls = 0
        return calls

    def before_network_call(self) -> None:
        if self._case_id is None:
            raise CanaryCallLimitError("provider invocation attempted outside a canary case")
        if self.total_calls >= self.max_total or self._case_calls >= self._case_limit:
            raise CanaryCallLimitError("quality-canary network-call ceiling reached")
        self.total_calls += 1
        self._case_calls += 1


class _GuardedProvider:
    """Adapter decorator enforcing the shared call ceiling and idempotent close."""

    def __init__(self, provider: LLMProviderAdapter, guard: _CallGuard) -> None:
        self._provider = provider
        self._guard = guard
        self.provider_name = provider.provider_name
        self.model_name = provider.model_name
        self.model_version = provider.model_version
        self._closed = False

    def supports_mode(self, mode: LLMInvocationMode) -> bool:
        return self._provider.supports_mode(mode)

    def supports_structured_schema(self, schema_name: str) -> bool:
        return self._provider.supports_structured_schema(schema_name)

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        self._guard.before_network_call()
        return self._provider.invoke(request)

    def invoke_batch(
        self, requests: Sequence[LLMInvocationRequest]
    ) -> Sequence[LLMInvocationResponse]:
        for _request in requests:
            self._guard.before_network_call()
        return self._provider.invoke_batch(requests)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._provider, "close", None)
        if callable(close):
            close()


def _isolated_settings(settings: Settings, routes: Sequence[CanaryRoute]) -> Settings:
    models = dict(settings.llm_models)
    providers = dict(settings.llm_tier_providers)
    for route in routes:
        models[route.tier.value] = route.model
        providers[route.tier.value] = route.provider
    return settings.model_copy(
        update={
            "llm_models": models,
            "llm_tier_providers": providers,
            "llm_tier_fallbacks": {},
            # The in-memory repository starts at zero spend, but disabling degradation makes the
            # attribution guarantee independent of an operator's monthly-budget override.
            "llm_budget_enforced": False,
        }
    )


def _run_entity(
    orchestrator: LLMOrchestrator, case: Mapping[str, Any]
) -> tuple[list[CanaryCheck], dict[str, Any], int]:
    mention, result = build_entity_fixture(case)
    outcome = MentionAdjudicator(orchestrator).adjudicate(mention, result)
    selected = str(outcome.entity_id) if outcome.entity_id is not None else None
    checks, observations = score_entity_case(
        selected_id=selected,
        expected_id=str(case["expected"]["selected_id"]),
    )
    checks.append(
        _check(
            "adjudication_domain_result",
            CanaryAxis.CONTRACT,
            outcome.decision is not AdjudicationDecision.FAILED,
        )
    )
    return checks, observations, 1


def _run_grounding(
    orchestrator: LLMOrchestrator, case: Mapping[str, Any]
) -> tuple[list[CanaryCheck], dict[str, Any], int]:
    block_text, claims = build_grounding_fixture(case)
    allowed_ids = tuple(str(claim.claim_id) for claim in claims)
    outcome = orchestrator.run(
        LLMOrchestratorRequest(
            job=grounding_job(_CANARY_DATE),
            prompt_name=GROUNDING_PROMPT_NAME,
            prompt_version=GROUNDING_PROMPT_VERSION,
            prompt_template_version=GROUNDING_PROMPT_TEMPLATE_VERSION,
            requested_schema=GROUNDING_SCHEMA,
            prompt=build_grounding_prompt(block_text=block_text, claims=claims),
            requested_tier=GROUNDING_TIER,
            risk_level=GROUNDING_RISK_LEVEL,
            current_event_hotness=0.0,
            trailing_7d_p90_hotness=None,
            is_realtime=True,
            is_essential=True,
            articles=(),
            allowed_ids=allowed_ids,
            temperature=GROUNDING_TEMPERATURE,
            context={
                "brief_date": _CANARY_DATE.isoformat(),
                "section_kind": SectionKind.TOP_EVENT.value,
                "block_index": 0,
                "grounding_round": 1,
                "allowed_claim_ids": list(allowed_ids),
            },
        )
    )
    contract = outcome.contract
    if not isinstance(contract, ClaimGrounding):
        raise LLMQualityCanaryError("grounding returned the wrong validated contract type")
    verdicts = [(item.claim_id, item.verdict.value) for item in contract.verdicts]
    checks, observations = score_grounding_case(
        verdicts=verdicts,
        expected={str(key): str(value) for key, value in case["expected"]["verdicts"].items()},
    )
    return checks, observations, 1


def _run_analogy(
    orchestrator: LLMOrchestrator, case: Mapping[str, Any]
) -> tuple[list[CanaryCheck], dict[str, Any], int]:
    event, candidates, regime_tags = build_analogy_fixture(case)
    result = rerank_candidates(
        orchestrator,
        event=event,
        candidates=candidates,
        current_regime_tags=regime_tags,
    )
    selected_ids = [str(selection.episode_id) for selection in result.selections]
    explanations = [selection.explanation for selection in result.selections]
    checks, observations = score_analogy_case(
        selected_ids=selected_ids,
        explanations=explanations,
        candidate_ids=[str(candidate.episode_id) for candidate in candidates],
        expected_ids=[str(item) for item in case["expected"]["selected_ids"]],
        matched=result.status is RerankStatus.MATCHED,
    )
    return checks, observations, 1


def _run_composition(
    orchestrator: LLMOrchestrator, case: Mapping[str, Any]
) -> tuple[list[CanaryCheck], dict[str, Any], int]:
    _event, claims, inputs, context, material = build_composition_fixture(case)
    section = compose_section(
        orchestrator,
        inputs=inputs,
        context=context,
        material=material,
    )
    blocks = tuple(
        ScoredBlock(
            text=block.text,
            claim_ids=tuple(str(claim_id) for claim_id in block.claim_ids),
        )
        for block in section.blocks
    )
    budget = case["budget"]
    checks, observations = score_composition_case(
        blocks=blocks,
        claims=claims,
        required_claim_ids=[str(value) for value in case["expected"]["required_claim_ids"]],
        minimum=int(budget["minimum"]),
        maximum=int(budget["maximum"]),
    )
    observations["attempt_word_counts"] = [attempt.word_count for attempt in section.attempts]
    observations["degradation_codes"] = [
        getattr(degradation.code, "value", str(degradation.code))
        for degradation in section.degradations
    ]
    composition_failed = any(
        getattr(degradation.code, "value", "") == "composition_failed"
        for degradation in section.degradations
    )
    checks.append(_check("composition_domain_mapping", CanaryAxis.CONTRACT, not composition_failed))
    return checks, observations, len(section.attempts)


_RUNNERS: Final[Mapping[str, Callable[..., tuple[list[CanaryCheck], dict[str, Any], int]]]] = {
    "entity_adjudication": _run_entity,
    "claim_grounding": _run_grounding,
    "analogy_rerank": _run_analogy,
    "report_composition": _run_composition,
}


def _case_result(
    orchestrator: LLMOrchestrator,
    repository: InMemoryLLMRuntimeRepository,
    guard: Any,
    *,
    route: CanaryRoute,
    case: Mapping[str, Any],
    call_limit: int | None = None,
) -> dict[str, Any]:
    start = len(repository.llm_runs)
    resolved_call_limit = (
        _WORKLOAD_CALL_CEILINGS[str(case["workload"])] if call_limit is None else call_limit
    )
    guard.begin_case(str(case["case_id"]), resolved_call_limit)
    error: dict[str, Any] | None = None
    observations: dict[str, Any] = {}
    workload_attempts = 0
    checks: list[CanaryCheck] = []
    try:
        runner = _RUNNERS[str(case["workload"])]
        checks, observations, workload_attempts = runner(orchestrator, case)
    except Exception as exc:  # noqa: BLE001 - report only class/status, never its message
        error = _safe_error(exc)
    finally:
        network_calls = guard.end_case()

    runs = repository.llm_runs[start:]
    _prompt, schema, _tier, prompt_name, prompt_template_version = _prompt_spec(case)
    checks = (
        _execution_checks(
            runs,
            route=route,
            schema=schema,
            prompt_name=prompt_name,
            prompt_template_version=prompt_template_version,
        )
        + checks
    )
    if error is not None:
        checks.append(_check("runner_completed", CanaryAxis.CONTRACT, False))
    retried = network_calls > 1 or workload_attempts > 1
    result = {
        "case_id": case["case_id"],
        "workload": case["workload"],
        "provider": route.provider,
        "model": route.model,
        "tier": route.tier.value,
        "route_role": route.role,
        "thinking_level": route.thinking_level,
        "status": check_decision(checks, retried=retried),
        "checks": [check.as_dict() for check in checks],
        "observations": observations,
        "metrics": _run_metrics(
            runs, network_calls=network_calls, workload_attempts=workload_attempts
        ),
    }
    if error is not None:
        result["failure"] = error
    return result


def _provider_key_is_present(settings: Settings, provider: str) -> bool:
    if provider == "openai":
        return bool(settings.openai_api_key)
    if provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    if provider == "deepseek":
        return bool(settings.deepseek_api_key)
    return False


def _aggregate(results: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    summaries: list[dict[str, Any]] = []
    for provider in dict.fromkeys(str(result["provider"]) for result in results):
        provider_results = [result for result in results if result["provider"] == provider]
        workloads = {str(result["workload"]) for result in provider_results}
        full_workload_coverage = len(provider_results) == len(
            _WORKLOAD_CALL_CEILINGS
        ) and workloads == set(_WORKLOAD_CALL_CEILINGS)
        passed = all(
            result["status"] in {"passed", "passed_with_retry"} for result in provider_results
        )
        summaries.append(
            {
                "provider": provider,
                "passed": passed,
                "full_workload_coverage": full_workload_coverage,
                "case_count": len(provider_results),
                "network_calls": sum(
                    result["metrics"]["network_calls"] for result in provider_results
                ),
                "estimated_cost_usd": sum(
                    result["metrics"]["estimated_cost_usd"] for result in provider_results
                ),
            }
        )
    if summaries and not all(item["full_workload_coverage"] for item in summaries):
        decision = "diagnostic_only"
    elif summaries and all(item["passed"] for item in summaries):
        decision = "advance_to_shadow"
    else:
        decision = "hold"
    return summaries, decision


def _active_public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Strip all model-derived observations and retain only fixed diagnostic dimensions."""

    checks = result.get("checks")
    safe_checks = checks if isinstance(checks, list) else []
    failed_checks = [
        {"name": str(check.get("name")), "axis": str(check.get("axis"))}
        for check in safe_checks
        if isinstance(check, Mapping) and check.get("passed") is False
    ]
    metrics = result.get("metrics")
    safe_metrics = metrics if isinstance(metrics, Mapping) else {}
    public = {
        "workload": str(result["workload"]),
        "provider": str(result["provider"]),
        "model": str(result["model"]),
        "tier": str(result["tier"]),
        "route_role": str(result["route_role"]),
        "status": str(result["status"]),
        "failed_checks": failed_checks,
        "metrics": {
            "network_calls": int(safe_metrics.get("network_calls", 0)),
            "workload_attempts": int(safe_metrics.get("workload_attempts", 0)),
            "validation_retries": int(safe_metrics.get("validation_retries", 0)),
            "input_tokens": int(safe_metrics.get("input_tokens", 0)),
            "output_tokens": int(safe_metrics.get("output_tokens", 0)),
            "latency_ms": int(safe_metrics.get("latency_ms", 0)),
            "estimated_cost_usd": float(safe_metrics.get("estimated_cost_usd", 0.0)),
        },
    }
    failure = result.get("failure")
    if isinstance(failure, Mapping):
        status = failure.get("http_status")
        public["failure"] = {
            "category": "execution_failure",
            "http_status": status if isinstance(status, int) else None,
        }
    return public


def _aggregate_active_results(
    results: Sequence[dict[str, Any]],
    *,
    routes: Sequence[CanaryRoute],
) -> dict[str, Any]:
    status_counts = Counter(str(result["status"]) for result in results)
    failed_check_counts: Counter[str] = Counter()
    failure_cases_by_axis: Counter[str] = Counter()
    for result in results:
        failed_axes: set[str] = set()
        for check in result.get("checks", ()):
            if not isinstance(check, Mapping) or check.get("passed") is not False:
                continue
            failed_check_counts[str(check.get("name"))] += 1
            failed_axes.add(str(check.get("axis")))
        failure_cases_by_axis.update(failed_axes)

    expected_workloads = set(_ACTIVE_ROUTE_CALL_CEILINGS)
    returned_workloads = {str(result["workload"]) for result in results}
    complete_profile = (
        len(results) == len(expected_workloads) and returned_workloads == expected_workloads
    )
    route_summaries: list[dict[str, Any]] = []
    for route in routes:
        route_results = [result for result in results if result["tier"] == route.tier.value]
        route_summaries.append(
            {
                "provider": route.provider,
                "model": route.model,
                "tier": route.tier.value,
                "passed": bool(route_results)
                and all(
                    result["status"] in {"passed", "passed_with_retry"} for result in route_results
                ),
                "workload_count": len(route_results),
                "network_calls": sum(
                    int(result["metrics"]["network_calls"]) for result in route_results
                ),
                "estimated_cost_usd": sum(
                    float(result["metrics"]["estimated_cost_usd"]) for result in route_results
                ),
            }
        )
    passed = complete_profile and all(
        result["status"] in {"passed", "passed_with_retry"} for result in results
    )
    return {
        "complete_profile": complete_profile,
        "passed": passed,
        "status_counts": dict(sorted(status_counts.items())),
        "failure_cases_by_axis": dict(sorted(failure_cases_by_axis.items())),
        "failed_check_cases": dict(sorted(failed_check_counts.items())),
        "route_summaries": route_summaries,
    }


def _close_unowned_adapters(adapters: Sequence[LLMProviderAdapter]) -> int:
    failures = 0
    seen: set[int] = set()
    for adapter in adapters:
        if id(adapter) in seen:
            continue
        seen.add(id(adapter))
        close = getattr(adapter, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception:  # noqa: BLE001 - active report retains a count only
            failures += 1
    return failures


def build_active_route_canary_plan(
    settings: Settings,
    *,
    max_calls: int = ACTIVE_ROUTE_MAX_CALLS,
    max_cost_usd: float = ACTIVE_ROUTE_MAX_COST_USD,
    cases_path: Path = DEFAULT_CASES_PATH,
) -> dict[str, Any]:
    """Plan the fixed Gemini-T1/OpenAI-T2 profile without clients, network, or writes."""

    cases_path = _resolve_cases_path(cases_path)
    _require_frozen_canary_fixture(cases_path)
    _require(
        isinstance(max_calls, int) and not isinstance(max_calls, bool) and max_calls > 0,
        "max_calls must be a positive integer",
    )
    _require(_finite_nonnegative(max_cost_usd), "max_cost_usd must be finite and nonnegative")
    _require(
        max_calls <= ACTIVE_ROUTE_MAX_CALLS,
        "max_calls exceeds the approved active-route ceiling",
    )
    _require(
        float(max_cost_usd) <= ACTIVE_ROUTE_MAX_COST_USD,
        "max_cost_usd exceeds the approved active-route ceiling",
    )
    cases = load_canary_cases(cases_path)
    routes = resolve_active_canary_routes(settings)
    reservation = preflight_active_route_reservation(settings, cases=cases, routes=routes)
    _require(
        reservation["max_network_calls"] <= max_calls,
        "active-route call ceiling is lower than the fixed profile reservation",
    )
    _require(
        reservation["reserved_cost_usd"] <= float(max_cost_usd),
        "active-route cost ceiling is lower than the fixed profile reservation",
    )
    return {
        "schema": ACTIVE_ROUTE_REPORT_SCHEMA,
        "mode": "active_route_plan",
        "profile": ACTIVE_ROUTE_PROFILE,
        "fixture": {
            "path": _fixture_path_label(cases_path),
            "sha256": fixture_sha256(cases_path),
            "schema": CASES_SCHEMA,
        },
        "routes": [route.as_dict() for route in routes],
        "workloads": [str(case["workload"]) for case in cases],
        "guardrails": {
            "configured_max_network_calls": max_calls,
            "configured_max_cost_usd": float(max_cost_usd),
            **reservation,
        },
        "external_io": {
            "network": False,
            "database": False,
            "redis": False,
            "broker": False,
            "cache": False,
            "files_written": False,
        },
        "promotion_eligible": False,
        "next_action": "rerun_active_route_profile_with_live_confirmation",
    }


def run_active_route_canary(
    settings: Settings,
    *,
    max_calls: int = ACTIVE_ROUTE_MAX_CALLS,
    max_cost_usd: float = ACTIVE_ROUTE_MAX_COST_USD,
    cases_path: Path = DEFAULT_CASES_PATH,
    provider_builder: ProviderBuilder = build_providers_by_tier,
) -> dict[str, Any]:
    """Run the fixed active primary routes under hard rolling call and cost ceilings."""

    cases_path = _resolve_cases_path(cases_path)
    plan = build_active_route_canary_plan(
        settings,
        max_calls=max_calls,
        max_cost_usd=max_cost_usd,
        cases_path=cases_path,
    )
    cases = load_canary_cases(cases_path)
    routes = resolve_active_canary_routes(settings)
    missing = [
        route.provider for route in routes if not _provider_key_is_present(settings, route.provider)
    ]
    _require(not missing, "one or more active-route provider API keys are not configured")

    guard = ShadowCostGuard(
        settings,
        max_calls=max_calls,
        max_cost_usd=max_cost_usd,
        approved_routes_only=False,
    )
    isolated = _isolated_settings(settings, routes)
    repository = InMemoryLLMRuntimeRepository()
    results: list[dict[str, Any]] = []
    raw_adapters: list[LLMProviderAdapter] = []
    orchestrator: LLMOrchestrator | None = None
    cleanup_failure_count = 0
    execution_status = "complete"
    try:
        built = provider_builder(isolated, tiers=(LLMTier.T1, LLMTier.T2))
        for tier in (LLMTier.T1, LLMTier.T2):
            raw_adapters.extend(built.get(tier.value, ()))
        guarded: dict[str, tuple[LLMProviderAdapter, ...]] = {}
        for tier in (LLMTier.T1, LLMTier.T2):
            adapters = built.get(tier.value, ())
            _require(len(adapters) == 1, "active canary tier must have exactly one provider")
            route = _active_route_for_tier(routes, tier)
            adapter = adapters[0]
            _require(
                adapter.provider_name == route.provider and adapter.model_name == route.model,
                "active canary adapter does not match the resolved primary route",
            )
            guarded[tier.value] = (
                guard.wrap(
                    adapter,
                    max_output_tokens=int(settings.llm_tier_max_output_tokens[tier.value]),
                ),
            )
        orchestrator = LLMOrchestrator(
            isolated,
            repository,
            guarded,
            cache=None,
            limiter=None,
        )
        for case in cases:
            tier = LLMTier(str(case["tier"]))
            route = _active_route_for_tier(routes, tier)
            try:
                results.append(
                    _case_result(
                        orchestrator,
                        repository,
                        guard,
                        route=route,
                        case=case,
                        call_limit=_ACTIVE_ROUTE_CALL_CEILINGS[str(case["workload"])],
                    )
                )
            finally:
                # Audit rows can contain prompt inputs and provider outputs. They are needed only
                # while reducing this one case to its safe scorecard and counters.
                repository.llm_runs.clear()
                repository.jobs.clear()
            if guard.halted:
                execution_status = "guard_stopped"
                break
    except Exception:  # noqa: BLE001 - only a fixed aggregate failure is published
        execution_status = "setup_or_evaluation_failed"
    finally:
        repository.llm_runs.clear()
        repository.jobs.clear()
        if orchestrator is not None:
            try:
                orchestrator.close()
            except Exception:  # noqa: BLE001 - active report retains a count only
                cleanup_failure_count += 1
        else:
            cleanup_failure_count += _close_unowned_adapters(raw_adapters)

    aggregation = _aggregate_active_results(results, routes=routes)
    cost_ledger = guard.snapshot()
    passed = (
        aggregation["passed"]
        and execution_status == "complete"
        and cleanup_failure_count == 0
        and not bool(cost_ledger["halted"])
    )
    return {
        "schema": ACTIVE_ROUTE_REPORT_SCHEMA,
        "mode": "active_route_live",
        "profile": ACTIVE_ROUTE_PROFILE,
        "fixture": plan["fixture"],
        "routes": plan["routes"],
        "guardrails": {**plan["guardrails"], "cost_ledger": cost_ledger},
        "external_io": {
            "network": guard.total_calls > 0,
            "database": False,
            "redis": False,
            "broker": False,
            "cache": False,
            "files_written": False,
        },
        "execution_status": execution_status,
        "cleanup_failure_count": cleanup_failure_count,
        "results": [_active_public_result(result) for result in results],
        **aggregation,
        "passed": passed,
        "decision": "pass" if passed else "hold",
        "decision_scope": "active_route_canary_only",
        "promotion_eligible": False,
    }


def build_canary_plan(
    settings: Settings,
    *,
    provider_names: Sequence[str] | None = None,
    case_ids: Sequence[str] | None = None,
    max_calls: int = DEFAULT_MAX_CALLS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    cases_path: Path = DEFAULT_CASES_PATH,
) -> dict[str, Any]:
    """Build a no-network/no-write execution plan and enforce launch ceilings."""

    cases_path = _resolve_cases_path(cases_path)
    _require(
        isinstance(max_calls, int) and not isinstance(max_calls, bool) and max_calls > 0,
        "max_calls must be a positive integer",
    )
    _require(_finite_nonnegative(max_cost_usd), "max_cost_usd must be finite and nonnegative")
    cases = select_canary_cases(load_canary_cases(cases_path), case_ids)
    routes = resolve_canary_routes(settings, provider_names)
    reservation = preflight_reservation(settings, cases=cases, routes=routes)
    _require(
        reservation["max_network_calls"] <= max_calls,
        "quality-canary call ceiling is lower than the required worst-case reservation",
    )
    _require(
        reservation["reserved_cost_usd"] <= float(max_cost_usd),
        "quality-canary cost ceiling is lower than the required preflight reservation",
    )
    return {
        "schema": REPORT_SCHEMA,
        "mode": "plan",
        "fixture": {
            "path": _fixture_path_label(cases_path),
            "sha256": fixture_sha256(cases_path),
            "schema": CASES_SCHEMA,
        },
        "routes": [route.as_dict() for route in routes],
        "case_ids": [case["case_id"] for case in cases],
        "guardrails": {
            "configured_max_network_calls": max_calls,
            "configured_max_cost_usd": float(max_cost_usd),
            **reservation,
        },
        "external_io": {
            "network": False,
            "database": False,
            "redis": False,
            "broker": False,
            "cache": False,
            "files_written": False,
        },
        "next_action": "rerun_with_live_confirmation",
    }


def run_quality_canary(
    settings: Settings,
    *,
    provider_names: Sequence[str] | None = None,
    case_ids: Sequence[str] | None = None,
    max_calls: int = DEFAULT_MAX_CALLS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    cases_path: Path = DEFAULT_CASES_PATH,
    provider_builder: ProviderBuilder = build_providers_by_tier,
) -> dict[str, Any]:
    """Run the bounded live matrix and return a strict sanitized report."""

    cases_path = _resolve_cases_path(cases_path)
    plan = build_canary_plan(
        settings,
        provider_names=provider_names,
        case_ids=case_ids,
        max_calls=max_calls,
        max_cost_usd=max_cost_usd,
        cases_path=cases_path,
    )
    cases = select_canary_cases(load_canary_cases(cases_path), case_ids)
    routes = resolve_canary_routes(settings, provider_names)
    providers = tuple(dict.fromkeys(route.provider for route in routes))
    missing = [
        provider for provider in providers if not _provider_key_is_present(settings, provider)
    ]
    _require(not missing, "one or more selected provider API keys are not configured")

    guard = _CallGuard(max_calls)
    results: list[dict[str, Any]] = []
    cleanup_failures: list[dict[str, Any]] = []
    for provider in providers:
        family_routes = tuple(route for route in routes if route.provider == provider)
        selected_tiers = tuple(
            tier
            for tier in (LLMTier.T1, LLMTier.T2)
            if any(case["tier"] == tier.value for case in cases)
        )
        isolated = _isolated_settings(settings, family_routes)
        orchestrator: LLMOrchestrator | None = None
        try:
            built = provider_builder(isolated, tiers=selected_tiers)
            guarded: dict[str, tuple[LLMProviderAdapter, ...]] = {}
            for tier in selected_tiers:
                adapters = built.get(tier.value, ())
                _require(len(adapters) == 1, "isolated canary tier must have exactly one provider")
                guarded[tier.value] = tuple(
                    _GuardedProvider(adapter, guard) for adapter in adapters
                )
            repository = InMemoryLLMRuntimeRepository()
            orchestrator = LLMOrchestrator(
                isolated,
                repository,
                guarded,
                cache=None,
                limiter=None,
            )
            for case in cases:
                tier = LLMTier(str(case["tier"]))
                route = _route_for(family_routes, provider, tier)
                results.append(
                    _case_result(
                        orchestrator,
                        repository,
                        guard,
                        route=route,
                        case=case,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - only safe class/status is retained
            # Construction failures have no case response to score.  Add one sanitized failure per
            # still-missing provider/case pair so aggregation cannot accidentally pass.
            completed = {(result["provider"], result["case_id"]) for result in results}
            for case in cases:
                key = (provider, str(case["case_id"]))
                if key in completed:
                    continue
                tier = LLMTier(str(case["tier"]))
                route = _route_for(family_routes, provider, tier)
                results.append(
                    {
                        "case_id": case["case_id"],
                        "workload": case["workload"],
                        "provider": provider,
                        "model": route.model,
                        "tier": route.tier.value,
                        "route_role": route.role,
                        "thinking_level": route.thinking_level,
                        "status": "failed_contract",
                        "checks": [
                            _check("runner_completed", CanaryAxis.CONTRACT, False).as_dict()
                        ],
                        "observations": {},
                        "metrics": _run_metrics((), network_calls=0, workload_attempts=0),
                        "failure": _safe_error(exc),
                    }
                )
        finally:
            if orchestrator is not None:
                try:
                    orchestrator.close()
                except Exception as exc:  # noqa: BLE001
                    cleanup_failures.append({"provider": provider, "failure": _safe_error(exc)})

    summaries, decision = _aggregate(results)
    actual_cost = sum(float(result["metrics"]["estimated_cost_usd"]) for result in results)
    report = {
        "schema": REPORT_SCHEMA,
        "mode": "live",
        "fixture": plan["fixture"],
        "guardrails": plan["guardrails"],
        "external_io": {
            "network": True,
            "database": False,
            "redis": False,
            "broker": False,
            "cache": False,
            "files_written": False,
        },
        "results": results,
        "provider_summaries": summaries,
        "totals": {
            "case_provider_pairs": len(results),
            "network_calls": guard.total_calls,
            "input_tokens": sum(result["metrics"]["input_tokens"] for result in results),
            "output_tokens": sum(result["metrics"]["output_tokens"] for result in results),
            "latency_ms": sum(result["metrics"]["latency_ms"] for result in results),
            "estimated_cost_usd": actual_cost,
        },
        "cleanup_failures": cleanup_failures,
        "decision": "hold" if cleanup_failures else decision,
        "decision_scope": (
            "partial_diagnostic_only"
            if not cleanup_failures and decision == "diagnostic_only"
            else "shadow_evaluation_only"
        ),
    }
    return report


__all__ = [
    "ACTIVE_ROUTE_MAX_CALLS",
    "ACTIVE_ROUTE_MAX_COST_USD",
    "ACTIVE_ROUTE_PROFILE",
    "ACTIVE_ROUTE_REPORT_SCHEMA",
    "CASES_SCHEMA",
    "DEFAULT_CASES_PATH",
    "DEFAULT_MAX_CALLS",
    "DEFAULT_MAX_COST_USD",
    "FROZEN_CANARY_FIXTURE_SHA256",
    "REPORT_SCHEMA",
    "CanaryAxis",
    "CanaryCheck",
    "LLMQualityCanaryError",
    "ScoredBlock",
    "build_active_route_canary_plan",
    "build_canary_plan",
    "build_composition_fixture",
    "build_entity_fixture",
    "build_grounding_fixture",
    "build_analogy_fixture",
    "check_decision",
    "fixture_sha256",
    "load_canary_cases",
    "preflight_reservation",
    "preflight_active_route_reservation",
    "resolve_active_canary_routes",
    "resolve_canary_routes",
    "run_active_route_canary",
    "run_quality_canary",
    "score_analogy_case",
    "score_composition_case",
    "score_entity_case",
    "score_grounding_case",
    "select_canary_cases",
]
