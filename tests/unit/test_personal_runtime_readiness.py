"""Synthetic configuration-only activation checks; no providers or feeds contacted."""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import httpx
import pytest

from packages.config.settings import Settings
from services.personal import paid_runtime
from services.personal.offline_fixture import offline_fixture_route
from services.personal.settings import feature_readiness


def synthetic_route(provider="openai"):
    generation = {
        "provider": provider,
        "model": "synthetic-generation",
        "model_version": "synthetic-v1",
        "price_revision": "synthetic-prices-not-live",
        "price_source_url": {
            "openai": "https://openai.com/api/pricing/",
            "anthropic": "https://www.anthropic.com/pricing",
            "gemini": "https://ai.google.dev/gemini-api/docs/pricing",
            "deepseek": "https://api-docs.deepseek.com/quick_start/pricing",
        }[provider],
        "input_usd_per_million_tokens": "10",
        "output_usd_per_million_tokens": "10",
        "max_input_tokens": 8192,
        "max_output_tokens": 1000,
        "deadline_seconds": 30,
    }
    embedding = {
        **generation,
        "provider": "openai",
        "model": "synthetic-embedding",
        "price_source_url": "https://openai.com/api/pricing/",
        "output_usd_per_million_tokens": "0",
        "max_output_tokens": 0,
    }
    return {"mode": "live", "generation": generation, "embedding": embedding}


def runtime(**overrides):
    return Settings(
        _env_file=None,
        **{
            "app_env": "test",
            "personal_paid_runtime_enabled": True,
            "openai_api_key": "synthetic-test-key",
            "anthropic_api_key": "synthetic-test-key",
            "gemini_api_key": "synthetic-test-key",
            "deepseek_api_key": "synthetic-test-key",
            "gemini_model_thinking_levels": {"synthetic-generation": "minimal"},
            **overrides,
        },
    )


def assisted_profile(route=None, **overrides):
    return SimpleNamespace(
        settings={
            "ai_enabled": True,
            "monthly_allowance_usd": "1",
            "model_route": route if route is not None else synthetic_route(),
            **overrides,
        },
        execution_profile="assisted",
        selected_source_ids=[uuid.uuid4()],
        schema_revision="personal-profile.v2",
    )


@pytest.fixture(autouse=True)
def forbid_external_dispatch(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Readiness must not construct providers or contact a transport")

    for name in ("build_paid_embedding_provider", "build_paid_orchestrator"):
        monkeypatch.setattr(paid_runtime, name, forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)


def test_server_activation_defaults_off_even_with_credentials_and_profile(monkeypatch):
    monkeypatch.delenv("PERSONAL_PAID_RUNTIME_ENABLED", raising=False)
    configured = Settings(_env_file=None, app_env="test", openai_api_key="synthetic-test-key")
    assert configured.personal_paid_runtime_enabled is False
    assert paid_runtime.runtime_readiness(configured, synthetic_route()) == "paid_runtime_disabled"
    features = feature_readiness(
        assisted_profile(), owner_ready=True, writer_ready=True, runtime=configured
    )
    assert features["embeddings"] == {"state": "disabled", "reason": "paid_runtime_disabled"}
    assert features["brief_generation"] == features["embeddings"]
    for name in ("raw_reader", "saved_and_briefs", "raw_collection"):
        assert features[name] == {"state": "ready", "reason": None}


@pytest.mark.parametrize("gate", [None, False, 0, 1, "true", "false", []])
def test_only_the_boolean_true_opens_the_gate(gate):
    configured = SimpleNamespace(personal_paid_runtime_enabled=gate)
    # The gate is checked before invalid route metadata or missing credentials.
    assert paid_runtime.runtime_readiness(configured, {}) == "paid_runtime_disabled"


@pytest.mark.parametrize("configured", [None, SimpleNamespace()])
def test_absent_runtime_or_gate_is_closed(configured):
    assert paid_runtime.runtime_readiness(configured, synthetic_route()) == "paid_runtime_disabled"
    assert feature_readiness(
        assisted_profile(), owner_ready=True, writer_ready=True, runtime=configured
    )["embeddings"] == {"state": "disabled", "reason": "paid_runtime_disabled"}


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "deepseek"])
def test_enabled_supported_synthetic_configuration_is_ready_and_not_mutated(provider):
    route = synthetic_route(provider)
    original = copy.deepcopy(route)
    configured = runtime()
    assert paid_runtime.runtime_readiness(configured, route) is None
    assert route == original
    features = feature_readiness(
        assisted_profile(route), owner_ready=True, writer_ready=True, runtime=configured
    )
    assert features["embeddings"] == {"state": "ready", "reason": None}
    assert features["brief_generation"] == features["embeddings"]


