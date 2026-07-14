"""FastAPI application shell.

Wires logging, conservative CORS, the request-id and API-key middleware, and the
health/metrics router. No business endpoints exist in Stage 1.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.analogies import router as analogies_router
from apps.api.entities import router as entities_router
from apps.api.health import router as health_router
from apps.api.intelligence import router as intelligence_router
from apps.api.middleware import APIKeyMiddleware, RequestIDMiddleware
from apps.api.provider_data import company_research_router
from apps.api.provider_data import router as provider_data_router
from apps.api.risk_intelligence import router as risk_intelligence_router
from packages.config.logging import configure_logging, get_logger
from packages.config.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger("apps.api")

    app = FastAPI(title=settings.app_name, version="0.1.0")

    # Conservative CORS: only the explicitly allow-listed origins, no wildcard.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
    )

    # Middleware runs in reverse registration order: request-id wraps api-key so every
    # request — including rejected mutations — gets a correlation id and is counted.
    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(RequestIDMiddleware)

    app.include_router(health_router)
    app.include_router(provider_data_router)
    app.include_router(company_research_router)
    app.include_router(entities_router)
    app.include_router(risk_intelligence_router)
    app.include_router(intelligence_router)
    app.include_router(analogies_router)

    logger.info("api initialized", extra={"env": settings.app_env})
    return app


app = create_app()
