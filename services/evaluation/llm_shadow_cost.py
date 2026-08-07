"""Hard per-run cost and call ceilings for sequential live LLM shadow evaluation.

The production orchestrator can issue a second provider request after structured-output
validation fails, and report composition can call the orchestrator a second time after a
word-budget miss.  A shadow runner therefore cannot enforce its spend ceiling by looking at
completed audit rows after a case finishes: every provider invocation must be admitted (or
refused) immediately before it can reach the network.

This module deliberately keeps a conservative ledger.  A call reserves its worst-case charge
at the configured cache-miss realtime price.  A response with usable token accounting releases
the unused part of that reservation; a timeout, an exception without billed usage, or a response
whose input usage is absent retains the full reservation.  The ledger uses integer nano-USD, so
floating-point rounding can never admit a call one fraction beyond the configured ceiling.

The input envelope treats each UTF-8 byte as one possible token, includes three full copies of
the contract schema (covering the schema, DeepSeek's shape example, and provider conversion), and
adds a fixed allowance for system instructions, message framing, and provider-added markers.
Validation feedback is already part of ``request.prompt`` when a retry reaches this boundary, so
it is re-measured instead of relying on a fixed retry allowance.  Gemini reserves twice the
configured output limit because visible and thought-token accounting has varied across API
surfaces.  These bounds intentionally trade capacity for a simple, auditable upper envelope.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Final

from packages.config.settings import Settings
from services.llm.adapters import (
    LLMInvocationMode,
    LLMInvocationRequest,
    LLMInvocationResponse,
    LLMProviderAdapter,
)
from services.llm.contracts import llm_contract_json_schema

NANO_USD_PER_USD: Final = 1_000_000_000
APPROVED_SHADOW_MAX_CALLS: Final = 700
APPROVED_SHADOW_MAX_COST_USD: Final = Decimal("5.00")

# Reviewed realtime/cache-miss list-price floors for the only four models admitted to this
# evaluation. Environment pricing may raise these values, but it can never lower a reservation
# below this checked-in floor. Provider list-price drift remains an operational dependency that
# must be reviewed before each later live run.
APPROVED_SHADOW_PRICE_FLOORS_USD_PER_1M: Final[Mapping[str, Mapping[str, Decimal]]] = {
    "gemini:gemini-3.5-flash-lite": {
        "input": Decimal("0.30"),
        "output": Decimal("2.50"),
    },
    "gemini:gemini-3.6-flash": {
        "input": Decimal("1.50"),
        "output": Decimal("7.50"),
    },
    "deepseek:deepseek-v4-flash": {
        "input": Decimal("0.14"),
        "output": Decimal("0.28"),
    },
    "deepseek:deepseek-v4-pro": {
        "input": Decimal("0.435"),
        "output": Decimal("0.87"),
    },
}
_TOKENS_PER_MILLION: Final = 1_000_000
_NANO_USD_PER_TOKEN_PRICE_UNIT: Final = NANO_USD_PER_USD // _TOKENS_PER_MILLION

# One schema is sent by Gemini. DeepSeek sends a schema and a schema-derived example. The third
# copy covers provider-specific schema conversion without depending on private adapter helpers.
_SCHEMA_BYTE_MULTIPLIER: Final = 3
# Covers the DeepSeek/Gemini system instruction, JSON/message framing, model names, and special
# provider tokens. It is intentionally much larger than the current wire scaffolding.
_FIXED_INPUT_OVERHEAD_BYTES: Final = 8_192


class ShadowCostGuardError(RuntimeError):
    """Base class for a shadow request refused by the local guard."""


class ShadowCostConfigurationError(ShadowCostGuardError):
    """The guard cannot establish a conservative cost envelope."""


class ShadowCostLimitError(ShadowCostGuardError):
    """The next call's reservation would exceed the configured cost ceiling."""


class ShadowCallLimitError(ShadowCostGuardError):
    """A global or per-case invocation ceiling was reached before the network call."""


