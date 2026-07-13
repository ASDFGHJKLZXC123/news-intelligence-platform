"""Application settings loaded from environment variables and an optional .env file.

Secrets are read from the environment only; they are never hard-coded here and are
never written to logs (see ``packages.config.logging``).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application configuration.

    All values come from the process environment or a local ``.env`` file. Defaults
    are conservative and safe for local development; production overrides everything
    via real environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -----------------------------------------------------------
    app_name: str = "news-intelligence-api"
    app_env: str = Field(default="local", description="local | dev | staging | prod")
    debug: bool = False
    log_level: str = "INFO"

    # --- Datastores ------------------------------------------------------------
    database_url: str = "postgresql+psycopg2://news:news@localhost:5432/news"
    redis_url: str = "redis://localhost:6379/0"

    # --- Celery ----------------------------------------------------------------
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- Security --------------------------------------------------------------
    # When empty, mutating endpoints are treated as local-only. When set, the API
    # key middleware stub enforces a matching ``X-API-Key`` header on mutations.
    api_key: str = ""
    # Comma-separated origins. Conservative default: localhost dev frontend only.
    cors_allow_origins: str = "http://localhost:3000"

    # --- External data providers ----------------------------------------------
    # API keys stay environment-only. Empty means the related provider task can
    # record lifecycle metadata but must not attempt authenticated live ingestion.
    fred_api_key: str = ""
    sec_user_agent: str = "news-intelligence-platform/0.1 (configure SEC_USER_AGENT)"
    gdelt_base_url: str = "https://api.gdeltproject.org/api/v2"
    ofac_base_url: str = "https://sanctionslistservice.ofac.treas.gov"
    gleif_base_url: str = "https://api.gleif.org/api/v1"
    world_bank_base_url: str = "https://api.worldbank.org/v2"
    reliefweb_base_url: str = "https://api.reliefweb.int/v1"
    reliefweb_app_name: str = "news-intelligence-platform"
    usgs_earthquake_base_url: str = "https://earthquake.usgs.gov/fdsnws/event/1"
    nasa_firms_base_url: str = "https://firms.modaps.eosdis.nasa.gov/api"
    nasa_firms_map_key: str = ""
    eia_base_url: str = "https://api.eia.gov/v2"
    eia_api_key: str = ""

    # --- NLP ------------------------------------------------------------------
    # Embedding readers must select one model space; comparing vectors produced by
    # different model/version pairs is undefined (ADR 0004).
    embedding_model: str = "text-embedding-3-small"
    embedding_model_version: str = "current"

    # --- LLM (Stage 2) -------------------------------------------------------
    # API secrets are read from the environment only.
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Budgeting controls for the orchestrator and runtime cost ceilings.
    llm_monthly_budget_usd: float = 10.0
    llm_budget_enforced: bool = True

    # Runtime model selection per service tier (ADR 0008). T0 is the embedding space
    # (ADR 0004); T1/T2 are Anthropic reasoning models; T3 is the cross-vendor OpenAI
    # second opinion. Model IDs live here and are never hard-coded at call sites.
    llm_models: dict[str, str] = {
        "T0": "text-embedding-3-small",
        "T1": "claude-haiku-4-5",
        "T2": "claude-sonnet-5",
        "T3": "gpt-4.1",
    }

    # Explicit tier -> provider mapping. Never infer the provider from the model string.
    llm_tier_providers: dict[str, str] = {
        "T0": "openai",
        "T1": "anthropic",
        "T2": "anthropic",
        "T3": "openai",
    }

    # Ordered cross-vendor fallbacks for reasoning tiers. T3 remains the OpenAI verifier.
    llm_tier_fallbacks: dict[str, list[dict[str, str]]] = {
        "T1": [{"provider": "openai", "model": "gpt-4.1-mini"}],
        "T2": [{"provider": "openai", "model": "gpt-4.1"}],
    }

    # Provider API base URLs (overridable for staging/proxy deployments).
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_api_version: str = "2023-06-01"
    openai_base_url: str = "https://api.openai.com"
    llm_request_timeout_seconds: float = 60.0

    # Provider request/throughput guardrails.
    llm_provider_rpm_limits: dict[str, int] = {
        "openai": 60,
        "anthropic": 60,
    }
    llm_provider_tpm_limits: dict[str, int] = {
        "openai": 120_000,
        "anthropic": 30_000,
    }

    # Provider pricing in USD per 1M tokens (input/output). Sub-cent precision matters:
    # a single T1 call costs a small fraction of a cent, and monthly spend is the sum of
    # hundreds of them, so these are never rounded to cents at the call site.
    llm_provider_token_price_usd_per_1m: dict[str, dict[str, float]] = {
        "anthropic:claude-haiku-4-5": {
            "input": 1.00,
            "output": 5.00,
        },
        "anthropic:claude-sonnet-5": {
            "input": 3.00,
            "output": 15.00,
        },
        "openai:gpt-4.1": {
            "input": 2.00,
            "output": 8.00,
        },
        "openai:gpt-4.1-mini": {
            "input": 0.40,
            "output": 1.60,
        },
        "openai:text-embedding-3-small": {
            "input": 0.02,
            "output": 0.00,
        },
    }

    # Provider Batch API discount (ADR 0008: daily batch stages take ~50% off).
    llm_batch_discount_multiplier: float = 0.5

    # Per-tier context and response token budgets.
    llm_tier_context_token_limits: dict[str, int] = {
        "T0": 8_000,
        "T1": 40_000,
        "T2": 64_000,
        "T3": 96_000,
    }
    llm_tier_max_output_tokens: dict[str, int] = {
        "T0": 0,
        "T1": 2_000,
        "T2": 4_000,
        "T3": 6_000,
    }

    # T2 retrieval cap.
    llm_t2_top_n: int = 5

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse the comma-separated CORS origins into a clean, explicit allow-list.

        Wildcard origins (``*``) are always rejected: combined with credentialed
        requests they are unsafe, so any ``*`` entry is dropped from the result.
        """
        return [
            origin.strip()
            for origin in self.cors_allow_origins.split(",")
            if origin.strip() and origin.strip() != "*"
        ]

    @property
    def is_local(self) -> bool:
        return self.app_env.lower() in {"local", "test"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance for the lifetime of the process."""
    return Settings()
