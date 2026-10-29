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
from platform_api.db.base import DEFAULT_PLATFORM_SCHEMA


def create_engine(
    settings_or_url: PlatformSettings | str | None = None,
    *,
    schema: str | None = None,
    **engine_kwargs,
) -> AsyncEngine:
    """Create an AsyncEngine for the platform database with search_path and schema translation."""
    if isinstance(settings_or_url, str):
        url = settings_or_url
        target_schema = schema or DEFAULT_PLATFORM_SCHEMA
    elif isinstance(settings_or_url, PlatformSettings):
        url = settings_or_url.database_url
        target_schema = schema or getattr(settings_or_url, "postgres_schema", DEFAULT_PLATFORM_SCHEMA)
    else:
        current_settings = get_settings()
        url = current_settings.database_url
        target_schema = schema or getattr(current_settings, "postgres_schema", DEFAULT_PLATFORM_SCHEMA)

    defaults: dict = {
        "echo": False,
        "pool_pre_ping": True,
    }
    defaults.update(engine_kwargs)

    connect_args = dict(defaults.pop("connect_args", None) or {})
    server_settings = dict(connect_args.get("server_settings") or {})
    if "search_path" not in server_settings and target_schema:
        server_settings["search_path"] = target_schema
    if server_settings:
        connect_args["server_settings"] = server_settings
    if connect_args:
        defaults["connect_args"] = connect_args

    if target_schema:
        exec_options = dict(defaults.pop("execution_options", None) or {})
        schema_map = dict(exec_options.get("schema_translate_map") or {})
        schema_map.setdefault(DEFAULT_PLATFORM_SCHEMA, target_schema)
        exec_options["schema_translate_map"] = schema_map
        defaults["execution_options"] = exec_options

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
