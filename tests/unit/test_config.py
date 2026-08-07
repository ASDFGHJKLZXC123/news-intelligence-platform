"""Settings/configuration tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from packages.config.settings import Settings


def test_defaults_are_local_and_conservative() -> None:
    settings = Settings()
    assert settings.is_local  # APP_ENV=test (set in conftest) counts as local
    assert settings.crisis_prediction_reads_enabled is False
    # CORS must not be a wildcard by default.
    assert "*" not in settings.cors_origins_list
    assert settings.cors_origins_list  # non-empty allow-list


def test_crisis_prediction_reads_require_explicit_environment_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRISIS_PREDICTION_READS_ENABLED", "true")
    assert Settings().crisis_prediction_reads_enabled is True


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
    assert settings.database_connect_timeout_seconds == 3
    assert settings.database_pool_timeout_seconds == 3
    assert settings.database_statement_timeout_ms == 5_000
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.embedding_model_version == "current"
    assert settings.embedding_require_registered_snapshot is True
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
    assert settings.wikidata_sparql_endpoint == "https://query.wikidata.org/sparql"
    assert "configure WIKIDATA_USER_AGENT" in settings.wikidata_user_agent
    assert settings.wikidata_timeout_seconds == 30.0


@pytest.mark.parametrize(
    "field",
    [
        "database_connect_timeout_seconds",
        "database_pool_timeout_seconds",
        "database_statement_timeout_ms",
    ],
)
def test_database_timeouts_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError, match="database timeouts must be positive"):
        Settings(**{field: 0})


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
    monkeypatch.setenv("WIKIDATA_SPARQL_ENDPOINT", "https://example.test/sparql")
    monkeypatch.setenv("WIKIDATA_USER_AGENT", "Example App test@example.com")
    monkeypatch.setenv("WIKIDATA_TIMEOUT_SECONDS", "5")

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
    assert settings.wikidata_sparql_endpoint == "https://example.test/sparql"
    assert settings.wikidata_user_agent == "Example App test@example.com"
    assert settings.wikidata_timeout_seconds == 5.0


def test_stage2_llm_settings_default_values() -> None:
    settings = Settings()

    assert settings.anthropic_api_key == ""
    assert settings.openai_api_key == ""
    assert settings.gemini_api_key == ""
    assert settings.deepseek_api_key == ""
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
    assert settings.gemini_base_url == "https://generativelanguage.googleapis.com"
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.llm_request_timeout_seconds == 60.0
    assert settings.gemini_model_thinking_levels == {
        "gemini-3.5-flash-lite": "minimal",
        "gemini-3.6-flash": "low",
    }

    assert settings.llm_provider_rpm_limits == {
        "openai": 60,
        "anthropic": 60,
        "gemini": 60,
        "deepseek": 60,
    }
    assert settings.llm_provider_tpm_limits == {
        "openai": 120_000,
        "anthropic": 30_000,
        "gemini": 120_000,
        "deepseek": 120_000,
    }
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
    assert settings.llm_provider_token_price_usd_per_1m["gemini:gemini-3.6-flash"] == {
        "input": 1.50,
        "output": 7.50,
    }
    assert settings.llm_provider_token_price_usd_per_1m["gemini:gemini-3.5-flash-lite"] == {
        "input": 0.30,
        "output": 2.50,
    }
    assert settings.llm_provider_token_price_usd_per_1m["deepseek:deepseek-v4-flash"] == {
        "input": 0.14,
        "output": 0.28,
    }
    assert settings.llm_provider_token_price_usd_per_1m["deepseek:deepseek-v4-pro"] == {
        "input": 0.435,
        "output": 0.87,
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
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
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
    monkeypatch.setenv("GEMINI_BASE_URL", "https://example.test/gemini")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.test/deepseek")
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv(
        "GEMINI_MODEL_THINKING_LEVELS",
        json.dumps({"gemini-test": "HIGH"}),
    )
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
    assert settings.gemini_api_key == "gemini-test-key"
    assert settings.deepseek_api_key == "deepseek-test-key"
    assert settings.llm_monthly_budget_usd == 11.25
    assert settings.llm_budget_enforced is False
    assert settings.llm_models == {"T1": "x1", "T2": "x2", "T3": "x3"}
    assert settings.llm_tier_providers == {"T1": "anthropic", "T2": "openai", "T3": "openai"}
    assert settings.llm_tier_fallbacks == {"T1": [{"provider": "openai", "model": "fallback"}]}
    assert settings.anthropic_base_url == "https://example.test/anthropic"
    assert settings.openai_base_url == "https://example.test/openai"
    assert settings.gemini_base_url == "https://example.test/gemini"
    assert settings.deepseek_base_url == "https://example.test/deepseek"
    assert settings.llm_request_timeout_seconds == 12.5
    assert settings.gemini_model_thinking_levels == {"gemini-test": "high"}
    assert settings.llm_provider_rpm_limits == {"openai": 42, "anthropic": 11}
    assert settings.llm_provider_tpm_limits == {"openai": 1000, "anthropic": 2000}
    assert settings.llm_provider_token_price_usd_per_1m["provider:test"] == {
        "input": 0.1,
        "output": 0.2,
    }
    assert settings.llm_tier_context_token_limits == {"T1": 1000, "T2": 2000, "T3": 3000}
    assert settings.llm_tier_max_output_tokens == {"T1": 100, "T2": 200, "T3": 300}
    assert settings.llm_t2_top_n == 12


@pytest.mark.parametrize("level", ["", "none", "disabled", "extreme"])
def test_gemini_thinking_level_rejects_unsupported_values(level: str) -> None:
    with pytest.raises(ValidationError, match="GEMINI_MODEL_THINKING_LEVELS"):
        Settings(gemini_model_thinking_levels={"gemini-test": level})


# --- Entity identity refresh configuration (ADR 0006) ---------------------------------
def test_identity_refresh_defaults_are_offline_safe() -> None:
    """A fresh checkout must not reach SEC, GLEIF, or WDQS from a scheduled refresh."""
    settings = Settings()

    # Both curated inputs are empty, so neither monthly refresh has anything to look up.
    assert settings.identity_watchlist_names == []
    assert settings.wikidata_qid_seed_list == []
    # The fair-access User-Agents are still placeholders, which is what makes the SEC and
    # Wikidata bindings record a skipped run instead of calling out.
    assert "configure SEC_USER_AGENT" in settings.sec_user_agent
    assert "configure WIKIDATA_USER_AGENT" in settings.wikidata_user_agent
    # Every live call is bounded by an explicit timeout.
    assert settings.sec_timeout_seconds > 0
    assert settings.gleif_timeout_seconds > 0
    assert settings.wikidata_timeout_seconds > 0


def test_identity_watchlist_parsing_cleans_and_deduplicates() -> None:
    settings = Settings(identity_watchlist="Nestle S.A., Siemens AG ,, Nestle S.A. ,Bosch GmbH")

    # Repeats collapse and configured order is preserved; blanks never become a search.
    assert settings.identity_watchlist_names == ["Nestle S.A.", "Siemens AG", "Bosch GmbH"]


def test_wikidata_qid_seed_parsing_normalizes_and_deduplicates() -> None:
    settings = Settings(wikidata_qid_seeds=" q312 ,Q95,, Q312 ")

    assert settings.wikidata_qid_seed_list == ["Q312", "Q95"]


@pytest.mark.parametrize("value", ["Q312, P31", "Q0", "312", "Q312x", "Q"])
def test_a_malformed_wikidata_qid_seed_is_rejected_at_configuration_time(value: str) -> None:
    """A typo'd seed would otherwise silently drop an entity from the monthly enrichment."""
    with pytest.raises(ValidationError, match="invalid Wikidata QID"):
        Settings(wikidata_qid_seeds=value)


