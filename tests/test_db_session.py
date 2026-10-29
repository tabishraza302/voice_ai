import asyncio
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from bot.db import session as db_session
from bot.agents.service import AgentService
from tests.support.postgres import disposable_postgres_schema


def _reset_engine_state(monkeypatch):
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_session_factory", None)
    monkeypatch.setattr(db_session, "_engine_url", None)
    monkeypatch.setattr(
        db_session,
        "POSTGRES_URL",
        "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:5432/synthetic",
    )


@pytest.mark.asyncio
async def test_concurrent_initialization_reuses_one_engine(monkeypatch):
    _reset_engine_state(monkeypatch)
    created = []

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, *args, **kwargs):
            pass

        async def run_sync(self, *args, **kwargs):
            pass

    class FakeEngine:
        def connect(self):
            return FakeConn()

        def begin(self):
            return FakeConn()

        async def dispose(self):
            pass

    def count_engine_creations(*args, **kwargs):
        engine = FakeEngine()
        created.append(engine)
        return engine

    monkeypatch.setattr(db_session, "create_async_engine", count_engine_creations)
    try:
        engines = await asyncio.gather(
            db_session.init_engine("postgresql+asyncpg://synthetic:5432/synthetic"),
            db_session.init_engine("postgresql+asyncpg://synthetic:5432/synthetic"),
        )
        assert engines[0] is engines[1]
        assert len(created) == 1
        assert db_session._session_factory is not None
        assert db_session.storage_state() == {"backend": "postgres"}
    finally:
        _reset_engine_state(monkeypatch)


@pytest.mark.asyncio
async def test_schema_upgrade_preserves_existing_rows_and_adds_constraints():
    async with disposable_postgres_schema() as (engine, factory, _schema):
        async with factory() as session:
            service = AgentService(session)
            await service.seed_default_presets()
            agents = await service.list_agents()
            assert len(agents) == 3

        async with engine.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(text(
                    "INSERT INTO agents (id, slug, is_active, created_at, updated_at) "
                    "VALUES ('dup-active', 'dup-active', true, NOW(), NOW())"
                ))


@pytest.mark.asyncio
async def test_postgres_failure_fails_closed(monkeypatch):
    _reset_engine_state(monkeypatch)
    attempts = []

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

    monkeypatch.setattr(
        db_session,
        "create_async_engine",
        lambda *args, **kwargs: FailingEngine(),
    )

    with pytest.raises(ConnectionRefusedError):
        await db_session.init_engine()

    assert attempts == ["postgres"]
    assert db_session._engine is None
    assert db_session._session_factory is None
    assert db_session.storage_state() == {"backend": "uninitialized"}


@pytest.mark.asyncio
async def test_schema_initialization_failure_propagates_and_disposes_engine(monkeypatch):
    _reset_engine_state(monkeypatch)
    disposed = []

    class WorkingConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, *args, **kwargs):
            pass

    class FailingSchemaConnection:
        async def __aenter__(self):
            raise OperationalError("CREATE TABLE", {}, Exception("syntax or permission error"))

        async def __aexit__(self, *args):
            pass

    class TestEngine:
        def connect(self):
            return WorkingConnection()

        def begin(self):
            return FailingSchemaConnection()

        async def dispose(self):
            disposed.append(True)

    monkeypatch.setattr(db_session, "create_async_engine", lambda *args, **kwargs: TestEngine())

    with pytest.raises(OperationalError):
        await db_session.init_engine()

    assert len(disposed) == 1
    assert db_session._engine is None
    assert db_session._session_factory is None
    assert db_session.storage_state() == {"backend": "uninitialized"}


@pytest.mark.asyncio
async def test_storage_state_reports_uninitialized_before_engine_init(monkeypatch):
    _reset_engine_state(monkeypatch)
    assert db_session.storage_state() == {"backend": "uninitialized"}


@pytest.mark.asyncio
async def test_storage_state_reports_postgres_when_initialized(monkeypatch):
    _reset_engine_state(monkeypatch)
    monkeypatch.setattr(db_session, "_engine", object())
    assert db_session.storage_state() == {"backend": "postgres"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_url",
    [
        "sqlite+aiosqlite:///:memory:",
        "sqlite:///voice_ai.db",
        "postgresql://voice_ai:voice_ai@127.0.0.1:5432/voice_ai",
        "mysql://localhost/db",
    ],
)
async def test_init_engine_rejects_non_async_postgres_url(invalid_url):
    with pytest.raises(ValueError, match=r"DATABASE_URL must use postgresql\+asyncpg"):
        await db_session.init_engine(invalid_url)
