"""Production adapters whose only paid HTTP path commits through SpendingLedger."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any

import httpx

from packages.config.settings import Settings
from packages.providers.openai_embeddings import OpenAIEmbeddingProvider
from services.llm.cache import RedisLLMPromptCache
from services.llm.http_providers import (
    AnthropicMessagesProvider,
    DeepSeekChatCompletionsProvider,
    GeminiInteractionsProvider,
    OpenAIChatCompletionsProvider,
)
from services.llm.limiter import build_provider_rate_limiter
from services.llm.orchestrator import LLMOrchestrator
from services.llm.repository import SQLAlchemyLLMRuntimeRepository
from services.personal.deadlines import bind_budget, check_budget, require_call_budget
from services.personal.live_smoke import (
    _assert_single_generation,
    _response_usage,
    conservative_payload_token_bound,
)
from services.personal.local_runtime import PersonalPaidRuntime
from services.personal.spending import (
    PaidRoute,
    PaidWorkBlocked,
    SpendingLedger,
    validate_model_route,
)

_BASE_URLS = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "deepseek": "https://api.deepseek.com",
}
_PROVIDERS = {
    "openai": OpenAIChatCompletionsProvider,
    "anthropic": AnthropicMessagesProvider,
    "gemini": GeminiInteractionsProvider,
    "deepseek": DeepSeekChatCompletionsProvider,
}
_PATHS = {
    "openai": "/v1/chat/completions",
    "anthropic": "/v1/messages",
    "gemini": "/v1/interactions",
    "deepseek": "/chat/completions",
}


class _LedgerAuditRepository(SQLAlchemyLLMRuntimeRepository):
    personal_paid_ledger_accounted = True

    def save_llm_run(self, run):  # noqa: ANN001, ANN202
        # The physical dispatch ledger is authoritative. Avoid importing this business
        # audit copy as another legacy charge after a restart or writer-mode change.
        run.model_params = {**(run.model_params or {}), "personal_paid_ledger_accounted": True}
        super().save_llm_run(run)


class DeadlineHTTPClient:
    """Synchronous adapter with one cancellable deadline over connect, headers and body.

    httpx's normal timeout applies separately to socket operations, allowing an
    indefinitely trickling response. An owned async request plus asyncio.timeout
    enforces the full elapsed deadline and closes its transport on cancellation.
    No request thread or executor is left running after this method returns.
    """

    def __init__(self, *, base_url: str, transport: Any = None) -> None:
        self.base_url = base_url
        self.transport = transport

    def validate_dispatch_context(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise PaidWorkBlocked(
            "configuration_missing",
            "The synchronous paid worker cannot run inside an active event loop",
        )

    def post(self, path: str, *, timeout: float, **kwargs: Any) -> httpx.Response:
        self.validate_dispatch_context()

        async def request() -> httpx.Response:
            try:
                async with asyncio.timeout(timeout):
                    async with httpx.AsyncClient(
                        base_url=self.base_url,
                        timeout=timeout,
                        transport=self.transport,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        return await client.post(path, **kwargs)
            except TimeoutError as exc:
                raise httpx.ReadTimeout("The paid request's total deadline elapsed") from exc

        return asyncio.run(request())

    def close(self) -> None:
        # Every post owns and closes its client, including cancellation/timeout paths.
        pass


def _output_cap(payload: Mapping[str, Any], route: PaidRoute) -> int:
    caps = [
        payload[key]
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens", "maxOutputTokens")
        if key in payload
    ]
    for key in ("generation_config", "generationConfig"):
        container = payload.get(key)
        if isinstance(container, Mapping):
            caps.extend(
                container[name]
                for name in ("max_output_tokens", "maxOutputTokens")
                if name in container
            )
    if route.role == "embedding":
        if caps:
            raise PaidWorkBlocked(
                "configuration_missing", "Embeddings cannot request output tokens"
            )
        return 0
    if (
        not caps
        or any(type(cap) is not int or not 1 <= cap <= route.max_output_tokens for cap in caps)
        or len(set(caps)) != 1
    ):
        raise PaidWorkBlocked(
            "configuration_missing", "Paid request requires one finite output-token cap"
        )
    return caps[0]


class DurablePaidHTTPClient:
    """One POST, one immutable request ID; every provider retry passes here again."""

    def __init__(
        self,
        *,
        route: PaidRoute,
        ledger: SpendingLedger,
        delegate: Any,
        local_runtime: PersonalPaidRuntime | None = None,
    ) -> None:
        self.route, self.ledger, self.delegate = route, ledger, delegate
        self.local_runtime = local_runtime

    def post(self, path: str, **kwargs: Any) -> Any:
        input_bound, output_bound = self._validate_request(path, kwargs)
        if self.local_runtime is None:
            return self._dispatch(path, input_bound, output_bound, kwargs)
        with self.local_runtime.call(self.route, input_bound + output_bound):
            return self._dispatch(path, input_bound, output_bound, kwargs)

    def _validate_request(self, path: str, kwargs: Mapping[str, Any]) -> tuple[int, int]:
        expected = (
            "/v1/embeddings" if self.route.role == "embedding" else _PATHS[self.route.provider]
        )
        if path != expected:
            raise PaidWorkBlocked(
                "configuration_missing", "Paid endpoint differs from the resolved route"
            )
        if set(kwargs) - {"json", "headers"}:
            raise PaidWorkBlocked(
                "configuration_missing", "Paid request cannot override its transport bounds"
            )
        payload = kwargs.get("json")
        if not isinstance(payload, Mapping) or payload.get("model") != self.route.model:
            raise PaidWorkBlocked(
                "configuration_missing", "Paid request model differs from the resolved route"
            )
        _assert_single_generation(payload, role=self.route.role)
        input_bound = conservative_payload_token_bound(payload)
        output_bound = _output_cap(payload, self.route)
        if input_bound > self.route.max_input_tokens:
            raise PaidWorkBlocked(
                "request_bound_exceeded", "Paid input exceeds the frozen finite token ceiling"
            )
        if isinstance(self.delegate, DeadlineHTTPClient):
            self.delegate.validate_dispatch_context()
        return input_bound, output_bound

    def _dispatch(
        self, path: str, input_bound: int, output_bound: int, kwargs: Mapping[str, Any]
    ) -> Any:
        require_call_budget(self.route.deadline_seconds)
        request_id = self.ledger.reserve(
            self.route, input_token_bound=input_bound, output_token_bound=output_bound
        )
        try:
            require_call_budget(self.route.deadline_seconds)
            self.ledger.dispatch(request_id)
        except BaseException:
            # Fatal cancellation after reservation is still provably pre-network.
            # Only accounting cleanup may outlive the graceful business budget.
            with bind_budget(None):
                self.ledger.cancel_before_dispatch(request_id)
            raise
        try:
            response = self.delegate.post(path, **kwargs, timeout=self.route.deadline_seconds)
        except BaseException:
            with bind_budget(None):
                self.ledger.mark_uncertain(request_id)
            raise
        if self.local_runtime is not None:
            self.local_runtime.record_retry_after(self.route.provider, response)
        usage = _response_usage(response, self.route.provider, self.route.role)
        if usage is None:
            with bind_budget(None):
                self.ledger.mark_uncertain(request_id, "provider_response_usage_unknown")
            # Failed HTTP responses may trigger a bounded provider retry, which must
            # get its own reservation; a successful response with unknown cost cannot
            # be presented as successfully accounted enrichment.
            if 200 <= response.status_code < 300:
                raise PaidWorkBlocked("provider_usage_unknown")
            return response
        provider_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        try:
            body = response.json()
            if provider_id is None and isinstance(body.get("id"), str):
                provider_id = body["id"]
        except (AttributeError, ValueError, TypeError):
            pass
        # Only this accounting path may outlive business cancellation. It is
        # scoped to the already dispatched request and the hard watchdog stays armed.
        with bind_budget(None):
            self.ledger.reconcile(
                request_id,
                input_tokens=usage[0],
                output_tokens=usage[1],
                provider_request_id=provider_id,
                evidence={
                    "kind": "provider_response_usage",
                    "http_status": response.status_code,
                    "usage_sha256": hashlib.sha256(json.dumps(list(usage)).encode()).hexdigest(),
                },
            )
        check_budget()
        return response

    def close(self) -> None:
        close = getattr(self.delegate, "close", None)
        if callable(close):
            close()


def runtime_readiness(settings: Settings | None, model_route: Mapping[str, Any]) -> str | None:
    """Inspect the activation gate and exact configuration without constructing providers."""
    if getattr(settings, "personal_paid_runtime_enabled", False) is not True:
        return "paid_runtime_disabled"
    try:
        route = validate_model_route(model_route)
    except (TypeError, ValueError):
        return "configuration_missing"
    provider = route["generation"]["provider"]
    for name in (f"{provider}_api_key", "openai_api_key"):
        key = getattr(settings, name, "")
        if not isinstance(key, str) or not key.strip():
            return "configuration_missing"
    if provider == "gemini":
        levels = getattr(settings, "gemini_model_thinking_levels", {})
        level = levels.get(route["generation"]["model"]) if isinstance(levels, Mapping) else None
        if not isinstance(level, str) or level not in {"minimal", "low", "medium", "high"}:
            return "configuration_missing"
    return None


def _client(
    route: PaidRoute,
    ledger: SpendingLedger,
    local_runtime: PersonalPaidRuntime | None = None,
) -> DurablePaidHTTPClient:
    return DurablePaidHTTPClient(
        route=route,
        ledger=ledger,
        delegate=DeadlineHTTPClient(base_url=_BASE_URLS[route.provider]),
        local_runtime=local_runtime,
    )


def build_paid_embedding_provider(
    *,
    settings: Settings,
    route: PaidRoute,
    ledger: SpendingLedger,
    local_runtime: PersonalPaidRuntime | None = None,
) -> OpenAIEmbeddingProvider:
    client = (
        _client(route, ledger) if local_runtime is None else _client(route, ledger, local_runtime)
    )
    try:
        return OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model_name=route.model,
            model_version=route.model_version,
            client=client,
        )
    except BaseException:
        client.close()
        raise


def build_paid_orchestrator(
    *,
    session: Any,
    settings: Settings,
    route: PaidRoute,
    ledger: SpendingLedger,
    redis_client: Any = None,
    local_runtime: PersonalPaidRuntime | None = None,
) -> LLMOrchestrator:
    """Use the same paid route; Redis remains an explicit legacy dependency."""
    if redis_client is not None and local_runtime is not None:
        raise ValueError("choose either local or Redis paid-runtime dependencies")
    if redis_client is None and local_runtime is None:
        local_runtime = PersonalPaidRuntime.from_settings(settings, {route.provider})
    routed = settings.model_copy(
        update={
            "llm_models": {"T1": route.model, "T2": route.model, "T3": route.model},
            "llm_tier_providers": {
                "T1": route.provider,
                "T2": route.provider,
                "T3": route.provider,
            },
            "llm_tier_fallbacks": {},
            # The old completed-call/float budget cannot be authoritative, or separately
            # reject work using stale sample budgets. Every physical POST uses the ledger.
            "llm_budget_enforced": False,
            "llm_provider_token_price_usd_per_1m": {
                f"{route.provider}:{route.model}": {
                    "input": float(route.input_usd_per_million_tokens),
                    "output": float(route.output_usd_per_million_tokens),
                }
            },
            "llm_request_timeout_seconds": route.deadline_seconds,
            "llm_tier_max_output_tokens": {
                "T1": route.max_output_tokens,
                "T2": route.max_output_tokens,
                "T3": route.max_output_tokens,
            },
        }
    )
    client = (
        _client(route, ledger) if local_runtime is None else _client(route, ledger, local_runtime)
    )
    try:
        provider = _PROVIDERS[route.provider](
            settings=routed,
            model_name=route.model,
            model_version=route.model_version,
            max_output_tokens=route.max_output_tokens,
            client=client,
        )
        result = LLMOrchestrator(
            settings=routed,
            repository=_LedgerAuditRepository(session, commit_on_write=False),
            providers_by_tier={"T1": (provider,), "T2": (provider,), "T3": (provider,)},
            cache=local_runtime.cache
            if local_runtime is not None
            else RedisLLMPromptCache(redis_client),
            # Local admission is at the physical HTTP boundary, including embeddings
            # and retries. Do not consume the same local buckets a second time here.
            limiter=None
            if local_runtime is not None
            else build_provider_rate_limiter(
                redis_client=redis_client,
                rpm_limits={
                    route.provider: settings.llm_provider_rpm_limits.get(route.provider, 0)
                },
                tpm_limits={
                    route.provider: settings.llm_provider_tpm_limits.get(route.provider, 0)
                },
            ),
        )
        return result
    except BaseException:
        client.close()
        raise
