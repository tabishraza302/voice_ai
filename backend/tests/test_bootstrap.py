import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")

from app.auth import hasher
from app.bootstrap import bootstrap
from app.db import Base, Session, engine
from app.models import User


@pytest.fixture
async def database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_bootstrap_creates_normalized_admin_and_is_idempotent(database, monkeypatch):
    password = "correct horse battery staple"
    monkeypatch.setenv("ADMIN_EMAIL", "  Voice.Admin@Example.com  ")
    monkeypatch.setenv("ADMIN_PASSWORD", password)

    await bootstrap()
    monkeypatch.setenv("ADMIN_PASSWORD", "different secure password")
    await bootstrap()

    async with Session() as session:
        users = list((await session.scalars(select(User))).all())
    assert len(users) == 1
    assert users[0].email == "voice.admin@example.com"
    assert users[0].role == "admin"
    assert hasher.verify(users[0].password_hash, password)


@pytest.mark.anyio
async def test_bootstrap_never_promotes_existing_user(database, monkeypatch):
    email = "owner@example.com"
    original_hash = hasher.hash("another secure password")
    monkeypatch.setenv("ADMIN_EMAIL", email)
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse battery staple")

    async with Session() as session:
        session.add(User(email=email, password_hash=original_hash, role="user"))
        await session.commit()

    with pytest.raises(RuntimeError, match="refusing promotion"):
        await bootstrap()

    async with Session() as session:
        user = await session.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.role == "user"
    assert user.password_hash == original_hash


@pytest.mark.anyio
async def test_concurrent_bootstraps_create_one_admin(database, monkeypatch):
    email = f"{uuid.uuid4()}@example.com"
    monkeypatch.setenv("ADMIN_EMAIL", email)
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse battery staple")

    await asyncio.gather(bootstrap(), bootstrap())

    async with Session() as session:
        user_count = await session.scalar(select(func.count()).select_from(User))
        user = await session.scalar(select(User).where(User.email == email))
    assert user_count == 1
    assert user is not None
    assert user.role == "admin"


@pytest.mark.anyio
async def test_bootstrap_requires_environment_credentials(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="ADMIN_EMAIL.*ADMIN_PASSWORD"):
        await bootstrap()


@pytest.mark.anyio
async def test_users_table_rejects_unsupported_role(database):
    async with Session() as session:
        session.add(User(email="valid@example.com", password_hash="non-empty", role="owner"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.anyio
async def test_users_table_rejects_non_normalized_email(database):
    async with Session() as session:
        session.add(User(email="Admin@Example.com", password_hash="non-empty", role="user"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