@pytest.mark.parametrize(
    "key", ["openai_api_key", "anthropic_api_key", "gemini_api_key", "deepseek_api_key"]
)
@pytest.mark.parametrize("value", ["", "   "])
def test_enabled_missing_generation_or_embedding_credentials_are_configuration_missing(key, value):
    provider = key.removesuffix("_api_key")
    configured = runtime(**{key: value})
    route = synthetic_route(provider)
    assert paid_runtime.runtime_readiness(configured, route) == "configuration_missing"
    assert feature_readiness(
        assisted_profile(route), owner_ready=True, writer_ready=True, runtime=configured
    )["embeddings"] == {"state": "blocked", "reason": "configuration_missing"}


def test_other_generation_provider_still_requires_openai_embedding_credentials():
    assert (
        paid_runtime.runtime_readiness(runtime(openai_api_key=""), synthetic_route("anthropic"))
        == "configuration_missing"
    )


def test_enabled_gemini_requires_explicit_supported_thinking_configuration():
    configured = runtime(gemini_model_thinking_levels={})
    assert (
        paid_runtime.runtime_readiness(configured, synthetic_route("gemini"))
        == "configuration_missing"
    )
    configured = configured.model_copy(
        update={"gemini_model_thinking_levels": {"synthetic-generation": "unsupported"}}
    )
    assert (
        paid_runtime.runtime_readiness(configured, synthetic_route("gemini"))
        == "configuration_missing"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda route: route.update(fallbacks=[]),
        lambda route: route["generation"].update(price_revision=""),
        lambda route: route["generation"].update(deadline_seconds=0),
        lambda route: route["generation"].update(max_input_tokens=None),
        lambda route: route["generation"].update(input_usd_per_million_tokens="NaN"),
        lambda route: route["embedding"].update(provider="anthropic"),
        lambda route: route["generation"].update(api_key="unexpected"),
    ],
)
def test_enabled_invalid_route_stays_closed(mutation):
    route = synthetic_route()
    mutation(route)
    assert paid_runtime.runtime_readiness(runtime(), route) == "configuration_missing"


def test_disabled_profile_allowance_and_raw_readiness_still_take_precedence():
    for subject, expected in [
        (assisted_profile(ai_enabled=False), "ai_disabled"),
        (assisted_profile(monthly_allowance_usd="0"), "allowance_reached"),
        (assisted_profile(model_route={}), "configuration_missing"),
    ]:
        features = feature_readiness(
            subject, owner_ready=True, writer_ready=True, runtime=runtime()
        )
        assert features["embeddings"]["reason"] == expected
        assert features["raw_collection"] == {"state": "ready", "reason": None}


def test_unready_collection_still_blocks_otherwise_ready_ai():
    for owner, writer, expected in [
        (False, True, "owner_selection_required"),
        (True, False, "personal_writer_unavailable"),
    ]:
        features = feature_readiness(
            assisted_profile(), owner_ready=owner, writer_ready=writer, runtime=runtime()
        )
        assert features["embeddings"] == {"state": "blocked", "reason": expected}
        assert features["raw_reader"] == {"state": "ready", "reason": None}
        assert features["saved_and_briefs"] == {"state": "ready", "reason": None}


def test_offline_fixture_and_legacy_live_profiles_preserve_their_boundary():
    subject = assisted_profile(offline_fixture_route("synthetic-case"))
    configured = runtime(
        personal_paid_runtime_enabled=False, personal_offline_fixture_path="fixture.json"
    )
    assert feature_readiness(subject, owner_ready=True, writer_ready=True, runtime=configured)[
        "embeddings"
    ] == {"state": "ready", "reason": None}
    subject = assisted_profile()
    subject.schema_revision = "personal-profile.v1"
    assert feature_readiness(subject, owner_ready=True, writer_ready=True, runtime=runtime())[
        "embeddings"
    ] == {"state": "blocked", "reason": "execution_route_unavailable"}


@pytest.mark.parametrize("extra", ["fallback", "route_extra", "role_extra"])
def test_v2_readiness_checks_exact_stored_route_before_public_redaction(extra):
    route = synthetic_route()
    if extra == "fallback":
        route["fallbacks"] = [{"provider": "anthropic", "model": "hidden-model"}]
    elif extra == "route_extra":
        route["unexpected"] = "not-an-execution-setting"
    else:
        route["generation"]["unexpected"] = "not-an-execution-setting"
    features = feature_readiness(
        assisted_profile(route), owner_ready=True, writer_ready=True, runtime=runtime()
    )
    assert features["embeddings"] == {"state": "blocked", "reason": "configuration_missing"}
    assert features["raw_reader"] == {"state": "ready", "reason": None}
