"""Settings/configuration tests."""

from __future__ import annotations

import json

from packages.config.settings import Settings


def test_defaults_are_local_and_conservative() -> None:
    settings = Settings()
    assert settings.is_local  # APP_ENV=test (set in conftest) counts as local
    # CORS must not be a wildcard by default.
    assert "*" not in settings.cors_origins_list
    assert settings.cors_origins_list  # non-empty allow-list


def test_cors_origins_parsing() -> None:
    settings = Settings(cors_allow_origins="http://a.com, http://b.com ,")
    assert settings.cors_origins_list == ["http://a.com", "http://b.com"]


def test_cors_wildcard_is_always_rejected() -> None:
    # A bare wildcard yields an empty allow-list...
    assert Settings(cors_allow_origins="*").cors_origins_list == []
    # ...and a wildcard mixed with explicit origins drops only the wildcard.
    settings = Settings(cors_allow_origins="http://a.com, *, http://b.com")
    assert settings.cors_origins_list == ["http://a.com", "http://b.com"]


def test_log_level_is_normalized_upper() -> None:
    settings = Settings(log_level="debug")
    assert settings.log_level == "DEBUG"


def test_api_key_empty_by_default() -> None:
    # Local-first default: no API key required until explicitly configured.
    assert Settings(api_key="").api_key == ""


def test_provider_settings_defaults_are_safe() -> None:
    settings = Settings()
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.embedding_model_version == "current"
    assert settings.fred_api_key == ""
    assert "configure SEC_USER_AGENT" in settings.sec_user_agent
    assert settings.gdelt_base_url == "https://api.gdeltproject.org/api/v2"
    assert settings.ofac_base_url == "https://sanctionslistservice.ofac.treas.gov"
    assert settings.gleif_base_url == "https://api.gleif.org/api/v1"
    assert settings.world_bank_base_url == "https://api.worldbank.org/v2"
    assert settings.reliefweb_base_url == "https://api.reliefweb.int/v1"
    assert settings.reliefweb_app_name == "news-intelligence-platform"
    assert settings.usgs_earthquake_base_url == "https://earthquake.usgs.gov/fdsnws/event/1"
    assert settings.nasa_firms_base_url == "https://firms.modaps.eosdis.nasa.gov/api"
    assert settings.nasa_firms_map_key == ""
    assert settings.eia_base_url == "https://api.eia.gov/v2"
    assert settings.eia_api_key == ""


def test_provider_settings_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-fred-key")
    monkeypatch.setenv("SEC_USER_AGENT", "Example App test@example.com")
    monkeypatch.setenv("GDELT_BASE_URL", "https://example.test/gdelt")
    monkeypatch.setenv("OFAC_BASE_URL", "https://example.test/ofac")
    monkeypatch.setenv("GLEIF_BASE_URL", "https://example.test/gleif")
    monkeypatch.setenv("WORLD_BANK_BASE_URL", "https://example.test/world-bank")
    monkeypatch.setenv("RELIEFWEB_BASE_URL", "https://example.test/reliefweb")
    monkeypatch.setenv("RELIEFWEB_APP_NAME", "signal-test")
    monkeypatch.setenv("USGS_EARTHQUAKE_BASE_URL", "https://example.test/usgs")
    monkeypatch.setenv("NASA_FIRMS_BASE_URL", "https://example.test/firms")
    monkeypatch.setenv("NASA_FIRMS_MAP_KEY", "test-firms-key")
    monkeypatch.setenv("EIA_BASE_URL", "https://example.test/eia")
    monkeypatch.setenv("EIA_API_KEY", "test-eia-key")

    settings = Settings()

    assert settings.fred_api_key == "test-fred-key"
    assert settings.sec_user_agent == "Example App test@example.com"
    assert settings.gdelt_base_url == "https://example.test/gdelt"
    assert settings.ofac_base_url == "https://example.test/ofac"
    assert settings.gleif_base_url == "https://example.test/gleif"
    assert settings.world_bank_base_url == "https://example.test/world-bank"
    assert settings.reliefweb_base_url == "https://example.test/reliefweb"
    assert settings.reliefweb_app_name == "signal-test"
    assert settings.usgs_earthquake_base_url == "https://example.test/usgs"
    assert settings.nasa_firms_base_url == "https://example.test/firms"
    assert settings.nasa_firms_map_key == "test-firms-key"
    assert settings.eia_base_url == "https://example.test/eia"
    assert settings.eia_api_key == "test-eia-key"


