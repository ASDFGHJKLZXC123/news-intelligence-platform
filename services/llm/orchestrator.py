"""Orchestrator for provider-selection, caching, validation, and persistence."""

from __future__ import annotations

import datetime
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic import ValidationError

from db.models.core import Job, LLMRun
from packages.config.settings import Settings
from services.llm import contracts
from services.llm.adapters import (
    LLMInvocationMode,
    LLMInvocationRequest,
    LLMInvocationResponse,
    LLMProviderAdapter,
)
from services.llm.cache import PromptCache, make_llm_request_cache_key
from services.llm.http_providers import (
    LLMRetryableProviderOutputError,
    provider_failure_diagnostic_code,
)
from services.llm.limiter import TokenBucketLimiter
from services.llm.policy import (
    LLMBudgetPolicy,
    LLMRoutingContext,
    LLMRoutingDecision,
    LLMTier,
)
from services.llm.pricing import estimate_completion_cost_usd
from services.llm.repository import LLMRuntimeRepository
from services.llm.selection import (
    RepresentativeArticleSelection,
    estimate_token_count,
    select_representative_articles,
)
from services.personal.deadlines import propagate_fatal


class LLMOrchestratorError(RuntimeError):
    """Base orchestration failure."""


class LLMValidationFailure(LLMOrchestratorError):
    """Raised when schema validation fails twice for one provider attempt."""


class LLMInvocationFailure(LLMOrchestratorError):
    """Raised when providers are exhausted or invoke fails."""


class LLMConfigurationError(LLMOrchestratorError):
    """Raised when a request reaches the orchestrator without an ID whitelist."""


@dataclass(frozen=True)
class LLMOrchestratorRequest:
    """Input required to execute one deterministic LLM run.

    ``allowed_ids`` is the ID whitelist injected for this run. ``None`` means the caller
    never configured one and the run is rejected before any provider is called; an empty
    tuple is a deliberate whitelist that permits zero model-emitted IDs.
    """

    job: Job
    prompt_name: str
    prompt_version: str
    prompt_template_version: str
    requested_schema: str
    prompt: str
    requested_tier: LLMTier = LLMTier.T1
    risk_level: str = "low"
    current_event_hotness: float = 0.0
    trailing_7d_p90_hotness: float | None = None
    is_realtime: bool = False
    ranking: int | None = None
    is_essential: bool = True
    articles: tuple[dict[str, Any], ...] = ()
    allowed_ids: tuple[str, ...] | None = None
    context: dict[str, Any] | None = None
    #: Sampling temperature for this run. ``None`` leaves the provider default in place, which
    #: is what every caller before ADR 0005 stage 3 relied on; a caller that needs a
    #: reproducible run asks for one (adjudication asks for 0).
    temperature: float | None = None


@dataclass(frozen=True)
class LLMOrchestratorResult:
    """Successful orchestrator output with execution telemetry."""

    run: LLMRun
    contract: contracts.BaseLLMContract
    trace_id: str
    cache_hit: bool
    tier: LLMTier
    mode: LLMInvocationMode
    queue: str
    degraded_provider: str | None
    route_degradation_reasons: tuple[str, ...]
    selected_articles: RepresentativeArticleSelection


