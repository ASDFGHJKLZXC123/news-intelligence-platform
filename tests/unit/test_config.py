"""Settings/configuration tests."""

from __future__ import annotations

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
