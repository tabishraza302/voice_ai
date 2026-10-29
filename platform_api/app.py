"""FastAPI application factory for the platform control plane.

Owns DB pool lifespan, route mounting, security middleware, and error mapping.
Never imports GPU models or bot.db.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
import uuid

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from platform_api.accounts.admin_routes import router as admin_router
from platform_api.accounts.routes import router as account_router
from platform_api.auth.errors import (
    PlatformError,
    RequestTooLargeError,
    platform_error_response,
)
from platform_api.auth.routes import router as auth_router
from platform_api.config import PlatformSettings, get_settings
from platform_api.db.base import Base
from platform_api.db.schema import check_schema_readiness
from platform_api.db.session import create_engine, get_session_factory
from platform_api.health import router as health_router
import platform_api.models  # Register complete platform models onto Base.metadata

AUTH_ROUTE_BODY_LIMIT = 8192  # 8 KiB


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Enforces Request ID tracking and request body limits before route handlers."""

    async def dispatch(self, request: Request, call_next):
        # 1. Resolve or generate valid Request UUID
        req_id_header = request.headers.get("x-request-id")
        try:
            req_id = uuid.UUID(req_id_header) if req_id_header else uuid.uuid4()
        except ValueError:
            req_id = uuid.uuid4()

        request.state.request_id = req_id

        if request.url.path.startswith("/api/v1/auth"):
            settings = getattr(request.app.state, "settings", None) or get_settings()
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > AUTH_ROUTE_BODY_LIMIT:
                return platform_error_response(
                    RequestTooLargeError("Request body exceeds 8 KiB limit"),
                    req_id,
                    settings,
                )

            # Consume and verify body size
            body = await request.body()
            if len(body) > AUTH_ROUTE_BODY_LIMIT:
                return platform_error_response(
                    RequestTooLargeError("Request body exceeds 8 KiB limit"),
                    req_id,
                    settings,
                )

        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = str(req_id)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager owning DB connection pools and clean shutdown."""
    settings: PlatformSettings = getattr(app.state, "settings", None) or get_settings()
    app.state.settings = settings

    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory

    try:
        try:
            app.state.schema_ready = await check_schema_readiness(
                engine,
                schema=settings.postgres_schema,
                expected_version=settings.schema_version,
            )
        except Exception:
            app.state.schema_ready = False

        yield
    finally:
        if hasattr(app.state, "engine") and app.state.engine is not None:
            await app.state.engine.dispose()


def create_app(settings: PlatformSettings | None = None) -> FastAPI:
    """Create and configure FastAPI platform application instance."""
    resolved_settings = settings or get_settings()

    app = FastAPI(
        title="Voice AI Platform Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings

    # Request ID and body limit middleware
    app.add_middleware(RequestContextMiddleware)

    # Allowed Host headers
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=resolved_settings.allowed_hosts,
    )

    # CORS middleware for development web origin
    if resolved_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "X-CSRF-Token",
                "Idempotency-Key",
                "X-Request-ID",
            ],
        )

    # Exception Handlers -------------------------------------------------------

    @app.exception_handler(PlatformError)
    async def handle_platform_error(request: Request, exc: PlatformError):
        req_id = getattr(request.state, "request_id", uuid.uuid4())
        settings = getattr(request.app.state, "settings", None) or get_settings()
        return platform_error_response(exc, req_id, settings)
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        req_id = getattr(request.state, "request_id", uuid.uuid4())
        fields = []
        for err in exc.errors():
            loc = err.get("loc", ())
            field_name = str(loc[-1]) if loc else "body"
            # Never reflect rejected input values or secrets
            fields.append({"field": field_name, "code": err.get("type", "invalid")})

        err_obj = PlatformError(
            message="Validation error",
            fields=fields,
        )
        err_obj.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        err_obj.code = "VALIDATION"
        return platform_error_response(err_obj, req_id)

    @app.exception_handler(Exception)
    async def handle_generic_exception(request: Request, exc: Exception):
        req_id = getattr(request.state, "request_id", uuid.uuid4())
        err_obj = PlatformError(
            message="An unexpected internal error occurred",
            fields=[],
        )
        err_obj.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        err_obj.code = "INTERNAL_ERROR"
        return platform_error_response(err_obj, req_id)

    # Mount platform routers under /api/v1
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(auth_router)
    api_v1.include_router(account_router)
    api_v1.include_router(admin_router)
    api_v1.include_router(health_router)

    app.include_router(api_v1)
    # Also include health_router at root for /health/live, /health/ready, /catalog
    app.include_router(health_router)

    return app
