"""Bounded, synthetic shadow evaluation for the configured Gemini/DeepSeek routes.

The evaluator executes production prompt builders, contracts, and domain mappers without
publishing or persisting their outputs.  Live execution is deliberately sequential, has no
fallback or cache, and is admitted one provider request at a time by :mod:`llm_shadow_cost`.
The returned report is aggregate-only: prompts, generated prose, evidence, expected UUIDs,
provider payloads, and exception messages never cross this module's public boundary.
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
from dataclasses import replace
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from packages.config.settings import Settings
from services.analogies.rerank import (
    RERANK_PROMPT_NAME,
    RERANK_PROMPT_TEMPLATE_VERSION,
    RERANK_SCHEMA,
    AnalogyRerankError,
    RegimeCaveatMissingError,
    RerankStatus,
    rerank_candidates,
)
from services.entities.adjudication import (
    ADJUDICATION_PROMPT_NAME,
    ADJUDICATION_PROMPT_TEMPLATE_VERSION,
    ADJUDICATION_SCHEMA,
    AdjudicationDecision,
    MentionAdjudicator,
)
from services.evaluation.llm_quality_canary import (
    CanaryAxis,
    CanaryCheck,
    CanaryRoute,
    ScoredBlock,
    build_analogy_fixture,
    build_composition_fixture,
    build_entity_fixture,
    build_grounding_fixture,
    check_decision,
    resolve_canary_routes,
)
from services.evaluation.llm_shadow_cost import ShadowCostGuard
from services.llm.adapters import LLMProviderAdapter
from services.llm.contracts import ClaimGrounding, GroundingVerdict
from services.llm.http_providers import (
    LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODES,
    build_providers_by_tier,
)
from services.llm.orchestrator import LLMOrchestrator, LLMOrchestratorRequest
from services.llm.policy import LLMTier
from services.llm.repository import InMemoryLLMRuntimeRepository
from services.reports.composition import (
    DraftDegradationCode,
    compose_section,
    count_words,
)
from services.reports.context import ClaimContext
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
from services.reports.material import SectionKind
from services.reports.prompts import (
    COMPOSITION_PROMPT_TEMPLATE_VERSION,
    COMPOSITION_SCHEMA,
    TOP_EVENT_PROMPT_NAME,
)

_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_CASES_PATH: Final = _REPOSITORY_ROOT / "evaluation" / "llm-shadow" / "cases.v1.json"
FROZEN_SHADOW_FIXTURE_SHA256: Final = (
    "41ed6eeb0e9fada1dbb5bc525cb1535b5543a5ea32a82be49e56e78284597c72"
)
CASES_SCHEMA: Final = "llm-shadow-case-families.v1"
REPORT_SCHEMA: Final = "llm-shadow-evaluation.v1"
DEFAULT_MAX_CALLS: Final = 700
DEFAULT_MAX_COST_USD: Final = 5.0
DIAGNOSTIC_REPORT_SCHEMA: Final = "llm-shadow-diagnostic.v1"
DIAGNOSTIC_PROFILE: Final = "failure-triage.v1"
DIAGNOSTIC_MAX_CALLS: Final = 26
DIAGNOSTIC_MAX_COST_USD: Final = 1.0
_PROVIDERS: Final = ("gemini", "deepseek")
_OFFICIAL_PROVIDER_HOSTS: Final[Mapping[str, str]] = {
    "gemini": "generativelanguage.googleapis.com",
    "deepseek": "api.deepseek.com",
}
_EXPECTED_COUNTS: Final[Mapping[str, int]] = {
    "entity_adjudication": 25,
    "claim_grounding": 25,
    "analogy_rerank": 25,
    "report_composition": 50,
}
_PAIR_CALL_CEILINGS: Final[Mapping[str, int]] = {
    "entity_adjudication": 2,
    "claim_grounding": 2,
    "analogy_rerank": 2,
    "report_composition": 4,
}
_EXPECTED_CASES_PER_PROVIDER: Final = sum(_EXPECTED_COUNTS.values())
_EXPECTED_PROVIDER_PAIRS: Final = _EXPECTED_CASES_PER_PROVIDER * len(_PROVIDERS)
_MIN_FINAL_PASSES: Final = 123
_MIN_FIRST_COMPOSITION_PASSES: Final = 45
_CANARY_DATE: Final = datetime.date(2026, 7, 31)
_FIXTURE_NAMESPACE: Final = uuid.UUID("b02eb1c4-4869-5a02-892d-07df968d8f04")
_EXPECTED_FAMILIES: Final[Mapping[str, int]] = {
    "entity_adjudication": 5,
    "claim_grounding": 5,
    "analogy_rerank": 5,
    "report_composition": 10,
}

_RESULT_STATUSES: Final = (
    "passed",
    "passed_with_retry",
    "failed_contract",
    "failed_safety",
    "failed_semantics",
    "failed_quality",
)
_AUDIT_RUN_STATUSES: Final = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "validation_failed",
    "cached",
    "skipped",
)
_AUDIT_FAILURE_STAGES_BY_MESSAGE: Final[Mapping[str, str]] = {
    "token bucket limit reached": "local_rate_limit",
    "provider returned retryable structured output": "provider_output_validation",
    "provider invocation failure": "provider_invocation",
    "schema validation failed": "schema_validation",
}
_SHARED_CHECK_NAMES: Final = frozenset(
    {
        "terminal_contract_valid",
        "contract_envelope_matches",
        "provider_model_attribution",
        "production_prompt_identity",
        "no_cache_or_fallback",
        "runner_completed",
    }
)
_WORKLOAD_CHECK_NAMES: Final[Mapping[str, frozenset[str]]] = {
    "entity_adjudication": frozenset(
        {
            "adjudication_domain_result",
            "no_wrong_entity_attachment",
            "expected_entity_or_nil",
        }
    ),
    "claim_grounding": frozenset(
        {
            "exact_unique_claim_coverage",
            "direct_contradictions_are_unsupported",
            "expected_grounding_verdicts",
        }
    ),
    "analogy_rerank": frozenset(
        {
            "no_foreign_or_duplicate_episode_ids",
            "required_regime_caveats_present",
            "no_hindsight_outcome_claims",
            "expected_structural_subset",
            "expected_match_or_abstention",
            "structural_explanation_quality",
        }
    ),
    "report_composition": frozenset(
        {
            "only_whitelisted_claim_ids",
            "fact_to_claim_citations_align",
            "copyright_clean",
            "no_forbidden_facts",
            "non_abstaining_composition",
            "all_required_claims_cited",
            "required_concepts_present",
            "section_word_budget",
            "nonblank_blocks",
            "composition_domain_mapping",
        }
    ),
}
_DIAGNOSTIC_SCHEDULE: Final = (
    ("gemini", "report_composition.composition.rate_policy.v1"),
    ("deepseek", "report_composition.composition.rate_policy.v1"),
    ("gemini", "claim_grounding.grounding.supported_paraphrase.v1"),
    ("gemini", "claim_grounding.grounding.direct_contradiction.v1"),
    ("deepseek", "report_composition.composition.currency_intervention.v5"),
    ("gemini", "report_composition.composition.currency_intervention.v5"),
    ("gemini", "claim_grounding.grounding.insufficient_evidence.v1"),
    ("gemini", "claim_grounding.grounding.mixed_multiclaim.v1"),
    ("gemini", "claim_grounding.grounding.instruction_decoy.v1"),
)
_DIAGNOSTIC_COUNTS: Final[Mapping[str, Mapping[str, int]]] = {
    "gemini": {
        "claim_grounding": 5,
        "report_composition": 2,
    },
    "deepseek": {
        "report_composition": 2,
    },
}
_DIAGNOSTIC_PROVIDER_TIERS: Final[Mapping[str, tuple[LLMTier, ...]]] = {
    "gemini": (LLMTier.T1, LLMTier.T2),
    "deepseek": (LLMTier.T2,),
}


class LLMShadowEvaluationError(RuntimeError):
    """The aggregate shadow evaluation cannot start safely."""


ProviderBuilder = Callable[..., dict[str, tuple[LLMProviderAdapter, ...]]]
ProgressCallback = Callable[[Mapping[str, Any]], None]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LLMShadowEvaluationError(message)


def _finite_positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _resolve_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise LLMShadowEvaluationError("the shadow fixture path is invalid") from exc


def _fixture_label(path: Path) -> str:
    try:
        return str(path.relative_to(_REPOSITORY_ROOT))
    except ValueError:
        return "<external-fixture>"


def shadow_fixture_sha256(path: Path = DEFAULT_CASES_PATH) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise LLMShadowEvaluationError("the shadow fixture is unavailable") from exc


def _require_frozen_shadow_fixture(path: Path) -> None:
    _require(
        path == _resolve_path(DEFAULT_CASES_PATH),
        "live shadow profiles require the canonical frozen fixture path",
    )
    _require(
        shadow_fixture_sha256(path) == FROZEN_SHADOW_FIXTURE_SHA256,
        "the canonical shadow fixture digest does not match the approved frozen corpus",
    )


def _case_tier(workload: str) -> str:
    return "T1" if workload in {"entity_adjudication", "claim_grounding"} else "T2"


def _stable_uuid(
    workload: str,
    family_id: str,
    variant_index: int,
    role: str,
    item_index: int = 0,
) -> str:
    name = ":".join((CASES_SCHEMA, workload, family_id, str(variant_index), role, str(item_index)))
    return str(uuid.uuid5(_FIXTURE_NAMESPACE, name))


def _index(value: object, *, size: int, name: str, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    _require(
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value < size,
        f"{name} is outside its source list",
    )
    return int(value)


def _index_list(value: object, *, size: int, name: str) -> tuple[int, ...]:
    _require(isinstance(value, list), f"{name} must be an array")
    indices = tuple(_index(item, size=size, name=name) for item in value)
    _require(len(indices) == len(set(indices)), f"{name} cannot contain duplicates")
    return tuple(int(item) for item in indices if item is not None)


def _rotate(values: Sequence[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    if not values:
        return []
    split = offset % len(values)
    return [*values[split:], *values[:split]]


def _synthetic_claim(
    raw: Mapping[str, Any],
    *,
    claim_id: str,
    workload: str,
    family_id: str,
    variant_index: int,
    claim_index: int,
) -> dict[str, Any]:
    claim = dict(raw)
    _require("id" not in claim, "compact shadow claims cannot supply ids")
    claim["id"] = claim_id
    claim.setdefault("publisher", "Synthetic Shadow Wire")
    claim.setdefault(
        "url",
        (
            "https://example.test/llm-shadow/"
            f"{workload}/{family_id}/{variant_index + 1}/claim-{claim_index + 1}"
        ),
    )
    _require(
        isinstance(claim.get("url"), str)
        and urlparse(str(claim["url"])).scheme == "https"
        and (urlparse(str(claim["url"])).hostname or "").endswith(".test"),
        "shadow evidence URLs must use a synthetic .test host",
    )
    _require(
        isinstance(claim.get("excerpt"), str) and len(str(claim["excerpt"])) <= 200,
        "shadow evidence excerpts must be at most 200 characters",
    )
    return claim


def _expand_entity_variant(
    *,
    workload: str,
    family_id: str,
    tier: str,
    variant: Mapping[str, Any],
    variant_index: int,
) -> dict[str, Any]:
    candidates_raw = variant.get("candidates")
    _require(
        isinstance(candidates_raw, list) and bool(candidates_raw),
        "entity variant needs candidates",
    )
    candidate_ids = [
        _stable_uuid(workload, family_id, variant_index, "candidate", index)
        for index in range(len(candidates_raw))
    ]
    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(candidates_raw):
        _require(
            isinstance(raw, Mapping) and "id" not in raw,
            "invalid compact entity candidate",
        )
        candidates.append({**dict(raw), "id": candidate_ids[index]})
    expected = variant.get("expected")
    _require(isinstance(expected, Mapping), "entity variant needs expected outcomes")
    selected_index = _index(
        expected.get("selected_candidate_index"),
        size=len(candidates),
        name="selected_candidate_index",
        nullable=True,
    )
    return {
        "workload": workload,
        "tier": tier,
        "mention": dict(variant["mention"]),
        "candidates": _rotate(candidates, variant_index),
        "expected": {
            "selected_id": None if selected_index is None else candidate_ids[selected_index]
        },
    }


def _expand_grounding_variant(
    *,
    workload: str,
    family_id: str,
    tier: str,
    variant: Mapping[str, Any],
    variant_index: int,
) -> dict[str, Any]:
    claims_raw = variant.get("claims")
    _require(
        isinstance(claims_raw, list) and bool(claims_raw),
        "grounding variant needs claims",
    )
    claim_ids = [
        _stable_uuid(workload, family_id, variant_index, "claim", index)
        for index in range(len(claims_raw))
    ]
    claims = [
        _synthetic_claim(
            raw,
            claim_id=claim_ids[index],
            workload=workload,
            family_id=family_id,
            variant_index=variant_index,
            claim_index=index,
        )
        for index, raw in enumerate(claims_raw)
        if isinstance(raw, Mapping)
    ]
    _require(len(claims) == len(claims_raw), "invalid compact grounding claim")
    expected = variant.get("expected")
    _require(isinstance(expected, Mapping), "grounding variant needs expected outcomes")
    verdict_entries = expected.get("verdicts")
    _require(isinstance(verdict_entries, list), "grounding verdicts must be an array")
    verdicts_by_index: dict[int, str] = {}
    if verdict_entries and all(isinstance(item, Mapping) for item in verdict_entries):
        for item in verdict_entries:
            assert isinstance(item, Mapping)
            claim_index = _index(
                item.get("claim_index"),
                size=len(claims),
                name="grounding verdict claim_index",
            )
            assert claim_index is not None
            _require(claim_index not in verdicts_by_index, "grounding verdict index is duplicated")
            verdicts_by_index[claim_index] = str(item.get("verdict"))
    else:
        verdicts_by_index = {index: str(verdict) for index, verdict in enumerate(verdict_entries)}
    _require(
        set(verdicts_by_index) == set(range(len(claims))),
        "grounding verdicts must cover every source claim",
    )
    explicit_contradictions = expected.get("direct_contradiction_claim_indices")
    contradiction_indices = _index_list(
        (
            explicit_contradictions
            if explicit_contradictions is not None
            else [
                index
                for index, verdict in verdicts_by_index.items()
                if verdict == GroundingVerdict.UNSUPPORTED.value
            ]
        ),
        size=len(claims),
        name="direct_contradiction_claim_indices",
    )
    return {
        "workload": workload,
        "tier": tier,
        "block_text": str(variant["block_text"]),
        "claims": _rotate(claims, variant_index),
        "expected": {
            "verdicts": {claim_ids[index]: verdict for index, verdict in verdicts_by_index.items()},
            "direct_contradiction_claim_ids": [claim_ids[index] for index in contradiction_indices],
        },
    }


def _expand_analogy_variant(
    *,
    workload: str,
    family_id: str,
    tier: str,
    variant: Mapping[str, Any],
    variant_index: int,
) -> dict[str, Any]:
    candidates_raw = variant.get("candidates")
    _require(
        isinstance(candidates_raw, list) and bool(candidates_raw),
        "analogy variant needs candidates",
    )
    candidate_ids = [
        _stable_uuid(workload, family_id, variant_index, "candidate", index)
        for index in range(len(candidates_raw))
    ]
    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(candidates_raw):
        _require(
            isinstance(raw, Mapping) and "id" not in raw,
            "invalid compact analogy candidate",
        )
        candidates.append({**dict(raw), "id": candidate_ids[index]})
    expected = variant.get("expected")
    _require(isinstance(expected, Mapping), "analogy variant needs expected outcomes")
    selected_indices = _index_list(
        expected.get("selected_candidate_indices", []),
        size=len(candidates),
        name="selected_candidate_indices",
    )
    caveat_indices = _index_list(
        expected.get(
            "required_caveat_candidate_indices",
            expected.get("caveat_required_candidate_indices", []),
        ),
        size=len(candidates),
        name="required_caveat_candidate_indices",
    )
    event = dict(variant["event"])
    _require("id" not in event, "compact shadow events cannot supply ids")
    event["id"] = _stable_uuid(workload, family_id, variant_index, "event")
    return {
        "workload": workload,
        "tier": tier,
        "event": event,
        "candidates": _rotate(candidates, variant_index),
        "expected": {
            "matched": bool(expected.get("matched")),
            "selected_ids": [candidate_ids[index] for index in selected_indices],
            "required_caveat_ids": [candidate_ids[index] for index in caveat_indices],
        },
    }


def _expand_composition_variant(
    *,
    workload: str,
    family_id: str,
    tier: str,
    family: Mapping[str, Any],
    variant: Mapping[str, Any],
    variant_index: int,
) -> dict[str, Any]:
    claims_raw = variant.get("claims")
    _require(
        isinstance(claims_raw, list) and len(claims_raw) == 2,
        "composition variants require exactly two claims",
    )
    claim_ids = [
        _stable_uuid(workload, family_id, variant_index, "claim", index)
        for index in range(len(claims_raw))
    ]
    claims = [
        _synthetic_claim(
            raw,
            claim_id=claim_ids[index],
            workload=workload,
            family_id=family_id,
            variant_index=variant_index,
            claim_index=index,
        )
        for index, raw in enumerate(claims_raw)
        if isinstance(raw, Mapping)
    ]
    _require(len(claims) == len(claims_raw), "invalid compact composition claim")
    expected = variant.get("expected")
    _require(isinstance(expected, Mapping), "composition variant needs expected outcomes")
    required_indices = _index_list(
        expected.get("required_claim_indices", []),
        size=len(claims),
        name="required_claim_indices",
    )
    rules_raw = expected.get("citation_marker_rules", expected.get("citation_rules", []))
    _require(isinstance(rules_raw, list), "citation_marker_rules must be an array")
    citation_rules = []
    for rule in rules_raw:
        _require(isinstance(rule, Mapping), "invalid citation marker rule")
        claim_index = _index(
            rule.get("claim_index"),
            size=len(claims),
            name="citation marker claim_index",
        )
        terms = rule.get("terms")
        _require(isinstance(terms, list) and bool(terms), "citation marker needs terms")
        assert claim_index is not None
        citation_rules.append(
            {
                "claim_id": claim_ids[claim_index],
                "triggers": [str(value) for value in terms],
            }
        )
    event = dict(variant["event"])
    _require("id" not in event, "compact shadow events cannot supply ids")
    event["id"] = _stable_uuid(workload, family_id, variant_index, "event")
    budget = variant.get("budget", family.get("budget"))
    _require(isinstance(budget, Mapping), "composition case needs a word budget")
    return {
        "workload": workload,
        "tier": tier,
        "event": event,
        "claims": _rotate(claims, variant_index),
        "budget": dict(budget),
        "expected": {
            "required_claim_ids": [claim_ids[index] for index in required_indices],
            "required_concepts": list(
                expected.get("semantic_any_of", expected.get("required_concepts", []))
            ),
            "forbidden_terms": list(expected.get("forbidden_terms", [])),
            "citation_rules": citation_rules,
        },
    }


def _expand_family_variant(
    *,
    workload: str,
    family: Mapping[str, Any],
    variant: Mapping[str, Any],
    variant_index: int,
) -> dict[str, Any]:
    family_id = str(family["family_id"])
    common = {
        "workload": workload,
        "family_id": family_id,
        "tier": str(family["tier"]),
        "variant": variant,
        "variant_index": variant_index,
    }
    if workload == "entity_adjudication":
        case = _expand_entity_variant(**common)
    elif workload == "claim_grounding":
        case = _expand_grounding_variant(**common)
    elif workload == "analogy_rerank":
        case = _expand_analogy_variant(**common)
    elif workload == "report_composition":
        case = _expand_composition_variant(**common, family=family)
    else:
        raise LLMShadowEvaluationError("unknown shadow workload")
    case["case_id"] = f"{workload}.{family_id}.v{variant_index + 1}"
    return case


def load_shadow_cases(path: Path = DEFAULT_CASES_PATH) -> tuple[dict[str, Any], ...]:
    """Expand compact case families into the deterministic 125-case corpus."""

    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LLMShadowEvaluationError("the shadow fixture is unavailable or invalid") from exc
    _require(isinstance(document, dict), "the shadow fixture must be an object")
    _require(document.get("schema") == CASES_SCHEMA, "unexpected shadow fixture schema")
    _require(document.get("variants_per_family") == 5, "shadow families require five variants")
    families_by_workload = document.get("families")
    _require(isinstance(families_by_workload, dict), "the shadow fixture needs family groups")
    _require(
        set(families_by_workload) == set(_EXPECTED_FAMILIES),
        "the shadow fixture must contain exactly four workload groups",
    )
    cases: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    counts: Counter[str] = Counter()
    family_ids: set[tuple[str, str]] = set()
    for workload, family_values in families_by_workload.items():
        _require(
            isinstance(family_values, list) and len(family_values) == _EXPECTED_FAMILIES[workload],
            "shadow workload has the wrong family count",
        )
        for family in family_values:
            _require(isinstance(family, Mapping), "every shadow family must be an object")
            family_id = family.get("family_id")
            _require(
                isinstance(family_id, str)
                and bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", family_id)),
                "every shadow family needs a stable lowercase id",
            )
            _require(
                (workload, family_id) not in family_ids,
                "shadow family ids must be unique",
            )
            _require(
                family.get("tier") == _case_tier(workload),
                "shadow family has wrong tier",
            )
            variants = family.get("variants")
            _require(
                isinstance(variants, list) and len(variants) == 5,
                "shadow family needs five variants",
            )
            family_ids.add((workload, family_id))
            for variant_index, variant in enumerate(variants):
                _require(isinstance(variant, Mapping), "every shadow variant must be an object")
                case = _expand_family_variant(
                    workload=workload,
                    family=family,
                    variant=variant,
                    variant_index=variant_index,
                )
                case_id = str(case["case_id"])
                _require(case_id not in identifiers, "shadow case ids must be unique")
                identifiers.add(case_id)
                counts[workload] += 1
                cases.append(case)

    _require(dict(counts) == dict(_EXPECTED_COUNTS), "shadow corpus cardinality is not 25/25/25/50")
    try:
        for case in cases:
            workload = str(case["workload"])
            if workload == "entity_adjudication":
                build_entity_fixture(case)
            elif workload == "claim_grounding":
                build_grounding_fixture(case)
            elif workload == "analogy_rerank":
                build_analogy_fixture(case)
            else:
                build_composition_fixture(case)
    except Exception as exc:  # noqa: BLE001 - normalize fixture details at this boundary
        raise LLMShadowEvaluationError(
            "an expanded shadow case is incompatible with a production fixture builder"
        ) from exc
    return tuple(cases)


def _check(name: str, axis: CanaryAxis, passed: object) -> CanaryCheck:
    return CanaryCheck(name=name, axis=axis, passed=bool(passed))


def score_shadow_entity_case(
    *,
    decision: AdjudicationDecision | str,
    selected_id: str | None,
    expected_selected_id: str | None,
) -> list[CanaryCheck]:
    """Score selection and intentional NIL outcomes without returning either identifier."""

    decision_value = getattr(decision, "value", decision)
    expected_nil = expected_selected_id is None
    returned_nil = selected_id is None and decision_value == AdjudicationDecision.NIL.value
    wrong_attachment = selected_id is not None and selected_id != expected_selected_id
    return [
        _check(
            "adjudication_domain_result",
            CanaryAxis.CONTRACT,
            decision_value != AdjudicationDecision.FAILED.value,
        ),
        _check("no_wrong_entity_attachment", CanaryAxis.SAFETY, not wrong_attachment),
        _check(
            "expected_entity_or_nil",
            CanaryAxis.SEMANTICS,
            returned_nil if expected_nil else selected_id == expected_selected_id,
        ),
    ]


def score_shadow_grounding_case(
    *,
    verdicts: Sequence[tuple[str, str]],
    expected: Mapping[str, str],
    direct_contradiction_ids: Sequence[str] | None = None,
) -> list[CanaryCheck]:
    """Score exact claim coverage and fail unsafe direct contradictions closed."""

    returned_ids = [claim_id for claim_id, _verdict in verdicts]
    by_id = {claim_id: verdict for claim_id, verdict in verdicts}
    exact_coverage = len(returned_ids) == len(set(returned_ids)) and set(returned_ids) == set(
        expected
    )
    contradicted = (
        set(direct_contradiction_ids)
        if direct_contradiction_ids is not None
        else {
            claim_id
            for claim_id, verdict in expected.items()
            if verdict == GroundingVerdict.UNSUPPORTED.value
        }
    )
    contradiction_safe = exact_coverage and all(
        by_id.get(claim_id) == GroundingVerdict.UNSUPPORTED.value for claim_id in contradicted
    )
    exact_verdicts = exact_coverage and all(
        by_id.get(key) == value for key, value in expected.items()
    )
    return [
        _check("exact_unique_claim_coverage", CanaryAxis.CONTRACT, exact_coverage),
        _check("direct_contradictions_are_unsupported", CanaryAxis.SAFETY, contradiction_safe),
        _check("expected_grounding_verdicts", CanaryAxis.SEMANTICS, exact_verdicts),
    ]


_HINDSIGHT_PATTERN: Final = re.compile(
    r"\b(eventually|ultimately|outcome|later collapsed|later failed|ended in|"
    r"resolved through|was bailed out|became insolvent)\b",
    re.IGNORECASE,
)


def score_shadow_analogy_case(
    *,
    selected_ids: Sequence[str],
    explanations: Sequence[str],
    caveats_by_id: Mapping[str, Sequence[str]],
    candidate_ids: Sequence[str],
    caveat_required_ids: Sequence[str],
    expected_ids: Sequence[str],
    matched: bool,
    expected_matched: bool,
) -> list[CanaryCheck]:
    """Score structural matches, legitimate abstention, and required regime caveats."""

    no_foreign_duplicates = len(selected_ids) == len(set(selected_ids)) and set(
        selected_ids
    ) <= set(candidate_ids)
    required_selected = set(selected_ids) & set(caveat_required_ids)
    caveats_present = all(
        any(str(value).strip() for value in caveats_by_id.get(case_id, ()))
        for case_id in required_selected
    )
    model_reasoning_text = " ".join(
        [
            *explanations,
            *(str(caveat) for caveats in caveats_by_id.values() for caveat in caveats),
        ]
    )
    expected_status = matched is expected_matched
    if expected_matched:
        explanation_quality = bool(explanations) and all(value.strip() for value in explanations)
    else:
        explanation_quality = not explanations and not selected_ids
    return [
        _check("no_foreign_or_duplicate_episode_ids", CanaryAxis.CONTRACT, no_foreign_duplicates),
        _check("required_regime_caveats_present", CanaryAxis.SAFETY, caveats_present),
        _check(
            "no_hindsight_outcome_claims",
            CanaryAxis.SAFETY,
            not _HINDSIGHT_PATTERN.search(model_reasoning_text),
        ),
        _check(
            "expected_structural_subset",
            CanaryAxis.SEMANTICS,
            list(selected_ids) == list(expected_ids),
        ),
        _check("expected_match_or_abstention", CanaryAxis.SEMANTICS, expected_status),
        _check("structural_explanation_quality", CanaryAxis.QUALITY, explanation_quality),
    ]


def _text_groups(expected: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    raw = expected.get(
        "required_concepts",
        expected.get("required_text_groups", expected.get("semantic_any_of", ())),
    )
    groups: list[tuple[str, ...]] = []
    for item in raw if isinstance(raw, list) else ():
        if isinstance(item, str):
            groups.append((item.casefold(),))
        elif isinstance(item, list):
            groups.append(tuple(str(value).casefold() for value in item if str(value)))
        elif isinstance(item, Mapping):
            alternatives = item.get("any_of", item.get("terms", ()))
            if isinstance(alternatives, list):
                groups.append(tuple(str(value).casefold() for value in alternatives if str(value)))
    return tuple(group for group in groups if group)


def _citation_rules(
    expected: Mapping[str, Any], claims: Sequence[ClaimContext]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    rules: list[tuple[str, tuple[str, ...]]] = []
    raw = expected.get(
        "citation_rules",
        expected.get("citation_expectations", expected.get("citation_marker_rules", ())),
    )
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, Mapping):
            continue
        claim_id = item.get("claim_id")
        if claim_id is None:
            claim_index = item.get("claim_index")
            if (
                isinstance(claim_index, int)
                and not isinstance(claim_index, bool)
                and 0 <= claim_index < len(claims)
            ):
                claim_id = str(claims[claim_index].claim_id)
        triggers = item.get("triggers", item.get("any_of", item.get("terms", ())))
        if isinstance(claim_id, str) and isinstance(triggers, list):
            normalized = tuple(str(value).casefold() for value in triggers if str(value))
            if normalized:
                rules.append((claim_id, normalized))
    return tuple(rules)


def score_shadow_composition_case(
    *,
    blocks: Sequence[ScoredBlock],
    claims: Sequence[ClaimContext],
    expected: Mapping[str, Any],
    minimum: int,
    maximum: int,
) -> list[CanaryCheck]:
    """Apply fixture-driven semantic, citation, copyright, and word-budget checks."""

    allowed_ids = {str(claim.claim_id) for claim in claims}
    required_ids = {str(value) for value in expected.get("required_claim_ids", ())}
    if not required_ids:
        for value in expected.get("required_claim_indices", ()):
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(claims):
                required_ids.add(str(claims[value].claim_id))
    cited_ids = {claim_id for block in blocks for claim_id in block.claim_ids}
    combined = " ".join(block.text for block in blocks)
    lowered = combined.casefold()
    word_count = count_words(combined)

    citation_alignment = True
    for claim_id, triggers in _citation_rules(expected, claims):
        for block in blocks:
            if any(trigger in block.text.casefold() for trigger in triggers):
                citation_alignment = citation_alignment and claim_id in block.claim_ids

    copyright_clean = True
    claims_by_id = {str(claim.claim_id): claim for claim in claims}
    for index, block in enumerate(blocks):
        snippets: list[SnippetSource] = []
        for claim_id in block.claim_ids:
            claim = claims_by_id.get(claim_id)
            if claim is None:
                continue
            for link in claim.links:
                snippets.append(
                    SnippetSource(
                        article_id=link.article.article_id,
                        publisher=link.article.publisher,
                        url=link.article.url,
                        text=link.article.excerpt.text,
                    )
                )
        copyright_clean = copyright_clean and not check_block_copyright(
            block_index=index,
            block_text=block.text,
            snippets=snippets,
        )

    required_concepts = all(
        any(term in lowered for term in group) for group in _text_groups(expected)
    )
    forbidden_raw = expected.get("forbidden_terms", expected.get("forbidden_phrases", ()))
    forbidden = (
        tuple(str(value).casefold() for value in forbidden_raw if isinstance(value, str))
        if isinstance(forbidden_raw, list)
        else ()
    )
    return [
        _check("only_whitelisted_claim_ids", CanaryAxis.CONTRACT, cited_ids <= allowed_ids),
        _check("fact_to_claim_citations_align", CanaryAxis.SAFETY, citation_alignment),
        _check("copyright_clean", CanaryAxis.SAFETY, copyright_clean),
        _check(
            "no_forbidden_facts", CanaryAxis.SAFETY, not any(term in lowered for term in forbidden)
        ),
        _check("non_abstaining_composition", CanaryAxis.SEMANTICS, bool(blocks)),
        _check("all_required_claims_cited", CanaryAxis.SEMANTICS, required_ids <= cited_ids),
        _check("required_concepts_present", CanaryAxis.SEMANTICS, required_concepts),
        _check("section_word_budget", CanaryAxis.QUALITY, minimum <= word_count <= maximum),
        _check(
            "nonblank_blocks",
            CanaryAxis.QUALITY,
            bool(blocks) and all(block.text.strip() for block in blocks),
        ),
    ]


def _route_for(routes: Sequence[CanaryRoute], provider: str, tier: LLMTier) -> CanaryRoute:
    matches = [route for route in routes if route.provider == provider and route.tier is tier]
    _require(len(matches) == 1, "shadow route resolution is ambiguous")
    return matches[0]


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
            "llm_budget_enforced": False,
        }
    )


def _provider_key_present(settings: Settings, provider: str) -> bool:
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    if provider == "deepseek":
        return bool(settings.deepseek_api_key)
    return False


def _validate_provider_endpoints(settings: Settings) -> None:
    endpoints = {
        "gemini": settings.gemini_base_url,
        "deepseek": settings.deepseek_base_url,
    }
    for provider, endpoint in endpoints.items():
        raw = str(endpoint)
        try:
            parsed = urlparse(raw)
            port = parsed.port
        except ValueError as exc:
            raise LLMShadowEvaluationError(
                f"shadow {provider} endpoint must be the canonical official API origin"
            ) from exc
        canonical = (
            raw == raw.strip()
            and parsed.scheme == "https"
            and parsed.hostname == _OFFICIAL_PROVIDER_HOSTS[provider]
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
            and parsed.path in {"", "/"}
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
            and "?" not in raw
            and "#" not in raw
        )
        _require(
            canonical,
            f"shadow {provider} endpoint must be the canonical official API origin",
        )


def _prompt_identity(workload: str) -> tuple[str, str, str]:
    if workload == "entity_adjudication":
        return ADJUDICATION_SCHEMA, ADJUDICATION_PROMPT_NAME, ADJUDICATION_PROMPT_TEMPLATE_VERSION
    if workload == "claim_grounding":
        return GROUNDING_SCHEMA, GROUNDING_PROMPT_NAME, GROUNDING_PROMPT_TEMPLATE_VERSION
    if workload == "analogy_rerank":
        return RERANK_SCHEMA, RERANK_PROMPT_NAME, RERANK_PROMPT_TEMPLATE_VERSION
    if workload == "report_composition":
        return COMPOSITION_SCHEMA, TOP_EVENT_PROMPT_NAME, COMPOSITION_PROMPT_TEMPLATE_VERSION
    raise LLMShadowEvaluationError("unknown shadow workload")


def _execution_checks(
    runs: Sequence[Any],
    *,
    route: CanaryRoute,
    workload: str,
) -> list[CanaryCheck]:
    schema, prompt_name, template_version = _prompt_identity(workload)
    successful = [run for run in runs if run.status in {"succeeded", "cached"}]
    terminal = successful[-1] if successful else None
    envelope_matches = bool(
        terminal is not None
        and terminal.output_schema_name == schema
        and terminal.output_schema_version == "1.0"
        and terminal.output.get("prompt_template_version") == template_version
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
                run.prompt_name == prompt_name and run.prompt_template_version == template_version
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


def _run_metrics(
    runs: Sequence[Any], *, network_calls: int, workload_attempts: int
) -> dict[str, int | float]:
    return {
        "network_calls": network_calls,
        "workload_attempts": workload_attempts,
        "validation_retries": sum(run.status == "validation_failed" for run in runs),
        "input_tokens": sum(int(run.input_tokens or 0) for run in runs),
        "output_tokens": sum(int(run.output_tokens or 0) for run in runs),
        "latency_ms": sum(int(run.latency_ms or 0) for run in runs),
        "estimated_cost_usd": sum(float(run.cost_usd or 0.0) for run in runs),
    }


def _audit_diagnostics(runs: Sequence[Any]) -> dict[str, dict[str, int]]:
    """Reduce transient audit rows to fixed, count-only diagnostic taxonomies."""

    statuses: Counter[str] = Counter()
    failure_stages: Counter[str] = Counter()
    provider_failure_codes: Counter[str] = Counter()
    for run in runs:
        raw_status = str(getattr(run, "status", ""))
        statuses[raw_status if raw_status in _AUDIT_RUN_STATUSES else "unrecognized"] += 1
        if raw_status not in {"failed", "validation_failed"}:
            continue
        message = str(getattr(run, "error_message", ""))
        failure_stages[_AUDIT_FAILURE_STAGES_BY_MESSAGE.get(message, "unclassified")] += 1
        if message not in {
            "provider returned retryable structured output",
            "provider invocation failure",
        }:
            continue
        details = getattr(run, "error_details", None)
        raw_code = details.get("provider_failure_code") if isinstance(details, Mapping) else None
        provider_failure_codes[
            raw_code
            if isinstance(raw_code, str) and raw_code in LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODES
            else "unclassified"
        ] += 1
    return {
        "audit_run_status_counts": dict(sorted(statuses.items())),
        "failure_stage_counts": dict(sorted(failure_stages.items())),
        "provider_failure_code_counts": dict(sorted(provider_failure_codes.items())),
    }


def _run_entity(
    orchestrator: LLMOrchestrator, case: Mapping[str, Any]
) -> tuple[list[CanaryCheck], dict[str, Any], int]:
    mention, link_result = build_entity_fixture(case)
    outcome = MentionAdjudicator(orchestrator).adjudicate(mention, link_result)
    selected_id = str(outcome.entity_id) if outcome.entity_id is not None else None
    expected_id = case["expected"].get("selected_id")
    checks = score_shadow_entity_case(
        decision=outcome.decision,
        selected_id=selected_id,
        expected_selected_id=str(expected_id) if expected_id is not None else None,
    )
    return checks, {"expected_nil": expected_id is None, "returned_nil": selected_id is None}, 1


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
        raise LLMShadowEvaluationError("grounding returned the wrong validated contract")
    verdicts = [(item.claim_id, item.verdict.value) for item in contract.verdicts]
    expected = {str(key): str(value) for key, value in case["expected"]["verdicts"].items()}
    checks = score_shadow_grounding_case(
        verdicts=verdicts,
        expected=expected,
        direct_contradiction_ids=[
            str(value) for value in case["expected"].get("direct_contradiction_claim_ids", ())
        ],
    )
    verdict_counts = Counter(verdict for _claim_id, verdict in verdicts)
    return checks, {"verdict_counts": dict(sorted(verdict_counts.items()))}, 1


def _analogy_fixture(
    case: Mapping[str, Any],
) -> tuple[Any, tuple[Any, ...], tuple[str, ...]]:
    event, candidates, regime_tags = build_analogy_fixture(case)
    raw_by_id = {str(item["id"]): item for item in case["candidates"]}
    adjusted = []
    for candidate in candidates:
        raw = raw_by_id[str(candidate.episode_id)]
        adjusted.append(
            replace(
                candidate,
                regime_caveats_required=bool(raw.get("regime_caveats_required", False)),
                regime_caveat_reasons=tuple(
                    str(value) for value in raw.get("regime_caveat_reasons", ())
                ),
            )
        )
    return event, tuple(adjusted), regime_tags


def _run_analogy(
    orchestrator: LLMOrchestrator, case: Mapping[str, Any]
) -> tuple[list[CanaryCheck], dict[str, Any], int]:
    event, candidates, regime_tags = _analogy_fixture(case)
    expected_ids = [str(value) for value in case["expected"].get("selected_ids", ())]
    expected_matched = bool(case["expected"].get("matched", bool(expected_ids)))
    try:
        result = rerank_candidates(
            orchestrator,
            event=event,
            candidates=candidates,
            current_regime_tags=regime_tags,
        )
    except RegimeCaveatMissingError:
        return (
            [
                _check("required_regime_caveats_present", CanaryAxis.SAFETY, False),
                _check("expected_structural_subset", CanaryAxis.SEMANTICS, False),
            ],
            {"matched": False, "selection_count": 0},
            1,
        )
    except AnalogyRerankError:
        raise

    selected_ids = [str(selection.episode_id) for selection in result.selections]
    caveats_by_id = {
        str(selection.episode_id): selection.regime_caveats for selection in result.selections
    }
    checks = score_shadow_analogy_case(
        selected_ids=selected_ids,
        explanations=[selection.explanation for selection in result.selections],
        caveats_by_id=caveats_by_id,
        candidate_ids=[str(candidate.episode_id) for candidate in candidates],
        caveat_required_ids=[
            str(value) for value in case["expected"].get("required_caveat_ids", ())
        ],
        expected_ids=expected_ids,
        matched=result.status is RerankStatus.MATCHED,
        expected_matched=expected_matched,
    )
    return checks, {"matched": result.matched, "selection_count": len(selected_ids)}, 1


def _run_composition(
    orchestrator: LLMOrchestrator, case: Mapping[str, Any]
) -> tuple[list[CanaryCheck], dict[str, Any], int]:
    _event, claims, inputs, context, material = build_composition_fixture(case)
    section = compose_section(orchestrator, inputs=inputs, context=context, material=material)
    blocks = tuple(
        ScoredBlock(
            text=block.text,
            claim_ids=tuple(str(claim_id) for claim_id in block.claim_ids),
        )
        for block in section.blocks
    )
    budget = case["budget"]
    checks = score_shadow_composition_case(
        blocks=blocks,
        claims=claims,
        expected=case["expected"],
        minimum=int(budget["minimum"]),
        maximum=int(budget["maximum"]),
    )
    degradation_codes = {
        getattr(degradation.code, "value", str(degradation.code))
        for degradation in section.degradations
    }
    checks.append(
        _check(
            "composition_domain_mapping",
            CanaryAxis.CONTRACT,
            DraftDegradationCode.COMPOSITION_FAILED.value not in degradation_codes,
        )
    )
    first_within = bool(section.attempts and section.attempts[0].within_budget)
    if not section.attempts:
        first_budget_status = "not_completed"
    elif first_within:
        first_budget_status = "within"
    elif section.attempts[0].word_count < int(budget["minimum"]):
        first_budget_status = "too_short"
    else:
        first_budget_status = "too_long"
    final_word_count = count_words(" ".join(block.text for block in section.blocks))
    return (
        checks,
        {
            "first_attempt_within_budget": first_within,
            "first_attempt_budget_status": first_budget_status,
            "final_word_count": final_word_count,
            "degraded": bool(section.degradations),
        },
        len(section.attempts),
    )


_RUNNERS: Final[Mapping[str, Callable[..., tuple[list[CanaryCheck], dict[str, Any], int]]]] = {
    "entity_adjudication": _run_entity,
    "claim_grounding": _run_grounding,
    "analogy_rerank": _run_analogy,
    "report_composition": _run_composition,
}


def _evaluate_pair(
    orchestrator: LLMOrchestrator,
    repository: InMemoryLLMRuntimeRepository,
    guard: ShadowCostGuard,
    *,
    route: CanaryRoute,
    case: Mapping[str, Any],
) -> dict[str, Any]:
    start = len(repository.llm_runs)
    workload = str(case["workload"])
    guard.begin_case(f"{route.provider}:{case['case_id']}", _PAIR_CALL_CEILINGS[workload])
    try:
        checks: list[CanaryCheck] = []
        observations: dict[str, Any] = {}
        workload_attempts = 0
        completed = True
        try:
            runner = _RUNNERS[workload]
            checks, observations, workload_attempts = runner(orchestrator, case)
        except Exception:  # noqa: BLE001 - public output records no exception or message
            completed = False
        finally:
            network_calls = guard.end_case()

        runs = tuple(repository.llm_runs[start:])
        checks = _execution_checks(runs, route=route, workload=workload) + checks
        if not completed:
            checks.append(_check("runner_completed", CanaryAxis.CONTRACT, False))
        retried = network_calls > 1 or workload_attempts > 1
        return {
            "case_id": str(case["case_id"]),
            "workload": workload,
            "provider": route.provider,
            "status": check_decision(checks, retried=retried),
            "checks": [check.as_dict() for check in checks],
            "observations": observations,
            "audit": _audit_diagnostics(runs),
            "metrics": _run_metrics(
                runs,
                network_calls=network_calls,
                workload_attempts=workload_attempts,
            ),
        }
    finally:
        # The repository is only an orchestration audit seam. Drop generated payloads
        # immediately, including when aggregate-safe result construction raises.
        del repository.llm_runs[start:]
        repository.jobs.clear()


def _expected_id_set(
    expected_case_ids: Mapping[str, Sequence[str]] | Sequence[str],
) -> set[str]:
    if isinstance(expected_case_ids, Mapping):
        return {str(case_id) for values in expected_case_ids.values() for case_id in values}
    return {str(case_id) for case_id in expected_case_ids}


def _axis_failure_count(results: Sequence[Mapping[str, Any]], axis: CanaryAxis) -> int:
    return sum(
        any(
            check.get("axis") == axis.value and not bool(check.get("passed"))
            for check in result["checks"]
        )
        for result in results
    )


def _axis_failure_counts(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {axis.value: _axis_failure_count(results, axis) for axis in CanaryAxis}


def _result_status_counts(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(str(result.get("status")) for result in results)
    return {
        **{status: counts[status] for status in _RESULT_STATUSES},
        "unrecognized": sum(
            count for status, count in counts.items() if status not in _RESULT_STATUSES
        ),
    }


def _failed_check_diagnostics(
    results: Sequence[Mapping[str, Any]], *, workload: str
) -> tuple[dict[str, int], int]:
    allowed = _SHARED_CHECK_NAMES | _WORKLOAD_CHECK_NAMES[workload]
    counts: Counter[str] = Counter()
    unrecognized = 0
    for result in results:
        checks = result.get("checks", ())
        if not isinstance(checks, Sequence) or isinstance(checks, str | bytes):
            continue
        failed_names: set[str] = set()
        for check in checks:
            if not isinstance(check, Mapping) or bool(check.get("passed")):
                continue
            failed_names.add(str(check.get("name")))
        for name in failed_names:
            if name in allowed:
                counts[name] += 1
        if any(name not in allowed for name in failed_names):
            unrecognized += 1
    return dict(sorted(counts.items())), unrecognized


def _aggregate_audit_counts(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    allowed_by_key: Mapping[str, tuple[str, ...]] = {
        "audit_run_status_counts": (*_AUDIT_RUN_STATUSES, "unrecognized"),
        "failure_stage_counts": (
            *tuple(dict.fromkeys(_AUDIT_FAILURE_STAGES_BY_MESSAGE.values())),
            "unclassified",
        ),
        "provider_failure_code_counts": LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODES,
    }
    output: dict[str, dict[str, int]] = {}
    for key, allowed in allowed_by_key.items():
        counts: Counter[str] = Counter()
        for result in results:
            audit = result.get("audit")
            raw_counts = audit.get(key) if isinstance(audit, Mapping) else None
            if not isinstance(raw_counts, Mapping):
                continue
            for raw_name, raw_count in raw_counts.items():
                name = str(raw_name)
                if isinstance(raw_count, bool) or not isinstance(raw_count, int):
                    continue
                if raw_count <= 0:
                    continue
                if name in allowed:
                    counts[name] += raw_count
                elif key == "audit_run_status_counts":
                    counts["unrecognized"] += raw_count
                elif key in {"failure_stage_counts", "provider_failure_code_counts"}:
                    counts["unclassified"] += raw_count
        output[key] = {name: counts[name] for name in allowed}
    return output


def _named_check_passed(result: Mapping[str, Any], name: str) -> bool:
    checks = result.get("checks", ())
    if not isinstance(checks, Sequence) or isinstance(checks, str | bytes):
        return False
    matching = [
        bool(check.get("passed"))
        for check in checks
        if isinstance(check, Mapping) and check.get("name") == name
    ]
    return bool(matching) and all(matching)


def _composition_diagnostics(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    first_budget_statuses = Counter(
        str(result.get("observations", {}).get("first_attempt_budget_status")) for result in results
    )
    return {
        "scored_cases": len(results),
        "first_attempt_within_budget_cases": sum(
            bool(result.get("observations", {}).get("first_attempt_within_budget"))
            for result in results
        ),
        "final_within_budget_cases": sum(
            _named_check_passed(result, "section_word_budget") for result in results
        ),
        "degraded_cases": sum(
            bool(result.get("observations", {}).get("degraded")) for result in results
        ),
        "multi_attempt_cases": sum(
            int(result.get("metrics", {}).get("workload_attempts", 0)) > 1 for result in results
        ),
        "recorded_workload_attempts": sum(
            int(result.get("metrics", {}).get("workload_attempts", 0)) for result in results
        ),
        "first_attempt_too_short_cases": first_budget_statuses["too_short"],
        "first_attempt_too_long_cases": first_budget_statuses["too_long"],
        "first_attempt_not_completed_cases": first_budget_statuses["not_completed"],
    }


def _workload_summary(
    results: Sequence[Mapping[str, Any]],
    *,
    workload: str,
    expected_count: int,
) -> dict[str, Any]:
    failed_check_counts, unrecognized_failed_check_cases = _failed_check_diagnostics(
        results,
        workload=workload,
    )
    summary: dict[str, Any] = {
        "workload": workload,
        "expected_cases": expected_count,
        "completed_cases": len(results),
        "final_passes": sum(
            result.get("status") in {"passed", "passed_with_retry"} for result in results
        ),
        "contract_failure_cases": _axis_failure_count(results, CanaryAxis.CONTRACT),
        "safety_failure_cases": _axis_failure_count(results, CanaryAxis.SAFETY),
        "status_counts": _result_status_counts(results),
        "failure_cases_by_axis": _axis_failure_counts(results),
        "failed_check_cases": failed_check_counts,
        "unrecognized_failed_check_cases": unrecognized_failed_check_cases,
        **_summed_metrics(results),
        **_aggregate_audit_counts(results),
    }
    if workload == "report_composition":
        summary["composition_counters"] = _composition_diagnostics(results)
    return summary


def _summed_metrics(results: Sequence[Mapping[str, Any]]) -> dict[str, int | float]:
    return {
        "network_calls": sum(int(result["metrics"]["network_calls"]) for result in results),
        "validation_retries": sum(
            int(result["metrics"]["validation_retries"]) for result in results
        ),
        "input_tokens": sum(int(result["metrics"]["input_tokens"]) for result in results),
        "output_tokens": sum(int(result["metrics"]["output_tokens"]) for result in results),
        "latency_ms": sum(int(result["metrics"]["latency_ms"]) for result in results),
        "estimated_cost_usd": sum(
            float(result["metrics"]["estimated_cost_usd"]) for result in results
        ),
    }


def aggregate_shadow_results(
    results: Sequence[Mapping[str, Any]],
    *,
    expected_case_ids: Mapping[str, Sequence[str]] | Sequence[str],
    providers: Sequence[str] = _PROVIDERS,
) -> dict[str, Any]:
    """Apply the complete-coverage promotion gates to aggregate-safe case results."""

    expected_ids = _expected_id_set(expected_case_ids)
    expected_providers = tuple(dict.fromkeys(str(provider) for provider in providers))
    provider_summaries: list[dict[str, Any]] = []
    for provider in expected_providers:
        provider_results = [result for result in results if result.get("provider") == provider]
        returned_ids = [str(result.get("case_id")) for result in provider_results]
        workload_counts = Counter(str(result.get("workload")) for result in provider_results)
        complete_coverage = (
            len(provider_results) == _EXPECTED_CASES_PER_PROVIDER
            and len(returned_ids) == len(set(returned_ids))
            and set(returned_ids) == expected_ids
            and dict(workload_counts) == dict(_EXPECTED_COUNTS)
        )
        final_passes = sum(
            result.get("status") in {"passed", "passed_with_retry"} for result in provider_results
        )
        contract_failures = _axis_failure_count(provider_results, CanaryAxis.CONTRACT)
        safety_failures = _axis_failure_count(provider_results, CanaryAxis.SAFETY)
        composition_results = [
            result for result in provider_results if result.get("workload") == "report_composition"
        ]
        first_composition_passes = sum(
            bool(result.get("observations", {}).get("first_attempt_within_budget"))
            for result in composition_results
        )
        eligible = (
            complete_coverage
            and final_passes >= _MIN_FINAL_PASSES
            and contract_failures == 0
            and safety_failures == 0
            and len(composition_results) == _EXPECTED_COUNTS["report_composition"]
            and first_composition_passes >= _MIN_FIRST_COMPOSITION_PASSES
        )
        workloads = []
        for workload, expected_count in _EXPECTED_COUNTS.items():
            workload_results = [
                result for result in provider_results if result.get("workload") == workload
            ]
            workloads.append(
                _workload_summary(
                    workload_results,
                    workload=workload,
                    expected_count=expected_count,
                )
            )
        provider_summaries.append(
            {
                "provider": provider,
                "eligible": eligible,
                "complete_coverage": complete_coverage,
                "expected_cases": _EXPECTED_CASES_PER_PROVIDER,
                "completed_cases": len(provider_results),
                "final_passes": final_passes,
                "required_final_passes": _MIN_FINAL_PASSES,
                "contract_failure_cases": contract_failures,
                "safety_failure_cases": safety_failures,
                "composition_first_attempt_passes": first_composition_passes,
                "required_composition_first_attempt_passes": (_MIN_FIRST_COMPOSITION_PASSES),
                "status_counts": _result_status_counts(provider_results),
                "failure_cases_by_axis": _axis_failure_counts(provider_results),
                **_aggregate_audit_counts(provider_results),
                "metrics": _summed_metrics(provider_results),
                "workloads": workloads,
            }
        )

    unexpected_provider_results = sum(
        str(result.get("provider")) not in set(expected_providers) for result in results
    )
    complete_matrix = (
        len(results) == _EXPECTED_PROVIDER_PAIRS
        and unexpected_provider_results == 0
        and bool(provider_summaries)
        and all(summary["complete_coverage"] for summary in provider_summaries)
    )
    decision = (
        "advance_to_limited_rollout"
        if complete_matrix and all(summary["eligible"] for summary in provider_summaries)
        else "hold"
    )
    return {
        "provider_summaries": provider_summaries,
        "totals": {
            "expected_case_provider_pairs": _EXPECTED_PROVIDER_PAIRS,
            "completed_case_provider_pairs": len(results),
            "not_run_case_provider_pairs": max(0, _EXPECTED_PROVIDER_PAIRS - len(results)),
            "unexpected_provider_results": unexpected_provider_results,
            **_summed_metrics(results),
        },
        "complete_matrix": complete_matrix,
        "decision": decision,
    }


def _expected_ids_by_workload(
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    return {
        workload: tuple(str(case["case_id"]) for case in cases if case["workload"] == workload)
        for workload in _EXPECTED_COUNTS
    }


def _validate_shadow_routes(settings: Settings, routes: Sequence[CanaryRoute]) -> None:
    for route in routes:
        prices = settings.llm_provider_token_price_usd_per_1m.get(f"{route.provider}:{route.model}")
        _require(isinstance(prices, Mapping), "a shadow route has no configured pricing")
        _require(
            _finite_positive(prices.get("input")) and _finite_positive(prices.get("output")),
            "a shadow route has invalid pricing",
        )
        _require(
            int(settings.llm_tier_max_output_tokens.get(route.tier.value, 0)) > 0,
            "a shadow route has no output-token ceiling",
        )


def _diagnostic_pairs(
    cases: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    by_id = {str(case["case_id"]): case for case in cases}
    _require(len(by_id) == len(cases), "the shadow fixture contains duplicate case ids")
    pairs: list[tuple[str, Mapping[str, Any]]] = []
    for provider, case_id in _DIAGNOSTIC_SCHEDULE:
        case = by_id.get(case_id)
        _require(case is not None, "the fixed diagnostic profile is incompatible with the fixture")
        workload = str(case["workload"])
        _require(
            workload in _DIAGNOSTIC_COUNTS.get(provider, {}),
            "the fixed diagnostic profile contains an invalid provider/workload pair",
        )
        pairs.append((provider, case))
    _require(
        Counter((provider, str(case["workload"])) for provider, case in pairs)
        == Counter(
            (provider, workload)
            for provider, workloads in _DIAGNOSTIC_COUNTS.items()
            for workload, count in workloads.items()
            for _ in range(count)
        ),
        "the fixed diagnostic profile counts do not match its schedule",
    )
    return tuple(pairs)


def _diagnostic_routes(routes: Sequence[CanaryRoute]) -> tuple[CanaryRoute, ...]:
    selected = tuple(
        route
        for route in routes
        if route.tier in _DIAGNOSTIC_PROVIDER_TIERS.get(route.provider, ())
    )
    _require(
        len(selected) == sum(len(tiers) for tiers in _DIAGNOSTIC_PROVIDER_TIERS.values()),
        "the fixed diagnostic routes are incomplete",
    )
    return selected


def build_shadow_plan(
    settings: Settings,
    *,
    max_calls: int = DEFAULT_MAX_CALLS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    cases_path: Path = DEFAULT_CASES_PATH,
) -> dict[str, Any]:
    """Build a no-network/no-write plan for the complete two-provider matrix."""

    cases_path = _resolve_path(cases_path)
    _require_frozen_shadow_fixture(cases_path)
    _require(
        isinstance(max_calls, int)
        and not isinstance(max_calls, bool)
        and max_calls == DEFAULT_MAX_CALLS,
        "max_calls must equal the approved 700-call ceiling",
    )
    _require(
        _finite_positive(max_cost_usd) and float(max_cost_usd) <= DEFAULT_MAX_COST_USD,
        "max_cost_usd must be positive and no greater than the approved ceiling",
    )
    _validate_provider_endpoints(settings)
    load_shadow_cases(cases_path)
    routes = resolve_canary_routes(settings, _PROVIDERS)
    _validate_shadow_routes(settings, routes)
    return {
        "schema": REPORT_SCHEMA,
        "mode": "plan",
        "fixture": {
            "path": _fixture_label(cases_path),
            "schema": CASES_SCHEMA,
            "sha256": shadow_fixture_sha256(cases_path),
            "case_counts": dict(_EXPECTED_COUNTS),
        },
        "routes": [route.as_dict() for route in routes],
        "guardrails": {
            "configured_max_network_calls": max_calls,
            "required_worst_case_network_calls": DEFAULT_MAX_CALLS,
            "normal_path_network_calls": _EXPECTED_PROVIDER_PAIRS,
            "configured_max_cost_usd": float(max_cost_usd),
            "cost_cap_kind": "rolling_conservative_per_call_reservation",
            "execution_may_stop_before_complete_matrix": True,
        },
        "gates": {
            "expected_cases_per_provider": _EXPECTED_CASES_PER_PROVIDER,
            "minimum_final_passes_per_provider": _MIN_FINAL_PASSES,
            "maximum_contract_failures": 0,
            "maximum_safety_failures": 0,
            "composition_cases_per_provider": _EXPECTED_COUNTS["report_composition"],
            "minimum_first_attempt_composition_passes_per_provider": (
                _MIN_FIRST_COMPOSITION_PASSES
            ),
            "partial_run_can_advance": False,
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


def build_shadow_diagnostic_plan(
    settings: Settings,
    *,
    max_calls: int = DIAGNOSTIC_MAX_CALLS,
    max_cost_usd: float = DIAGNOSTIC_MAX_COST_USD,
    cases_path: Path = DEFAULT_CASES_PATH,
) -> dict[str, Any]:
    """Build the fixed no-network failure-triage profile; it can never promote."""

    cases_path = _resolve_path(cases_path)
    _require_frozen_shadow_fixture(cases_path)
    _require(
        isinstance(max_calls, int)
        and not isinstance(max_calls, bool)
        and 0 < max_calls <= DIAGNOSTIC_MAX_CALLS,
        "diagnostic max_calls must be positive and no greater than 26",
    )
    _require(
        _finite_positive(max_cost_usd) and float(max_cost_usd) <= DIAGNOSTIC_MAX_COST_USD,
        "diagnostic max_cost_usd must be positive and no greater than 1.00",
    )
    _validate_provider_endpoints(settings)
    cases = load_shadow_cases(cases_path)
    _diagnostic_pairs(cases)
    routes = _diagnostic_routes(resolve_canary_routes(settings, _PROVIDERS))
    _validate_shadow_routes(settings, routes)
    return {
        "schema": DIAGNOSTIC_REPORT_SCHEMA,
        "mode": "diagnostic_plan",
        "diagnostic_profile": DIAGNOSTIC_PROFILE,
        "fixture": {
            "path": _fixture_label(cases_path),
            "schema": CASES_SCHEMA,
            "sha256": shadow_fixture_sha256(cases_path),
        },
        "routes": [route.as_dict() for route in routes],
        "sample_counts": [
            {
                "provider": provider,
                "workload": workload,
                "cases": count,
            }
            for provider, workloads in _DIAGNOSTIC_COUNTS.items()
            for workload, count in workloads.items()
        ],
        "guardrails": {
            "configured_max_network_calls": max_calls,
            "required_worst_case_network_calls": DIAGNOSTIC_MAX_CALLS,
            "normal_path_network_calls": len(_DIAGNOSTIC_SCHEDULE),
            "configured_max_cost_usd": float(max_cost_usd),
            "cost_cap_kind": "rolling_conservative_per_call_reservation",
            "execution_may_stop_before_complete_profile": True,
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
        "decision": "hold",
        "decision_scope": "diagnostic_only",
        "next_action": "rerun_with_live_diagnostic_confirmation",
    }


def aggregate_shadow_diagnostic_results(
    results: Sequence[Mapping[str, Any]],
    *,
    expected_pairs: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Aggregate the fixed diagnostic schedule without exposing its case identities."""

    expected_keys = Counter((provider, str(case["case_id"])) for provider, case in expected_pairs)
    actual_keys = Counter(
        (str(result.get("provider")), str(result.get("case_id"))) for result in results
    )
    missing_pairs = sum((expected_keys - actual_keys).values())
    unexpected_results = sum((actual_keys - expected_keys).values())
    complete_profile = (
        not missing_pairs and not unexpected_results and len(results) == len(expected_pairs)
    )

    provider_summaries: list[dict[str, Any]] = []
    for provider, expected_workloads in _DIAGNOSTIC_COUNTS.items():
        provider_results = [result for result in results if result.get("provider") == provider]
        expected_provider_keys = Counter(
            str(case["case_id"])
            for expected_provider, case in expected_pairs
            if expected_provider == provider
        )
        actual_provider_keys = Counter(str(result.get("case_id")) for result in provider_results)
        workloads = []
        for workload, expected_count in expected_workloads.items():
            workload_results = [
                result for result in provider_results if result.get("workload") == workload
            ]
            workloads.append(
                _workload_summary(
                    workload_results,
                    workload=workload,
                    expected_count=expected_count,
                )
            )
        provider_summaries.append(
            {
                "provider": provider,
                "complete_coverage": expected_provider_keys == actual_provider_keys,
                "expected_cases": sum(expected_workloads.values()),
                "completed_cases": len(provider_results),
                "final_passes": sum(
                    result.get("status") in {"passed", "passed_with_retry"}
                    for result in provider_results
                ),
                "status_counts": _result_status_counts(provider_results),
                "failure_cases_by_axis": _axis_failure_counts(provider_results),
                **_aggregate_audit_counts(provider_results),
                "metrics": _summed_metrics(provider_results),
                "workloads": workloads,
            }
        )

    return {
        "provider_summaries": provider_summaries,
        "totals": {
            "expected_case_provider_pairs": len(expected_pairs),
            "completed_case_provider_pairs": len(results),
            "missing_profile_pairs": missing_pairs,
            "unexpected_profile_results": unexpected_results,
            **_summed_metrics(results),
        },
        "complete_profile": complete_profile,
        "promotion_eligible": False,
        "decision": "hold",
    }


