"""Disposable PostgreSQL fixtures and test helpers.

Integration tests use an explicit TEST_DATABASE_URL pointing to an owned,
disposable PostgreSQL cluster. Each test or fixture creates a uniquely named
schema, runs migrations/metadata creation inside that schema, and cascades
the drop of only that schema on teardown.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from bot.db.base import Base
from bot.db.session import _ensure_declared_indexes
import bot.db.models  # noqa: F401

async def verify_test_cluster_ownership(connection) -> None:
    """Verify that the target PostgreSQL cluster has the disposable test ownership token.

    Never run tests, create schemas, or drop schemas against unverified or user databases.
    Requires TEST_DATABASE_OWNERSHIP_TOKEN to be explicitly set in the environment.
    """
    expected_token = os.environ.get("TEST_DATABASE_OWNERSHIP_TOKEN")
    if not expected_token:
        raise RuntimeError(
            "TEST_DATABASE_OWNERSHIP_TOKEN must be set in the environment to verify "
            "test cluster ownership; refusing to run tests."
        )

    try:
        result = await connection.execute(
            text("SELECT token FROM public._verification_cluster_ownership LIMIT 1")
        )
        token = result.scalar_one_or_none()
    except Exception as exc:
        raise RuntimeError(
            "Refusing to run tests: target PostgreSQL cluster lacks "
            "public._verification_cluster_ownership marker."
        ) from exc

    if token != expected_token:
        raise RuntimeError(
            f"Refusing to run tests: cluster ownership token mismatch ({token!r} != {expected_token!r})."
        )


def get_test_database_url() -> str | None:
    """Return explicit TEST_DATABASE_URL or None. Live user DB is never accessed."""
    return os.environ.get("TEST_DATABASE_URL")


def require_test_database_url() -> str:
    """Return TEST_DATABASE_URL or skip the running test."""
    url = get_test_database_url()
    if not url:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests against disposable schema")
    return url


@asynccontextmanager
async def disposable_postgres_schema(
    database_url: str | None = None,
) -> AsyncGenerator[tuple[AsyncEngine, async_sessionmaker[AsyncSession], str], None]:
    """Create a temporary schema in owned PostgreSQL and drop it on exit.

    Uses NullPool to avoid cross-event-loop connection sharing between async
    fixtures and Starlette TestClient threads.
    """
    url = database_url or require_test_database_url()
    schema = f"voice_ai_test_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(url, poolclass=NullPool)
    async with admin_engine.begin() as conn:
        await verify_test_cluster_ownership(conn)
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_ensure_declared_indexes)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        yield engine, factory, schema
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await verify_test_cluster_ownership(conn)
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()
