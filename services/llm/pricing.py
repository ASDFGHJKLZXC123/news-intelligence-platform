"""Price utilities for token-cost accounting."""

from __future__ import annotations

from typing import Any

from packages.config.settings import Settings
from services.llm.adapters import LLMInvocationMode


def estimate_completion_cost_usd(
    settings: Settings,
    provider_name: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    invocation_mode: LLMInvocationMode = LLMInvocationMode.REALTIME,
) -> float:
    """Return estimated USD cost for an input/output token pair.

    The result keeps sub-cent precision. A single T1 call costs a small fraction of a
    cent, so rounding per call would floor month-to-date spend to zero and defeat the
    budget ceiling that the whole tier policy is built on.
    """

    key = f"{provider_name}:{model_name}"
    provider_pricing: dict[str, dict[str, Any]] = settings.llm_provider_token_price_usd_per_1m
    price = provider_pricing.get(key)
    if not price:
        return 0.0

    input_price = float(price.get("input", 0.0))
    output_price = float(price.get("output", 0.0))
    cost = ((input_tokens / 1_000_000) * input_price) + (
        (output_tokens / 1_000_000) * output_price
    )
    if invocation_mode is LLMInvocationMode.BATCH:
        cost *= settings.llm_batch_discount_multiplier
    return cost
