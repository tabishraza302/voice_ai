import importlib

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth import COOKIE, hasher
from app.db import Base, Session, engine
from app.main import app
from app.models import User


@pytest.fixture
async def database(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("ALLOW_DEV_LOGIN", "true")
    monkeypatch.setenv("APP_ORIGIN", "http://localhost:3000")
    monkeypatch.setenv("ADMIN_EMAIL", "  Admin@Example.com  ")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin correct horse battery staple")
    monkeypatch.setenv("DEV_USER_EMAIL", "  User@Example.com  ")
    monkeypatch.setenv("DEV_USER_PASSWORD", "user correct horse battery staple")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


@pytest.mark.anyio
async def test_seed_creates_both_dev_logins_and_preserves_accounts_on_rerun(database):
    seed = importlib.import_module("app.seed").seed
    await seed()
    async with Session() as db:
        before = {user.email: (user.id, user.password_hash) for user in await db.scalars(select(User))}
    await seed()
    async with Session() as db:
        after = {user.email: (user.id, user.password_hash) for user in await db.scalars(select(User))}
    assert after == before
    assert set(after) == {"admin@example.com", "user@example.com"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000") as client:
        for role, email in (("admin", "admin@example.com"), ("user", "user@example.com")):
            response = await client.post(
                "/auth/dev-login", json={"role": role}, headers={"Origin": "http://localhost:3000"}
            )
            assert response.status_code == 200
            assert response.json()["email"] == email
            assert response.json()["role"] == role
            assert COOKIE in response.cookies


@pytest.mark.anyio
@pytest.mark.parametrize("environment", ["production", "test", ""])
async def test_seed_refuses_non_development_without_creating_accounts(database, monkeypatch, environment):
    seed = importlib.import_module("app.seed").seed
    monkeypatch.setenv("ENVIRONMENT", environment)
    with pytest.raises(RuntimeError):
        await seed()
    async with Session() as db:
        assert list(await db.scalars(select(User))) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name,value",
    [
        ("DEV_USER_PASSWORD", "short"),
        ("ADMIN_EMAIL", "invalid"),
        ("DEV_USER_EMAIL", ""),
        ("DEV_USER_EMAIL", "ADMIN@example.com"),
    ],
)
async def test_seed_validates_all_credentials_before_creating_accounts(database, monkeypatch, name, value):
    seed = importlib.import_module("app.seed").seed
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError):
        await seed()
    async with Session() as db:
        assert list(await db.scalars(select(User))) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "email,role,password_hash",
    [
        ("admin@example.com", "user", None),
        ("user@example.com", "admin", None),
        ("user@example.com", "user", None),
        ("user@example.com", "user", "invalid-hash"),
    ],
)
async def test_seed_preserves_conflicting_accounts_and_rolls_back_new_accounts(database, email, role, password_hash):
    seed = importlib.import_module("app.seed").seed
    original_hash = password_hash or hasher.hash("original account password")
    async with Session() as db:
        original = User(email=email, role=role, password_hash=original_hash)
        db.add(original)
        await db.commit()
        original_id = original.id

    with pytest.raises(RuntimeError):
        await seed()

    async with Session() as db:
        users = list(await db.scalars(select(User)))
    assert [(user.id, user.email, user.role, user.password_hash) for user in users] == [
        (original_id, email, role, original_hash)
    ]
