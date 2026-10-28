"""Contract-focused tests for platform schema, settings, and database isolation.

Covers:
- Fresh initialization on empty PostgreSQL database
- Repeat initialization no-op behavior
- Schema mismatch refusal on unknown nonempty or modified schemas
- Database separation validation and refusal of SQLite URLs (no fallback)
- Independent SQLAlchemy Base and MetaData isolation from bot.db
- Model constraints for auth identity and prepaid wallet
- Isolation from implicit root .env loading
"""

import base64
from datetime import datetime, timezone
import os
import uuid
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.support.postgres import FreshPostgres
from platform_api.models import User, WalletAccount
from platform_api.config import PlatformSettings
from platform_api.db.base import Base
from platform_api.db.schema import (
    SchemaMismatch,
    compute_metadata_hash,
    initialize_schema,
    check_schema_readiness,
)
import platform_api.models  # Ensure all platform models are registered

TEST_CSRF_KEY = base64.urlsafe_b64encode(b"C" * 32).decode("ascii")
TEST_RATE_KEY = base64.urlsafe_b64encode(b"R" * 32).decode("ascii")
TEST_MFA_KEY = base64.urlsafe_b64encode(b"M" * 32).decode("ascii")
TEST_KEYS = {
    "auth_csrf_key": TEST_CSRF_KEY,
    "auth_rate_key": TEST_RATE_KEY,
    "auth_mfa_key": TEST_MFA_KEY,
}

def test_platform_settings_no_implicit_dotenv(monkeypatch):
    """PlatformSettings must never implicitly load root .env when env vars are absent."""
    # Ensure PLATFORM_DATABASE_URL is not in environment
    monkeypatch.delenv("PLATFORM_DATABASE_URL", raising=False)
    monkeypatch.delenv("database_url", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        PlatformSettings()

    # Must fail because database_url is required
    errors = exc_info.value.errors()
    assert any("database_url" in str(e["loc"]) or "PLATFORM_DATABASE_URL" in str(e["loc"]) for e in errors)


def test_platform_settings_rejects_sqlite():
    """PlatformSettings must refuse SQLite URLs; no local fallback is permitted."""
    with pytest.raises(ValidationError) as exc_info:
        PlatformSettings(
            database_url="sqlite+aiosqlite:///platform.db",
            **TEST_KEYS,
        )
    assert "SQLite is not supported" in str(exc_info.value)


def test_platform_settings_rejects_same_database_as_local():
    """PlatformSettings must refuse connection if pointing to same DB as local DATABASE_URL."""
    with pytest.raises(ValidationError) as exc_info:
        PlatformSettings(
            database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai",
            local_database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai",
            **TEST_KEYS,
        )
    assert ("same database" in str(exc_info.value)) or ("default local bot database" in str(exc_info.value))


def test_platform_settings_accepts_separate_postgres():
    """PlatformSettings accepts distinct PostgreSQL database."""
    settings = PlatformSettings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai_platform",
        local_database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai",
        **TEST_KEYS,
    )
    assert settings.database_url == "postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai_platform"
    assert settings.schema_version == "platform-v1"


