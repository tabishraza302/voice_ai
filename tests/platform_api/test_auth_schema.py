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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("database_url", raising=False)
    monkeypatch.delenv("PLATFORM_DATABASE_URL", raising=False)
    monkeypatch.delenv("PLATFORM_AUTH_CSRF_KEY", raising=False)
    monkeypatch.delenv("PLATFORM_AUTH_RATE_KEY", raising=False)
    monkeypatch.delenv("PLATFORM_AUTH_MFA_KEY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        PlatformSettings()

    # Must fail because database_url and auth keys are required
    errors = exc_info.value.errors()
    assert any("database_url" in str(e["loc"]).lower() for e in errors)
    assert any("csrf" in str(e["loc"]).lower() or "rate" in str(e["loc"]).lower() for e in errors)

def test_platform_settings_rejects_sqlite():
    """PlatformSettings must refuse SQLite URLs; no local fallback is permitted."""
    with pytest.raises(ValidationError) as exc_info:
        PlatformSettings(
            database_url="sqlite+aiosqlite:///platform.db",
            **TEST_KEYS,
        )
    assert "SQLite is not supported" in str(exc_info.value)


def test_platform_settings_accepts_shared_database_with_schema_separation():
    """PlatformSettings must accept shared DB with schema separation."""
    settings = PlatformSettings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai",
        **TEST_KEYS,
    )
    assert settings.database_url == "postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai"
    assert settings.postgres_schema == "platform"


def test_platform_settings_rejects_public_schema():
    """PlatformSettings must refuse 'public' schema to prevent collision with local tables."""
    with pytest.raises(ValidationError) as exc_info:
        PlatformSettings(
            database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai",
            postgres_schema="public",
            **TEST_KEYS,
        )
    assert "Platform schema cannot be 'public'" in str(exc_info.value)


def test_platform_settings_rejects_phoenix_schema():
    """PlatformSettings must refuse 'phoenix' schema to prevent collision with telemetry tables."""
    with pytest.raises(ValidationError) as exc_info:
        PlatformSettings(
            database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai",
            postgres_schema="phoenix",
            **TEST_KEYS,
        )
    assert "Platform schema cannot be 'phoenix'" in str(exc_info.value)


