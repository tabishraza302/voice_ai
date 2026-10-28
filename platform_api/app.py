"""FastAPI application factory for the platform control plane.

Owns DB pool lifespan and route mounting. Never imports GPU models or bot.db.
Imports complete platform metadata for identical schema reflection with CLI.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from platform_api.config import PlatformSettings, get_settings
from platform_api.db.base import Base
from platform_api.db.schema import check_schema_readiness
from platform_api.db.session import create_engine, get_session_factory
from platform_api.health import router as health_router
import platform_api.models  # Register complete platform models onto Base.metadata


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager owning DB connection pools and clean shutdown."""
    settings: PlatformSettings = getattr(app.state, "settings", None) or get_settings()
    app.state.settings = settings

    # Initialize engine and session factory on app state
    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory

    try:
        # Startup checks schema readiness without mutating or seeding
        try:
            app.state.schema_ready = await check_schema_readiness(
                engine,
                expected_version=settings.schema_version,
            )
        except Exception:
            app.state.schema_ready = False

        yield
    finally:
        # Shutdown: cleanly dispose engine pools even if exceptions occur during startup or run
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

    # Enforce allowed Host headers before routing
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=resolved_settings.allowed_hosts,
    )

    # Configure CORS middleware if cors_origins is configured
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

    # Mount health and system routers
    app.include_router(health_router)

    return app
