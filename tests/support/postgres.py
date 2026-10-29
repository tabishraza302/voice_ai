"""Disposable PostgreSQL fixtures and management.

Two fixtures share one owned cluster:

- ``fresh_pg`` creates a disposable *database* for the platform control-plane
  tests (verification-plan.md Sections 3 and 4).
- ``disposable_postgres_schema`` creates a disposable *schema* for the local
  agent-storage tests, so they never touch a real database.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import os
import re
from typing import Any, AsyncGenerator
import uuid
import pytest
import pytest_asyncio
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



async def _check_cluster_ownership_token(conn: Any, token: str) -> bool:
    """Verify that the PostgreSQL cluster has the run-owned verification token marker."""
    if not token or not token.strip():
        return False
    try:
        table_exists = await conn.scalar(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables "
                "  WHERE table_schema = 'public' AND table_name = '_verification_cluster_ownership'"
                ")"
            )
        )
        if not table_exists:
            return False

        has_token = await conn.scalar(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM _verification_cluster_ownership "
                "  WHERE token = :token"
                ")"
            ),
            {"token": token},
        )
        return bool(has_token)
    except Exception:
        return False


def _derive_admin_url(candidate_url: str) -> str:
    """Derive maintenance/admin database URL connecting to postgres database."""
    if "sqlite" in candidate_url.lower():
        raise ValueError("SQLite fallback is forbidden for platform database")

    parsed = re.sub(r"/([^/?]+)(\?.*)?$", r"/postgres\2", candidate_url)
    return parsed
def _build_database_url(admin_url: str, db_name: str) -> str:
    """Build URL for target database from admin URL."""
    return re.sub(r"/([^/?]+)(\?.*)?$", f"/{db_name}\\2", admin_url)

class FreshPostgres:
    def __init__(
        self,
        admin_url: str,
        db_name: str,
        url: str,
        engine: AsyncEngine,
    ) -> None:
        self.admin_url = admin_url
        self.db_name = db_name
        self.url = url
        self.engine = engine
        self.sessions = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    def new_session(self) -> AsyncSession:
        return self.sessions()

    async def initialize(
        self,
        *,
        expected_version: str = "platform-v1",
        metadata_hash: str | None = None,
    ) -> None:
        """Initialize platform schema using production schema initializer."""
        from platform_api.db.schema import initialize_schema, compute_metadata_hash
        from platform_api.db.base import Base

        if metadata_hash is None:
            metadata_hash = compute_metadata_hash(Base.metadata)

        await initialize_schema(
            self.engine,
            expected_version=expected_version,
            metadata_hash=metadata_hash,
        )
    async def dispose(self) -> None:
        await self.engine.dispose()

@pytest_asyncio.fixture
async def fresh_pg() -> FreshPostgres:
    admin_url_env = os.environ.get("PLATFORM_TEST_DATABASE_ADMIN_URL")
    run_token = (
        os.environ.get("PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN")
        or os.environ.get("_VERIFY_RUN_OWNERSHIP_TOKEN")
    )

    if not admin_url_env or not run_token:
        if os.environ.get("VERIFY_REQUIRED") == "1":
            pytest.fail(
                "required qualification prerequisite unavailable: "
                "PLATFORM_TEST_DATABASE_ADMIN_URL or PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN missing"
            )
        pytest.skip("PLATFORM_TEST_DATABASE_ADMIN_URL or ownership token not configured")

    if "sqlite" in admin_url_env.lower():
        raise ValueError("SQLite fallback is forbidden for platform database")

    admin_url = _derive_admin_url(admin_url_env)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")

    # Verify cluster ownership marker before creating database
    async with admin_engine.connect() as conn:
        has_marker = await _check_cluster_ownership_token(conn, run_token)
        if not has_marker:
            await admin_engine.dispose()
            raise PermissionError(
                "Cluster failed ownership verification: missing valid run ownership token marker; "
                "refusing database operations on unowned cluster"
            )

    bounded_token = run_token[:12]
    db_name = f"platform_verify_{bounded_token}_{uuid.uuid4().hex[:12]}"
    assert len(db_name) <= 63, f"database name {db_name} exceeds PostgreSQL 63-byte limit"
    target_url = _build_database_url(admin_url, db_name)

    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))

    engine = create_async_engine(target_url, pool_pre_ping=True)
    fresh = FreshPostgres(admin_url=admin_url, db_name=db_name, url=target_url, engine=engine)

    try:
        yield fresh
    finally:
        await fresh.dispose()
        try:
            async with admin_engine.connect() as conn:
                # Terminate any lingering connections to this test database
                # Verify cluster ownership marker before dropping database
                has_marker = await _check_cluster_ownership_token(conn, run_token)
                if not has_marker:
                    raise PermissionError(
                        "Cluster failed ownership verification on teardown: missing valid run ownership token marker; refusing drop"
                    )

                assert db_name.startswith(f"platform_verify_{bounded_token}_"), f"refusing to drop unowned database {db_name}"
                await conn.execute(
                    text(
                        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
                    )
                )
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await admin_engine.dispose()


# --- Local agent storage: disposable schema on the same owned cluster ---

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
