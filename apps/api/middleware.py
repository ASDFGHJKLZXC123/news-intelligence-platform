"""HTTP middleware and error handling: request-id correlation, an API-key stub, and the
uniform JSON error envelope.

RequestIDMiddleware tags every request with a correlation id (honoring an inbound
``X-Request-ID``), stores it on ``request.state`` so inner middleware and exception
handlers can read it, binds it to the logging context, and bumps the request counter.

APIKeyMiddleware is the Stage 1 security stub: mutating methods and internal operator
reads are local-only by default, and once ``API_KEY`` is configured they require a
matching ``X-API-Key`` header before nonlocal exposure. Public read-only methods and
``/health`` stay open.

Every non-2xx JSON response -- whether raised inside a route, produced by request
validation, returned by the API-key stub, or the result of an unhandled error -- is
rendered as ``{"error": {"code", "message", "request_id"}}`` with the same request id
surfaced in the ``X-Request-ID`` header (api-adapter-contract, "Error envelope").
"""

from __future__ import annotations

import hmac
import ipaddress
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from packages.config import metrics
from packages.config.logging import get_logger, set_request_id
from packages.config.settings import get_settings

logger = get_logger("apps.api.middleware")

REQUEST_ID_HEADER = "X-Request-ID"

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Paths exempt from the API-key requirement even when mutating.
_EXEMPT_PATHS = {"/health", "/metrics"}
_INTERNAL_PREFIX = "/api/v1/internal/"


def _is_loopback(host: str | None) -> bool:
    """True only for a parseable loopback address (127.0.0.0/8 or ::1)."""
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _personal_same_origin(request: Request, origin: str, settings: Any) -> bool:
    """Allow the loopback app's own browser without trusting a foreign Host/origin."""
    return (
        settings.personal_processing_transport == "subprocess"
        and settings.personal_processing_mode == "personal"
        and request.client is not None
        and _is_loopback(request.client.host)
        and (request.url.hostname == "localhost" or _is_loopback(request.url.hostname))
        and origin == str(request.base_url).rstrip("/")
    )


def get_request_id(request: Request) -> str:
    """The correlation id for this request.

    RequestIDMiddleware stores it on ``request.state`` before any inner middleware or
    handler runs, so that is the source of truth; the header and a fresh id are only
    fallbacks for a request that somehow bypassed the middleware.
    """
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        return request_id
    return request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())


def error_envelope(*, code: str, message: str, request_id: str) -> dict[str, Any]:
    """The uniform error body shared by every non-2xx JSON response."""
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _status_slug(status_code: int) -> str:
    """A stable machine-readable code for a status, e.g. 404 -> ``not_found``."""
    try:
        return HTTPStatus(status_code).name.lower()
    except ValueError:
        return f"http_{status_code}"


def _error_response(request: Request, *, status_code: int, code: str, message: str) -> JSONResponse:
    """Build an error-envelope response carrying the request's correlation id.

    The id is set both in the body and on the ``X-Request-ID`` header so it is present
    even on the 500 path, which is handled outside RequestIDMiddleware and so is never
    stamped by it.
    """
    request_id = get_request_id(request)
    response = JSONResponse(
        status_code=status_code,
        content=error_envelope(code=code, message=message, request_id=request_id),
    )
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def _http_exception_message(exc: StarletteHTTPException) -> str:
    """A string message for the envelope, without leaking a 5xx's internal detail."""
    detail = exc.detail
    if isinstance(detail, str):
        return detail
    # A structured detail (e.g. a contract-violation 500) contributes only its human
    # message; the rest is server-side context that must not cross the wire.
    if isinstance(detail, Mapping) and isinstance(detail.get("message"), str):
        return detail["message"]
    return HTTPStatus(exc.status_code).phrase


def _validation_message(exc: RequestValidationError) -> str:
    """A concise, field-scoped summary of a 422 for an error toast."""
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(
            str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path")
        )
        msg = error.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "request validation failed"


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render any HTTPException (including a routed 404) as the error envelope."""
    response = _error_response(
        request,
        status_code=exc.status_code,
        code=_status_slug(exc.status_code),
        message=_http_exception_message(exc),
    )
    # Preserve headers the exception carried (e.g. WWW-Authenticate, Retry-After).
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render a request-validation failure (422) as the error envelope."""
    return _error_response(
        request,
        status_code=422,
        code="validation_error",
        message=_validation_message(exc),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an unhandled error as a 500 envelope, logging details server-side only.

    This runs in the outermost ServerErrorMiddleware -- outside RequestIDMiddleware --
    so it reads the id from ``request.state`` and stamps the header itself. The message
    is generic; the traceback and correlation id go to the log, never to the client.
    """
    request_id = get_request_id(request)
    logger.exception(
        "unhandled exception", extra={"request_id": request_id, "path": request.url.path}
    )
    return _error_response(
        request,
        status_code=500,
        code="internal_error",
        message="internal server error",
    )


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        # Persist on the request scope so inner middleware and exception handlers (which
        # build their own Request from the same scope) can read the same id.
        request.state.request_id = request_id
        set_request_id(request_id)
        metrics.increment(metrics.REQUESTS)
        try:
            response = await call_next(request)
        finally:
            set_request_id(None)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Protect mutations and internal operator reads once an API key is configured."""

    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()
        path = request.url.path
        protected = request.method in _MUTATING_METHODS or path.startswith(_INTERNAL_PREFIX)
        if protected and path not in _EXEMPT_PATHS:
            origin = request.headers.get("Origin")
            if (
                origin is not None
                and origin not in settings.cors_origins_list
                and not _personal_same_origin(request, origin, settings)
            ):
                logger.warning(
                    "rejected protected browser request: origin is not allowed",
                    extra={"path": path},
                )
                return _error_response(
                    request,
                    status_code=403,
                    code="forbidden",
                    message="browser origin is not allowed for protected requests",
                )
            if settings.api_key:
                provided = request.headers.get("X-API-Key", "")
                # Constant-time comparison avoids leaking the key via timing.
                if not hmac.compare_digest(
                    provided.encode("utf-8"), settings.api_key.encode("utf-8")
                ):
                    logger.warning("rejected protected request: bad api key", extra={"path": path})
                    return _error_response(
                        request, status_code=401, code="unauthorized", message="invalid api key"
                    )
            else:
                # No key configured: only a local environment calling from loopback may
                # mutate. A missing, unparseable, or nonlocal host is denied.
                host = request.client.host if request.client else None
                if not (settings.is_local and _is_loopback(host)):
                    logger.warning(
                        "rejected protected request: no api key and not local loopback",
                        extra={"path": path},
                    )
                    return _error_response(
                        request,
                        status_code=401,
                        code="unauthorized",
                        message="protected requests require an api key",
                    )
        return await call_next(request)