class ShadowCostAccountingError(ShadowCostGuardError):
    """Returned usage exceeded the envelope that was reserved before the call."""


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ShadowCostConfigurationError(f"{name} must be a finite nonnegative number")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ShadowCostConfigurationError(f"{name} must be a finite nonnegative number") from exc
    if not converted.is_finite() or converted < 0:
        raise ShadowCostConfigurationError(f"{name} must be a finite nonnegative number")
    return converted


def _usd_to_nanos(value: object, *, name: str) -> int:
    amount = _decimal(value, name=name)
    return int((amount * NANO_USD_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def _cost_nanos(
    *,
    input_tokens: int,
    output_tokens: int,
    input_price_usd_per_1m: Decimal,
    output_price_usd_per_1m: Decimal,
) -> int:
    raw_nanos = (
        (Decimal(input_tokens) * input_price_usd_per_1m)
        + (Decimal(output_tokens) * output_price_usd_per_1m)
    ) * _NANO_USD_PER_TOKEN_PRICE_UNIT
    return int(raw_nanos.to_integral_value(rounding=ROUND_CEILING))


def _safe_usd(nanos: int) -> float:
    """Return a report-friendly numeric counter without exposing request/provider content."""

    return float(Decimal(nanos) / NANO_USD_PER_USD)


def _positive_integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShadowCostConfigurationError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class _TokenPrices:
    input_usd_per_1m: Decimal
    output_usd_per_1m: Decimal


@dataclass(frozen=True)
class _Reservation:
    provider_name: str
    model_name: str
    input_token_bound: int
    output_token_bound: int
    cost_nanos: int


@dataclass
class _RouteTotals:
    network_calls: int = 0
    unknown_usage_calls: int = 0
    settled_usage_cost_nanos: int = 0
    unknown_reserved_cost_nanos: int = 0


def _strict_prices(
    settings: Settings,
    *,
    provider_name: str,
    model_name: str,
    approved_routes_only: bool,
) -> _TokenPrices:
    key = f"{provider_name}:{model_name}"
    raw = settings.llm_provider_token_price_usd_per_1m.get(key)
    if not isinstance(raw, Mapping):
        raise ShadowCostConfigurationError(
            f"configured shadow provider model '{key}' has no token pricing"
        )
    input_price = _decimal(raw.get("input"), name=f"{key} input token price")
    output_price = _decimal(raw.get("output"), name=f"{key} output token price")
    # A zero price is valid for some embedding outputs, but not for either side of the live
    # completion routes this guard protects. Treat it as missing/stale configuration rather than
    # silently converting a potentially billable shadow call into a free one.
    if input_price <= 0 or output_price <= 0:
        raise ShadowCostConfigurationError(
            f"configured shadow provider model '{key}' requires positive input/output pricing"
        )
    if approved_routes_only:
        floor = APPROVED_SHADOW_PRICE_FLOORS_USD_PER_1M.get(key)
        if floor is None:
            raise ShadowCostConfigurationError(
                f"configured shadow provider model '{key}' is not an approved shadow route"
            )
        input_price = max(input_price, floor["input"])
        output_price = max(output_price, floor["output"])
    return _TokenPrices(input_price, output_price)


def conservative_input_token_bound(request: LLMInvocationRequest) -> int:
    """Upper-bound billable input using UTF-8 bytes and explicit provider overhead.

    Contract schemas are generated locally and are part of every live structured-output call.
    Counting three copies covers the full schema, DeepSeek's generated example, and schema
    conversion/framing.  The prompt is measured anew on every retry, so validation feedback and
    report word-budget feedback cannot escape the bound.
    """

    try:
        schema = llm_contract_json_schema(request.requested_schema)
    except Exception as exc:  # noqa: BLE001 - normalize registry/config errors at this boundary
        raise ShadowCostConfigurationError(
            "shadow request has no resolvable structured-output schema"
        ) from exc
    schema_json = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    prompt_bytes = len(request.prompt.encode("utf-8"))
    schema_bytes = len(schema_json.encode("utf-8"))
    return max(
        1,
        prompt_bytes + (_SCHEMA_BYTE_MULTIPLIER * schema_bytes) + _FIXED_INPUT_OVERHEAD_BYTES,
    )


class ShadowCostGuard:
    """Shared sequential ledger for one bounded live shadow run."""

    def __init__(
        self,
        settings: Settings,
        *,
        max_cost_usd: object = Decimal("5.00"),
        max_calls: int,
        approved_routes_only: bool = False,
    ) -> None:
        self._settings = settings
        self._max_cost_nanos = _usd_to_nanos(max_cost_usd, name="max_cost_usd")
        self._max_calls = _positive_integer(max_calls, name="max_calls")
        if approved_routes_only:
            approved_max_cost_nanos = _usd_to_nanos(
                APPROVED_SHADOW_MAX_COST_USD,
                name="approved shadow max cost",
            )
            if self._max_cost_nanos > approved_max_cost_nanos:
                raise ShadowCostConfigurationError(
                    "max_cost_usd exceeds the approved shadow ceiling"
                )
            if self._max_calls > APPROVED_SHADOW_MAX_CALLS:
                raise ShadowCostConfigurationError("max_calls exceeds the approved shadow ceiling")
        self._approved_routes_only = approved_routes_only
        self._total_calls = 0
        self._settled_usage_cost_nanos = 0
        self._unknown_reserved_cost_nanos = 0
        self._unknown_usage_calls = 0
        self._refused_calls = 0
        self._accounting_errors = 0
        self._cost_limit_reached = False
        self._call_limit_reached = False
        self._case_id: str | None = None
        self._case_limit = 0
        self._case_calls = 0
        self._active: _Reservation | None = None
        self._route_totals: dict[tuple[str, str], _RouteTotals] = {}

    @property
    def total_calls(self) -> int:
        return self._total_calls

    @property
    def cost_exhausted(self) -> bool:
        return self._cost_limit_reached or self.accounted_cost_nanos >= self._max_cost_nanos

    @property
    def halted(self) -> bool:
        """Whether scheduling must stop after a ceiling refusal or accounting anomaly."""

        return self._cost_limit_reached or self._call_limit_reached or self._accounting_errors > 0

    @property
    def accounted_cost_nanos(self) -> int:
        active = self._active.cost_nanos if self._active is not None else 0
        return self._settled_usage_cost_nanos + self._unknown_reserved_cost_nanos + active

    def begin_case(self, case_id: str, max_calls: int) -> None:
        if self._case_id is not None or self._active is not None:
            raise ShadowCostConfigurationError("a shadow cost-guard case is already active")
        if not isinstance(case_id, str) or not case_id:
            raise ShadowCostConfigurationError("case_id must be a non-empty string")
        self._case_id = case_id
        self._case_limit = _positive_integer(max_calls, name="case max_calls")
        self._case_calls = 0

    def end_case(self) -> int:
        if self._case_id is None:
            raise ShadowCostConfigurationError("no shadow cost-guard case is active")
        if self._active is not None:
            raise ShadowCostConfigurationError("cannot end a case with an active reservation")
        calls = self._case_calls
        self._case_id = None
        self._case_limit = 0
        self._case_calls = 0
        return calls

    def wrap(
        self,
        provider: LLMProviderAdapter,
        *,
        max_output_tokens: int,
    ) -> CostGuardedProvider:
        """Validate/freeze this route's price and return its guarded adapter decorator."""

        output_limit = _positive_integer(max_output_tokens, name="max_output_tokens")
        prices = _strict_prices(
            self._settings,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            approved_routes_only=self._approved_routes_only,
        )
        return CostGuardedProvider(
            provider,
            self,
            max_output_tokens=output_limit,
            prices=prices,
        )

    def _reserve(
        self,
        *,
        provider_name: str,
        model_name: str,
        request: LLMInvocationRequest,
        max_output_tokens: int,
        prices: _TokenPrices,
    ) -> _Reservation:
        if self._active is not None:
            raise ShadowCostConfigurationError(
                "shadow cost guard permits only one active provider call"
            )
        if self._case_id is None:
            self._call_limit_reached = True
            raise ShadowCallLimitError("provider invocation attempted outside a shadow case")
        if self._total_calls >= self._max_calls or self._case_calls >= self._case_limit:
            self._refused_calls += 1
            self._call_limit_reached = True
            raise ShadowCallLimitError("shadow network-call ceiling reached")

        input_bound = conservative_input_token_bound(request)
        output_bound = max_output_tokens * (2 if provider_name == "gemini" else 1)
        reservation = _Reservation(
            provider_name=provider_name,
            model_name=model_name,
            input_token_bound=input_bound,
            output_token_bound=output_bound,
            cost_nanos=_cost_nanos(
                input_tokens=input_bound,
                output_tokens=output_bound,
                input_price_usd_per_1m=prices.input_usd_per_1m,
                output_price_usd_per_1m=prices.output_usd_per_1m,
            ),
        )
        if self.accounted_cost_nanos + reservation.cost_nanos > self._max_cost_nanos:
            self._refused_calls += 1
            self._cost_limit_reached = True
            raise ShadowCostLimitError(
                "shadow cost ceiling cannot reserve the next provider invocation"
            )

        # Reserve before entering the provider. A timeout, cancellation, or process-level
        # exception after this assignment cannot make another call consume this same balance.
        self._active = reservation
        self._total_calls += 1
        self._case_calls += 1
        route_totals = self._route_totals.setdefault(
            (reservation.provider_name, reservation.model_name),
            _RouteTotals(),
        )
        route_totals.network_calls += 1
        return reservation

    @staticmethod
    def _usable_usage(response: object, reservation: _Reservation) -> bool:
        if not isinstance(response, LLMInvocationResponse):
            return False
        if (
            response.provider_name != reservation.provider_name
            or response.model_name != reservation.model_name
        ):
            return False
        input_tokens = response.input_tokens
        output_tokens = response.output_tokens
        return (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and input_tokens > 0
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
            and output_tokens >= 0
        )

    def _settle(self, response: object, *, prices: _TokenPrices) -> None:
        reservation = self._active
        if reservation is None:
            raise ShadowCostConfigurationError("no active shadow reservation to settle")
        self._active = None
        route_totals = self._route_totals[(reservation.provider_name, reservation.model_name)]

        if not self._usable_usage(response, reservation):
            self._unknown_reserved_cost_nanos += reservation.cost_nanos
            self._unknown_usage_calls += 1
            route_totals.unknown_reserved_cost_nanos += reservation.cost_nanos
            route_totals.unknown_usage_calls += 1
            return

        assert isinstance(response, LLMInvocationResponse)  # narrowed by _usable_usage
        actual_cost = _cost_nanos(
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            input_price_usd_per_1m=prices.input_usd_per_1m,
            output_price_usd_per_1m=prices.output_usd_per_1m,
        )
        if (
            response.input_tokens > reservation.input_token_bound
            or response.output_tokens > reservation.output_token_bound
            or actual_cost > reservation.cost_nanos
        ):
            # Preserve the known amount rather than hiding it behind the smaller reservation.
            # This means the provider violated a premise of the local hard ceiling, so execution
            # must stop instead of making another call from a now-untrusted balance.
            self._settled_usage_cost_nanos += actual_cost
            route_totals.settled_usage_cost_nanos += actual_cost
            self._accounting_errors += 1
            raise ShadowCostAccountingError(
                "provider usage exceeded the pre-send shadow cost envelope"
            )
        self._settled_usage_cost_nanos += actual_cost
        route_totals.settled_usage_cost_nanos += actual_cost

    def snapshot(self) -> dict[str, object]:
        """Return counters only; no provider payloads, prompts, case IDs, or exception text."""

        accounted = self.accounted_cost_nanos
        return {
            "configured_max_calls": self._max_calls,
            "network_calls": self._total_calls,
            "refused_calls": self._refused_calls,
            "unknown_usage_calls": self._unknown_usage_calls,
            "accounting_errors": self._accounting_errors,
            "halted": self.halted,
            "configured_max_cost_usd": _safe_usd(self._max_cost_nanos),
            "settled_usage_cost_usd": _safe_usd(self._settled_usage_cost_nanos),
            "unknown_reserved_cost_usd": _safe_usd(self._unknown_reserved_cost_nanos),
            "accounted_cost_upper_bound_usd": _safe_usd(accounted),
            "remaining_cost_usd": _safe_usd(max(0, self._max_cost_nanos - accounted)),
            "routes": [
                {
                    "provider_name": provider_name,
                    "model_name": model_name,
                    "network_calls": totals.network_calls,
                    "unknown_usage_calls": totals.unknown_usage_calls,
                    "settled_usage_cost_usd": _safe_usd(totals.settled_usage_cost_nanos),
                    "unknown_reserved_cost_usd": _safe_usd(totals.unknown_reserved_cost_nanos),
                }
                for (provider_name, model_name), totals in sorted(self._route_totals.items())
            ],
        }


class CostGuardedProvider:
    """Provider-protocol decorator that reserves before every live invocation."""

    def __init__(
        self,
        provider: LLMProviderAdapter,
        guard: ShadowCostGuard,
        *,
        max_output_tokens: int,
        prices: _TokenPrices,
    ) -> None:
        self._provider = provider
        self._guard = guard
        self._max_output_tokens = max_output_tokens
        self._prices = prices
        self.provider_name = provider.provider_name
        self.model_name = provider.model_name
        self.model_version = provider.model_version
        self._closed = False

    @property
    def cache_variant(self) -> Mapping[str, Any] | None:
        value = getattr(self._provider, "cache_variant", None)
        return value if isinstance(value, Mapping) else None

    def supports_mode(self, mode: LLMInvocationMode) -> bool:
        return self._provider.supports_mode(mode)

    def supports_structured_schema(self, schema_name: str) -> bool:
        return self._provider.supports_structured_schema(schema_name)

    def invoke(self, request: LLMInvocationRequest) -> LLMInvocationResponse:
        self._guard._reserve(
            provider_name=self.provider_name,
            model_name=self.model_name,
            request=request,
            max_output_tokens=self._max_output_tokens,
            prices=self._prices,
        )
        try:
            response = self._provider.invoke(request)
        except BaseException as exc:
            billed_response = getattr(exc, "response", None)
            self._guard._settle(billed_response, prices=self._prices)
            raise
        self._guard._settle(response, prices=self._prices)
        return response

    def invoke_batch(
        self, requests: Sequence[LLMInvocationRequest]
    ) -> Sequence[LLMInvocationResponse]:
        # The production HTTP adapters used by the shadow runner are realtime-only. Refusing here
        # is safer than reserving N logical requests around an opaque provider-side batch whose
        # transport and billing semantics differ from the sequential proof above.
        del requests
        raise ShadowCostConfigurationError(
            "live shadow cost guard supports sequential realtime invocations only"
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._provider, "close", None)
        if callable(close):
            close()


__all__ = [
    "APPROVED_SHADOW_MAX_CALLS",
    "APPROVED_SHADOW_MAX_COST_USD",
    "APPROVED_SHADOW_PRICE_FLOORS_USD_PER_1M",
    "CostGuardedProvider",
    "NANO_USD_PER_USD",
    "ShadowCallLimitError",
    "ShadowCostAccountingError",
    "ShadowCostConfigurationError",
    "ShadowCostGuard",
    "ShadowCostGuardError",
    "ShadowCostLimitError",
    "conservative_input_token_bound",
]
