"""LLM request/response DTOs and provider adapter protocols.

The adapters are intentionally transport-neutral: they only require an adapter to
accept a canonical request and return a canonical response object.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

# Both vendors accept 0.0-2.0, and so does ``llm_runs.temperature``
# (``ck_llm_runs_temperature_range``). A request outside it would be rejected by the
# provider or by the audit row, so it is rejected here instead.
TEMPERATURE_MIN: Final[float] = 0.0
TEMPERATURE_MAX: Final[float] = 2.0


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
    """A canonical request passed to all provider adapters.

    ``temperature`` is optional and defaults to ``None``, which means *the caller did not ask
    for one*: no sampling parameter is sent and the provider's own default stands, exactly as
    before this field existed. A caller that needs a deterministic run (ADR 0005 stage-3
    adjudication asks for 0) sets it explicitly, and it then travels all the way through --
    into the provider request body, into the cache identity, and into ``llm_runs.temperature``.
    """

    prompt_name: str
    prompt_version: str
    prompt_template_version: str
    prompt: str
    requested_schema: str
    requested_schema_version: str = "1.0"
    context: Mapping[str, Any] = field(default_factory=dict)
    mode: LLMInvocationMode = LLMInvocationMode.REALTIME
    metadata: Mapping[str, Any] = field(default_factory=dict)
    temperature: float | None = None

    def __post_init__(self) -> None:
        if self.temperature is None:
            return
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, int | float):
            msg = f"temperature must be a number, got {type(self.temperature).__name__}"
            raise ValueError(msg)
        if not TEMPERATURE_MIN <= float(self.temperature) <= TEMPERATURE_MAX:
            msg = (
                f"temperature must be between {TEMPERATURE_MIN} and {TEMPERATURE_MAX}, "
                f"got {self.temperature}"
            )
            raise ValueError(msg)

    def with_feedback(self, feedback: str) -> LLMInvocationRequest:
        """Return a new request that appends structured feedback text.

        Every other field is carried over by ``replace``, so a validation retry cannot quietly
        drop one -- a re-ask at a different temperature than the ask would not be a retry.
        """

        return replace(self, prompt=f"{self.prompt}\n\nValidation feedback: {feedback}")


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
    "TEMPERATURE_MAX",
    "TEMPERATURE_MIN",
    "LLMInvocationMode",
    "LLMInvocationRequest",
    "LLMInvocationResponse",
    "LLMProviderAdapter",
    "SupportsStructuredJSON",
]
