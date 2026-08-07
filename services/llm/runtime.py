"""Production assembly for the Stage 2 LLM runtime."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from packages.config.settings import Settings
from services.llm.cache import RedisLLMPromptCache
from services.llm.http_providers import build_providers_by_tier
from services.llm.limiter import build_provider_rate_limiter
from services.llm.orchestrator import LLMOrchestrator
from services.llm.repository import SQLAlchemyLLMRuntimeRepository


def build_production_orchestrator(
    *,
    settings: Settings,
    session: Session,
    redis_client: Any,
    commit_on_write: bool = True,
) -> LLMOrchestrator:
    """Assemble durable persistence, live providers, cache, and RPM/TPM limiting.

    ``commit_on_write`` is the repository's own switch, surfaced here for the one caller shape
    that needs it: a task that runs the orchestrator *inside* a larger unit of work of its own
    (ADR 0005 entity linking calls it once per ambiguous mention while linking one event). Left
    at the default, each audit/job write is its own committed transaction. Set to false, the
    writes are flushed and the caller's commit or rollback covers them too -- so a failure
    halfway through cannot leave half an event committed behind a successful-looking retry.
    """

    providers_by_tier = build_providers_by_tier(settings)
    routed_provider_names = {
        provider.provider_name
        for tier_providers in providers_by_tier.values()
        for provider in tier_providers
    }
    try:
        limiter = build_provider_rate_limiter(
            redis_client=redis_client,
            # Environment JSON overrides replace an entire mapping. Scope construction to
            # routes that can actually run so adding an unrouted provider does not break an
            # older deployment override, while a routed provider missing either limit still
            # fails closed through the limiter's positive-limit validation.
            rpm_limits={
                name: settings.llm_provider_rpm_limits.get(name, 0)
                for name in routed_provider_names
            },
            tpm_limits={
                name: settings.llm_provider_tpm_limits.get(name, 0)
                for name in routed_provider_names
            },
        )
    except Exception as limiter_error:
        cleanup_errors: list[Exception] = []
        seen: set[int] = set()
        for tier_providers in providers_by_tier.values():
            for provider in tier_providers:
                if id(provider) in seen:
                    continue
                seen.add(id(provider))
                close = getattr(provider, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as cleanup_error:  # noqa: BLE001
                        cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            limiter_error.add_note(
                "provider cleanup also failed: "
                + "; ".join(
                    f"{type(error).__name__}: {error}" for error in cleanup_errors
                )
            )
        raise

    return LLMOrchestrator(
        settings=settings,
        repository=SQLAlchemyLLMRuntimeRepository(session, commit_on_write=commit_on_write),
        providers_by_tier=providers_by_tier,
        cache=RedisLLMPromptCache(redis_client=redis_client),
        limiter=limiter,
    )


__all__ = ["build_production_orchestrator"]
