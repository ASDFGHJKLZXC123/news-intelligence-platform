"""Production HTTP adapters for the Anthropic and OpenAI providers (ADR 0008).

Both vendors expose *asynchronous* Batch APIs: a batch is submitted, polled until it
ends, and only then are results fetched. A synchronous adapter therefore cannot honour a
batch invocation, and pretending a pending submission is a completed response would
fabricate output. These adapters reject batch mode outright (`supports_mode` returns
False, `invoke_batch` raises) so the orchestrator can degrade the call to realtime.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from packages.config.settings import Settings
from services.llm.adapters import (
    LLMInvocationMode,
    LLMInvocationRequest,
    LLMInvocationResponse,
    LLMProviderAdapter,
)
from services.llm.contracts import (
    LLMContractLookupError,
    llm_contract_json_schema,
    resolve_llm_contract,
)
from services.llm.policy import LLMTier, resolve_tier_model


class LLMProviderError(RuntimeError):
    """Raised when a provider call fails or returns an unusable payload."""


class LLMBatchModeUnsupported(LLMProviderError):
    """Raised when batch mode is requested from a realtime-only adapter."""


def _with_temperature(
    body: dict[str, Any], request: LLMInvocationRequest
) -> dict[str, Any]:
    """Add the requested sampling temperature to a provider body, and only when one was asked for.

    Both vendors name the field ``temperature`` and both default it themselves. A request that
    asks for none therefore sends none, so every call written before the field existed keeps the
    exact body it had; a request that asks for 0 (ADR 0005 stage-3 adjudication) sends 0.
    """

    if request.temperature is None:
        return body
    return {**body, "temperature": float(request.temperature)}


class _HTTPProviderAdapter:
    """Shared transport plumbing for the live HTTP providers."""

    provider_name: str = ""

    def __init__(
        self,
        *,
        settings: Settings,
        model_name: str,
        max_output_tokens: int,
        api_key: str,
        base_url: str,
        model_version: str = "current",
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise LLMProviderError(f"{self.provider_name} API key is not configured")
        if max_output_tokens <= 0:
            raise LLMProviderError(
                f"{self.provider_name} requires a positive max output token budget"
            )
        self.model_name = model_name
        self.model_version = model_version
        self._settings = settings
        self._api_key = api_key
        self._max_output_tokens = max_output_tokens
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=settings.llm_request_timeout_seconds,
        )

    def supports_mode(self, mode: LLMInvocationMode) -> bool:
        return mode == LLMInvocationMode.REALTIME

    def supports_structured_schema(self, schema_name: str) -> bool:
        try:
            resolve_llm_contract(schema_name)
        except LLMContractLookupError:
            return False
        return True

    def invoke_batch(
        self, requests: Sequence[LLMInvocationRequest]
    ) -> Sequence[LLMInvocationResponse]:
        raise LLMBatchModeUnsupported(
            f"{self.provider_name} batch submission is asynchronous and cannot be served "
            "synchronously; route this call as realtime instead"
        )

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            response = self._client.post(path, json=dict(payload), headers=self._headers())
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"{self.provider_name} request failed: {exc}") from exc
        except ValueError as exc:
            raise LLMProviderError(f"{self.provider_name} returned non-JSON body") from exc

        if not isinstance(body, Mapping):
            raise LLMProviderError(f"{self.provider_name} response body must be a JSON object")
        return body


class AnthropicMessagesProvider(_HTTPProviderAdapter):
    """Anthropic Messages API adapter using a forced tool call for structured output."""

    provider_name = "anthropic"

    def __init__(self, *, settings: Settings, model_name: str, **kwargs: Any) -> None:
        super().__init__(
            settings=settings,
            model_name=model_name,
            api_key=kwargs.pop("api_key", settings.anthropic_api_key),
            base_url=kwargs.pop("base_url", settings.anthropic_base_url),
            **kwargs,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._settings.anthropic_api_version,
            "content-type": "application/json",
        }

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        schema_name = request.requested_schema
        body = self._post(
            "/v1/messages",
            _with_temperature(
                {
                    "model": self.model_name,
                    "max_tokens": self._max_output_tokens,
                    "messages": [{"role": "user", "content": request.prompt}],
                    "tools": [
                        {
                            "name": schema_name,
                            "description": f"Return exactly one {schema_name} payload.",
                            "input_schema": llm_contract_json_schema(schema_name),
                        }
                    ],
                    # Forcing the tool is what makes the response schema-shaped, not prose.
                    "tool_choice": {"type": "tool", "name": schema_name},
                },
                request,
            ),
        )

        structured = self._tool_input(body, schema_name)
        usage = body.get("usage") or {}
        return LLMInvocationResponse(
            text=json.dumps(structured, sort_keys=True, separators=(",", ":")),
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_version=self.model_version,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            structured=structured,
            raw_metadata={
                "response_id": body.get("id"),
                "stop_reason": body.get("stop_reason"),
            },
        )

    def _tool_input(self, body: Mapping[str, Any], schema_name: str) -> Mapping[str, Any]:
        for block in body.get("content") or ():
            if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                continue
            if block.get("name") != schema_name:
                continue
            payload = block.get("input")
            if not isinstance(payload, Mapping):
                raise LLMProviderError("anthropic tool_use input must be a JSON object")
            return dict(payload)
        raise LLMProviderError(f"anthropic response carried no {schema_name} tool_use block")


class OpenAIChatCompletionsProvider(_HTTPProviderAdapter):
    """OpenAI Chat Completions adapter using json_schema response formatting."""

    provider_name = "openai"

    def __init__(self, *, settings: Settings, model_name: str, **kwargs: Any) -> None:
        super().__init__(
            settings=settings,
            model_name=model_name,
            api_key=kwargs.pop("api_key", settings.openai_api_key),
            base_url=kwargs.pop("base_url", settings.openai_base_url),
            **kwargs,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        schema_name = request.requested_schema
        body = self._post(
            "/v1/chat/completions",
            _with_temperature(
                {
                    "model": self.model_name,
                    "max_completion_tokens": self._max_output_tokens,
                    "messages": [{"role": "user", "content": request.prompt}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            # Non-strict: the contract schemas carry $refs and range/length
                            # constraints that strict mode rejects. Pydantic re-validates the
                            # payload, and the orchestrator retries once with the errors.
                            "strict": False,
                            "schema": llm_contract_json_schema(schema_name),
                        },
                    },
                },
                request,
            ),
        )

        structured = self._message_payload(body)
        usage = body.get("usage") or {}
        return LLMInvocationResponse(
            text=json.dumps(structured, sort_keys=True, separators=(",", ":")),
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_version=self.model_version,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            structured=structured,
            raw_metadata={"response_id": body.get("id")},
        )

    @staticmethod
    def _message_payload(body: Mapping[str, Any]) -> Mapping[str, Any]:
        choices = body.get("choices") or ()
        if not choices or not isinstance(choices[0], Mapping):
            raise LLMProviderError("openai response carried no choices")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise LLMProviderError("openai response carried no message content")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMProviderError("openai message content is not valid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise LLMProviderError("openai message content must be a JSON object")
        return parsed


_PROVIDER_CLASSES: dict[str, type[_HTTPProviderAdapter]] = {
    AnthropicMessagesProvider.provider_name: AnthropicMessagesProvider,
    OpenAIChatCompletionsProvider.provider_name: OpenAIChatCompletionsProvider,
}


def build_provider_for_tier(
    settings: Settings,
    tier: LLMTier,
    *,
    client: httpx.Client | None = None,
) -> LLMProviderAdapter:
    """Build the configured live adapter for one tier."""

    provider_name, model_name, model_version = resolve_tier_model(settings, tier)
    provider_class = _PROVIDER_CLASSES.get(provider_name)
    if provider_class is None:
        raise LLMProviderError(
            f"tier {tier.value} is configured with unsupported provider '{provider_name}'"
        )
    return provider_class(
        settings=settings,
        model_name=model_name,
        model_version=model_version,
        max_output_tokens=settings.llm_tier_max_output_tokens.get(tier.value, 0),
        client=client,
    )


def _build_provider(
    settings: Settings,
    tier: LLMTier,
    *,
    provider_name: str,
    model_name: str,
    client: httpx.Client | None,
) -> LLMProviderAdapter:
    provider_class = _PROVIDER_CLASSES.get(provider_name)
    if provider_class is None:
        raise LLMProviderError(
            f"tier {tier.value} is configured with unsupported provider '{provider_name}'"
        )
    return provider_class(
        settings=settings,
        model_name=model_name,
        model_version="current",
        max_output_tokens=settings.llm_tier_max_output_tokens.get(tier.value, 0),
        client=client,
    )


def build_providers_by_tier(
    settings: Settings,
    *,
    tiers: Sequence[LLMTier] = (LLMTier.T1, LLMTier.T2, LLMTier.T3),
    client: httpx.Client | None = None,
    clients_by_provider: Mapping[str, httpx.Client] | None = None,
) -> dict[str, tuple[LLMProviderAdapter, ...]]:
    """Build ordered primary/fallback adapters from explicit settings."""

    providers_by_tier: dict[str, tuple[LLMProviderAdapter, ...]] = {}
    for tier in tiers:
        primary_provider, primary_model, _version = resolve_tier_model(settings, tier)
        configured = [
            {"provider": primary_provider, "model": primary_model},
            *settings.llm_tier_fallbacks.get(tier.value, []),
        ]
        adapters = []
        for entry in configured:
            provider_name = entry.get("provider", "")
            model_name = entry.get("model", "")
            if not provider_name or not model_name:
                raise LLMProviderError(f"tier {tier.value} has an invalid fallback entry")
            provider_client = (
                clients_by_provider.get(provider_name) if clients_by_provider else client
            )
            adapters.append(
                _build_provider(
                    settings,
                    tier,
                    provider_name=provider_name,
                    model_name=model_name,
                    client=provider_client,
                )
            )
        providers_by_tier[tier.value] = tuple(adapters)
    return providers_by_tier


__all__ = [
    "AnthropicMessagesProvider",
    "LLMBatchModeUnsupported",
    "LLMProviderError",
    "OpenAIChatCompletionsProvider",
    "build_provider_for_tier",
    "build_providers_by_tier",
]
