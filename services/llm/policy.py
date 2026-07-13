"""Tier/routing budget policy for the LLM runtime (ADR 0008)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from packages.config.settings import Settings
from services.llm.adapters import LLMInvocationMode


class LLMTierConfigurationError(RuntimeError):
    """Raised when a tier has no explicit provider/model configuration."""


class LLMTier(StrEnum):
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


#: Tiers a caller may ask for directly. T3 is the cross-vendor verification tier and is
#: reachable only by escalation (High/Critical risk link, or the P90 hotness trigger).
CALLABLE_TIERS: tuple[LLMTier, ...] = (LLMTier.T0, LLMTier.T1, LLMTier.T2)


@dataclass(frozen=True)
class LLMRoutingContext:
    """Inputs required to resolve tier and mode."""

    risk_level: str
    current_event_hotness: float
    trailing_7d_p90_hotness: float | None
    is_realtime: bool
    is_essential: bool
    ranking: int | None = None
    requested_tier: LLMTier = LLMTier.T1

    def __post_init__(self) -> None:
        if self.requested_tier not in CALLABLE_TIERS:
            raise ValueError(
                f"tier {self.requested_tier.value} cannot be requested directly; it is "
                "reached only by risk/hotness escalation"
            )


@dataclass(frozen=True)
class LLMRoutingDecision:
    """Resolved routing decision used by the orchestrator."""

    tier: LLMTier
    mode: LLMInvocationMode
    queue: str
    degraded_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded_reasons)


class LLMBudgetPolicy:
    """Route requests across tiers with monthly budget degradation rules."""

    _t3_risk_levels: frozenset[str] = frozenset({"high", "critical"})
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _requires_t3_escalation(self, context: LLMRoutingContext) -> bool:
        return (
            context.risk_level.lower() in self._t3_risk_levels
            or (
                context.trailing_7d_p90_hotness is not None
                and context.current_event_hotness >= context.trailing_7d_p90_hotness
            )
        )

    def _base_tier(self, context: LLMRoutingContext) -> LLMTier:
        if self._requires_t3_escalation(context):
            return LLMTier.T3
        return context.requested_tier

    def determine_mode(self, context: LLMRoutingContext) -> LLMInvocationMode:
        return LLMInvocationMode.REALTIME if context.is_realtime else LLMInvocationMode.BATCH

    def context_token_budget(self, tier: LLMTier) -> int:
        limits = self._settings.llm_tier_context_token_limits
        return limits.get(tier.value, limits["T1"])

    def output_token_budget(self, tier: LLMTier) -> int:
        limits = self._settings.llm_tier_max_output_tokens
        return limits.get(tier.value, limits["T1"])

    def decide(
        self,
        context: LLMRoutingContext,
        *,
        monthly_spend_usd: float,
    ) -> LLMRoutingDecision:
        tier = self._base_tier(context)
        degraded_reasons: list[str] = []
        queue = "essential"

        if (
            self._settings.llm_budget_enforced
            and monthly_spend_usd >= self._settings.llm_monthly_budget_usd
        ):
            if tier == LLMTier.T3:
                tier = LLMTier.T2
                degraded_reasons.append("t3_disabled")

            if tier == LLMTier.T2 and context.ranking is not None:
                if context.ranking > self._settings.llm_t2_top_n:
                    tier = LLMTier.T1
                    degraded_reasons.append("t2_top_n_restriction")

            if not context.is_essential:
                queue = "nonessential"
                degraded_reasons.append("nonessential_queue")

        return LLMRoutingDecision(
            tier=tier,
            mode=self.determine_mode(context),
            queue=queue,
            degraded_reasons=tuple(degraded_reasons),
        )


def resolve_tier_model(settings: Settings, tier: LLMTier) -> tuple[str, str, str]:
    """Return provider, model, and model version for a tier.

    The provider comes from ``llm_tier_providers`` only. It is never guessed from the
    model string: model IDs change shape across vendors and a prefix match would silently
    route a call to the wrong API.
    """

    provider_name = settings.llm_tier_providers.get(tier.value)
    model_name = settings.llm_models.get(tier.value)
    if not provider_name or not model_name:
        raise LLMTierConfigurationError(
            f"tier {tier.value} has no provider/model configured in settings"
        )
    # Version is pinned to a single family for runtime accounting and cache keys.
    return provider_name, model_name, "current"


__all__ = [
    "CALLABLE_TIERS",
    "LLMBudgetPolicy",
    "LLMRoutingContext",
    "LLMRoutingDecision",
    "LLMTier",
    "LLMTierConfigurationError",
    "resolve_tier_model",
]