@pytest.mark.parametrize("field", ["gleif_search_limit", "wikidata_batch_size"])
def test_a_non_positive_identity_bound_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: 0})


def test_identity_settings_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "news-intel/1.0 (ops@example.com)")
    monkeypatch.setenv("WIKIDATA_USER_AGENT", "news-intel/1.0 (ops@example.com)")
    monkeypatch.setenv("GLEIF_USER_AGENT", "news-intel/1.0 (ops@example.com)")
    monkeypatch.setenv("IDENTITY_WATCHLIST", "Nestle S.A.,Siemens AG")
    monkeypatch.setenv("WIKIDATA_QID_SEEDS", "Q312,Q95")
    monkeypatch.setenv("WIKIDATA_SPARQL_ENDPOINT", "https://wdqs.example.test/sparql")
    monkeypatch.setenv("SEC_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("GLEIF_SEARCH_LIMIT", "5")
    monkeypatch.setenv("WIKIDATA_BATCH_SIZE", "50")

    settings = Settings()

    assert settings.sec_user_agent == "news-intel/1.0 (ops@example.com)"
    assert settings.gleif_user_agent == "news-intel/1.0 (ops@example.com)"
    assert settings.identity_watchlist_names == ["Nestle S.A.", "Siemens AG"]
    assert settings.wikidata_qid_seed_list == ["Q312", "Q95"]
    assert settings.wikidata_sparql_endpoint == "https://wdqs.example.test/sparql"
    assert settings.sec_timeout_seconds == 12.5
    assert settings.gleif_search_limit == 5
    assert settings.wikidata_batch_size == 50


# --- Mention extraction configuration (ADR 0005) --------------------------------------
def test_ner_defaults_name_the_transformer_pipeline_and_a_cpu_batch() -> None:
    settings = Settings()

    assert settings.ner_model == "en_core_web_trf"
    assert settings.ner_batch_size >= 1


def test_ner_settings_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("NER_MODEL", " en_core_web_sm ")
    monkeypatch.setenv("NER_BATCH_SIZE", "32")

    settings = Settings()

    assert settings.ner_model == "en_core_web_sm"  # surrounding whitespace is stripped
    assert settings.ner_batch_size == 32


@pytest.mark.parametrize("value", [0, -4])
def test_a_non_positive_ner_batch_size_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError, match="positive number of articles"):
        Settings(ner_batch_size=value)


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_ner_model_name_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="must name an installed spaCy pipeline"):
        Settings(ner_model=value)
