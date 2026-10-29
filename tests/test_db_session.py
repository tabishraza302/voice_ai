import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from bot.db import session as db_session


@pytest.mark.asyncio
async def test_concurrent_initialization_reuses_one_engine(monkeypatch):
    previous_engine = db_session._engine
    previous_factory = db_session._session_factory
    previous_url = getattr(db_session, "_engine_url", None)
    previous_requested_url = getattr(db_session, "_requested_url", None)
    previous_fallback = getattr(db_session, "_fallback_active", False)
    db_session._engine = None
    db_session._session_factory = None
    create_engine = db_session.create_async_engine
    created = []

    def count_engine_creations(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        created.append(engine)
        return engine

    monkeypatch.setattr(db_session, "create_async_engine", count_engine_creations)
    try:
        engines = await asyncio.gather(
            db_session.init_engine("sqlite+aiosqlite:///:memory:"),
            db_session.init_engine("sqlite+aiosqlite:///:memory:"),
        )
        assert engines[0] is engines[1]
        assert len(created) == 1
        assert db_session._session_factory is not None
    finally:
        if db_session._engine is not None:
            await db_session._engine.dispose()
        db_session._engine = previous_engine
        db_session._session_factory = previous_factory
        db_session._engine_url = previous_url
        db_session._requested_url = previous_requested_url
        db_session._fallback_active = previous_fallback


@pytest.mark.asyncio
async def test_schema_upgrade_preserves_existing_rows_and_adds_constraints(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'existing.db'}"
    legacy_engine = db_session.create_async_engine(database_url)
    async with legacy_engine.begin() as connection:
        await connection.execute(text(
            "CREATE TABLE agents (id VARCHAR(36) PRIMARY KEY, slug VARCHAR(64) NOT NULL, is_active BOOLEAN NOT NULL)"
        ))
        await connection.execute(text(
            "CREATE TABLE prompt_revisions (id VARCHAR(36) PRIMARY KEY, base_prompt_id VARCHAR(36) NOT NULL, version INTEGER NOT NULL)"
        ))
        await connection.execute(text(
            "INSERT INTO agents (id, slug, is_active) VALUES ('agent-1', 'kept-agent', 1)"
        ))
        await connection.execute(text(
            "INSERT INTO prompt_revisions (id, base_prompt_id, version) VALUES ('revision-1', 'base-1', 1)"
        ))
    await legacy_engine.dispose()

    old_engine = db_session._engine
    old_factory = db_session._session_factory
    old_engine_url = db_session._engine_url
    old_requested_url = db_session._requested_url
    old_fallback = db_session._fallback_active
    db_session._engine = None
    db_session._session_factory = None
    db_session._engine_url = None
    db_session._requested_url = None
    db_session._fallback_active = False
    try:
        upgraded = await db_session.init_engine(database_url)
        async with upgraded.connect() as connection:
            assert (await connection.execute(text("SELECT slug FROM agents WHERE id='agent-1'"))).scalar_one() == "kept-agent"
            assert (await connection.execute(text("SELECT version FROM prompt_revisions WHERE id='revision-1'"))).scalar_one() == 1

        async with upgraded.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(text(
                    "INSERT INTO agents (id, slug, is_active) VALUES ('agent-2', 'active-two', 1)"
                ))
        async with upgraded.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(text(
                    "INSERT INTO prompt_revisions (id, base_prompt_id, version) VALUES ('revision-2', 'base-1', 1)"
                ))
    finally:
        if db_session._engine is not None:
            await db_session._engine.dispose()
        db_session._engine = old_engine
        db_session._session_factory = old_factory
        db_session._engine_url = old_engine_url
        db_session._requested_url = old_requested_url
        db_session._fallback_active = old_fallback


def _reset_engine_state(monkeypatch):
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_session_factory", None)
    monkeypatch.setattr(db_session, "_engine_url", None)
    monkeypatch.setattr(db_session, "_requested_url", None)
    monkeypatch.setattr(db_session, "_is_sqlite", False)
    monkeypatch.setattr(db_session, "_fallback_active", False)
    monkeypatch.setattr(
        db_session,
        "POSTGRES_URL",
        "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:5432/synthetic",
    )

def _failing_postgres_engine(real_create_engine, attempts):
    class FailingConnection:
        async def __aenter__(self):
            attempts.append("postgres")
            raise ConnectionRefusedError("Simulated PostgreSQL connection failure")

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class FailingEngine:
        def connect(self):
            return FailingConnection()

        async def dispose(self):
            pass

    def create(target_url, *args, **kwargs):
        if "postgresql" in str(target_url):
            return FailingEngine()
        return real_create_engine(target_url, *args, **kwargs)

    return create


@pytest.mark.asyncio
async def test_postgres_failure_fails_closed_by_default(monkeypatch, tmp_path):
    _reset_engine_state(monkeypatch)
    monkeypatch.setattr(
        db_session,
        "POSTGRES_URL",
        "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:5432/synthetic",
    )
    monkeypatch.setattr(db_session, "ALLOW_SQLITE_FALLBACK", False)
    monkeypatch.setattr(db_session, "SQLITE_FALLBACK_URL", f"sqlite+aiosqlite:///{tmp_path / 'never.db'}")
    attempts = []
    monkeypatch.setattr(
        db_session,
        "create_async_engine",
        _failing_postgres_engine(db_session.create_async_engine, attempts),
    )

    with pytest.raises(ConnectionRefusedError):
        await db_session.init_engine()

    assert attempts == ["postgres"]
    assert db_session._engine is None
    assert db_session._session_factory is None
    assert db_session.storage_state() == {"backend": "uninitialized", "fallback": False}


@pytest.mark.asyncio
async def test_init_engine_falls_back_to_sqlite_when_postgres_fails(monkeypatch, tmp_path):
    _reset_engine_state(monkeypatch)
    sqlite_path = tmp_path / "fallback.db"
    fallback_url = f"sqlite+aiosqlite:///{sqlite_path}"

    attempts = []
    monkeypatch.setattr(db_session, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(db_session, "SQLITE_FALLBACK_URL", fallback_url)
    monkeypatch.setattr(
        db_session,
        "create_async_engine",
        _failing_postgres_engine(db_session.create_async_engine, attempts),
    )

    try:
        engine = await db_session.init_engine()
        assert engine is not None
        assert db_session.storage_state() == {"backend": "sqlite", "fallback": True}
        # The fallback engine is recorded as the store actually opened, not
        # as if PostgreSQL had answered.
        assert db_session._requested_url == fallback_url

        # Verify schema tables were created on the real SQLite fallback database
        async with engine.connect() as conn:
            tables = (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                )
            ).scalars().all()
            assert "agents" in tables
            assert "base_prompts" in tables
            assert "prompt_revisions" in tables

        # Verify actual data write and read using session factory and ORM model
        from bot.db.models.agent import Agent
        from sqlalchemy import select

        factory = await db_session.get_session_factory()
        async with factory() as session:
            async with session.begin():
                agent = Agent(
                    slug="fallback-agent",
                    name="Fallback Agent",
                )
                session.add(agent)

        async with factory() as session:
            result = (
                await session.execute(
                    select(Agent).where(Agent.slug == "fallback-agent")
                )
            ).scalar_one()
            assert result.name == "Fallback Agent"
            assert result.slug == "fallback-agent"
            assert result.llm_model == "qwen3.5:4b"
        # Verify the file was written to disk
        assert sqlite_path.exists()
        assert sqlite_path.stat().st_size > 0
    finally:
        if db_session._engine is not None:
            await db_session._engine.dispose()


@pytest.mark.asyncio
async def test_repeated_init_retries_postgres_instead_of_reusing_fallback(monkeypatch, tmp_path):
    _reset_engine_state(monkeypatch)
    monkeypatch.setattr(db_session, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(db_session, "SQLITE_FALLBACK_URL", f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}")
    attempts = []
    monkeypatch.setattr(
        db_session,
        "create_async_engine",
        _failing_postgres_engine(db_session.create_async_engine, attempts),
    )

    try:
        await db_session.init_engine()
        assert db_session.storage_state()["fallback"] is True
        assert attempts == ["postgres"]

        # A second default init must not return the fallback as if it were
        # the configured database: it retries PostgreSQL.
        await db_session.init_engine()
        assert attempts == ["postgres", "postgres"]
        assert db_session.storage_state() == {"backend": "sqlite", "fallback": True}
    finally:
        if db_session._engine is not None:
            await db_session._engine.dispose()

@pytest.mark.asyncio
async def test_get_session_factory_retries_postgres_when_fallback_active(monkeypatch, tmp_path):
    _reset_engine_state(monkeypatch)
    monkeypatch.setattr(db_session, "ALLOW_SQLITE_FALLBACK", True)
    fallback_url = f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}"
    monkeypatch.setattr(db_session, "SQLITE_FALLBACK_URL", fallback_url)
    attempts = []
    monkeypatch.setattr(
        db_session,
        "create_async_engine",
        _failing_postgres_engine(db_session.create_async_engine, attempts),
    )

    try:
        # 1. First initialization fails PostgreSQL, falls back to SQLite
        factory1 = await db_session.get_session_factory()
        assert factory1 is not None
        assert db_session.storage_state() == {"backend": "sqlite", "fallback": True}
        assert attempts == ["postgres"]

        # 2. Simulate PostgreSQL recovery by restoring real create_async_engine with an in-memory SQLite store pretending to be postgres URL
        postgres_recovered = []
        real_create_engine = db_session.create_async_engine

        class RecoveredConnection:
            async def __aenter__(self):
                postgres_recovered.append("recovered")
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def execute(self, *args, **kwargs):
                class Result:
                    def scalar_one_or_none(self):
                        return 1
                    def scalars(self):
                        class Scalars:
                            def all(self):
                                return []
                        return Scalars()
                return Result()

            async def run_sync(self, fn, *args, **kwargs):
                pass

        class RecoveredEngine:
            def connect(self):
                return RecoveredConnection()

            def begin(self):
                return RecoveredConnection()

            async def dispose(self):
                pass

        def create_recovered(target_url, *args, **kwargs):
            if "postgresql" in str(target_url):
                return RecoveredEngine()
            return real_create_engine(target_url, *args, **kwargs)

        monkeypatch.setattr(db_session, "create_async_engine", create_recovered)

        # 3. get_session_factory() detects fallback is active and retries init_engine()
        factory2 = await db_session.get_session_factory()
        assert "recovered" in postgres_recovered
        assert db_session.storage_state() == {"backend": "postgres", "fallback": False}
    finally:
        if db_session._engine is not None:
            await db_session._engine.dispose()


@pytest.mark.asyncio
async def test_storage_state_reports_uninitialized_before_engine_init(monkeypatch):
    _reset_engine_state(monkeypatch)
    assert db_session.storage_state() == {"backend": "uninitialized", "fallback": False}


@pytest.mark.asyncio
async def test_repeated_failed_retries_preserve_memory_sqlite_data(monkeypatch):
    _reset_engine_state(monkeypatch)
    monkeypatch.setattr(
        db_session,
        "POSTGRES_URL",
        "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:5432/synthetic",
    )
    monkeypatch.setattr(db_session, "ALLOW_SQLITE_FALLBACK", True)
    monkeypatch.setattr(db_session, "SQLITE_FALLBACK_URL", "sqlite+aiosqlite:///:memory:")
    attempts = []
    monkeypatch.setattr(
        db_session,
        "create_async_engine",
        _failing_postgres_engine(db_session.create_async_engine, attempts),
    )

    try:
        # Initial call fails PostgreSQL, falls back to in-memory SQLite
        engine = await db_session.init_engine()
        assert engine is not None
        assert db_session.storage_state() == {"backend": "sqlite", "fallback": True}

        from bot.db.models.agent import Agent
        from sqlalchemy import select

        # Insert data into the active in-memory SQLite fallback
        factory = await db_session.get_session_factory()
        async with factory() as session:
            async with session.begin():
                session.add(Agent(slug="preserved-memory-agent", name="Preserved Memory Agent"))

        # Verify data is readable before retry
        async with factory() as session:
            result = (
                await session.execute(
                    select(Agent).where(Agent.slug == "preserved-memory-agent")
                )
            ).scalar_one()
            assert result.name == "Preserved Memory Agent"

        # Retries via init_engine: fails PostgreSQL again, must keep existing engine
        engine_after_retry = await db_session.init_engine()
        assert engine_after_retry is engine
        assert db_session.storage_state() == {"backend": "sqlite", "fallback": True}

        # Retries via get_session_factory: fails PostgreSQL again, must keep existing fallback factory
        factory_after_retry = await db_session.get_session_factory()
        assert db_session.storage_state() == {"backend": "sqlite", "fallback": True}

        # Crucial check: verify in-memory SQLite consumer data was retained across failed retries
        async with factory_after_retry() as session:
            result = (
                await session.execute(
                    select(Agent).where(Agent.slug == "preserved-memory-agent")
                )
            ).scalar_one()
            assert result.name == "Preserved Memory Agent"
            assert result.slug == "preserved-memory-agent"
    finally:
        if db_session._engine is not None:
            await db_session._engine.dispose()

@pytest.mark.asyncio
async def test_configured_primary_sqlite_equal_to_fallback_url_not_flagged_fallback(monkeypatch, tmp_path):
    _reset_engine_state(monkeypatch)
    sqlite_url = f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}"
    monkeypatch.setattr(db_session, "POSTGRES_URL", sqlite_url)
    monkeypatch.setattr(db_session, "SQLITE_FALLBACK_URL", sqlite_url)
    monkeypatch.setattr(db_session, "ALLOW_SQLITE_FALLBACK", True)

    try:
        engine = await db_session.init_engine()
        assert engine is not None
        assert db_session.storage_state() == {"backend": "sqlite", "fallback": False}
        assert db_session._fallback_active is False
        assert db_session._requested_url == sqlite_url

        from bot.db.models.agent import Agent
        from sqlalchemy import select

        factory = await db_session.get_session_factory()
        async with factory() as session:
            async with session.begin():
                session.add(Agent(slug="primary-agent", name="Primary Agent"))

        # Subsequent initialization calls must reuse the primary engine without flagging fallback
        engine2 = await db_session.init_engine()
        assert engine2 is engine
        assert db_session.storage_state() == {"backend": "sqlite", "fallback": False}

        factory2 = await db_session.get_session_factory()
        async with factory2() as session:
            result = (
                await session.execute(
                    select(Agent).where(Agent.slug == "primary-agent")
                )
            ).scalar_one()
            assert result.name == "Primary Agent"
            assert result.slug == "primary-agent"
    finally:
        if db_session._engine is not None:
            await db_session._engine.dispose()


@pytest.mark.asyncio
async def test_configured_primary_sqlite_not_flagged_fallback(monkeypatch, tmp_path):
    _reset_engine_state(monkeypatch)
    primary_url = f"sqlite+aiosqlite:///{tmp_path / 'primary_custom.db'}"
    fallback_url = f"sqlite+aiosqlite:///{tmp_path / 'fallback_custom.db'}"
    monkeypatch.setattr(db_session, "POSTGRES_URL", primary_url)
    monkeypatch.setattr(db_session, "SQLITE_FALLBACK_URL", fallback_url)
    monkeypatch.setattr(db_session, "ALLOW_SQLITE_FALLBACK", True)

    try:
        engine = await db_session.init_engine()
        assert engine is not None
        assert db_session.storage_state() == {"backend": "sqlite", "fallback": False}
        assert db_session._fallback_active is False
        assert db_session._engine_url == primary_url
    finally:
        if db_session._engine is not None:
            await db_session._engine.dispose()
