"""Production HTTP adapters for Anthropic, OpenAI, Gemini, and DeepSeek.

Provider batch APIs are asynchronous: a batch is submitted, polled until it ends, and only
then are results fetched. A synchronous adapter therefore cannot honour a batch invocation,
and pretending a pending submission is a completed response would fabricate output. These
adapters reject batch mode outright (``supports_mode`` returns false, ``invoke_batch`` raises)
so the orchestrator can degrade the call to realtime.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from packages.config.settings import Settings
from services.llm.adapters import (
    LLMInvocationMode,
    LLMInvocationRequest,
    LLMInvocationResponse,
    LLMProviderAdapter,
)
from services.llm.contracts import (
    NIL_DECISION,
    LLMContractLookupError,
    ReportComposition,
    llm_contract_json_schema,
    resolve_llm_contract,
)
from services.llm.policy import LLMTier, resolve_tier_model

LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODES: Final = (
    "http_authentication",
    "http_permission",
    "http_not_found",
    "http_timeout",
    "http_rate_limited",
    "http_request_rejected",
    "http_server_error",
    "http_unexpected_status",
    "transport_timeout",
    "transport_error",
    "response_non_json",
    "response_not_object",
    "output_incomplete",
    "output_missing",
    "output_invalid_json",
    "output_not_object",
    "output_invalid_shape",
    "request_unsupported",
    "configuration_error",
    "provider_error",
    "unclassified",
)
_LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODE_SET: Final = frozenset(LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODES)


class LLMProviderError(RuntimeError):
    """Raised when a provider call fails or returns an unusable payload.

    ``diagnostic_code`` is deliberately a fixed, message-free taxonomy. It can cross an
    aggregate diagnostics boundary without exposing provider response bodies, request URLs,
    prompts, generated output, or arbitrary exception text.
    """

    def __init__(self, message: str, *, diagnostic_code: str = "provider_error") -> None:
        super().__init__(message)
        self.diagnostic_code = (
            diagnostic_code
            if diagnostic_code in _LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODE_SET
            else "unclassified"
        )


def provider_failure_diagnostic_code(error: BaseException) -> str:
    """Return only an allowlisted provider-failure category for audit aggregation."""

    code = getattr(error, "diagnostic_code", None)
    return code if code in _LLM_PROVIDER_FAILURE_DIAGNOSTIC_CODE_SET else "unclassified"


class LLMBatchModeUnsupported(LLMProviderError):
    """Raised when batch mode is requested from a realtime-only adapter."""


class LLMProviderOutputError(LLMProviderError):
    """A billed provider response that cannot be used, with usage retained for accounting."""

    def __init__(
        self,
        message: str,
        *,
        response: LLMInvocationResponse,
        diagnostic_code: str = "provider_error",
    ) -> None:
        super().__init__(message, diagnostic_code=diagnostic_code)
        self.response = response


class LLMRetryableProviderOutputError(LLMProviderOutputError):
    """Raised when provider guidance recommends correcting the prompt and retrying."""


def _with_temperature(body: dict[str, Any], request: LLMInvocationRequest) -> dict[str, Any]:
    """Add the requested sampling temperature to a provider body, and only when one was asked for.

    Anthropic, OpenAI, and DeepSeek name the field ``temperature`` and default it themselves.
    A request that asks for none therefore sends none, so every call written before the field
    existed keeps the exact body it had; a request that asks for 0 sends 0. Gemini has a separate
    generation-config contract and handles its current-model compatibility in its own adapter.
    """

    if request.temperature is None:
        return body
    return {**body, "temperature": float(request.temperature)}


def _json_mapping(text: str, *, provider_name: str) -> Mapping[str, Any]:
    """Parse one provider's structured text and reject arrays, scalars, or empty output."""

    if not text.strip():
        raise LLMProviderError(
            f"{provider_name} response carried no message content",
            diagnostic_code="output_missing",
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMProviderError(
            f"{provider_name} message content is not valid JSON",
            diagnostic_code="output_invalid_json",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise LLMProviderError(
            f"{provider_name} message content must be a JSON object",
            diagnostic_code="output_not_object",
        )
    return dict(parsed)


def _chat_completion_message_payload(
    body: Mapping[str, Any],
    *,
    provider_name: str,
) -> Mapping[str, Any]:
    """Extract an OpenAI-format non-streaming chat-completion message."""

    choices = body.get("choices") or ()
    if not choices or not isinstance(choices[0], Mapping):
        raise LLMProviderError(
            f"{provider_name} response carried no choices",
            diagnostic_code="output_invalid_shape",
        )
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise LLMProviderError(
            f"{provider_name} response carried no message content",
            diagnostic_code="output_missing",
        )
    return _json_mapping(content, provider_name=provider_name)


_GEMINI_SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "properties",
        "additionalProperties",
        "required",
    }
)


