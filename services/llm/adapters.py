"""LLM request/response DTOs and provider adapter protocols.

The adapters are intentionally transport-neutral: they only require an adapter to
accept a canonical request and return a canonical response object.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class LLMInvocationMode(StrEnum):
    """Delivery mode for an invocation."""

    BATCH = "batch"
    REALTIME = "realtime"


@runtime_checkable
class SupportsStructuredJSON(Protocol):
    """Provider-level protocol for structured JSON support checks."""

    def supports_structured_schema(self, schema_name: str) -> bool: ...


@dataclass(frozen=True)
class LLMInvocationRequest:
    """A canonical request passed to all provider adapters."""

    prompt_name: str
    prompt_version: str
    prompt_template_version: str
    prompt: str
    requested_schema: str
    requested_schema_version: str = "1.0"
    context: Mapping[str, Any] = field(default_factory=dict)
    mode: LLMInvocationMode = LLMInvocationMode.REALTIME
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def with_feedback(self, feedback: str) -> LLMInvocationRequest:
        """Return a new request that appends structured feedback text."""

        return LLMInvocationRequest(
            prompt_name=self.prompt_name,
            prompt_version=self.prompt_version,
            prompt_template_version=self.prompt_template_version,
            requested_schema=self.requested_schema,
            requested_schema_version=self.requested_schema_version,
            prompt=f"{self.prompt}\n\nValidation feedback: {feedback}",
            context=self.context,
            mode=self.mode,
            metadata=self.metadata,
        )


@dataclass(frozen=True)
class LLMInvocationResponse:
    """Canonical output returned by provider adapters."""

    text: str
    provider_name: str
    model_name: str
    model_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    structured: Mapping[str, Any] | None = None
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)

    def parsed_payload(self) -> Mapping[str, Any]:
        """Return structured payload, deriving from text when needed."""

        if self.structured is not None:
            return self.structured

        parsed = json.loads(self.text)
        if not isinstance(parsed, Mapping):
            msg = "LLM response JSON payload must be a mapping"
            raise TypeError(msg)
        return parsed


@runtime_checkable
class LLMProviderAdapter(SupportsStructuredJSON, Protocol):
    """Shared protocol used by fake and live provider adapters."""

    provider_name: str
    model_name: str
    model_version: str

    def supports_mode(self, mode: LLMInvocationMode) -> bool: ...

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse: ...

    def invoke_batch(self, requests: Sequence[LLMInvocationRequest]) -> Sequence[LLMInvocationResponse]: ...


__all__ = [
    "LLMInvocationMode",
    "LLMInvocationRequest",
    "LLMInvocationResponse",
    "LLMProviderAdapter",
    "SupportsStructuredJSON",
]
