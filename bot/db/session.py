"""PostgreSQL session management for agent configuration storage.

There is exactly one runtime store: the async PostgreSQL database named by
``DATABASE_URL``. There is no local SQLite mode and no fallback URL, so a
failure to reach PostgreSQL is a failure of the process that needed it —
never a silent switch to a different store whose writes would disappear
when the real database came back.

Schema creation is idempotent (``create_all`` plus the indexes declared
after a table was first created), and serialization of concurrent writers
is PostgreSQL's own: savepoints for racing inserts and ``FOR UPDATE`` row
locks for the singleton rows that guard activation and prompt versions.
"""

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

# Global engine and sessionmaker holders
_engine = None
_session_factory = None
_engine_url: str | None = None
_init_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class StorageUnavailableError(RuntimeError):
    """PostgreSQL is not reachable.

    Raised where the store is needed rather than swallowed: a caller that
    asked for a saved agent profile must be told the database is down, not
    that the profile does not exist, and a session that asked for nothing
    in particular must not be silently started against another store.
    """


def storage_state() -> dict[str, object]:
    """The backend this process is actually serving from.

    Deliberately carries no URL, host, user or credential: this is served
    by the unauthenticated local API.
    """
    return {
        "backend": "postgres" if _engine is not None else "uninitialized",
    }


def _init_lock_for_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _init_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _init_locks[loop] = lock
    return lock


def _ensure_declared_indexes(connection):
    """Idempotently add constraints introduced after a table was created.

    ``create_all`` only issues ``CREATE TABLE`` for tables that are missing,
    so a database created by an older schema keeps its old shape. These are
    the two partial unique indexes the agent configuration depends on; they
    are created from the same metadata definition in ``bot/db/models``.
    """
    indexes = (
        Base.metadata.tables["agents"].indexes,
        Base.metadata.tables["prompt_revisions"].indexes,
    )
    for group in indexes:
        for index in group:
            if index.name in {"uq_agents_single_active", "uq_prompt_revision_version"}:
                index.create(connection, checkfirst=True)


async def init_engine(url: str | None = None):
    """Open the PostgreSQL engine and create the schema, once per process.

    Every failure propagates. A configured database that cannot be reached
    is a startup failure, not an invitation to serve from somewhere else.
    """
    global _engine, _session_factory, _engine_url
    requested_url = Settings.validate_database_url_is_async_postgres(url or POSTGRES_URL)

    async with _init_lock_for_loop():
        if (
            _engine is not None
            and _session_factory is not None
            and _engine_url == requested_url
        ):
            return _engine

        engine = create_async_engine(requested_url, echo=False)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            await engine.dispose()
            logger.error(
                "PostgreSQL is unreachable at the configured DATABASE_URL (%s)",
                type(exc).__name__,
            )
            raise

        # Schema errors (including legacy duplicate rows blocking a new
        # uniqueness index) must surface; they are not a database outage.
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.run_sync(_ensure_declared_indexes)
        except Exception as exc:
            await engine.dispose()
            logger.error("Database schema initialization failed (%s)", type(exc).__name__)
            raise

        previous = _engine
        _engine = engine
        _engine_url = requested_url
        _session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        if previous is not None and previous is not engine:
            await previous.dispose()
        logger.info("PostgreSQL storage initialized")
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
