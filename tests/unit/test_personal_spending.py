"""Synthetic-only route and final-HTTP guard boundaries; no provider credentials."""

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from db.models import LLMRun
from services.personal.paid_runtime import DeadlineHTTPClient, DurablePaidHTTPClient
from services.personal.spending import (
    PaidRoute,
    PaidWorkBlocked,
    decimal_usd,
    established_legacy_cost,
    validate_model_route,
)


def route_mapping(role="generation"):
    return {
        "provider": "openai",
        "model": "synthetic-only",
        "model_version": "fixture-v1",
        "price_revision": "synthetic-test-prices-not-live",
        "price_source_url": "https://openai.com/api/pricing/",
        "input_usd_per_million_tokens": "10",
        "output_usd_per_million_tokens": "10" if role == "generation" else "0",
        "max_input_tokens": 8192,
        "max_output_tokens": 1000 if role == "generation" else 0,
        "deadline_seconds": 30,
    }


def model_route():
    return {"mode": "live", "generation": route_mapping(), "embedding": route_mapping("embedding")}


@pytest.mark.parametrize(
    "value", [0.1, float("nan"), "NaN", "Infinity", "-0.1", "1e9", "0.0000000000001", True, None]
)
def test_allowance_never_accepts_binary_float_or_unbounded_decimal(value):
    with pytest.raises(ValueError):
        decimal_usd(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_input_tokens", 0),
        ("max_output_tokens", None),
        ("deadline_seconds", 0),
        ("input_usd_per_million_tokens", "0"),
        ("output_usd_per_million_tokens", "NaN"),
        ("price_revision", ""),
        ("model_version", ""),
        ("price_source_url", "https://openai.com.evil.invalid/pricing"),
        ("provider", "hidden-fallback"),
    ],
)
def test_live_route_must_resolve_every_finite_priced_boundary(field, value):
    config = route_mapping()
    config[field] = value
    with pytest.raises(ValueError):
        PaidRoute.from_mapping(config, role="generation")


def test_price_roundtrip_and_conservative_fixed_precision():
    mapping = model_route()
    assert validate_model_route(mapping) == mapping
    route = PaidRoute.from_mapping(mapping["generation"], role="generation")
    assert route.cost(1000, 1000) == Decimal("0.02")
    with pytest.raises(ValueError):
        validate_model_route({**mapping, "fallbacks": ["vendor"]})


def guard(delegate=None):
    ledger = Mock()
    ledger.reserve.return_value = "request-1"
    route = PaidRoute.from_mapping(route_mapping(), role="generation")
    return DurablePaidHTTPClient(route=route, ledger=ledger, delegate=delegate or Mock()), ledger


def test_dispatch_is_durable_before_the_physical_send_and_reconciles_receipt():
    steps = []
    response = httpx.Response(
        200,
        headers={"x-request-id": "receipt-1"},
        json={"usage": {"prompt_tokens": 25, "completion_tokens": 20}},
    )
    client, ledger = guard(
        SimpleNamespace(
            post=lambda *args, **kwargs: steps.append(("network", kwargs["timeout"])) or response
        )
    )
    ledger.reserve.side_effect = lambda *_args, **_kwargs: steps.append("reserve") or "request-1"
    ledger.dispatch.side_effect = lambda *_args: steps.append("dispatching")
    client.post("/v1/chat/completions", json={"model": "synthetic-only", "max_tokens": 1000})
    assert steps == ["reserve", "dispatching", ("network", 30)]
    assert ledger.reconcile.call_args.kwargs["provider_request_id"] == "receipt-1"
    assert ledger.reconcile.call_args.kwargs["input_tokens"] == 25


@pytest.mark.parametrize(
    "mutation",
    [
        {"model": "another-vendor"},
        {"max_tokens": 1001},
        {"max_tokens": None},
        {"n": 2},
        {"prompt": "x" * 8192},
    ],
)
def test_guard_blocks_invalid_payload_before_reservation_or_network(mutation):
    client, ledger = guard()
    with pytest.raises((ValueError, RuntimeError)):
        client.post(
            "/v1/chat/completions", json={"model": "synthetic-only", "max_tokens": 1000, **mutation}
        )
    ledger.reserve.assert_not_called()
    client.delegate.post.assert_not_called()


