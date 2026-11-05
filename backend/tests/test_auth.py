import os
import uuid
from typing import Annotated

import pytest
from argon2 import PasswordHasher
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app import auth as auth_module
from app.auth import COOKIE, admin_user, router as auth_router
from app.db import Base, Session, engine
from app.main import app
from app.models import User

admin_test_app = FastAPI()
admin_test_app.include_router(auth_router)


@admin_test_app.get("/_test/admin")
async def admin_check(user: Annotated[User, Depends(admin_user)]):
    return {"role": user.role}


@pytest.fixture
async def admin_client(client):
    async with AsyncClient(transport=ASGITransport(app=admin_test_app), base_url="http://localhost:3000") as test_client:
        yield test_client


@pytest.fixture
async def client():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000") as test_client:
        yield test_client


@pytest.mark.anyio
async def test_registration_normalizes_email_and_never_grants_admin(client):
    email = f"{uuid.uuid4()}@example.com"
    credentials = {"email": f"  {email.upper()}  ", "password": "correct horse battery staple"}
    response = await client.post("/auth/register", json={**credentials, "role": "admin"})
    assert response.status_code == 422
    response = await client.post("/auth/register", json=credentials)
    assert response.status_code == 201
    assert response.json()["email"] == email
    assert response.json()["role"] == "user"
    assert "password_hash" not in response.json()
    duplicate = await client.post("/auth/register", json={"email": f" {email.upper()} ", "password": credentials["password"]})
    assert duplicate.status_code == 409


@pytest.mark.anyio
async def test_registration_stores_an_argon2_password_hash(client):
    email = f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    assert (await client.post("/auth/register", json={"email": email, "password": password})).status_code == 201
    async with Session() as db:
        user = await db.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert PasswordHasher().verify(user.password_hash, password)


