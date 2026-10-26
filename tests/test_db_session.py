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
    db_session._engine = None
    db_session._session_factory = None
    db_session._engine_url = None
    db_session._requested_url = None
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
