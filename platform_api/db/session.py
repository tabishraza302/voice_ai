"""Session and engine lifecycle for the platform control plane.

Owns AsyncEngine and AsyncSession factories. Completely isolated from bot.db.
FastAPI dependencies resolve session factories strictly from request.app.state,
with no global session fallback.
"""

from __future__ import annotations

from typing import AsyncIterator
from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from platform_api.config import PlatformSettings, get_settings


def create_engine(
    settings_or_url: PlatformSettings | str | None = None,
    **engine_kwargs,
) -> AsyncEngine:
    """Create an AsyncEngine for the platform database."""
    if isinstance(settings_or_url, str):
        url = settings_or_url
    elif isinstance(settings_or_url, PlatformSettings):
        url = settings_or_url.database_url
    else:
        url = get_settings().database_url

    defaults = {
        "echo": False,
        "pool_pre_ping": True,
    }
    defaults.update(engine_kwargs)
    return create_async_engine(url, **defaults)


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async sessionmaker for the given engine."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an isolated AsyncSession from request.app.state.

    Explicitly requires app.state.session_factory. No global session fallback is permitted.
    """
    if (
        request is None
        or not hasattr(request, "app")
        or not hasattr(request.app.state, "session_factory")
        or request.app.state.session_factory is None
    ):
        raise RuntimeError(
            "Database session factory is not configured on app.state. "
            "Ensure the FastAPI app was created via create_app() with lifespan active."
        )

    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory

    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