@pytest.mark.anyio
async def test_login_normalizes_email(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    email = f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    await client.post("/auth/register", json={"email": f" {email.upper()} ", "password": password})
    response = await client.post("/auth/login", json={"email": f" {email.upper()} ", "password": password})
    assert response.status_code == 200
    assert response.json()["email"] == email
    assert "secure" not in response.headers["set-cookie"].lower()
    assert (await client.get("/auth/me")).json()["email"] == email


@pytest.mark.anyio
async def test_login_cookie_is_expiring_http_only_lax_and_secure_in_production(client, monkeypatch):
    email = f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    await client.post("/auth/register", json={"email": email, "password": password})
    monkeypatch.setenv("ENVIRONMENT", "production")
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{COOKIE}=")
    assert "max-age=43200" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" in cookie


@pytest.mark.anyio
async def test_login_rejects_unknown_wrong_and_corrupt_credentials(client):
    email = f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    unknown = await client.post("/auth/login", json={"email": email, "password": password})
    assert (await client.post("/auth/register", json={"email": email, "password": password})).status_code == 201
    wrong = await client.post("/auth/login", json={"email": email, "password": "another correct password"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    async with Session() as db:
        user = await db.scalar(select(User).where(User.email == email))
        user.password_hash = "not-a-valid-argon2-hash"
        await db.commit()
    corrupt = await client.post("/auth/login", json={"email": email, "password": password})
    assert corrupt.status_code == 401
    assert corrupt.json() == unknown.json()


@pytest.mark.anyio
async def test_dev_login_shortcut_is_removed_even_when_enabled(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("ALLOW_DEV_LOGIN", "true")
    dev_email = f"{uuid.uuid4()}@example.com"
    dev_password = "correct horse battery staple"
    admin_email = f"{uuid.uuid4()}@example.com"
    admin_password = "another correct horse battery staple"
    monkeypatch.setenv("DEV_USER_EMAIL", dev_email)
    monkeypatch.setenv("DEV_USER_PASSWORD", dev_password)
    monkeypatch.setenv("ADMIN_EMAIL", admin_email)
    monkeypatch.setenv("ADMIN_PASSWORD", admin_password)

    assert (await client.post("/auth/register", json={"email": dev_email, "password": dev_password})).status_code == 201
    assert (await client.post("/auth/register", json={"email": admin_email, "password": admin_password})).status_code == 201
    async with Session() as db:
        admin = await db.scalar(select(User).where(User.email == admin_email))
        assert admin is not None
        admin.role = "admin"
        await db.commit()

    assert (await client.get("/auth/dev-login/available")).status_code == 404
    for role in ("user", "admin"):
        for headers in (None, {"Origin": "http://localhost:3000"}, {"Origin": "https://attacker.example"}):
            response = await client.post("/auth/dev-login", json={"role": role}, headers=headers)
            assert response.status_code == 404
            assert "set-cookie" not in response.headers
    assert client.cookies.get(COOKIE) is None


@pytest.mark.anyio
async def test_session_cookie_rejects_tampering_and_expiry(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    email = f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    await client.post("/auth/register", json={"email": email, "password": password})
    assert (await client.post("/auth/login", json={"email": email, "password": password})).status_code == 200
    signed_cookie = client.cookies.get(COOKIE)
    assert signed_cookie is not None
    client.cookies.clear()
    assert (await client.get("/auth/me", headers={"Cookie": f"{COOKIE}={signed_cookie}tampered"})).status_code == 401
    async with Session() as db:
        user = await db.scalar(select(User).where(User.email == email))
    assert user is not None
    monkeypatch.setattr(auth_module, "MAX_AGE", -1)
    expired = auth_module.create_session_token(user.id, "csrf-token")
    assert (await client.get("/auth/me", headers={"Cookie": f"{COOKIE}={expired}"})).status_code == 401
    assert (await client.get("/auth/me")).status_code == 401


@pytest.mark.anyio
async def test_session_cookie_rejects_malformed_signed_payload(client):
    email = f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    registered = await client.post("/auth/register", json={"email": email, "password": password})
    malformed = auth_module.create_session_token(registered.json()["id"], 42)
    client.cookies.set(COOKIE, malformed, domain="localhost", path="/")
    assert (await client.get("/auth/me")).status_code == 401


def test_session_token_is_jwt_with_expiry():
    import jwt

    token = auth_module.create_session_token(uuid.uuid4(), "csrf-token")
    parts = token.split(".")
    assert len(parts) == 3
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "HS256"
    payload = auth_module.decode_session_token(token)
    assert "sub" in payload and "csrf" in payload and "exp" in payload and "iat" in payload


@pytest.mark.anyio
async def test_authenticated_mutation_requires_matching_origin_and_csrf(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("APP_ORIGIN", "http://localhost:3000")
    email = f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    await client.post("/auth/register", json={"email": email, "password": password})
    login = await client.post("/auth/login", json={"email": email, "password": password})
    csrf_token = login.json()["csrf_token"]
    assert (await client.post("/auth/logout")).status_code == 403
    assert (await client.post("/auth/logout", headers={"Origin": "http://localhost:3000.evil", "X-CSRF-Token": csrf_token})).status_code == 403
    assert (await client.post("/auth/logout", headers={"Origin": "http://localhost:3000", "X-CSRF-Token": "wrong"})).status_code == 403
    assert (await client.post("/auth/logout", headers=[(b"Origin", b"http://localhost:3000"), (b"X-CSRF-Token", b"invalid-\xe9")])).status_code == 403
    response = await client.post("/auth/logout", headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token})
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert (await client.get("/auth/me")).status_code == 401


@pytest.mark.anyio
async def test_admin_dependency_returns_401_for_anonymous_and_403_for_users(admin_client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert (await admin_client.get("/_test/admin")).status_code == 401
    email = f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    await admin_client.post("/auth/register", json={"email": email, "password": password})
    assert (await admin_client.post("/auth/login", json={"email": email, "password": password})).status_code == 200
    assert (await admin_client.get("/_test/admin")).status_code == 403
    async with Session() as db:
        user = await db.scalar(select(User).where(User.email == email))
        user.role = "admin"
        await db.commit()
    response = await admin_client.get("/_test/admin")
    assert response.status_code == 200
    assert response.json() == {"role": "admin"}


@pytest.fixture
def anyio_backend():
    return "asyncio"