class LLMOrchestrator:
    """Provider-neutral runtime that executes schema-bound LLM invocations."""

    _default_cache_ttl_seconds = 3600

    def __init__(
        self,
        settings: Settings,
        repository: LLMRuntimeRepository,
        providers_by_tier: dict[str, tuple[LLMProviderAdapter, ...]],
        *,
        cache: PromptCache | None = None,
        limiter: TokenBucketLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._providers_by_tier = providers_by_tier
        self._cache = cache
        self._limiter = limiter
        self._policy = LLMBudgetPolicy(settings)
        self._closed = False

    def close(self) -> None:
        """Close provider-owned HTTP clients when this runtime's unit of work ends."""

        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        seen: set[int] = set()
        for tier_providers in self._providers_by_tier.values():
            for provider in tier_providers:
                if id(provider) in seen:
                    continue
                seen.add(id(provider))
                close = getattr(provider, "close", None)
                if not callable(close):
                    continue
                try:
                    close()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("failed to close LLM provider clients", errors)

    def __enter__(self) -> LLMOrchestrator:
        return self

    def __exit__(
        self,
        _exc_type: object,
        cause: BaseException | None,
        _traceback: object,
    ) -> None:
        if cause is None:
            self.close()
            return
        try:
            self.close()
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "LLM orchestration and provider cleanup both failed",
                [cause, cleanup_error],
            ) from cause

    @staticmethod
    def _utc_now() -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    @staticmethod
    def _build_trace_id() -> str:
        return uuid.uuid4().hex

    def _providers_for_tier(self, tier: LLMTier) -> tuple[LLMProviderAdapter, ...]:
        return self._providers_by_tier.get(tier.value, ()) or self._providers_by_tier.get("T1", ())

    @staticmethod
    def _article_text(article: dict[str, Any]) -> str:
        for field in ("summary", "text", "title", "body"):
            value = article.get(field)
            if isinstance(value, str) and value.strip():
                return value
        return str(article)

    @staticmethod
    def _resolve_mode(
        decision: LLMRoutingDecision,
        providers: tuple[LLMProviderAdapter, ...],
    ) -> LLMRoutingDecision:
        """Degrade batch to realtime when no provider in the tier accepts batch mode.

        Provider Batch APIs are asynchronous (submit, poll, fetch), so the live adapters
        reject batch rather than pass a pending submission off as a completed response.
        Routing falls back here instead of failing the run.
        """

        if decision.mode is not LLMInvocationMode.BATCH:
            return decision
        if any(provider.supports_mode(LLMInvocationMode.BATCH) for provider in providers):
            return decision
        if not any(provider.supports_mode(LLMInvocationMode.REALTIME) for provider in providers):
            return decision
        return replace(
            decision,
            mode=LLMInvocationMode.REALTIME,
            degraded_reasons=(*decision.degraded_reasons, "batch_unsupported_realtime_fallback"),
        )

    def _build_request(
        self,
        request: LLMOrchestratorRequest,
        selection: RepresentativeArticleSelection,
        *,
        mode: LLMInvocationMode,
    ) -> LLMInvocationRequest:
        selected_context: dict[str, Any] = {
            "selected_article_count": selection.selected_count,
            "selected_article_tokens": selection.used_token_budget,
            "selected_article_dropped": selection.dropped_count,
            "selected_article_truncated_by_budget": selection.truncated_by_budget,
        }
        if request.context:
            selected_context.update(request.context)

        if selection.selected_count == 0:
            prompt = request.prompt
        else:
            bullets = [
                f"{idx}. {self._article_text(article)}"
                for idx, article in enumerate(selection.selected_articles, start=1)
            ]
            prompt = "\n\n".join(
                (
                    request.prompt,
                    "Representative articles:",
                    *bullets,
                )
            )

        return LLMInvocationRequest(
            prompt_name=request.prompt_name,
            prompt_version=request.prompt_version,
            prompt_template_version=request.prompt_template_version,
            prompt=prompt,
            requested_schema=request.requested_schema,
            context=selected_context,
            mode=mode,
            temperature=request.temperature,
        )

    @staticmethod
    def _with_feedback(request: LLMInvocationRequest, errors: str) -> LLMInvocationRequest:
        return request.with_feedback(f"Validation errors: {errors}")

    @staticmethod
    def _copy_job(
        job: Job,
        *,
        state: str,
        error: dict[str, Any] | None,
        now: datetime.datetime,
    ) -> Job:
        return Job(
            id=job.id,
            job_key=job.job_key,
            job_type=job.job_type,
            state=state,
            attempt=job.attempt,
            max_attempts=job.max_attempts,
            related_ids=job.related_ids,
            error=error,
            safe_to_rerun=job.safe_to_rerun,
            created_at=job.created_at,
            updated_at=now,
        )

    def _build_run(
        self,
        *,
        request: LLMOrchestratorRequest,
        routing_tier: LLMTier,
        mode: LLMInvocationMode,
        queue: str,
        trace_id: str,
        degraded_provider: str | None,
        route_degraded_reasons: tuple[str, ...],
        cache_hash: str,
        provider: LLMProviderAdapter,
        status: str,
        attempt: int,
        response: LLMInvocationResponse,
        started_at: datetime.datetime,
        completed_at: datetime.datetime,
        latency_ms: int,
        cache_hit: bool,
        cost_usd: float,
        contract: contracts.BaseLLMContract | None = None,
        payload: Mapping[str, Any] | None = None,
        raw_payload: Mapping[str, Any] | None = None,
        error_message: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> LLMRun:
        output_payload: Mapping[str, Any] = payload if payload is not None else {}
        output_schema_name = output_schema_version = no_finding_reason = None
        if contract is not None:
            output_schema_name = contract.schema_name
            output_schema_version = contract.schema_version
            no_finding_reason = contract.no_finding_reason
            if payload is None:
                # JSON mode: the run output is persisted to a JSON column, which does not
                # accept a `datetime.date`.
                output_payload = contract.to_normalized_payload()

        adapter_params = {
            key: response.raw_metadata[key]
            for key in (
                "temperature_requested",
                "temperature_sent",
                "temperature_strategy",
                "thinking_mode",
                "thinking_level",
                "report_composition_budget_instruction",
            )
            if key in response.raw_metadata
        }
        model_params: dict[str, Any] = {
            "tier": routing_tier.value,
            "mode": mode.value,
            "queue": queue,
            "degraded_provider": degraded_provider,
            "degraded_reasons": list(route_degraded_reasons),
        }
        if adapter_params:
            # Canonical parameters sometimes need an explicit provider translation (for
            # example, Gemini's deterministic-instruction replacement for temperature 0).
            # Persist it so the audit row never implies that an unsupported wire field was sent.
            model_params["adapter_params"] = adapter_params

        return LLMRun(
            prompt_name=request.prompt_name,
            prompt_version=request.prompt_version,
            prompt_template_version=request.prompt_template_version,
            prompt_hash=cache_hash,
            provider=provider.provider_name,
            model=provider.model_name,
            model_params=model_params,
            temperature=request.temperature,
            seed=None,
            status=status,
            attempt=attempt,
            error_message=error_message,
            error_details=error_details,
            output_schema_name=output_schema_name,
            output_schema_version=output_schema_version,
            input_refs={
                "trace_id": trace_id,
                "job_key": request.job.job_key,
                "requested_schema": request.requested_schema,
                "cache_hit": cache_hit,
            },
            output=dict(output_payload),
            raw_output=dict(raw_payload) if raw_payload is not None else None,
            evidence_refs=request.context or {},
            no_finding_reason=no_finding_reason,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            trace_id=trace_id,
            started_at=started_at,
            completed_at=completed_at,
        )

    @staticmethod
    def _validate_payload(
        *,
        payload: Mapping[str, Any],
        allowed_ids: tuple[str, ...],
    ) -> contracts.BaseLLMContract:
        # An empty tuple is forwarded as an empty list, never collapsed to None: it is a
        # whitelist that admits no IDs, not a missing whitelist.
        return contracts.validate_llm_payload(payload=payload, allowed_ids=list(allowed_ids))

    def _invoke(
        self, provider: LLMProviderAdapter, request: LLMInvocationRequest
    ) -> LLMInvocationResponse:
        if request.mode == LLMInvocationMode.BATCH:
            if not provider.supports_mode(LLMInvocationMode.BATCH):
                raise LLMInvocationFailure(
                    f"provider {provider.provider_name} does not support batch mode"
                )
            responses = provider.invoke_batch((request,))
            if not responses:
                raise LLMInvocationFailure("provider returned empty batch response")
            if len(responses) != 1:
                raise LLMInvocationFailure("provider returned multiple batch responses")
            return responses[0]

        if not provider.supports_mode(LLMInvocationMode.REALTIME):
            raise LLMInvocationFailure(
                f"provider {provider.provider_name} does not support realtime mode"
            )
        return provider.invoke(request)

    def _cache_fetch(self, cache_key: str) -> Mapping[str, Any] | None:
        if self._cache is None:
            return None
        return self._cache.get(cache_key)

    @staticmethod
    def _estimate_cost(
        *,
        settings: Settings,
        provider: LLMProviderAdapter,
        response: LLMInvocationResponse,
        cache_hit: bool,
        mode: LLMInvocationMode,
    ) -> float:
        if cache_hit:
            return 0.0
        return estimate_completion_cost_usd(
            settings=settings,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            invocation_mode=mode,
        )

    @staticmethod
    def _response_from_cache(
        *,
        cache_payload: Mapping[str, Any],
        provider: LLMProviderAdapter,
    ) -> LLMInvocationResponse:
        adapter_variant = getattr(provider, "cache_variant", None)
        raw_metadata = dict(adapter_variant) if isinstance(adapter_variant, Mapping) else {}
        return LLMInvocationResponse(
            text=json.dumps(cache_payload, sort_keys=True, separators=(",", ":")),
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            model_version=provider.model_version,
            input_tokens=0,
            output_tokens=0,
            structured=dict(cache_payload),
            raw_metadata=raw_metadata,
        )

    def run(self, request: LLMOrchestratorRequest) -> LLMOrchestratorResult:
        if request.allowed_ids is None:
            raise LLMConfigurationError(
                "allowed_ids whitelist is required: orchestration must inject the IDs the "
                "model may reference, or pass an explicit empty tuple to permit none"
            )
        allowed_ids: tuple[str, ...] = request.allowed_ids

        trace_id = self._build_trace_id()

        routing_context = LLMRoutingContext(
            risk_level=request.risk_level,
            current_event_hotness=request.current_event_hotness,
            trailing_7d_p90_hotness=request.trailing_7d_p90_hotness,
            is_realtime=request.is_realtime,
            is_essential=request.is_essential,
            ranking=request.ranking,
            requested_tier=request.requested_tier,
        )
        spent_this_month = self._repository.monthly_spend_usd(as_of=self._utc_now())
        decision = self._policy.decide(routing_context, monthly_spend_usd=spent_this_month)
        providers = self._providers_for_tier(decision.tier)

        if not providers:
            failed_job = self._copy_job(
                request.job,
                state="failed",
                error={"code": "no_provider", "message": "no providers configured"},
                now=self._utc_now(),
            )
            self._repository.save_job(failed_job)
            raise LLMInvocationFailure(f"no providers configured for tier {decision.tier.value}")

        decision = self._resolve_mode(decision, providers)
        selection = self._select_articles(
            request,
            context_token_budget=self._policy.context_token_budget(decision.tier),
        )
        base_request = self._build_request(request, selection, mode=decision.mode)

        attempt = 1
        last_error: str | None = None

        for provider_index, provider in enumerate(providers):
            if not provider.supports_structured_schema(request.requested_schema):
                continue
            if not provider.supports_mode(decision.mode):
                continue

            degraded_provider = provider.provider_name if provider_index > 0 else None
            working_request = base_request

            for retry in range(2):
                call_attempt = attempt
                attempt += 1

                started_at = self._utc_now()
                cache_key = make_llm_request_cache_key(
                    request=working_request,
                    provider_name=provider.provider_name,
                    model_name=provider.model_name,
                    model_version=provider.model_version,
                    mode=decision.mode,
                    adapter_variant=getattr(provider, "cache_variant", None),
                )
                cache_hit = False

                cached_payload = self._cache_fetch(cache_key)
                if cached_payload is not None:
                    response = self._response_from_cache(
                        cache_payload=cached_payload,
                        provider=provider,
                    )
                    latency_ms = 0
                    cache_hit = True
                else:
                    latency_ms = 0
                    if self._limiter is not None:
                        # Every live adapter sends the contract schema in addition to the user
                        # prompt. DeepSeek also sends a schema-derived example. Reserve twice
                        # the schema estimate plus small instruction overhead so the local TPM
                        # ceiling covers the full structured request as well as possible output.
                        schema_text = json.dumps(
                            contracts.llm_contract_json_schema(working_request.requested_schema),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        token_cost = max(
                            1,
                            estimate_token_count(working_request.prompt)
                            + (2 * estimate_token_count(schema_text))
                            + 64
                            + self._policy.output_token_budget(decision.tier),
                        )
                        allowed = self._limiter.consume(
                            key=f"{provider.provider_name}:{provider.model_name}",
                            tokens=token_cost,
                        )
                        if not allowed:
                            self._repository.save_llm_run(
                                self._build_run(
                                    request=request,
                                    routing_tier=decision.tier,
                                    mode=decision.mode,
                                    queue=decision.queue,
                                    degraded_provider=degraded_provider,
                                    route_degraded_reasons=decision.degraded_reasons,
                                    cache_hash=cache_key,
                                    trace_id=trace_id,
                                    provider=provider,
                                    status="failed",
                                    attempt=call_attempt,
                                    response=LLMInvocationResponse(
                                        text="",
                                        provider_name=provider.provider_name,
                                        model_name=provider.model_name,
                                        model_version=provider.model_version,
                                    ),
                                    started_at=started_at,
                                    completed_at=self._utc_now(),
                                    latency_ms=0,
                                    cache_hit=False,
                                    cost_usd=0.0,
                                    error_message="token bucket limit reached",
                                    error_details={
                                        "provider": provider.provider_name,
                                        "mode": decision.mode.value,
                                    },
                                )
                            )
                            last_error = "token bucket limit reached"
                            break

                    start = time.monotonic()
                    try:
                        response = self._invoke(provider, working_request)
                        latency_ms = int((time.monotonic() - start) * 1000)
                    except LLMRetryableProviderOutputError as exc:
                        latency_ms = int((time.monotonic() - start) * 1000)
                        response = exc.response
                        self._repository.save_llm_run(
                            self._build_run(
                                request=request,
                                routing_tier=decision.tier,
                                mode=decision.mode,
                                queue=decision.queue,
                                degraded_provider=degraded_provider,
                                route_degraded_reasons=decision.degraded_reasons,
                                cache_hash=cache_key,
                                trace_id=trace_id,
                                provider=provider,
                                status="validation_failed",
                                attempt=call_attempt,
                                response=response,
                                started_at=started_at,
                                completed_at=self._utc_now(),
                                latency_ms=latency_ms,
                                cache_hit=False,
                                cost_usd=self._estimate_cost(
                                    settings=self._settings,
                                    provider=provider,
                                    response=response,
                                    cache_hit=False,
                                    mode=decision.mode,
                                ),
                                error_message="provider returned retryable structured output",
                                error_details={
                                    "provider": provider.provider_name,
                                    "mode": decision.mode.value,
                                    "error": str(exc),
                                    "provider_failure_code": provider_failure_diagnostic_code(exc),
                                },
                            )
                        )
                        last_error = str(exc)
                        if retry == 0:
                            working_request = self._with_feedback(
                                working_request,
                                "Return one non-empty JSON object matching the requested schema.",
                            )
                            continue
                        break
                    except Exception as exc:  # noqa: BLE001
                        propagate_fatal(exc)
                        latency_ms = int((time.monotonic() - start) * 1000)
                        billed_response = getattr(exc, "response", None)
                        if not isinstance(billed_response, LLMInvocationResponse):
                            billed_response = LLMInvocationResponse(
                                text="",
                                provider_name=provider.provider_name,
                                model_name=provider.model_name,
                                model_version=provider.model_version,
                            )
                        self._repository.save_llm_run(
                            self._build_run(
                                request=request,
                                routing_tier=decision.tier,
                                mode=decision.mode,
                                queue=decision.queue,
                                degraded_provider=degraded_provider,
                                route_degraded_reasons=decision.degraded_reasons,
                                cache_hash=cache_key,
                                trace_id=trace_id,
                                provider=provider,
                                status="failed",
                                attempt=call_attempt,
                                response=billed_response,
                                started_at=started_at,
                                completed_at=self._utc_now(),
                                latency_ms=latency_ms,
                                cache_hit=False,
                                cost_usd=self._estimate_cost(
                                    settings=self._settings,
                                    provider=provider,
                                    response=billed_response,
                                    cache_hit=False,
                                    mode=decision.mode,
                                ),
                                error_message="provider invocation failure",
                                error_details={
                                    "provider": provider.provider_name,
                                    "mode": decision.mode.value,
                                    "error": str(exc),
                                    "provider_failure_code": provider_failure_diagnostic_code(exc),
                                },
                            )
                        )
                        last_error = str(exc)
                        break

                completed_at = self._utc_now()
                try:
                    parsed_payload = dict(response.parsed_payload())
                    contract = self._validate_payload(
                        payload=parsed_payload,
                        allowed_ids=allowed_ids,
                    )
                except (
                    TypeError,
                    json.JSONDecodeError,
                    ValidationError,
                    contracts.LLMContractValidationError,
                ) as exc:
                    self._repository.save_llm_run(
                        self._build_run(
                            request=request,
                            routing_tier=decision.tier,
                            mode=decision.mode,
                            queue=decision.queue,
                            degraded_provider=degraded_provider,
                            route_degraded_reasons=decision.degraded_reasons,
                            cache_hash=cache_key,
                            trace_id=trace_id,
                            provider=provider,
                            status="validation_failed",
                            attempt=call_attempt,
                            response=response,
                            started_at=started_at,
                            completed_at=completed_at,
                            latency_ms=0 if cache_hit else latency_ms,
                            cache_hit=cache_hit,
                            cost_usd=self._estimate_cost(
                                settings=self._settings,
                                provider=provider,
                                response=response,
                                cache_hit=cache_hit,
                                mode=decision.mode,
                            ),
                            error_message="schema validation failed",
                            error_details={
                                "provider": provider.provider_name,
                                "attempt": call_attempt,
                                "errors": str(exc),
                                "mode": decision.mode.value,
                            },
                        )
                    )
                    if retry == 0:
                        working_request = self._with_feedback(working_request, str(exc))
                        continue

                    failed_job = self._copy_job(
                        request.job,
                        state="llm_validation_failed",
                        error={
                            "code": "schema_validation",
                            "message": str(exc),
                            "provider": provider.provider_name,
                        },
                        now=completed_at,
                    )
                    self._repository.save_job(failed_job)
                    raise LLMValidationFailure(str(exc)) from exc

                cost_usd = self._estimate_cost(
                    settings=self._settings,
                    provider=provider,
                    response=response,
                    cache_hit=cache_hit,
                    mode=decision.mode,
                )
                status = "cached" if cache_hit else "succeeded"
                run = self._build_run(
                    request=request,
                    routing_tier=decision.tier,
                    mode=decision.mode,
                    queue=decision.queue,
                    degraded_provider=degraded_provider,
                    route_degraded_reasons=decision.degraded_reasons,
                    cache_hash=cache_key,
                    trace_id=trace_id,
                    provider=provider,
                    status=status,
                    attempt=call_attempt,
                    response=response,
                    payload=contract.to_normalized_payload(),
                    raw_payload=parsed_payload,
                    started_at=started_at,
                    completed_at=completed_at,
                    latency_ms=0 if cache_hit else latency_ms,
                    cache_hit=cache_hit,
                    cost_usd=cost_usd,
                    contract=contract,
                )
                self._repository.save_llm_run(run)

                if not cache_hit and self._cache is not None:
                    # Cache the provider-replay shape, never `run.output`: a cache hit is
                    # re-validated through the same contract as a live response, and
                    # `run.output` carries derived `severity`, which `extra="forbid"` would
                    # (correctly) reject on the way back in.
                    self._cache.set(
                        cache_key,
                        contract.to_provider_payload(),
                        ttl_seconds=self._default_cache_ttl_seconds,
                    )

                self._repository.save_job(
                    self._copy_job(
                        request.job,
                        state="succeeded",
                        error=None,
                        now=self._utc_now(),
                    )
                )
                return LLMOrchestratorResult(
                    run=run,
                    contract=contract,
                    trace_id=trace_id,
                    cache_hit=cache_hit,
                    tier=decision.tier,
                    mode=decision.mode,
                    queue=decision.queue,
                    degraded_provider=degraded_provider,
                    route_degradation_reasons=decision.degraded_reasons,
                    selected_articles=selection,
                )

        failed_job = self._copy_job(
            request.job,
            state="failed",
            error={"code": "provider_exhausted", "message": last_error},
            now=self._utc_now(),
        )
        self._repository.save_job(failed_job)
        raise LLMInvocationFailure("all providers failed")

    @staticmethod
    def _select_articles(
        request: LLMOrchestratorRequest,
        *,
        context_token_budget: int,
    ) -> RepresentativeArticleSelection:
        return select_representative_articles(
            request.articles,
            max_articles=12,
            context_token_budget=context_token_budget,
            score_key="relevance_score",
        )


__all__ = [
    "LLMConfigurationError",
    "LLMOrchestrator",
    "LLMOrchestratorError",
    "LLMOrchestratorRequest",
    "LLMOrchestratorResult",
    "LLMValidationFailure",
    "LLMInvocationFailure",
]
