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

    return LLMOrchestrator(
        settings=settings,
        repository=SQLAlchemyLLMRuntimeRepository(session, commit_on_write=commit_on_write),
        providers_by_tier=build_providers_by_tier(settings),
        cache=RedisLLMPromptCache(redis_client=redis_client),
        limiter=build_provider_rate_limiter(
            redis_client=redis_client,
            rpm_limits=settings.llm_provider_rpm_limits,
            tpm_limits=settings.llm_provider_tpm_limits,
        ),
    )


__all__ = ["build_production_orchestrator"]
