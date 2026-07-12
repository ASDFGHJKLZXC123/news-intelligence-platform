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