def _gemini_json_schema(
    value: Any,
    *,
    parent_key: str | None = None,
) -> Any:
    """Reduce a contract schema to Gemini's documented JSON-Schema subset.

    Contract validation still runs against the complete Pydantic model after the response.
    ``const`` is represented as a one-value ``enum``; unsupported prompt-only constraints such
    as ``default`` and ``minLength`` are omitted instead of making Gemini reject the request.
    """

    if isinstance(value, list):
        return [_gemini_json_schema(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    if parent_key in {"$defs", "properties"}:
        return {str(name): _gemini_json_schema(schema) for name, schema in value.items()}

    sanitized: dict[str, Any] = {}
    for key, child in value.items():
        if key == "const":
            sanitized.setdefault("enum", [child])
            continue
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        sanitized[key] = _gemini_json_schema(child, parent_key=key)
    return sanitized


def _json_schema_example(
    schema: Mapping[str, Any],
    *,
    root: Mapping[str, Any] | None = None,
    seen_refs: frozenset[str] = frozenset(),
) -> Any:
    """Build a bounded shape example for providers whose JSON mode asks for one."""

    root_schema = root or schema
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in seen_refs:
            return {}
        target: Any = root_schema
        if reference.startswith("#/"):
            for segment in reference[2:].split("/"):
                if not isinstance(target, Mapping):
                    return {}
                key = segment.replace("~1", "/").replace("~0", "~")
                target = target.get(key)
            if isinstance(target, Mapping):
                return _json_schema_example(
                    target,
                    root=root_schema,
                    seen_refs=seen_refs | {reference},
                )
        return {}

    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        branch = next(
            (item for item in any_of if isinstance(item, Mapping) and item.get("type") != "null"),
            None,
        )
        if isinstance(branch, Mapping):
            return _json_schema_example(
                branch,
                root=root_schema,
                seen_refs=seen_refs,
            )

    value_type = schema.get("type")
    if isinstance(value_type, list):
        value_type = next((item for item in value_type if item != "null"), "null")

    if value_type == "object" or isinstance(schema.get("properties"), Mapping):
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return {}
        example = {
            str(name): _json_schema_example(
                child,
                root=root_schema,
                seen_refs=seen_refs,
            )
            for name, child in properties.items()
            if isinstance(name, str) and isinstance(child, Mapping)
        }
        # The contracts' output arrays use default factories and therefore are not
        # JSON-Schema-required. Including every property with empty arrays and an
        # abstention reason produces a complete, contract-valid example instead of
        # an envelope that omits the actual output shape.
        if "no_finding_reason" in properties:
            example["no_finding_reason"] = "No qualifying findings were found."
        if "decision" in properties:
            example["decision"] = NIL_DECISION
        return example
    if value_type == "array":
        if int(schema.get("minItems", 0) or 0) == 0:
            return []
        items = schema.get("items")
        if isinstance(items, Mapping):
            return [
                _json_schema_example(
                    items,
                    root=root_schema,
                    seen_refs=seen_refs,
                )
            ]
        return []
    if value_type == "string":
        return {
            "date": "2000-01-01",
            "date-time": "2000-01-01T00:00:00Z",
            "time": "00:00:00Z",
        }.get(str(schema.get("format")), "example")
    if value_type == "integer":
        return int(schema.get("minimum", 0) or 0)
    if value_type == "number":
        return float(schema.get("minimum", 0.0) or 0.0)
    if value_type == "boolean":
        return False
    return None


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
            raise LLMProviderError(
                f"{self.provider_name} API key is not configured",
                diagnostic_code="configuration_error",
            )
        if max_output_tokens <= 0:
            raise LLMProviderError(
                f"{self.provider_name} requires a positive max output token budget",
                diagnostic_code="configuration_error",
            )
        self.model_name = model_name
        self.model_version = model_version
        self._settings = settings
        self._api_key = api_key
        self._max_output_tokens = max_output_tokens
        # The adapter owns its client, including an injected client. This keeps factory
        # rollback and orchestrator cleanup deterministic; callers must not reuse an
        # injected client after handing it to an adapter.
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=settings.llm_request_timeout_seconds,
        )
        self._closed = False

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
        if self._closed:
            return
        self._closed = True
        self._client.close()

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            response = self._client.post(path, json=dict(payload), headers=self._headers())
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMProviderError(
                f"{self.provider_name} request failed: {exc}",
                diagnostic_code="transport_timeout",
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 401:
                diagnostic_code = "http_authentication"
            elif status_code == 403:
                diagnostic_code = "http_permission"
            elif status_code == 404:
                diagnostic_code = "http_not_found"
            elif status_code == 408:
                diagnostic_code = "http_timeout"
            elif status_code == 429:
                diagnostic_code = "http_rate_limited"
            elif 400 <= status_code < 500:
                diagnostic_code = "http_request_rejected"
            elif 500 <= status_code < 600:
                diagnostic_code = "http_server_error"
            else:
                diagnostic_code = "http_unexpected_status"
            raise LLMProviderError(
                f"{self.provider_name} request failed: {exc}",
                diagnostic_code=diagnostic_code,
            ) from exc
        except httpx.RequestError as exc:
            raise LLMProviderError(
                f"{self.provider_name} request failed: {exc}",
                diagnostic_code="transport_error",
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                f"{self.provider_name} request failed: {exc}",
                diagnostic_code="transport_error",
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise LLMProviderError(
                f"{self.provider_name} returned non-JSON body",
                diagnostic_code="response_non_json",
            ) from exc

        if not isinstance(body, Mapping):
            raise LLMProviderError(
                f"{self.provider_name} response body must be a JSON object",
                diagnostic_code="response_not_object",
            )
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
        return _chat_completion_message_payload(body, provider_name="openai")


class GeminiInteractionsProvider(_HTTPProviderAdapter):
    """Google Gemini Interactions v1 adapter with native structured output."""

    provider_name = "gemini"

    def __init__(self, *, settings: Settings, model_name: str, **kwargs: Any) -> None:
        thinking_level = settings.gemini_model_thinking_levels.get(model_name)
        if thinking_level is None:
            raise LLMProviderError(
                f"gemini model '{model_name}' has no explicit thinking-level configuration",
                diagnostic_code="configuration_error",
            )
        self._thinking_level = thinking_level
        super().__init__(
            settings=settings,
            model_name=model_name,
            api_key=kwargs.pop("api_key", settings.gemini_api_key),
            base_url=kwargs.pop("base_url", settings.gemini_base_url),
            **kwargs,
        )

    @property
    def cache_variant(self) -> Mapping[str, Any]:
        """Provider controls outside the canonical request that change generated output."""

        return {"thinking_level": self._thinking_level}

    def _headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self._api_key,
            "content-type": "application/json",
        }

    def _invocation_response(
        self,
        body: Mapping[str, Any],
        *,
        status: Any,
        thinking_level: str | None,
        structured: Mapping[str, Any] | None,
    ) -> LLMInvocationResponse:
        usage = body.get("usage") or {}
        thought_tokens = int(usage.get("total_thought_tokens", 0) or 0)
        return LLMInvocationResponse(
            text=(
                ""
                if structured is None
                else json.dumps(structured, sort_keys=True, separators=(",", ":"))
            ),
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_version=self.model_version,
            input_tokens=int(usage.get("total_input_tokens", 0) or 0),
            # Gemini bills thinking tokens as output tokens.
            output_tokens=int(usage.get("total_output_tokens", 0) or 0) + thought_tokens,
            structured=structured,
            raw_metadata={
                "response_id": body.get("id"),
                "provider_model": body.get("model"),
                "status": status,
                "thought_tokens": thought_tokens,
                "cached_tokens": int(usage.get("total_cached_tokens", 0) or 0),
                "tool_use_tokens": int(usage.get("total_tool_use_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
                "thinking_level": thinking_level,
            },
        )

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        uses_instruction_for_determinism = (
            request.temperature is not None and float(request.temperature) == 0.0
        )
        if request.temperature is not None and not uses_instruction_for_determinism:
            raise LLMProviderError(
                "gemini Interactions v1 has no declared nonzero sampling-parameter "
                "support; remove the temperature or use another provider",
                diagnostic_code="request_unsupported",
            )

        schema_name = request.requested_schema
        generation_config: dict[str, Any] = {
            "max_output_tokens": self._max_output_tokens,
        }
        thinking_level = self._thinking_level
        generation_config["thinking_level"] = thinking_level
        temperature_sent = False

        payload: dict[str, Any] = {
            "model": self.model_name,
            "input": request.prompt,
            # The orchestrator is stateless and persists its own audit record. Avoid
            # provider-side request/response storage by default.
            "store": False,
            "stream": False,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": _gemini_json_schema(llm_contract_json_schema(schema_name)),
            },
            "generation_config": generation_config,
        }
        if uses_instruction_for_determinism:
            # Current Gemini models and future/unknown IDs do not get an optimistic
            # sampling field. Google recommends explicit instructions for deterministic
            # work, so translate only exact zero-temperature intent and record it below.
            payload["system_instruction"] = (
                "Apply the request's decision rules consistently and deterministically. "
                "Do not introduce stylistic variation. Return only the schema-bound JSON result."
            )

        body = self._post(
            "/v1/interactions",
            payload,
        )
        status = body.get("status")
        if status != "completed":
            raise LLMProviderOutputError(
                f"gemini interaction did not complete successfully (status: {status})",
                response=self._invocation_response(
                    body,
                    status=status,
                    thinking_level=thinking_level,
                    structured=None,
                ),
                diagnostic_code="output_incomplete",
            )

        try:
            structured = self._model_output_payload(body)
        except LLMProviderError as exc:
            # A completed interaction is still billable even when its final model-output step is
            # missing or cannot be parsed as the requested JSON object.  Preserve that response's
            # usage so the orchestrator's failed audit row and monthly budget remain accurate.
            raise LLMProviderOutputError(
                str(exc),
                response=self._invocation_response(
                    body,
                    status=status,
                    thinking_level=thinking_level,
                    structured=None,
                ),
                diagnostic_code=provider_failure_diagnostic_code(exc),
            ) from exc
        response = self._invocation_response(
            body,
            status=status,
            thinking_level=thinking_level,
            structured=structured,
        )
        metadata = dict(response.raw_metadata)
        metadata.update(
            {
                "temperature_requested": request.temperature,
                "temperature_sent": temperature_sent,
                "temperature_strategy": (
                    "deterministic_system_instruction"
                    if uses_instruction_for_determinism
                    else "generation_config"
                    if temperature_sent
                    else "provider_default"
                ),
            }
        )
        return LLMInvocationResponse(
            text=response.text,
            provider_name=response.provider_name,
            model_name=response.model_name,
            model_version=response.model_version,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            structured=response.structured,
            raw_metadata=metadata,
        )

    @classmethod
    def _model_output_payload(
        cls,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Parse the final model-output step from a completed interaction."""

        steps = body.get("steps") or ()
        model_output = next(
            (
                step
                for step in reversed(steps)
                if isinstance(step, Mapping) and step.get("type") == "model_output"
            ),
            None,
        )
        if model_output is None:
            raise LLMProviderError(
                "gemini response carried no model_output step",
                diagnostic_code="output_missing",
            )

        text_parts = [
            block.get("text")
            for block in (model_output.get("content") or ())
            if isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return _json_mapping("".join(text_parts), provider_name=cls.provider_name)


class DeepSeekChatCompletionsProvider(_HTTPProviderAdapter):
    """DeepSeek OpenAI-format chat adapter using the provider's JSON Output mode."""

    provider_name = "deepseek"
    _REPORT_COMPOSITION_BUDGET_INSTRUCTION_VERSION = "v3"

    def __init__(self, *, settings: Settings, model_name: str, **kwargs: Any) -> None:
        super().__init__(
            settings=settings,
            model_name=model_name,
            api_key=kwargs.pop("api_key", settings.deepseek_api_key),
            base_url=kwargs.pop("base_url", settings.deepseek_base_url),
            **kwargs,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }

    @property
    def cache_variant(self) -> Mapping[str, Any]:
        """Rotate DeepSeek caches when its composition-only control instruction changes."""

        return {
            "report_composition_budget_instruction": (
                self._REPORT_COMPOSITION_BUDGET_INSTRUCTION_VERSION
            )
        }

    def _composition_budget_instruction(self, request: LLMInvocationRequest) -> str | None:
        if request.requested_schema != ReportComposition.SCHEMA_NAME:
            return None
        instruction = (
            "REPORT COMPOSITION WORD-BUDGET CONTROL "
            f"({self._REPORT_COMPOSITION_BUDGET_INSTRUCTION_VERSION}): The word range in the "
            "user message is a hard acceptance condition. Aim close to its stated target, with "
            "an inner safety margin from both bounds. The schema example above is shape-only: "
            "do not copy its empty blocks or abstention reason. Composition is requested only "
            "when citable claims exist, so return at least one non-empty block, cover every "
            "required fact and concept, and preserve the exact supporting claim_ids while "
            "adjusting length. Before returning JSON, silently join all blocks[].text values "
            "with spaces and count each maximal run of non-whitespace characters as one word. "
            "Revise the prose until that combined count is inside the stated range. Do not count "
            "JSON syntax or claim_ids, and do not add a word-count field or any other field."
        )
        budget_values = tuple(
            request.context.get(key)
            for key in (
                "word_budget_target",
                "word_budget_minimum",
                "word_budget_maximum",
            )
        )
        if all(isinstance(value, int) and not isinstance(value, bool) for value in budget_values):
            target, minimum, maximum = budget_values
            if 0 < minimum <= target <= maximum:
                lower_slack = target - minimum
                upper_slack = maximum - target
                inner_minimum = target - max(1, lower_slack // 3) if lower_slack else target
                inner_maximum = target + max(1, upper_slack // 3) if upper_slack else target
                instruction += (
                    f" For this request, {minimum}-{maximum} words is the accepted outer range. "
                    f"Draft and revise into the safer {inner_minimum}-{inner_maximum} word band "
                    f"around the {target}-word target."
                )
        if request.context.get("budget_attempt") == 2:
            instruction += (
                " This is the single corrective budget attempt. Use the previous word count and "
                "too-short or too-long direction appended to the user message. Rewrite the full "
                "section rather than appending or merely summarizing; retain every required fact, "
                "concept, and exact claim_id, then recount before returning."
            )
        return instruction

    def _invocation_response(
        self,
        body: Mapping[str, Any],
        *,
        finish_reason: Any,
        text: str,
        structured: Mapping[str, Any] | None,
        composition_budget_instruction: str | None,
    ) -> LLMInvocationResponse:
        usage = body.get("usage") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        raw_metadata: dict[str, Any] = {
            "response_id": body.get("id"),
            "provider_model": body.get("model"),
            "finish_reason": finish_reason,
            "system_fingerprint": body.get("system_fingerprint"),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "prompt_cache_hit_tokens": int(usage.get("prompt_cache_hit_tokens", 0) or 0),
            "prompt_cache_miss_tokens": int(usage.get("prompt_cache_miss_tokens", 0) or 0),
            "reasoning_tokens": (
                int(completion_details.get("reasoning_tokens", 0) or 0)
                if isinstance(completion_details, Mapping)
                else 0
            ),
            "thinking_mode": "disabled",
        }
        if composition_budget_instruction is not None:
            raw_metadata["report_composition_budget_instruction"] = (
                self._REPORT_COMPOSITION_BUDGET_INSTRUCTION_VERSION
            )
        return LLMInvocationResponse(
            text=text,
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_version=self.model_version,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            structured=structured,
            raw_metadata=raw_metadata,
        )

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        schema_name = request.requested_schema
        schema = llm_contract_json_schema(schema_name)
        composition_budget_instruction = self._composition_budget_instruction(request)
        system_instruction = (
            "Return only one valid JSON object matching this JSON Schema. "
            "Do not wrap it in Markdown or add prose.\n\nJSON SCHEMA:\n"
            + json.dumps(
                schema,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n\nEXAMPLE JSON OUTPUT (shape only; replace example "
            "values with task-specific values):\n"
            + json.dumps(
                _json_schema_example(schema),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if composition_budget_instruction is not None:
            system_instruction = f"{system_instruction}\n\n{composition_budget_instruction}"
        body = self._post(
            "/chat/completions",
            _with_temperature(
                {
                    "model": self.model_name,
                    "max_tokens": self._max_output_tokens,
                    "messages": [
                        {
                            "role": "system",
                            "content": system_instruction,
                        },
                        {"role": "user", "content": request.prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "stream": False,
                    # V4 defaults to thinking mode, which ignores temperature. Keep this
                    # adapter in non-thinking mode so the canonical request is honoured.
                    "thinking": {"type": "disabled"},
                },
                request,
            ),
        )
        choices = body.get("choices") or ()
        if not choices or not isinstance(choices[0], Mapping):
            raise LLMProviderOutputError(
                "deepseek response carried no choices",
                response=self._invocation_response(
                    body,
                    finish_reason=None,
                    text="",
                    structured=None,
                    composition_budget_instruction=composition_budget_instruction,
                ),
                diagnostic_code="output_invalid_shape",
            )
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason != "stop":
            raise LLMProviderOutputError(
                f"deepseek response did not finish normally (finish_reason: {finish_reason})",
                response=self._invocation_response(
                    body,
                    finish_reason=finish_reason,
                    text="",
                    structured=None,
                    composition_budget_instruction=composition_budget_instruction,
                ),
                diagnostic_code="output_incomplete",
            )

        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str) and not content.strip():
            response = self._invocation_response(
                body,
                finish_reason=finish_reason,
                text="",
                structured=None,
                composition_budget_instruction=composition_budget_instruction,
            )
            raise LLMRetryableProviderOutputError(
                "deepseek response carried empty JSON-mode message content",
                response=response,
                diagnostic_code="output_missing",
            )

        try:
            structured = _chat_completion_message_payload(
                body,
                provider_name=self.provider_name,
            )
        except LLMProviderError as exc:
            raise LLMProviderOutputError(
                str(exc),
                response=self._invocation_response(
                    body,
                    finish_reason=finish_reason,
                    text="",
                    structured=None,
                    composition_budget_instruction=composition_budget_instruction,
                ),
                diagnostic_code=provider_failure_diagnostic_code(exc),
            ) from exc
        return self._invocation_response(
            body,
            finish_reason=finish_reason,
            text=json.dumps(structured, sort_keys=True, separators=(",", ":")),
            structured=structured,
            composition_budget_instruction=composition_budget_instruction,
        )


_PROVIDER_CLASSES: dict[str, type[_HTTPProviderAdapter]] = {
    AnthropicMessagesProvider.provider_name: AnthropicMessagesProvider,
    OpenAIChatCompletionsProvider.provider_name: OpenAIChatCompletionsProvider,
    GeminiInteractionsProvider.provider_name: GeminiInteractionsProvider,
    DeepSeekChatCompletionsProvider.provider_name: DeepSeekChatCompletionsProvider,
}


def _require_provider_pricing(
    settings: Settings,
    *,
    provider_name: str,
    model_name: str,
) -> None:
    """Fail closed before a configured live route can accrue unpriced spend."""

    key = f"{provider_name}:{model_name}"
    price = settings.llm_provider_token_price_usd_per_1m.get(key)
    if not isinstance(price, Mapping):
        raise LLMProviderError(f"configured provider model '{key}' has no token pricing")

    for direction in ("input", "output"):
        value = price.get(direction)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise LLMProviderError(
                f"configured provider model '{key}' has invalid {direction} token pricing"
            )


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
    _require_provider_pricing(
        settings,
        provider_name=provider_name,
        model_name=model_name,
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
    _require_provider_pricing(
        settings,
        provider_name=provider_name,
        model_name=model_name,
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
    """Build ordered primary/fallback adapters from explicit settings.

    Any injected client is transferred to the constructed adapters and closed with them.
    """

    providers_by_tier: dict[str, tuple[LLMProviderAdapter, ...]] = {}
    created_adapters: list[LLMProviderAdapter] = []
    try:
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
                adapter = _build_provider(
                    settings,
                    tier,
                    provider_name=provider_name,
                    model_name=model_name,
                    client=provider_client,
                )
                adapters.append(adapter)
                created_adapters.append(adapter)
            providers_by_tier[tier.value] = tuple(adapters)
    except Exception as construction_error:
        cleanup_errors: list[Exception] = []
        for adapter in reversed(created_adapters):
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as cleanup_error:  # noqa: BLE001
                    cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            construction_error.add_note(
                "provider cleanup also failed: "
                + "; ".join(f"{type(error).__name__}: {error}" for error in cleanup_errors)
            )
        raise
    return providers_by_tier


__all__ = [
    "AnthropicMessagesProvider",
    "DeepSeekChatCompletionsProvider",
    "GeminiInteractionsProvider",
    "LLMBatchModeUnsupported",
    "LLMProviderError",
    "LLMProviderOutputError",
    "LLMRetryableProviderOutputError",
    "OpenAIChatCompletionsProvider",
    "build_provider_for_tier",
    "build_providers_by_tier",
]
