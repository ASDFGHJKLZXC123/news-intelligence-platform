"""HTTP middleware: request-id correlation and an API-key stub for mutations.

RequestIDMiddleware tags every request with a correlation id (honoring an inbound
``X-Request-ID``), binds it to the logging context, and bumps the request counter.

APIKeyMiddleware is the Stage 1 security stub: mutating methods are local-only by
default, and once ``API_KEY`` is configured they require a matching ``X-API-Key``
header before nonlocal exposure. Read-only methods and ``/health`` stay open.
"""

from __future__ import annotations

import hmac
import ipaddress
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from packages.config import metrics
from packages.config.logging import get_logger, set_request_id
from packages.config.settings import get_settings

logger = get_logger("apps.api.middleware")

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Paths exempt from the API-key requirement even when mutating.
_EXEMPT_PATHS = {"/health", "/metrics"}


def _is_loopback(host: str | None) -> bool:
    """True only for a parseable loopback address (127.0.0.0/8 or ::1)."""
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        set_request_id(request_id)
        metrics.increment(metrics.REQUESTS)
        try:
            response = await call_next(request)
        finally:
            set_request_id(None)
        response.headers["X-Request-ID"] = request_id
        return response


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Protect mutating endpoints with an API key once one is configured."""

    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()
        path = request.url.path
        if request.method in _MUTATING_METHODS and path not in _EXEMPT_PATHS:
            if settings.api_key:
                provided = request.headers.get("X-API-Key", "")
                # Constant-time comparison avoids leaking the key via timing.
                if not hmac.compare_digest(
                    provided.encode("utf-8"), settings.api_key.encode("utf-8")
                ):
                    logger.warning("rejected mutating request: bad api key", extra={"path": path})
                    return JSONResponse(status_code=401, content={"detail": "invalid api key"})
            else:
                # No key configured: only a local environment calling from loopback may
                # mutate. A missing, unparseable, or nonlocal host is denied.
                host = request.client.host if request.client else None
                if not (settings.is_local and _is_loopback(host)):
                    logger.warning(
                        "rejected mutating request: no api key and not local loopback",
                        extra={"path": path},
                    )
                    return JSONResponse(
                        status_code=401, content={"detail": "mutations require an api key"}
                    )
        return await call_next(request)