def test_stage2_llm_settings_default_values() -> None:
    settings = Settings()

    assert settings.anthropic_api_key == ""
    assert settings.openai_api_key == ""
    assert settings.llm_monthly_budget_usd == 10.0
    assert settings.llm_budget_enforced is True

    # ADR 0008 tiering: T0 embeddings, T1 Haiku-class, T2 Sonnet-class, T3 OpenAI GPT-class.
    assert settings.llm_models == {
        "T0": "text-embedding-3-small",
        "T1": "claude-haiku-4-5",
        "T2": "claude-sonnet-5",
        "T3": "gpt-4.1",
    }
    # The provider is configured per tier, never inferred from the model string.
    assert settings.llm_tier_providers == {
        "T0": "openai",
        "T1": "anthropic",
        "T2": "anthropic",
        "T3": "openai",
    }
    assert settings.llm_tier_fallbacks == {
        "T1": [{"provider": "openai", "model": "gpt-4.1-mini"}],
        "T2": [{"provider": "openai", "model": "gpt-4.1"}],
    }

    assert settings.anthropic_base_url == "https://api.anthropic.com"
    assert settings.anthropic_api_version == "2023-06-01"
    assert settings.openai_base_url == "https://api.openai.com"
    assert settings.llm_request_timeout_seconds == 60.0

    assert settings.llm_provider_rpm_limits == {"openai": 60, "anthropic": 60}
    assert settings.llm_provider_tpm_limits == {"openai": 120_000, "anthropic": 30_000}
    assert settings.llm_provider_token_price_usd_per_1m["anthropic:claude-haiku-4-5"] == {
        "input": 1.00,
        "output": 5.00,
    }
    assert settings.llm_provider_token_price_usd_per_1m["anthropic:claude-sonnet-5"] == {
        "input": 3.00,
        "output": 15.00,
    }
    assert settings.llm_provider_token_price_usd_per_1m["openai:gpt-4.1"] == {
        "input": 2.00,
        "output": 8.00,
    }
    assert settings.llm_batch_discount_multiplier == 0.5

    # Every tier that routing can select must carry a context and output budget.
    assert settings.llm_tier_context_token_limits == {
        "T0": 8_000,
        "T1": 40_000,
        "T2": 64_000,
        "T3": 96_000,
    }
    assert settings.llm_tier_max_output_tokens == {
        "T0": 0,
        "T1": 2_000,
        "T2": 4_000,
        "T3": 6_000,
    }
    assert settings.llm_t2_top_n == 5


def test_stage2_llm_settings_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "11.25")
    monkeypatch.setenv("LLM_BUDGET_ENFORCED", "false")
    monkeypatch.setenv("LLM_MODELS", json.dumps({"T1": "x1", "T2": "x2", "T3": "x3"}))
    monkeypatch.setenv(
        "LLM_TIER_PROVIDERS",
        json.dumps({"T1": "anthropic", "T2": "openai", "T3": "openai"}),
    )
    monkeypatch.setenv(
        "LLM_TIER_FALLBACKS",
        json.dumps({"T1": [{"provider": "openai", "model": "fallback"}]}),
    )
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.test/anthropic")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/openai")
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("LLM_PROVIDER_RPM_LIMITS", json.dumps({"openai": 42, "anthropic": 11}))
    monkeypatch.setenv("LLM_PROVIDER_TPM_LIMITS", json.dumps({"openai": 1000, "anthropic": 2000}))
    monkeypatch.setenv(
        "LLM_PROVIDER_TOKEN_PRICE_USD_PER_1M",
        json.dumps({"provider:test": {"input": 0.1, "output": 0.2}}),
    )
    monkeypatch.setenv(
        "LLM_TIER_CONTEXT_TOKEN_LIMITS",
        json.dumps({"T1": 1000, "T2": 2000, "T3": 3000}),
    )
    monkeypatch.setenv(
        "LLM_TIER_MAX_OUTPUT_TOKENS",
        json.dumps({"T1": 100, "T2": 200, "T3": 300}),
    )
    monkeypatch.setenv("LLM_T2_TOP_N", "12")

    settings = Settings()

    assert settings.anthropic_api_key == "anthropic-test-key"
    assert settings.openai_api_key == "openai-test-key"
    assert settings.llm_monthly_budget_usd == 11.25
    assert settings.llm_budget_enforced is False
    assert settings.llm_models == {"T1": "x1", "T2": "x2", "T3": "x3"}
    assert settings.llm_tier_providers == {"T1": "anthropic", "T2": "openai", "T3": "openai"}
    assert settings.llm_tier_fallbacks == {
        "T1": [{"provider": "openai", "model": "fallback"}]
    }
    assert settings.anthropic_base_url == "https://example.test/anthropic"
    assert settings.openai_base_url == "https://example.test/openai"
    assert settings.llm_request_timeout_seconds == 12.5
    assert settings.llm_provider_rpm_limits == {"openai": 42, "anthropic": 11}
    assert settings.llm_provider_tpm_limits == {"openai": 1000, "anthropic": 2000}
    assert settings.llm_provider_token_price_usd_per_1m["provider:test"] == {
        "input": 0.1,
        "output": 0.2,
    }
    assert settings.llm_tier_context_token_limits == {"T1": 1000, "T2": 2000, "T3": 3000}
    assert settings.llm_tier_max_output_tokens == {"T1": 100, "T2": 200, "T3": 300}
    assert settings.llm_t2_top_n == 12
