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
# Off unless the deployment explicitly opts in. A configured PostgreSQL
# failure must fail closed: writing agent edits to SQLite while PostgreSQL
# is down makes them invisible when PostgreSQL returns.
ALLOW_SQLITE_FALLBACK = _database_settings.allow_sqlite_fallback

# Global engine and sessionmaker holders
_engine = None
_session_factory = None
_is_sqlite = False
_engine_url: str | None = None
_requested_url: str | None = None
_fallback_active = False
_init_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class StorageUnavailableError(RuntimeError):
    """A requested durable storage capability is not available.

    Raised when a caller asks for a saved agent profile while the process
    is serving from the explicit SQLite fallback. The profile may exist in
    PostgreSQL, so answering from the fallback would be a lie.
    """


def storage_state() -> dict[str, object]:
    """The backend this process is actually serving from.

    Deliberately carries no URL, host, user or credential: this is served
    by the unauthenticated local API.
    """
    if _engine is None:
        backend = "uninitialized"
    elif _is_sqlite:
        backend = "sqlite"
    else:
        backend = "postgres"
    return {
        "backend": backend,
        "fallback": _fallback_active,
    }


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
    global _engine, _session_factory, _is_sqlite, _engine_url, _requested_url, _fallback_active
    requested_url = url or POSTGRES_URL
    target_url = requested_url
    # Only the implicit default URL may fall back, and only when the
    # deployment opted in and the primary store is not already SQLite.
    # An explicit URL always means exactly that URL.
    allow_fallback = (
        url is None
        and target_url == POSTGRES_URL
        and not target_url.startswith("sqlite")
        and target_url != SQLITE_FALLBACK_URL
        and ALLOW_SQLITE_FALLBACK
    )
    async with _init_lock_for_loop():
        # A fallback engine is never reused as if it satisfied a request for
        # PostgreSQL: _requested_url records the store actually opened, so
        # the next init_engine() retries the configured database. An explicit
        # SQLite request also replaces a fallback engine and clears the flag.
        if (
            _engine is not None
            and _session_factory is not None
            and _requested_url == requested_url
            and not _fallback_active
        ):
            return _engine

        engine = create_async_engine(target_url, echo=False)
        fell_back = False
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            await engine.dispose()
            if not allow_fallback:
                logger.error(
                    "Database initialization failed for configured storage (%s); "
                    "refusing to switch stores. Set ALLOW_SQLITE_FALLBACK=1 only "
                    "if a local-only SQLite store is acceptable.",
                    type(exc).__name__,
                )
                raise
            if _fallback_active and _engine is not None and _engine_url == SQLITE_FALLBACK_URL:
                logger.warning(
                    "Configured database still unavailable (%s); ALLOW_SQLITE_FALLBACK=1 is set, "
                    "retaining active SQLite fallback. Agent edits made now will not appear "
                    "in PostgreSQL.",
                    type(exc).__name__,
                )
                if _session_factory is None:
                    _session_factory = async_sessionmaker(
                        _engine, expire_on_commit=False, class_=AsyncSession
                    )
                return _engine
            logger.warning(
                "Configured database unavailable (%s); ALLOW_SQLITE_FALLBACK=1 is set, "
                "falling back to local SQLite. Agent edits made now will not appear "
                "in PostgreSQL.",
                type(exc).__name__,
            )
            target_url = SQLITE_FALLBACK_URL
            engine = create_async_engine(target_url, echo=False)
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            except Exception:
                await engine.dispose()
                raise
            fell_back = True
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
        # Record the store actually opened. If this is the fallback, a later
        # default init_engine() sees SQLite here and retries PostgreSQL.
        _requested_url = target_url
        _is_sqlite = "sqlite" in target_url
        _fallback_active = fell_back
        _session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        if previous is not None and previous is not engine:
            await previous.dispose()
        logger.info(
            "Database initialized (%s%s)",
            "SQLite" if _is_sqlite else "PostgreSQL",
            ", fallback" if _fallback_active else "",
        )
        return engine


async def get_engine():
    global _engine
    if _engine is None or _fallback_active:
        await init_engine()
    return _engine


async def get_session_factory():
    global _session_factory
    if _session_factory is None or _fallback_active:
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
