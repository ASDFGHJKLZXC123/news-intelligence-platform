"""Secret-safe, no-network production configuration preflight.

The check deliberately reports only stable control names and booleans.  It never serializes
credentials, connection strings, provider endpoints, or validation exception messages.
"""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from packages.config.settings import Settings
from services.nlp.snapshot_registry import LEGACY_MODEL_VERSION, active_snapshot

PRODUCTION_CONFIG_SCHEMA: Final = "production-config-readiness.v1"
_PRODUCTION_ENVIRONMENTS: Final = frozenset({"staging", "prod"})
_PROVIDER_KEY_FIELDS: Final[Mapping[str, str]] = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
    "deepseek": "deepseek_api_key",
}
_PROVIDER_BASE_URL_FIELDS: Final[Mapping[str, str]] = {
    "anthropic": "anthropic_base_url",
    "openai": "openai_base_url",
    "gemini": "gemini_base_url",
    "deepseek": "deepseek_base_url",
}
_WEAK_SECRETS: Final = frozenset({"", "change-me", "changeme", "news", "password", "postgres"})


def _nonlocal_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.casefold().rstrip(".")
    if normalized in {"localhost", "host.docker.internal"} or normalized.endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return True


def _strong_secret(value: object, *, minimum: int = 32) -> bool:
    return (
        isinstance(value, str) and len(value) >= minimum and value.casefold() not in _WEAK_SECRETS
    )


def _database_url_is_hardened(value: str) -> bool:
    try:
        url = make_url(value)
        sslmode = str(url.query.get("sslmode", "")).casefold()
        password = url.password or ""
        return (
            url.drivername.startswith("postgresql")
            and _nonlocal_hostname(url.host)
            and bool(url.username)
            and _strong_secret(password, minimum=16)
            and bool(url.database)
            and sslmode in {"require", "verify-ca", "verify-full"}
        )
    except (TypeError, ValueError):
        return False


def _redis_url_is_hardened(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme.casefold() == "rediss"
            and _nonlocal_hostname(parsed.hostname)
            and _strong_secret(parsed.password or "", minimum=16)
        )
    except ValueError:
        return False


def _cors_is_hardened(settings: Settings) -> bool:
    origins = settings.cors_origins_list
    configured = {item.strip() for item in settings.cors_allow_origins.split(",")}
    if not origins or "*" in configured:
        return False
    try:
        parsed = [urlsplit(origin) for origin in origins]
    except ValueError:
        return False
    return all(
        origin.scheme.casefold() == "https"
        and _nonlocal_hostname(origin.hostname)
        and origin.username is None
        and origin.password is None
        for origin in parsed
    )


def _routed_models(settings: Settings) -> tuple[tuple[str, str], ...]:
    routes: list[tuple[str, str]] = []
    for tier, provider in settings.llm_tier_providers.items():
        model = settings.llm_models.get(tier, "")
        routes.append((provider, model))
        for fallback in settings.llm_tier_fallbacks.get(tier, ()):
            routes.append((fallback.get("provider", ""), fallback.get("model", "")))
    return tuple(dict.fromkeys(routes))


def _llm_routes_are_hardened(settings: Settings) -> bool:
    routes = _routed_models(settings)
    providers = {provider for provider, _model in routes}
    if not routes or not providers <= set(_PROVIDER_KEY_FIELDS):
        return False
    for provider, model in routes:
        if not provider or not model:
            return False
        if not _strong_secret(getattr(settings, _PROVIDER_KEY_FIELDS[provider], ""), minimum=16):
            return False
        try:
            parsed_base_url = urlsplit(getattr(settings, _PROVIDER_BASE_URL_FIELDS[provider]))
        except ValueError:
            return False
        if parsed_base_url.scheme.casefold() != "https" or not _nonlocal_hostname(
            parsed_base_url.hostname
        ):
            return False
        price = settings.llm_provider_token_price_usd_per_1m.get(f"{provider}:{model}")
        if not isinstance(price, Mapping):
            return False
        for price_kind in ("input", "output"):
            value = price.get(price_kind)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                return False
        if settings.llm_provider_rpm_limits.get(provider, 0) <= 0:
            return False
        if settings.llm_provider_tpm_limits.get(provider, 0) <= 0:
            return False
        if provider == "gemini" and model not in settings.gemini_model_thinking_levels:
            return False
    return True


def _embedding_snapshot_is_hardened(settings: Settings) -> bool:
    if (
        not settings.embedding_require_registered_snapshot
        or settings.embedding_model_version == LEGACY_MODEL_VERSION
    ):
        return False
    try:
        record = active_snapshot(
            model=settings.embedding_model,
            dimension=1536,
        )
    except Exception:  # noqa: BLE001 - the public result is deliberately a boolean only
        return False
    return record.snapshot_id == settings.embedding_model_version and record.status == "active"


def build_production_config_report(settings: Settings) -> dict[str, Any]:
    """Return deterministic pass/fail controls without touching network or mutable state."""

    checks = (
        ("deployment_environment", settings.app_env.casefold() in _PRODUCTION_ENVIRONMENTS),
        ("debug_disabled", settings.debug is False),
        ("api_key_strong", _strong_secret(settings.api_key)),
        ("cors_https_nonlocal", _cors_is_hardened(settings)),
        ("database_tls_nonlocal", _database_url_is_hardened(settings.database_url)),
        (
            "redis_tls_nonlocal",
            all(
                _redis_url_is_hardened(value)
                for value in (
                    settings.redis_url,
                    settings.celery_broker_url,
                    settings.celery_result_backend,
                )
            ),
        ),
        (
            "llm_budget_enforced",
            settings.llm_budget_enforced
            and math.isfinite(settings.llm_monthly_budget_usd)
            and settings.llm_monthly_budget_usd > 0,
        ),
        ("llm_routes_complete", _llm_routes_are_hardened(settings)),
        ("embedding_snapshot_active", _embedding_snapshot_is_hardened(settings)),
        ("crisis_prediction_reads_closed", settings.crisis_prediction_reads_enabled is False),
    )
    results = [{"name": name, "passed": bool(passed)} for name, passed in checks]
    return {
        "schema": PRODUCTION_CONFIG_SCHEMA,
        "ready": all(check["passed"] for check in results),
        "checks": results,
        "external_io": {
            "network": False,
            "database": False,
            "redis": False,
            "broker": False,
            "files_written": False,
        },
    }


__all__ = ["PRODUCTION_CONFIG_SCHEMA", "build_production_config_report"]
