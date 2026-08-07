"""Application settings loaded from environment variables and an optional .env file.

Secrets are read from the environment only; they are never hard-coded here and are
never written to logs (see ``packages.config.logging``).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# Config is the lowest layer and must not import the provider package, so the QID shape is
# checked here directly; ``packages.providers`` re-validates every QID it is handed.
_WIKIDATA_QID_PATTERN = re.compile(r"^Q[1-9][0-9]*$")
_GEMINI_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high"})


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated setting into clean, non-empty entries."""
    return [item.strip() for item in value.split(",") if item.strip()]


def _deduplicate(values: list[str]) -> list[str]:
    """Drop repeats while preserving the configured order."""
    return list(dict.fromkeys(values))


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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Keep test processes isolated from developer secrets and live routing in ``.env``."""

        del cls, settings_cls
        if os.environ.get("APP_ENV", "").casefold() == "test":
            return init_settings, env_settings, file_secret_settings
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    # --- Application -----------------------------------------------------------
    app_name: str = "news-intelligence-api"
    app_env: str = Field(default="local", description="local | dev | staging | prod")
    debug: bool = False
    log_level: str = "INFO"
    # Gate G remains closed until prediction-backed public reads receive explicit approval.
    # Model generation and persistence are intentionally independent of this read switch.
    crisis_prediction_reads_enabled: bool = False

    # --- Datastores ------------------------------------------------------------
    database_url: str = "postgresql+psycopg2://news:news@localhost:5432/news"
    database_connect_timeout_seconds: int = 3
    database_pool_timeout_seconds: int = 3
    database_statement_timeout_ms: int = 5_000
    redis_url: str = "redis://localhost:6379/0"

    # --- Celery ----------------------------------------------------------------
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- Security --------------------------------------------------------------
    # When empty, mutating endpoints are treated as local-only. When set, the API
    # key middleware stub enforces a matching ``X-API-Key`` header on mutations.
    api_key: str = ""
    # Comma-separated origins. Conservative default: the no-build static frontend served
    # locally via ``python -m http.server 3000 --directory frontend`` (ADR 0007), reachable
    # on both host spellings. Never a wildcard (see ``cors_origins_list``).
    cors_allow_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

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
    # WDQS enforces a user-agent policy like SEC fair access: a generic agent gets blocked.
    wikidata_sparql_endpoint: str = "https://query.wikidata.org/sparql"
    wikidata_user_agent: str = "news-intelligence-platform/0.1 (configure WIKIDATA_USER_AGENT)"
    wikidata_timeout_seconds: float = 30.0

    # --- Entity identity refreshes (ADR 0006) ---------------------------------
    # The three scheduled identity refreshes are bounded by explicit configuration. Every
    # default here is offline-safe: the User-Agent placeholders make the SEC and Wikidata
    # tasks record a skipped run instead of calling out, and the curated inputs are empty,
    # so importing this module or running the unit suite never reaches a live endpoint.
    sec_company_tickers_url: str = "https://www.sec.gov/files/company_tickers.json"
    sec_timeout_seconds: float = 30.0
    gleif_user_agent: str = "news-intelligence-platform/0.1 (configure GLEIF_USER_AGENT)"
    gleif_timeout_seconds: float = 30.0
    gleif_search_limit: int = 20
    # Curated legal names GLEIF may search beyond the SEC-seeded profiles: the non-US and
    # private parents the SEC company_tickers seed never lists. Comma-separated.
    identity_watchlist: str = ""
    # Explicit QIDs the Wikidata refresh may enrich on top of the entities that SEC/GLEIF
    # identifiers already resolve to. Comma-separated; there is no free-text discovery.
    wikidata_qid_seeds: str = ""
    wikidata_batch_size: int = 100

    # --- NLP ------------------------------------------------------------------
    # Embedding readers must select one model space; comparing vectors produced by
    # different model/version pairs is undefined (ADR 0004).
    embedding_model: str = "text-embedding-3-small"
    embedding_model_version: str = "current"
    # New production writes fail closed on the legacy/unverifiable ``current`` label. Tests and
    # explicitly isolated migration tooling may opt out, but a live adapter must use a captured,
    # source-registered snapshot identity.
    embedding_require_registered_snapshot: bool = True

    # Mention extraction (ADR 0005 stage 1). The transformer pipeline is an explicit
    # deployment prerequisite (``python -m spacy download en_core_web_trf``): nothing in
    # the application downloads a model, and no import loads one. The batch size is the
    # number of articles handed to one ``nlp.pipe`` call on CPU.
    ner_model: str = "en_core_web_trf"
    ner_batch_size: int = 16

    # Deterministic news-mention linking (ADR 0005 stage 2). The candidate list a mention keeps is
    # injected into the stage-3 adjudication prompt, so its size is a cost ceiling, not a display
    # preference, and it is bounded here rather than at each call site.
    entity_link_max_candidates: int = 8

    # --- LLM (Stage 2) -------------------------------------------------------
    # API secrets are read from the environment only.
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    deepseek_api_key: str = ""

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
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_request_timeout_seconds: float = 60.0

    # Interactions API thinking tokens share ``max_output_tokens`` with visible JSON. Keep the
    # cheap T1 model at its minimal default and leave enough T2 budget for the composed report.
    # A routed model absent from this map fails adapter construction before a network call.
    gemini_model_thinking_levels: dict[str, str] = {
        "gemini-3.5-flash-lite": "minimal",
        "gemini-3.6-flash": "low",
    }

    # Application-side request/throughput guardrails. Gemini quotas vary by project/model/tier,
    # while DeepSeek publishes account-level concurrency rather than fixed RPM/TPM values.
    llm_provider_rpm_limits: dict[str, int] = {
        "openai": 60,
        "anthropic": 60,
        "gemini": 60,
        "deepseek": 60,
    }
    llm_provider_tpm_limits: dict[str, int] = {
        "openai": 120_000,
        "anthropic": 30_000,
        "gemini": 120_000,
        "deepseek": 120_000,
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
        "gemini:gemini-3.6-flash": {
            "input": 1.50,
            "output": 7.50,
        },
        "gemini:gemini-3.5-flash-lite": {
            "input": 0.30,
            "output": 2.50,
        },
        # DeepSeek publishes separate cache-hit and cache-miss input rates. The generic
        # accounting contract has one input rate, so use the conservative cache-miss price.
        "deepseek:deepseek-v4-flash": {
            "input": 0.14,
            "output": 0.28,
        },
        "deepseek:deepseek-v4-pro": {
            "input": 0.435,
            "output": 0.87,
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

    @field_validator("wikidata_qid_seeds")
    @classmethod
    def _validate_wikidata_qid_seeds(cls, value: str) -> str:
        """Reject a malformed QID at startup rather than on the monthly run.

        A typo'd seed is otherwise invisible: it would just quietly drop that entity from
        the enrichment set, and nothing downstream can tell the difference from an entity
        Wikidata has no item for.
        """
        for qid in _split_csv(value):
            if not _WIKIDATA_QID_PATTERN.match(qid.upper()):
                msg = f"WIKIDATA_QID_SEEDS contains an invalid Wikidata QID: {qid!r}"
                raise ValueError(msg)
        return value

    @field_validator("gleif_search_limit", "wikidata_batch_size")
    @classmethod
    def _validate_positive_bound(cls, value: int) -> int:
        """A non-positive bound would either fetch nothing or divide a batch by zero."""
        if value < 1:
            msg = "identity refresh bounds must be positive"
            raise ValueError(msg)
        return value

    @field_validator(
        "database_connect_timeout_seconds",
        "database_pool_timeout_seconds",
        "database_statement_timeout_ms",
    )
    @classmethod
    def _validate_database_timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError("database timeouts must be positive")
        return value

    @field_validator("ner_model")
    @classmethod
    def _validate_ner_model(cls, value: str) -> str:
        """An empty model name would only ever surface as an opaque spaCy load failure."""
        model = value.strip()
        if not model:
            msg = "NER_MODEL must name an installed spaCy pipeline"
            raise ValueError(msg)
        return model

    @field_validator("ner_batch_size")
    @classmethod
    def _validate_ner_batch_size(cls, value: int) -> int:
        """A non-positive batch is not a smaller batch: ``nlp.pipe`` would reject it."""
        if value < 1:
            msg = "NER_BATCH_SIZE must be a positive number of articles"
            raise ValueError(msg)
        return value

    @field_validator("entity_link_max_candidates")
    @classmethod
    def _validate_entity_link_max_candidates(cls, value: int) -> int:
        """A cap below one would drop the very candidate list stage 3 has to choose from."""
        if value < 1:
            msg = "ENTITY_LINK_MAX_CANDIDATES must keep at least one candidate"
            raise ValueError(msg)
        return value

    @field_validator("gemini_model_thinking_levels")
    @classmethod
    def _validate_gemini_model_thinking_levels(cls, value: dict[str, str]) -> dict[str, str]:
        """Only send documented Interactions thinking levels for explicitly named models."""

        normalized: dict[str, str] = {}
        for raw_model, raw_level in value.items():
            model = raw_model.strip()
            level = raw_level.strip().casefold()
            if not model:
                raise ValueError("GEMINI_MODEL_THINKING_LEVELS contains an empty model id")
            if level not in _GEMINI_THINKING_LEVELS:
                allowed = ", ".join(sorted(_GEMINI_THINKING_LEVELS))
                raise ValueError(
                    f"GEMINI_MODEL_THINKING_LEVELS for {model!r} must be one of: {allowed}"
                )
            normalized[model] = level
        return normalized

    @property
    def identity_watchlist_names(self) -> list[str]:
        """The curated legal names the GLEIF refresh searches, cleaned and de-duplicated."""
        return _deduplicate(_split_csv(self.identity_watchlist))

    @property
    def wikidata_qid_seed_list(self) -> list[str]:
        """The explicit QIDs the Wikidata refresh enriches, validated and de-duplicated."""
        return _deduplicate([qid.upper() for qid in _split_csv(self.wikidata_qid_seeds)])

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