def test_platform_metadata_independent_from_bot_db():
    """Base.metadata must declare platform tables only, completely disjoint from bot.db.Base."""
    import bot.db.models  # noqa: F401 - ensure bot models are loaded onto bot Base
    from bot.db.base import Base as BotBase

    assert Base.metadata is not BotBase.metadata, "platform Base must use an independent MetaData instance"

    platform_tables = set(Base.metadata.tables.keys())
    bot_tables = set(BotBase.metadata.tables.keys())

    assert platform_tables, "platform Base.metadata has no declared tables"
    assert bot_tables, "bot Base.metadata has no declared tables"
    assert platform_tables.isdisjoint(bot_tables), (
        f"Overlapping tables between platform Base and bot Base: {platform_tables & bot_tables}"
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_invalid_owner_role_rejected(fresh_pg: FreshPostgres):
    """Inserting a user with an invalid role or status violates CHECK constraints in PostgreSQL."""
    from sqlalchemy.exc import IntegrityError

    await fresh_pg.initialize()
    invalid_user_id = uuid.uuid4()
    now_utc = datetime.now(timezone.utc)

    # 1. Invalid role ('member' is not in ('user', 'admin'), fits in String(8))
    async with fresh_pg.new_session() as session:
        session.add(
            User(
                id=invalid_user_id,
                email="badrole@example.com",
                email_key="badrole@example.com",
                display_name="Bad Role",
                password_hash="argon2id$dummy",
                role="member",
                status="active",
                email_verified_at=now_utc,
                preferred_language="en",
                timezone="UTC",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
    # 2. Invalid status ('banned' is not in ('active', 'suspended'))
    async with fresh_pg.new_session() as session:
        session.add(
            User(
                id=invalid_user_id,
                email="badstatus@example.com",
                email_key="badstatus@example.com",
                display_name="Bad Status",
                password_hash="argon2id$dummy",
                role="user",
                status="banned",
                email_verified_at=now_utc,
                preferred_language="en",
                timezone="UTC",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_invalid_balance_state_rejected(fresh_pg: FreshPostgres):
    """Inserting or updating wallet account balances violating invariants is rejected in PostgreSQL."""
    from sqlalchemy.exc import IntegrityError

    await fresh_pg.initialize()
    valid_user_id = uuid.uuid4()
    now_utc = datetime.now(timezone.utc)

    # Create valid user first (foreign key required for wallet_accounts)
    async with fresh_pg.new_session() as session:
        session.add(
            User(
                id=valid_user_id,
                email="wallet_test@example.com",
                email_key="wallet_test@example.com",
                display_name="Wallet Test",
                password_hash="argon2id$dummy",
                role="user",
                status="active",
                email_verified_at=now_utc,
                preferred_language="en",
                timezone="UTC",
            )
        )
        await session.commit()

    # 1. Invalid balance: reserved_minor > posted_minor (e.g. posted=100, reserved=200)
    async with fresh_pg.new_session() as session:
        session.add(
            WalletAccount(
                user_id=valid_user_id,
                currency="INR",
                posted_minor=100,
                reserved_minor=200,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    # 2. Invalid balance: negative reserved_minor (e.g. posted=100, reserved=-10)
    async with fresh_pg.new_session() as session:
        session.add(
            WalletAccount(
                user_id=valid_user_id,
                currency="INR",
                posted_minor=100,
                reserved_minor=-10,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_lifecycle_contract(fresh_pg: FreshPostgres):
    """Verify schema initialization lifecycle: fresh -> repeat no-op -> mismatch refusal.

    Uses the isolated disposable PostgreSQL database from fresh_pg fixture.
    """
    metadata_hash = compute_metadata_hash(Base.metadata)

    # 1. Fresh schema initialization using production initializer
    await initialize_schema(
        fresh_pg.engine,
        expected_version="platform-v1",
        metadata_hash=metadata_hash,
    )

    # 2. Verify readiness check passes
    ready = await check_schema_readiness(
        fresh_pg.engine,
        expected_version="platform-v1",
        metadata_hash=metadata_hash,
    )
    assert ready is True

    # 3. Repeat initialization must be a safe no-op
    await initialize_schema(
        fresh_pg.engine,
        expected_version="platform-v1",
        metadata_hash=metadata_hash,
    )

    # 4. Mismatched version refusal
    with pytest.raises(SchemaMismatch):
        await initialize_schema(
            fresh_pg.engine,
            expected_version="platform-v2",
            metadata_hash=metadata_hash,
        )

    # 5. Mismatched hash refusal
    with pytest.raises(SchemaMismatch):
        await initialize_schema(
            fresh_pg.engine,
            expected_version="platform-v1",
            metadata_hash="deadbeef" * 8,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_concurrent_initialization(fresh_pg: FreshPostgres):
    """Concurrent initializations on independent connections serialize via PostgreSQL advisory lock."""
    import asyncio

    # Create an independent connection engine to the same isolated test database
    engine2 = create_async_engine(fresh_pg.url)
    metadata_hash = compute_metadata_hash(Base.metadata)

    try:
        # Run concurrent schema initializations
        results = await asyncio.gather(
            initialize_schema(
                fresh_pg.engine,
                expected_version="platform-v1",
                metadata_hash=metadata_hash,
            ),
            initialize_schema(
                engine2,
                expected_version="platform-v1",
                metadata_hash=metadata_hash,
            ),
            return_exceptions=True,
        )

        # Neither concurrent initializer should raise an exception
        for r in results:
            assert not isinstance(r, Exception), f"Concurrent initialization raised: {r}"

        # Verify schema readiness is True
        ready = await check_schema_readiness(fresh_pg.engine)
        assert ready is True
    finally:
        await engine2.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_mismatch_refusal(fresh_pg: FreshPostgres):
    """Detect and reject missing required tables, extra unexpected tables, and unexpected views."""
    metadata_hash = compute_metadata_hash(Base.metadata)

    # Initialize clean schema
    await initialize_schema(fresh_pg.engine, metadata_hash=metadata_hash)
    assert await check_schema_readiness(fresh_pg.engine) is True

    # 1. Missing required table: drop wallet_accounts
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("DROP TABLE wallet_accounts CASCADE"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine, metadata_hash=metadata_hash)

    # 2. Recreate missing table structure so table count is restored, then add extra table
    async with fresh_pg.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE TABLE unexpected_extra_table (id INT PRIMARY KEY)"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine, metadata_hash=metadata_hash)

    # 3. Drop extra table, add unexpected view
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("DROP TABLE unexpected_extra_table"))
        await conn.execute(text("CREATE VIEW unexpected_view AS SELECT 1 AS col"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine, metadata_hash=metadata_hash)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_rejects_unknown_nonempty_database(fresh_pg: FreshPostgres):
    """Refuse to initialize an unknown non-empty database that lacks schema_info."""
    # Create an arbitrary non-platform table before initialization
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("CREATE TABLE foreign_untracked (id INT PRIMARY KEY, name TEXT)"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_rejects_standalone_sequence(fresh_pg: FreshPostgres):
    """Refuse to initialize an empty database that contains a pre-existing standalone sequence."""
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("CREATE SEQUENCE standalone_test_seq"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_rejects_custom_type(fresh_pg: FreshPostgres):
    """Refuse to initialize an empty database that contains a pre-existing custom type/enum."""
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("CREATE TYPE custom_test_enum AS ENUM ('first', 'second')"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_rejects_custom_user_schema(fresh_pg: FreshPostgres):
    """Refuse to initialize a database that contains an unexpected user-created schema."""
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA custom_user_schema"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine)

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_platform_initialization_leaves_legacy_db_untouched(fresh_pg: FreshPostgres):
    """Verify platform schema initialization does not modify or contaminate the legacy database."""
    from sqlalchemy import inspect

    legacy_url = os.environ.get("DATABASE_URL") or os.environ.get("LOCAL_DATABASE_URL")
    legacy_engine = None
    legacy_tables_before: set[str] = set()

    if legacy_url and "sqlite" not in legacy_url:
        try:
            legacy_engine = create_async_engine(legacy_url)
            def _get_tables(conn):
                return set(inspect(conn).get_table_names())
            async with legacy_engine.connect() as conn:
                legacy_tables_before = await conn.run_sync(_get_tables)
        except Exception:
            legacy_engine = None

    try:
        # Initialize platform schema on isolated fresh_pg
        await initialize_schema(fresh_pg.engine)

        # 1. Assert fresh_pg contains only platform tables, none from legacy bot models
        def _inspect_fresh(conn):
            return set(inspect(conn).get_table_names())
        async with fresh_pg.engine.connect() as conn:
            platform_tables = await conn.run_sync(_inspect_fresh)

        legacy_bot_tables = {"bot_agent_presets", "session_history", "voice_sessions"}
        assert not (platform_tables & legacy_bot_tables), "Legacy tables contaminated platform database"

        # 2. Assert legacy database (if accessible) is completely untouched
        if legacy_engine is not None:
            async with legacy_engine.connect() as conn:
                legacy_tables_after = await conn.run_sync(_get_tables)
            assert legacy_tables_before == legacy_tables_after, "Legacy database tables were modified by platform init"
            assert "schema_info" not in legacy_tables_after, "Platform schema_info leaked into legacy database"
    finally:
        if legacy_engine is not None:
            await legacy_engine.dispose()

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cli_init_and_independent_app_readiness(fresh_pg: FreshPostgres):
    """Verify real CLI init-db command initializes schema, and independent app readiness verifies it."""
    from platform_api.cli import _init_db_command, _check_schema_command
    from platform_api.app import create_app

    settings = PlatformSettings(
        database_url=fresh_pg.url,
        **TEST_KEYS,
    )

    # 1. Execute real CLI init-db command
    exit_code = await _init_db_command(settings)
    assert exit_code == 0

    # 2. Real CLI check-schema command verifies the initialized schema
    check_code = await _check_schema_command(settings)
    assert check_code == 0

    # 3. Independent app engine checks readiness and matches exactly
    app_engine = create_async_engine(settings.database_url)
    try:
        ready = await check_schema_readiness(
            app_engine,
            expected_version=settings.schema_version,
        )
        assert ready is True
    finally:
        await app_engine.dispose()
