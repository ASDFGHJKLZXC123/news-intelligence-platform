"""Offline proof tests for the live-shadow cost/call guard."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from packages.config.settings import Settings
from services.evaluation.llm_shadow_cost import (
    ShadowCallLimitError,
    ShadowCostAccountingError,
    ShadowCostConfigurationError,
    ShadowCostGuard,
    ShadowCostLimitError,
    conservative_input_token_bound,
)
from services.llm.adapters import (
    LLMInvocationMode,
    LLMInvocationRequest,
    LLMInvocationResponse,
)


def _settings(
    *,
    provider: str = "test",
    model: str = "model-v1",
    input_price: object = 1.0,
    output_price: object = 2.0,
) -> Settings:
    return Settings(
        llm_provider_token_price_usd_per_1m={
            f"{provider}:{model}": {
                "input": input_price,
                "output": output_price,
            }
        }
    )


def _request(prompt: str = "Classify this synthetic event.") -> LLMInvocationRequest:
    return LLMInvocationRequest(
        prompt_name="shadow_test",
        prompt_version="v1",
        prompt_template_version="v1",
        prompt=prompt,
        requested_schema="EventExtraction",
        mode=LLMInvocationMode.REALTIME,
    )


class _BilledFailure(RuntimeError):
    def __init__(self, response: LLMInvocationResponse) -> None:
        super().__init__("provider returned a billed unusable response")
        self.response = response


class _Provider:
    def __init__(
        self,
        outcomes: Sequence[LLMInvocationResponse | BaseException],
        *,
        provider_name: str = "test",
        model_name: str = "model-v1",
    ) -> None:
        self.provider_name = provider_name
        self.model_name = model_name
        self.model_version = "current"
        self.cache_variant = {"safe_variant": "v1"}
        self._outcomes = list(outcomes)
        self.invocations = 0
        self.batch_invocations = 0
        self.close_calls = 0

    def supports_mode(self, mode: LLMInvocationMode) -> bool:
        return mode is LLMInvocationMode.REALTIME

    def supports_structured_schema(self, schema_name: str) -> bool:
        return schema_name == "EventExtraction"

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        del request
        self.invocations += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def invoke_batch(
        self, requests: Sequence[LLMInvocationRequest]
    ) -> Sequence[LLMInvocationResponse]:
        del requests
        self.batch_invocations += 1
        return ()

    def close(self) -> None:
        self.close_calls += 1


def _response(
    *,
    provider: str = "test",
    model: str = "model-v1",
    input_tokens: int = 20,
    output_tokens: int = 10,
) -> LLMInvocationResponse:
    return LLMInvocationResponse(
        text="{}",
        provider_name=provider,
        model_name=model,
        model_version="current",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        structured={},
    )


def _one_call_cap(
    request: LLMInvocationRequest,
    *,
    max_output_tokens: int,
    input_price: Decimal = Decimal("1"),
    output_price: Decimal = Decimal("2"),
    output_multiplier: int = 1,
) -> Decimal:
    input_bound = conservative_input_token_bound(request)
    output_bound = max_output_tokens * output_multiplier
    return (
        (Decimal(input_bound) * input_price) + (Decimal(output_bound) * output_price)
    ) / Decimal(1_000_000)


def test_utf8_input_bound_includes_prompt_schema_example_and_system_allowance() -> None:
    plain = _request("a")
    unicode_retry = _request("a\n\nValidation feedback: " + ("\N{SNOWMAN}" * 500))

    plain_bound = conservative_input_token_bound(plain)
    retry_bound = conservative_input_token_bound(unicode_retry)

    assert plain_bound > len(plain.prompt.encode("utf-8")) + 8_000
    assert retry_bound - plain_bound == (
        len(unicode_retry.prompt.encode("utf-8")) - len(plain.prompt.encode("utf-8"))
    )


@pytest.mark.parametrize(
    ("pricing", "message"),
    [
        ({}, "has no token pricing"),
        ({"test:model-v1": {"input": 0.0, "output": 2.0}}, "requires positive"),
        ({"test:model-v1": {"input": 1.0, "output": float("nan")}}, "finite"),
        ({"test:model-v1": {"input": 1.0}}, "finite"),
    ],
)
def test_pricing_is_strict_before_any_provider_invocation(
    pricing: dict[str, dict[str, Any]], message: str
) -> None:
    settings = Settings(llm_provider_token_price_usd_per_1m=pricing)
    provider = _Provider((_response(),))
    guard = ShadowCostGuard(settings, max_calls=1)

    with pytest.raises(ShadowCostConfigurationError, match=message):
        guard.wrap(provider, max_output_tokens=100)

    assert provider.invocations == 0


def test_approved_routes_reject_unknown_models_and_upward_cap_overrides() -> None:
    with pytest.raises(ShadowCostConfigurationError, match="approved shadow ceiling"):
        ShadowCostGuard(
            _settings(),
            max_cost_usd="5.000000001",
            max_calls=700,
            approved_routes_only=True,
        )
    with pytest.raises(ShadowCostConfigurationError, match="approved shadow ceiling"):
        ShadowCostGuard(
            _settings(),
            max_cost_usd="5",
            max_calls=701,
            approved_routes_only=True,
        )

    guard = ShadowCostGuard(
        _settings(),
        max_cost_usd="5",
        max_calls=700,
        approved_routes_only=True,
    )
    with pytest.raises(ShadowCostConfigurationError, match="not an approved shadow route"):
        guard.wrap(_Provider((_response(),)), max_output_tokens=100)


def test_approved_route_floor_cannot_be_lowered_by_environment_pricing() -> None:
    request = _request()
    official_floor_cost = _one_call_cap(
        request,
        max_output_tokens=100,
        input_price=Decimal("0.30"),
        output_price=Decimal("2.50"),
        output_multiplier=2,
    )
    provider = _Provider(
        (_response(provider="gemini", model="gemini-3.5-flash-lite"),),
        provider_name="gemini",
        model_name="gemini-3.5-flash-lite",
    )
    guard = ShadowCostGuard(
        _settings(
            provider="gemini",
            model="gemini-3.5-flash-lite",
            input_price=0.000001,
            output_price=0.000001,
        ),
        max_cost_usd=official_floor_cost - Decimal("0.000000001"),
        max_calls=1,
        approved_routes_only=True,
    )
    guarded = guard.wrap(provider, max_output_tokens=100)
    guard.begin_case("price-floor", 1)

    with pytest.raises(ShadowCostLimitError):
        guarded.invoke(request)

    assert guard.end_case() == 0
    assert provider.invocations == 0


def test_successful_usage_settles_actual_and_releases_the_unused_reservation() -> None:
    request = _request()
    provider = _Provider((_response(input_tokens=20, output_tokens=10),))
    guard = ShadowCostGuard(_settings(), max_cost_usd="5.00", max_calls=1)
    guarded = guard.wrap(provider, max_output_tokens=100)
    guard.begin_case("safe-case", 1)

    assert guarded.invoke(request).structured == {}
    assert guard.end_case() == 1

    snapshot = guard.snapshot()
    assert snapshot["network_calls"] == 1
    assert snapshot["unknown_usage_calls"] == 0
    assert snapshot["settled_usage_cost_usd"] == pytest.approx(0.00004)
    assert snapshot["unknown_reserved_cost_usd"] == 0.0
    assert snapshot["accounted_cost_upper_bound_usd"] == pytest.approx(0.00004)
    assert snapshot["remaining_cost_usd"] == pytest.approx(4.99996)
    assert snapshot["routes"] == [
        {
            "provider_name": "test",
            "model_name": "model-v1",
            "network_calls": 1,
            "unknown_usage_calls": 0,
            "settled_usage_cost_usd": pytest.approx(0.00004),
            "unknown_reserved_cost_usd": 0.0,
        }
    ]


def test_billed_exception_response_settles_its_reported_usage_including_zero_output() -> None:
    billed = _response(input_tokens=20, output_tokens=0)
    provider = _Provider((_BilledFailure(billed),))
    guard = ShadowCostGuard(_settings(), max_cost_usd="5", max_calls=1)
    guarded = guard.wrap(provider, max_output_tokens=100)
    guard.begin_case("billed-failure", 1)

    with pytest.raises(_BilledFailure):
        guarded.invoke(_request())
    guard.end_case()

    snapshot = guard.snapshot()
    assert snapshot["unknown_usage_calls"] == 0
    assert snapshot["settled_usage_cost_usd"] == pytest.approx(0.00002)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("outcome unknown"),
        _BilledFailure(_response(input_tokens=0, output_tokens=0)),
    ],
)
def test_unknown_or_zero_usage_retains_the_full_reservation(failure: BaseException) -> None:
    request = _request()
    cap = _one_call_cap(request, max_output_tokens=100)
    provider = _Provider((failure, _response()))
    guard = ShadowCostGuard(_settings(), max_cost_usd=cap, max_calls=2)
    guarded = guard.wrap(provider, max_output_tokens=100)
    guard.begin_case("unknown-cost", 2)

    with pytest.raises(type(failure)):
        guarded.invoke(request)
    with pytest.raises(ShadowCostLimitError):
        guarded.invoke(request)
    guard.end_case()

    snapshot = guard.snapshot()
    assert provider.invocations == 1
    assert snapshot["network_calls"] == 1
    assert snapshot["refused_calls"] == 1
    assert snapshot["unknown_usage_calls"] == 1
    assert snapshot["unknown_reserved_cost_usd"] == pytest.approx(float(cap))
    assert snapshot["remaining_cost_usd"] == 0.0
    assert snapshot["routes"] == [
        {
            "provider_name": "test",
            "model_name": "model-v1",
            "network_calls": 1,
            "unknown_usage_calls": 1,
            "settled_usage_cost_usd": 0.0,
            "unknown_reserved_cost_usd": pytest.approx(float(cap)),
        }
    ]


def test_cost_ceiling_refuses_one_nano_over_before_the_network() -> None:
    request = _request()
    exact_cap = _one_call_cap(request, max_output_tokens=100)
    provider = _Provider((_response(),))
    guard = ShadowCostGuard(
        _settings(),
        max_cost_usd=exact_cap - Decimal("0.000000001"),
        max_calls=1,
    )
    guarded = guard.wrap(provider, max_output_tokens=100)
    guard.begin_case("too-expensive", 1)

    with pytest.raises(ShadowCostLimitError):
        guarded.invoke(request)
    assert guard.end_case() == 0
    assert provider.invocations == 0
    assert guard.snapshot()["network_calls"] == 0


def test_exact_cost_boundary_is_admitted() -> None:
    request = _request()
    exact_cap = _one_call_cap(request, max_output_tokens=100)
    provider = _Provider((TimeoutError("unknown"),))
    guard = ShadowCostGuard(_settings(), max_cost_usd=exact_cap, max_calls=1)
    guarded = guard.wrap(provider, max_output_tokens=100)
    guard.begin_case("exact-boundary", 1)

    with pytest.raises(TimeoutError):
        guarded.invoke(request)

    assert guard.end_case() == 1
    assert provider.invocations == 1
    assert guard.snapshot()["accounted_cost_upper_bound_usd"] == pytest.approx(float(exact_cap))


def test_gemini_reserves_twice_the_configured_output_limit() -> None:
    request = _request()
    one_output_cap = _one_call_cap(
        request,
        max_output_tokens=100,
        output_multiplier=1,
    )
    provider = _Provider(
        (_response(provider="gemini", model="gemini-test"),),
        provider_name="gemini",
        model_name="gemini-test",
    )
    guard = ShadowCostGuard(
        _settings(provider="gemini", model="gemini-test"),
        max_cost_usd=one_output_cap,
        max_calls=1,
    )
    guarded = guard.wrap(provider, max_output_tokens=100)
    guard.begin_case("gemini-thinking", 1)

    with pytest.raises(ShadowCostLimitError):
        guarded.invoke(request)

    assert guard.end_case() == 0
    assert provider.invocations == 0


def test_per_case_and_global_call_limits_refuse_before_provider_invocation() -> None:
    responses = (_response(), _response(), _response())
    provider = _Provider(responses)
    guard = ShadowCostGuard(_settings(), max_cost_usd="5", max_calls=2)
    guarded = guard.wrap(provider, max_output_tokens=100)

    guard.begin_case("first", 1)
    guarded.invoke(_request())
    with pytest.raises(ShadowCallLimitError):
        guarded.invoke(_request())
    assert guard.end_case() == 1

    guard.begin_case("second", 2)
    guarded.invoke(_request())
    with pytest.raises(ShadowCallLimitError):
        guarded.invoke(_request())
    assert guard.end_case() == 1

    assert provider.invocations == 2
    assert guard.snapshot()["refused_calls"] == 2


def test_usage_outside_the_reserved_envelope_fails_closed() -> None:
    provider = _Provider((_response(input_tokens=10**9, output_tokens=10),))
    guard = ShadowCostGuard(_settings(), max_cost_usd="5", max_calls=1)
    guarded = guard.wrap(provider, max_output_tokens=100)
    guard.begin_case("bad-usage", 1)

    with pytest.raises(ShadowCostAccountingError, match="exceeded"):
        guarded.invoke(_request())
    guard.end_case()


def test_decorator_proxies_capabilities_and_cache_variant_and_closes_once() -> None:
    provider = _Provider((_response(),))
    guard = ShadowCostGuard(_settings(), max_cost_usd="5", max_calls=1)
    guarded = guard.wrap(provider, max_output_tokens=100)

    assert guarded.provider_name == provider.provider_name
    assert guarded.model_name == provider.model_name
    assert guarded.model_version == provider.model_version
    assert guarded.cache_variant == {"safe_variant": "v1"}
    assert guarded.supports_mode(LLMInvocationMode.REALTIME) is True
    assert guarded.supports_structured_schema("EventExtraction") is True

    with pytest.raises(ShadowCostConfigurationError, match="sequential realtime"):
        guarded.invoke_batch((_request(),))
    assert provider.batch_invocations == 0

    guarded.close()
    guarded.close()
    assert provider.close_calls == 1


def test_route_totals_are_aggregated_and_sorted_without_changing_global_totals() -> None:
    settings = Settings(
        llm_provider_token_price_usd_per_1m={
            "gemini:gemini-test": {"input": 1.0, "output": 2.0},
            "deepseek:deepseek-test": {"input": 1.0, "output": 2.0},
        }
    )
    gemini = _Provider(
        (
            _response(provider="gemini", model="gemini-test", input_tokens=20, output_tokens=10),
            TimeoutError("sensitive-gemini-error-marker"),
        ),
        provider_name="gemini",
        model_name="gemini-test",
    )
    deepseek = _Provider(
        (
            _response(
                provider="deepseek",
                model="deepseek-test",
                input_tokens=30,
                output_tokens=5,
            ),
            TimeoutError("sensitive-deepseek-error-marker"),
        ),
        provider_name="deepseek",
        model_name="deepseek-test",
    )
    request = _request("sensitive-route-prompt-marker")
    gemini_unknown_reservation = _one_call_cap(
        request,
        max_output_tokens=100,
        output_multiplier=2,
    )
    deepseek_unknown_reservation = _one_call_cap(request, max_output_tokens=100)
    guard = ShadowCostGuard(settings, max_cost_usd="5", max_calls=4)
    guarded_gemini = guard.wrap(gemini, max_output_tokens=100)
    guarded_deepseek = guard.wrap(deepseek, max_output_tokens=100)
    guard.begin_case("sensitive-route-case-marker", 4)

    guarded_gemini.invoke(request)
    with pytest.raises(TimeoutError):
        guarded_gemini.invoke(request)
    guarded_deepseek.invoke(request)
    with pytest.raises(TimeoutError):
        guarded_deepseek.invoke(request)
    assert guard.end_case() == 4

    snapshot = guard.snapshot()
    assert snapshot["network_calls"] == 4
    assert snapshot["unknown_usage_calls"] == 2
    assert snapshot["settled_usage_cost_usd"] == pytest.approx(0.00008)
    assert snapshot["unknown_reserved_cost_usd"] == pytest.approx(
        float(gemini_unknown_reservation + deepseek_unknown_reservation)
    )
    assert snapshot["routes"] == [
        {
            "provider_name": "deepseek",
            "model_name": "deepseek-test",
            "network_calls": 2,
            "unknown_usage_calls": 1,
            "settled_usage_cost_usd": pytest.approx(0.00004),
            "unknown_reserved_cost_usd": pytest.approx(float(deepseek_unknown_reservation)),
        },
        {
            "provider_name": "gemini",
            "model_name": "gemini-test",
            "network_calls": 2,
            "unknown_usage_calls": 1,
            "settled_usage_cost_usd": pytest.approx(0.00004),
            "unknown_reserved_cost_usd": pytest.approx(float(gemini_unknown_reservation)),
        },
    ]

    serialized = str(snapshot)
    assert "sensitive-route-prompt-marker" not in serialized
    assert "sensitive-route-case-marker" not in serialized
    assert "sensitive-gemini-error-marker" not in serialized
    assert "sensitive-deepseek-error-marker" not in serialized


def test_snapshot_contains_only_safe_numeric_counters() -> None:
    secret_prompt = "secret-prompt-marker"
    provider = _Provider((_response(),))
    guard = ShadowCostGuard(_settings(), max_cost_usd="5", max_calls=1)
    guarded = guard.wrap(provider, max_output_tokens=100)
    guard.begin_case("secret-case-marker", 1)
    guarded.invoke(_request(secret_prompt))
    guard.end_case()

    snapshot = guard.snapshot()
    scalar_counters = {key: value for key, value in snapshot.items() if key != "routes"}
    assert all(isinstance(value, int | float) for value in scalar_counters.values())
    assert snapshot["routes"] == [
        {
            "provider_name": "test",
            "model_name": "model-v1",
            "network_calls": 1,
            "unknown_usage_calls": 0,
            "settled_usage_cost_usd": pytest.approx(0.00004),
            "unknown_reserved_cost_usd": 0.0,
        }
    ]
    assert all(
        set(route)
        == {
            "provider_name",
            "model_name",
            "network_calls",
            "unknown_usage_calls",
            "settled_usage_cost_usd",
            "unknown_reserved_cost_usd",
        }
        for route in snapshot["routes"]
    )
    serialized = str(snapshot)
    assert secret_prompt not in serialized
    assert "secret-case-marker" not in serialized
