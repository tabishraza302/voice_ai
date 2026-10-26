import logging
import asyncio
import weakref
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from bot.db.base import Base
from bot.config import Settings
import bot.db.models  # noqa: F401 - ensure all tables are registered with Base.metadata
logger = logging.getLogger(__name__)

_database_settings = Settings()
POSTGRES_URL = _database_settings.database_url
SQLITE_FALLBACK_URL = _database_settings.sqlite_database_url

# Global engine and sessionmaker holders
_engine = None
_session_factory = None
_is_sqlite = False
_engine_url: str | None = None
_requested_url: str | None = None
_init_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _init_lock_for_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _init_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _init_locks[loop] = lock
    return lock


def _create_local_indexes(connection):
    """Idempotently add constraints introduced after local tables were created."""
    indexes = (
        Base.metadata.tables["agents"].indexes,
        Base.metadata.tables["prompt_revisions"].indexes,
    )
    for group in indexes:
        for index in group:
            if index.name in {"uq_agents_single_active", "uq_prompt_revision_version"}:
                index.create(connection, checkfirst=True)


async def init_engine(url: str | None = None):
    global _engine, _session_factory, _is_sqlite, _engine_url, _requested_url
    requested_url = url or POSTGRES_URL
    target_url = requested_url
    allow_fallback = url is None and target_url == POSTGRES_URL
    async with _init_lock_for_loop():
        if _engine is not None and _session_factory is not None and _requested_url == requested_url:
            return _engine

        engine = create_async_engine(target_url, echo=False)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            await engine.dispose()
            if not allow_fallback:
                logger.error("Database initialization failed for explicitly configured storage (%s)", type(exc).__name__)
                raise
            logger.warning("Configured database unavailable (%s); falling back to local SQLite.", type(exc).__name__)
            target_url = SQLITE_FALLBACK_URL
            engine = create_async_engine(target_url, echo=False)
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            except Exception:
                await engine.dispose()
                raise

        # Schema errors (including legacy duplicate rows blocking a new
        # uniqueness index) must surface; they are not a database outage.
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.run_sync(_create_local_indexes)
        except Exception as exc:
            await engine.dispose()
            logger.error("Database schema initialization failed (%s)", type(exc).__name__)
            raise

        previous = _engine
        _engine = engine
        _engine_url = target_url
        _requested_url = requested_url
        _is_sqlite = "sqlite" in target_url
        _session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        if previous is not None and previous is not engine:
            await previous.dispose()
        logger.info("Database initialized (%s)", "SQLite" if _is_sqlite else "PostgreSQL")
        return engine


async def get_engine():
    global _engine
    if _engine is None:
        await init_engine()
    return _engine


async def get_session_factory():
    global _session_factory
    if _session_factory is None:
        await init_engine()
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = await get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()

# Alias for FastAPI Depends and initialization
get_db_session = get_db
init_db_schema = init_engine
