"""Fail-closed settings and raw-reader readiness without network/runtime initialization."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from services.personal.offline_fixture import offline_fixture_route
from services.personal.settings import (
    PersonalSettingsUpdate,
    effective_run_allowance,
    feature_readiness,
    processing_ai_enabled,
    processing_block_reason,
    safe_model_route,
    validated_settings,
)


def profile(**settings):
    return SimpleNamespace(
        settings=settings,
        execution_profile="assisted",
        selected_source_ids=[uuid.uuid4()],
        schema_revision="personal-profile.v2",
    )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.01", "1e1000", "1e-1000", 0.25, True])
def test_invalid_usd_values_are_rejected(value):
    with pytest.raises(ValidationError):
        PersonalSettingsUpdate(monthly_allowance_usd=value)


@pytest.mark.parametrize(
    "field", ["daily_article_limit", "run_article_limit", "enrichment_article_limit"]
)
def test_limits_require_bounded_integers(field):
    for value in (-1, 1.5, True, 100001):
        with pytest.raises(ValidationError):
            PersonalSettingsUpdate(**{field: value})
    assert getattr(PersonalSettingsUpdate(**{field: 0}), field) == 0


def test_unknown_secrets_and_fallbacks_cannot_enter_settings():
    with pytest.raises(ValidationError):
        PersonalSettingsUpdate(model_route={"mode": "live", "api_key": "not-allowed"})
    with pytest.raises(ValidationError):
        PersonalSettingsUpdate(model_route={"mode": "live", "fallbacks": ["hidden"]})
    with pytest.raises(ValidationError):
        PersonalSettingsUpdate(
            model_route={"mode": "offline_fixture", "generation": {"api_key": "not-allowed"}}
        )
    assert safe_model_route(
        {"mode": "live", "api_key": "secret", "generation": {"model": "test", "api_key": "secret"}}
    ) == {"mode": "live", "generation": {"model": "test"}}


def test_legacy_test_allowance_never_becomes_recurring_credit():
    old = profile(authorized_spend_usd=0.25, model_route={"mode": "live"})
    old.schema_revision = "personal-profile.v1"
    assert validated_settings(old)["monthly_allowance_usd"] is None
    assert effective_run_allowance(old) == Decimal("0")
    assert processing_block_reason(old) == "ai_disabled"


def test_disabled_has_zero_allowance_and_run_default_never_creates_credit():
    raw = profile(ai_enabled=True, monthly_allowance_usd="10")
    raw.execution_profile = "raw"
    assert effective_run_allowance(raw) == 0
    with pytest.raises(ValidationError, match="assisted"):
        PersonalSettingsUpdate(execution_profile="raw", ai_enabled=True)
    assert effective_run_allowance(profile(monthly_allowance_usd="10")) == 0
    assert effective_run_allowance(
        profile(ai_enabled=True, monthly_allowance_usd="0.03")
    ) == Decimal("0.03")
    assert effective_run_allowance(
        profile(ai_enabled=True, monthly_allowance_usd="0.03", run_allowance_usd="10")
    ) == Decimal("0.03")
    assert effective_run_allowance(
        profile(ai_enabled=True, monthly_allowance_usd="0.03", run_allowance_usd="0.01")
    ) == Decimal("0.01")


def test_raw_reader_is_ready_while_optional_ai_is_disabled_or_missing():
    for subject, reason in [
        (profile(), "ai_disabled"),
        (profile(ai_enabled=True), "configuration_missing"),
        (
            profile(ai_enabled=True, monthly_allowance_usd="0", model_route={"mode": "live"}),
            "allowance_reached",
        ),
    ]:
        features = feature_readiness(subject, owner_ready=True, writer_ready=True)
        assert features["raw_reader"]["state"] == "ready"
        assert features["raw_collection"]["state"] == "ready"
        assert features["embeddings"]["reason"] == reason
        assert features["forecasting"]["reason"] == "disabled_by_profile"


def test_offline_fixture_remains_a_free_explicit_processing_profile():
    subject = profile(model_route=offline_fixture_route("case-1"))
    assert processing_ai_enabled(subject)
    assert effective_run_allowance(subject) == 0
    runtime = SimpleNamespace(app_env="test", personal_offline_fixture_path="fixture.json")
    assert (
        feature_readiness(subject, owner_ready=True, writer_ready=True, runtime=runtime)[
            "embeddings"
        ]["state"]
        == "ready"
    )
    runtime.personal_offline_fixture_path = ""
    assert (
        feature_readiness(subject, owner_ready=True, writer_ready=True, runtime=runtime)[
            "embeddings"
        ]["reason"]
        == "offline_fixture_unavailable"
    )


def test_timezone_phrase_and_feed_selection_validation():
    with pytest.raises(ValidationError, match="timezone"):
        PersonalSettingsUpdate(timezone="Missing/Zone")
    with pytest.raises(ValidationError, match="letter or digit"):
        PersonalSettingsUpdate(include_phrases=["___"])
    with pytest.raises(ValidationError, match="max_enabled_feeds"):
        PersonalSettingsUpdate(
            max_enabled_feeds=1, selected_source_ids=[uuid.uuid4(), uuid.uuid4()]
        )
    source = uuid.uuid4()
    assert PersonalSettingsUpdate(selected_source_ids=[source, source]).selected_source_ids == [
        source
    ]