def test_platform_settings_requires_explicit_database_url(monkeypatch):
    """PlatformSettings requires explicit shared DATABASE_URL in environment with no silent default."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("database_url", raising=False)
    monkeypatch.delenv("PLATFORM_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        PlatformSettings(**TEST_KEYS)

    errors = exc_info.value.errors()
    assert any("database_url" in str(e["loc"]).lower() for e in errors)

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
    """Detect and reject missing required tables, extra unexpected tables, and unexpected views within platform schema."""
    metadata_hash = compute_metadata_hash(Base.metadata)

    # Initialize clean schema
    await initialize_schema(fresh_pg.engine, metadata_hash=metadata_hash)
    assert await check_schema_readiness(fresh_pg.engine) is True

    # 1. Missing required table: drop platform.wallet_accounts
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("DROP TABLE platform.wallet_accounts CASCADE"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine, metadata_hash=metadata_hash)

    # 2. Recreate missing table structure so table count is restored, then add extra table in platform schema
    async with fresh_pg.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE TABLE platform.unexpected_extra_table (id INT PRIMARY KEY)"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine, metadata_hash=metadata_hash)

    # 3. Drop extra table, add unexpected view in platform schema
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("DROP TABLE platform.unexpected_extra_table"))
        await conn.execute(text("CREATE VIEW platform.unexpected_view AS SELECT 1 AS col"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine, metadata_hash=metadata_hash)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_rejects_unknown_nonempty_database(fresh_pg: FreshPostgres):
    """Refuse to initialize an unknown non-empty platform schema that lacks schema_info."""
    # Create an arbitrary non-platform table inside the platform schema before initialization
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        await conn.execute(text("CREATE TABLE platform.foreign_untracked (id INT PRIMARY KEY, name TEXT)"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_rejects_standalone_sequence(fresh_pg: FreshPostgres):
    """Refuse to initialize when platform schema contains a pre-existing standalone sequence."""
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        await conn.execute(text("CREATE SEQUENCE platform.standalone_test_seq"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_rejects_custom_type(fresh_pg: FreshPostgres):
    """Refuse to initialize when platform schema contains a pre-existing custom type/enum."""
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        await conn.execute(text("CREATE TYPE platform.custom_test_enum AS ENUM ('first', 'second')"))

    assert await check_schema_readiness(fresh_pg.engine) is False
    with pytest.raises(SchemaMismatch):
        await initialize_schema(fresh_pg.engine)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_schema_tolerates_local_and_phoenix_schemas(fresh_pg: FreshPostgres):
    """Platform initialization tolerates legitimate local tables in public and external schemas like phoenix."""
    # Create legitimate local bot tables in public schema and an external Phoenix schema
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS phoenix"))
        await conn.execute(text("CREATE TABLE phoenix.spans (id TEXT PRIMARY KEY, span_name TEXT)"))
        await conn.execute(text("CREATE TABLE public.local_agents (id TEXT PRIMARY KEY, name TEXT)"))
        await conn.execute(text("CREATE TABLE public.voice_sessions (id TEXT PRIMARY KEY)"))
        await conn.execute(text("CREATE VIEW public.local_sessions_view AS SELECT id FROM public.voice_sessions"))

    # Platform schema initialization must succeed despite non-empty public and phoenix schemas
    await initialize_schema(fresh_pg.engine)
    assert await check_schema_readiness(fresh_pg.engine) is True

    # Verify public and phoenix tables/views remain completely intact and unaffected
    async with fresh_pg.engine.connect() as conn:
        local_tables = await conn.execute(
            text("SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'")
        )
        assert {"local_agents", "voice_sessions"} <= {r[0] for r in local_tables.all()}

        phoenix_tables = await conn.execute(
            text("SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'phoenix' AND c.relkind = 'r'")
        )
        assert {"spans"} <= {r[0] for r in phoenix_tables.all()}

        # Verify platform tables only exist in platform schema, not leaked into public
        public_table_names = {
            r[0]
            for r in (
                await conn.execute(
                    text("SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'")
                )
            ).all()
        }
        assert "schema_info" not in public_table_names
        assert "users" not in public_table_names
        assert "wallet_accounts" not in public_table_names


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_platform_initialization_leaves_legacy_db_untouched(fresh_pg: FreshPostgres):
    """Verify platform schema initialization does not modify or contaminate local/legacy tables."""
    from sqlalchemy import inspect

    # Pre-populate public schema with local bot tables
    async with fresh_pg.engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS public.bot_agent_presets (id TEXT PRIMARY KEY)"))
        await conn.execute(text("CREATE TABLE IF NOT EXISTS public.session_history (id TEXT PRIMARY KEY)"))

    def _get_public_tables(conn):
        return set(inspect(conn).get_table_names(schema="public"))

    async with fresh_pg.engine.connect() as conn:
        public_before = await conn.run_sync(_get_public_tables)

    # Initialize platform schema on the same database
    await initialize_schema(fresh_pg.engine)

    async with fresh_pg.engine.connect() as conn:
        public_after = await conn.run_sync(_get_public_tables)

        def _get_platform_tables(c):
            return set(inspect(c).get_table_names(schema="platform"))

        platform_tables = await conn.run_sync(_get_platform_tables)

    assert public_before == public_after, "Local public tables were modified by platform initialization"
    assert "schema_info" not in public_after, "Platform schema_info leaked into public schema"
    assert "users" not in public_after, "Platform users table leaked into public schema"
    assert "schema_info" in platform_tables, "Platform schema_info missing from platform schema"
    assert "users" in platform_tables, "Platform users table missing from platform schema"
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

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_nondefault_platform_schema_initializes_and_checks_readiness(fresh_pg: FreshPostgres):
    """The configured platform schema is created and verified without touching the default schema."""
    schema = "platform_custom"

    await fresh_pg.initialize(schema=schema)
    assert await check_schema_readiness(fresh_pg.engine, schema=schema) is True

    async with fresh_pg.engine.connect() as conn:
        assert await conn.scalar(
            text("SELECT to_regclass(:qualified_name) IS NOT NULL"),
            {"qualified_name": f"{schema}.schema_info"},
        ) is True
        assert await conn.scalar(
            text("SELECT to_regclass('platform.schema_info') IS NOT NULL")
        ) is False