def test_revalidation_failure_cancels_only_pre_dispatch_reservation():
    client, ledger = guard()
    ledger.dispatch.side_effect = PaidWorkBlocked("allowance_reached")
    with pytest.raises(PaidWorkBlocked):
        client.post("/v1/chat/completions", json={"model": "synthetic-only", "max_tokens": 1000})
    ledger.cancel_before_dispatch.assert_called_once_with("request-1")
    client.delegate.post.assert_not_called()


def test_timeout_retains_uncertainty_and_never_cancels():
    client, ledger = guard()
    client.delegate.post.side_effect = httpx.ReadTimeout("scripted timeout")
    with pytest.raises(httpx.ReadTimeout):
        client.post("/v1/chat/completions", json={"model": "synthetic-only", "max_tokens": 1000})
    ledger.mark_uncertain.assert_called_once_with("request-1")
    ledger.cancel_before_dispatch.assert_not_called()


def test_success_without_usage_remains_unresolved():
    client, ledger = guard()
    client.delegate.post.return_value = httpx.Response(200, json={"id": "receipt", "choices": []})
    with pytest.raises(PaidWorkBlocked, match="provider usage unknown"):
        client.post("/v1/chat/completions", json={"model": "synthetic-only", "max_tokens": 1000})
    ledger.mark_uncertain.assert_called_once()
    ledger.reconcile.assert_not_called()


def test_absolute_url_or_timeout_override_cannot_escape_route():
    client, ledger = guard()
    with pytest.raises(PaidWorkBlocked):
        client.post("https://api.other.invalid/v1/chat/completions", json={})
    with pytest.raises(PaidWorkBlocked):
        client.post("/v1/chat/completions", json={}, timeout=None)
    ledger.reserve.assert_not_called()


@pytest.mark.parametrize(
    "cost,status,fields,expected",
    [
        (
            "0",
            "failed",
            {"error_message": "provider invocation failure", "input_tokens": 0, "output_tokens": 0},
            None,
        ),
        ("0", "succeeded", {"input_tokens": 100, "output_tokens": 50}, None),
        (None, "succeeded", {}, None),
        ("0.001", "succeeded", {}, Decimal("0.001")),
        ("0", "cached", {"input_refs": {"cache_hit": True}}, Decimal(0)),
        (
            "0",
            "failed",
            {
                "error_message": "token bucket limit reached",
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": 0,
            },
            Decimal(0),
        ),
        (
            "0",
            "failed",
            {
                "error_message": "token bucket limit reached",
                "input_tokens": 100,
                "output_tokens": 0,
                "latency_ms": 0,
            },
            None,
        ),
        (
            "0",
            "succeeded",
            {
                "provider": "offline-personal-fixture",
                "model": "offline-personal-fixture:fixture-v1",
            },
            Decimal(0),
        ),
    ],
)
def test_legacy_zero_requires_proof_of_no_billable_dispatch(cost, status, fields, expected):
    row = LLMRun(
        **{
            "provider": "openai",
            "model": "synthetic",
            "status": status,
            "cost_usd": Decimal(cost) if cost is not None else None,
            **fields,
        }
    )
    assert established_legacy_cost(row) == expected


@pytest.mark.parametrize("slow_part", ["headers", "body"])
def test_total_deadline_cancels_slow_headers_and_trickling_body_without_live_network(slow_part):
    closed = []

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"x"

        async def aclose(self):
            closed.append("body")

    async def script(request):
        if slow_part == "headers":
            try:
                await asyncio.sleep(10)
            finally:
                closed.append("headers")
        return httpx.Response(200, stream=SlowBody())

    delegate = DeadlineHTTPClient(
        base_url="https://synthetic.invalid", transport=httpx.MockTransport(script)
    )
    started = time.monotonic()
    with pytest.raises(httpx.ReadTimeout, match="total deadline"):
        delegate.post("/v1/chat/completions", json={}, timeout=0.05)
    assert time.monotonic() - started < 0.5
    assert slow_part in closed