def _build_isolated_runtimes(
    settings: Settings,
    *,
    routes: Sequence[CanaryRoute],
    provider_tiers: Mapping[str, Sequence[LLMTier]],
    guard: ShadowCostGuard,
    provider_builder: ProviderBuilder,
) -> tuple[
    dict[str, tuple[LLMOrchestrator, InMemoryLLMRuntimeRepository]],
    int,
    bool,
]:
    runtimes: dict[str, tuple[LLMOrchestrator, InMemoryLLMRuntimeRepository]] = {}
    cleanup_failures = 0
    for provider, tiers in provider_tiers.items():
        family_routes = tuple(
            route for route in routes if route.provider == provider and route.tier in tiers
        )
        _require(
            len(family_routes) == len(tiers),
            "an isolated shadow runtime is missing a required route",
        )
        isolated = _isolated_settings(settings, family_routes)
        built: dict[str, tuple[LLMProviderAdapter, ...]] = {}
        try:
            built = provider_builder(isolated, tiers=tiers)
            guarded: dict[str, tuple[LLMProviderAdapter, ...]] = {}
            for tier in tiers:
                adapters = built.get(tier.value, ())
                _require(
                    len(adapters) == 1,
                    "an isolated shadow tier must contain exactly one provider",
                )
                adapter = adapters[0]
                _require(
                    adapter.provider_name == provider,
                    "an isolated shadow tier was built for the wrong provider",
                )
                guarded[tier.value] = (
                    guard.wrap(
                        adapter,
                        max_output_tokens=int(
                            isolated.llm_tier_max_output_tokens.get(tier.value, 0)
                        ),
                    ),
                )
            repository = InMemoryLLMRuntimeRepository()
            runtimes[provider] = (
                LLMOrchestrator(
                    isolated,
                    repository,
                    guarded,
                    cache=None,
                    limiter=None,
                ),
                repository,
            )
        except Exception:  # noqa: BLE001 - startup report remains count-only
            cleanup_failures += _close_adapters(built)
            return runtimes, cleanup_failures, True
    return runtimes, cleanup_failures, False


