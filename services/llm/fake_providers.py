"""Deterministic providers for orchestration tests and offline runtimes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from services.llm.adapters import LLMInvocationMode, LLMInvocationRequest, LLMInvocationResponse

ResponseFactory = Callable[[LLMInvocationRequest], Mapping[str, Any] | str | BaseException]


def _coerce_structured_payload(value: Any) -> Mapping[str, Any]:
    """Normalize fake payload outputs to a JSON-like mapping."""

    if isinstance(value, BaseException):
        raise value
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, Mapping):
            msg = "fake LLM payload string must be a JSON object"
            raise TypeError(msg)
        return parsed
    raise TypeError("fake LLM provider output must be mapping, JSON string, or exception")


def _build_output_tokens(output: str) -> int:
    return max(1, len(output.split()))


def _build_input_tokens(prompt: str) -> int:
    return max(1, len(prompt.split()))


class ScriptedLLMProvider:
    """Deterministic provider that returns scripted responses per call."""

    def __init__(
        self,
        scripts: Sequence[Mapping[str, Any] | str | BaseException | None],
        *,
        provider_name: str = "fake-provider",
        model_name: str = "fake-llm",
        model_version: str = "v1",
    ) -> None:
        self.provider_name = provider_name
        self.model_name = model_name
        self.model_version = model_version
        self._scripts = list(scripts)
        self.call_count = 0
        self.supported_modes = (LLMInvocationMode.REALTIME, LLMInvocationMode.BATCH)

    def supports_mode(self, mode: LLMInvocationMode) -> bool:
        return mode in self.supported_modes

    def supports_structured_schema(self, schema_name: str) -> bool:
        del schema_name
        return True

    def _next_payload(self) -> Mapping[str, Any] | None:
        if not self._scripts:
            return None
        if self.call_count >= len(self._scripts):
            value = self._scripts[-1]
        else:
            value = self._scripts[self.call_count]
        self.call_count += 1
        if value is None:
            return {}
        return _coerce_structured_payload(value)

    def _derive_payload_text(
        self,
        request: LLMInvocationRequest,
        payload: Mapping[str, Any] | None,
    ) -> str:
        payload_text = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
        request_snapshot = {
            "prompt_name": request.prompt_name,
            "prompt_version": request.prompt_version,
            "prompt_template_version": request.prompt_template_version,
            "requested_schema": request.requested_schema,
            "requested_schema_version": request.requested_schema_version,
            "prompt": request.prompt,
            "context": request.context,
            "metadata": request.metadata,
            "mode": request.mode.value,
        }
        payload_text = (
            f"{payload_text}|{json.dumps(request_snapshot, sort_keys=True, separators=(',', ':'))}"
        )
        return payload_text

    def _build_response(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        payload = self._next_payload()
        payload_text = self._derive_payload_text(request, payload)
        model_run_id = hashlib.sha256(
            (
                f"{self.provider_name}|{self.model_name}|{self.model_version}|"
                f"{request.prompt_name}|{request.prompt_version}|{request.requested_schema}|"
                f"{request.prompt}|{request.context}|{payload_text}"
            ).encode()
        ).hexdigest()[:16]
        return LLMInvocationResponse(
            text=payload_text,
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_version=self.model_version,
            input_tokens=_build_input_tokens(request.prompt),
            output_tokens=_build_output_tokens(payload_text),
            structured=payload,
            raw_metadata={
                "model_run_id": model_run_id,
                "script_position": self.call_count - 1,
            },
        )

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        return self._build_response(request)

    def invoke_batch(self, requests: Sequence[LLMInvocationRequest]) -> Sequence[LLMInvocationResponse]:
        return [self._build_response(request) for request in requests]


class CallableLLMProvider(ScriptedLLMProvider):
    """Provider wrapper around a deterministic callable."""

    def __init__(
        self,
        response_factory: ResponseFactory,
        *,
        provider_name: str = "callable-provider",
        model_name: str = "callable-llm",
        model_version: str = "v1",
    ) -> None:
        self._response_factory = response_factory
        super().__init__(
            scripts=[],
            provider_name=provider_name,
            model_name=model_name,
            model_version=model_version,
        )

    def _next_payload(self, request: LLMInvocationRequest) -> Mapping[str, Any] | None:
        value = self._response_factory(request)
        self.call_count += 1
        if value is None:
            return {}
        if isinstance(value, BaseException):
            raise value
        return _coerce_structured_payload(value)

    def _build_response(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        structured = self._next_payload(request)
        text = json.dumps(structured, sort_keys=True, separators=(",", ":"))
        return LLMInvocationResponse(
            text=text,
            provider_name=self.provider_name,
            model_name=self.model_name,
            model_version=self.model_version,
            input_tokens=_build_input_tokens(request.prompt),
            output_tokens=_build_output_tokens(text),
            structured=structured,
            raw_metadata={"schema": request.requested_schema, "script_position": self.call_count - 1},
        )