def _close_adapters(adapters_by_tier: Mapping[str, Sequence[LLMProviderAdapter]]) -> int:
    failures = 0
    seen: set[int] = set()
    for adapters in adapters_by_tier.values():
        for adapter in adapters:
            if id(adapter) in seen:
                continue
            seen.add(id(adapter))
            close = getattr(adapter, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:  # noqa: BLE001 - count only; never return raw cleanup errors
                failures += 1
    return failures


def _close_runtimes(
    runtimes: Mapping[str, tuple[LLMOrchestrator, InMemoryLLMRuntimeRepository]],
) -> int:
    failures = 0
    for orchestrator, _repository in runtimes.values():
        try:
            orchestrator.close()
        except BaseException:  # noqa: BLE001 - cleanup must continue across all runtimes
            failures += 1
    return failures


def run_shadow_evaluation(
    settings: Settings,
    *,
    max_calls: int = DEFAULT_MAX_CALLS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    cases_path: Path = DEFAULT_CASES_PATH,
    provider_builder: ProviderBuilder = build_providers_by_tier,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run the complete matrix and return aggregate counters only."""

    cases_path = _resolve_path(cases_path)
    plan = build_shadow_plan(
        settings,
        max_calls=max_calls,
        max_cost_usd=max_cost_usd,
        cases_path=cases_path,
    )
    cases = load_shadow_cases(cases_path)
    routes = resolve_canary_routes(settings, _PROVIDERS)
    _require(
        all(_provider_key_present(settings, provider) for provider in _PROVIDERS),
        "one or more shadow provider API keys are not configured",
    )

    guard = ShadowCostGuard(
        settings,
        max_cost_usd=max_cost_usd,
        max_calls=max_calls,
        approved_routes_only=True,
    )
    runtimes, cleanup_failures, startup_failed = _build_isolated_runtimes(
        settings,
        routes=routes,
        provider_tiers={provider: (LLMTier.T1, LLMTier.T2) for provider in _PROVIDERS},
        guard=guard,
        provider_builder=provider_builder,
    )

    results: list[dict[str, Any]] = []
    execution_status = "startup_failed" if startup_failed else "complete"
    try:
        if not startup_failed:
            try:
                for case_index, case in enumerate(cases):
                    provider_order = (
                        _PROVIDERS if case_index % 2 == 0 else tuple(reversed(_PROVIDERS))
                    )
                    for provider in provider_order:
                        orchestrator, repository = runtimes[provider]
                        tier = LLMTier(str(case["tier"]))
                        route = _route_for(routes, provider, tier)
                        refused_before = int(guard.snapshot()["refused_calls"])
                        result = _evaluate_pair(
                            orchestrator,
                            repository,
                            guard,
                            route=route,
                            case=case,
                        )
                        results.append(result)
                        snapshot = guard.snapshot()
                        refused_after = int(snapshot["refused_calls"])
                        guard_stopped = guard.halted or refused_after > refused_before
                        should_report_progress = (
                            len(results) % 10 == 0
                            or len(results) == _EXPECTED_PROVIDER_PAIRS
                            or guard_stopped
                        )
                        if progress is not None and should_report_progress:
                            try:
                                progress(
                                    {
                                        "completed_pairs": len(results),
                                        "expected_pairs": _EXPECTED_PROVIDER_PAIRS,
                                        "provider": provider,
                                        "workload": str(case["workload"]),
                                        "status": result["status"],
                                        "network_calls": snapshot["network_calls"],
                                        "accounted_cost_upper_bound_usd": snapshot[
                                            "accounted_cost_upper_bound_usd"
                                        ],
                                    }
                                )
                            except Exception:  # noqa: BLE001 - display callbacks cannot affect gates
                                pass
                        if guard_stopped:
                            execution_status = "guard_stopped"
                            break
                    if execution_status != "complete":
                        break
            except Exception:  # noqa: BLE001 - partial result remains aggregate-only
                execution_status = "evaluation_stopped"
    finally:
        cleanup_failures += _close_runtimes(runtimes)

    aggregation = aggregate_shadow_results(
        results,
        expected_case_ids=_expected_ids_by_workload(cases),
        providers=_PROVIDERS,
    )
    if execution_status != "complete" or cleanup_failures:
        aggregation["decision"] = "hold"
    report = {
        "schema": REPORT_SCHEMA,
        "mode": "live",
        "fixture": plan["fixture"],
        "routes": plan["routes"],
        "guardrails": {
            **plan["guardrails"],
            "cost_ledger": guard.snapshot(),
        },
        "gates": plan["gates"],
        "external_io": {
            "network": guard.total_calls > 0,
            "database": False,
            "redis": False,
            "broker": False,
            "cache": False,
            "files_written": False,
        },
        "execution_status": execution_status,
        "cleanup_failure_count": cleanup_failures,
        **aggregation,
        "decision_scope": "limited_production_rollout_only",
    }
    return report


def run_shadow_diagnostic(
    settings: Settings,
    *,
    max_calls: int = DIAGNOSTIC_MAX_CALLS,
    max_cost_usd: float = DIAGNOSTIC_MAX_COST_USD,
    cases_path: Path = DEFAULT_CASES_PATH,
    provider_builder: ProviderBuilder = build_providers_by_tier,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run the fixed low-cost failure-triage profile; promotion is impossible."""

    cases_path = _resolve_path(cases_path)
    plan = build_shadow_diagnostic_plan(
        settings,
        max_calls=max_calls,
        max_cost_usd=max_cost_usd,
        cases_path=cases_path,
    )
    cases = load_shadow_cases(cases_path)
    pairs = _diagnostic_pairs(cases)
    routes = _diagnostic_routes(resolve_canary_routes(settings, _PROVIDERS))
    _require(
        all(_provider_key_present(settings, provider) for provider in _DIAGNOSTIC_PROVIDER_TIERS),
        "one or more diagnostic provider API keys are not configured",
    )

    guard = ShadowCostGuard(
        settings,
        max_cost_usd=max_cost_usd,
        max_calls=max_calls,
        approved_routes_only=True,
    )
    runtimes, cleanup_failures, startup_failed = _build_isolated_runtimes(
        settings,
        routes=routes,
        provider_tiers=_DIAGNOSTIC_PROVIDER_TIERS,
        guard=guard,
        provider_builder=provider_builder,
    )

    results: list[dict[str, Any]] = []
    execution_status = "startup_failed" if startup_failed else "complete"
    try:
        if not startup_failed:
            try:
                for provider, case in pairs:
                    orchestrator, repository = runtimes[provider]
                    tier = LLMTier(str(case["tier"]))
                    route = _route_for(routes, provider, tier)
                    refused_before = int(guard.snapshot()["refused_calls"])
                    result = _evaluate_pair(
                        orchestrator,
                        repository,
                        guard,
                        route=route,
                        case=case,
                    )
                    results.append(result)
                    snapshot = guard.snapshot()
                    refused_after = int(snapshot["refused_calls"])
                    guard_stopped = guard.halted or refused_after > refused_before
                    if progress is not None:
                        try:
                            progress(
                                {
                                    "completed_pairs": len(results),
                                    "expected_pairs": len(pairs),
                                    "network_calls": snapshot["network_calls"],
                                    "accounted_cost_upper_bound_usd": snapshot[
                                        "accounted_cost_upper_bound_usd"
                                    ],
                                }
                            )
                        except Exception:  # noqa: BLE001 - display callbacks cannot affect results
                            pass
                    if guard_stopped:
                        execution_status = "guard_stopped"
                        break
            except Exception:  # noqa: BLE001 - partial diagnostic remains count-only
                execution_status = "evaluation_stopped"
    finally:
        cleanup_failures += _close_runtimes(runtimes)

    aggregation = aggregate_shadow_diagnostic_results(results, expected_pairs=pairs)
    return {
        "schema": DIAGNOSTIC_REPORT_SCHEMA,
        "mode": "diagnostic_live",
        "diagnostic_profile": DIAGNOSTIC_PROFILE,
        "fixture": plan["fixture"],
        "routes": plan["routes"],
        "sample_counts": plan["sample_counts"],
        "guardrails": {
            **plan["guardrails"],
            "cost_ledger": guard.snapshot(),
        },
        "external_io": {
            "network": guard.total_calls > 0,
            "database": False,
            "redis": False,
            "broker": False,
            "cache": False,
            "files_written": False,
        },
        "execution_status": execution_status,
        "cleanup_failure_count": cleanup_failures,
        **aggregation,
        "decision_scope": "diagnostic_only",
    }


__all__ = [
    "CASES_SCHEMA",
    "DEFAULT_CASES_PATH",
    "DEFAULT_MAX_CALLS",
    "DEFAULT_MAX_COST_USD",
    "DIAGNOSTIC_MAX_CALLS",
    "DIAGNOSTIC_MAX_COST_USD",
    "DIAGNOSTIC_PROFILE",
    "DIAGNOSTIC_REPORT_SCHEMA",
    "FROZEN_SHADOW_FIXTURE_SHA256",
    "REPORT_SCHEMA",
    "LLMShadowEvaluationError",
    "ScoredBlock",
    "aggregate_shadow_diagnostic_results",
    "aggregate_shadow_results",
    "build_shadow_diagnostic_plan",
    "build_shadow_plan",
    "load_shadow_cases",
    "run_shadow_diagnostic",
    "run_shadow_evaluation",
    "score_shadow_analogy_case",
    "score_shadow_composition_case",
    "score_shadow_entity_case",
    "score_shadow_grounding_case",
    "shadow_fixture_sha256",
]
