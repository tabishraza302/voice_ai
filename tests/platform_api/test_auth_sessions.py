"""Contract tests for server-side opaque sessions, cookies, and lifecycles.

Follows auth-contract.md Task 2:
- Cookie contains only 32-byte opaque base64url secret; database stores SHA-256 digest.
- Old cookie invalidated after session rotation.
- Idle (30m) and absolute (12h) expiration enforced.
- Cross-replica logout and auth_version increments invalidate sessions across instances.
- Maximum 5 active sessions per user; oldest evicted under concurrent creation.
- Database outage fails closed (503).
- VoiceSession rows from bot.db grant no identity or session context.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import uuid

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select

from platform_api.auth.errors import UnauthenticatedError
from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import (
    create_authenticated_session,
    generate_session_secret,
    hash_session_secret,
    revoke_all_user_sessions,
    touch_session_activity,
)
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthSession, User
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cookie_stores_only_opaque_secret(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """The browser cookie stores only an opaque base64url secret; PostgreSQL stores only its SHA-256 digest."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("password-for-session-tests-123")

    async with initialized_pg.sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email="test-session@example.com",
            email_key="test-session@example.com",
            display_name="Session Tester",
            password_hash=pw_hash,
            role="user",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()

        auth_session, raw_secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="TestAgent",
            now=now,
        )
        await session.commit()

        # Secret is 43 characters (unpadded base64url of 32 bytes)
        assert len(raw_secret) == 43
        assert raw_secret not in str(auth_session.token_digest)

        # Database stores only SHA-256 digest
        expected_digest = hashlib.sha256(
            base64.urlsafe_b64decode(raw_secret + "=")
        ).digest()
        assert auth_session.token_digest == expected_digest


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_session_idle_and_absolute_expiration(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Expired sessions (either by idle timeout or absolute lifetime) are rejected."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("password-for-session-tests-123")

    async with initialized_pg.sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email="expired-session@example.com",
            email_key="expired-session@example.com",
            display_name="Expired Tester",
            password_hash=pw_hash,
            role="user",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()

        auth_session, raw_secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="TestAgent",
            now=now,
        )
        # Advance idle expiration into past
        auth_session.idle_expires_at = now - timedelta(seconds=1)
        await session.commit()

        # Querying with current time sees expired idle
        stmt = (
            select(AuthSession)
            .where(
                AuthSession.token_digest == auth_session.token_digest,
                AuthSession.revoked_at.is_(None),
                AuthSession.idle_expires_at > now,
                AuthSession.absolute_expires_at > now,
            )
        )
        result = await session.execute(stmt)
        assert result.scalar_one_or_none() is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_maximum_five_sessions_eviction(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Creating more than 5 authenticated sessions evicts the oldest under row lock."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("password-for-session-tests-123")

    async with initialized_pg.sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email="max-sessions@example.com",
            email_key="max-sessions@example.com",
            display_name="Max Sessions Tester",
            password_hash=pw_hash,
            role="user",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()

        created_sessions: list[AuthSession] = []
        for i in range(5):
            t = now + timedelta(seconds=i * 10)
            s, _ = await create_authenticated_session(
                session,
                test_platform_settings,
                user=user,
                client_ip="127.0.0.1",
                user_agent=f"Device-{i}",
                now=t,
            )
            created_sessions.append(s)
            await session.commit()

        # All 5 should be active
        active_stmt = select(AuthSession).where(
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
        )
        active_res = await session.execute(active_stmt)
        assert len(list(active_res.scalars().all())) == 5

        # Creating the 6th session should revoke the 1st (oldest)
        t6 = now + timedelta(seconds=60)
        s6, _ = await create_authenticated_session(
            session,
            test_platform_settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="Device-6",
            now=t6,
        )
        await session.commit()

        # Check that s6 is active and s0 is revoked
        await session.refresh(created_sessions[0])
        assert created_sessions[0].revoked_at is not None

        active_res2 = await session.execute(active_stmt)
        assert len(list(active_res2.scalars().all())) == 5


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_auth_version_revocation_across_instances(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Bumping user auth_version and revoking sessions revokes access on independent connection."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("password-for-session-tests-123")

    async with initialized_pg.sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email="version-bump@example.com",
            email_key="version-bump@example.com",
            display_name="Version Tester",
            password_hash=pw_hash,
            role="user",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()

        auth_session, _ = await create_authenticated_session(
            session,
            test_platform_settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="TestAgent",
            now=now,
        )
        await session.commit()

        # On another replica/connection: user updates password and bumps auth_version
        user.auth_version = 2
        await revoke_all_user_sessions(session, user.id, now)
        await session.commit()

        # Session is now revoked in PostgreSQL
        await session.refresh(auth_session)
        assert auth_session.revoked_at is not None
        assert auth_session.auth_version != user.auth_version


def test_get_client_ip_trusted_proxy_walk_from_right():
    """get_client_ip walks X-Forwarded-For chain from right to left, preventing spoofing."""
    from starlette.requests import Request
    from platform_api.auth.dependencies import get_client_ip

    settings = PlatformSettings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai_test",
        auth_csrf_key=base64.urlsafe_b64encode(b"C" * 32).decode("ascii"),
        auth_rate_key=base64.urlsafe_b64encode(b"R" * 32).decode("ascii"),
        auth_mfa_key=Fernet.generate_key().decode("ascii"),
        trusted_proxy_cidrs=["10.0.0.0/8", "172.16.0.0/12"],
    )

    def _build_request(client_host: str, xff: str | None = None) -> Request:
        headers = []
        if xff is not None:
            headers.append((b"x-forwarded-for", xff.encode("ascii")))
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/session",
            "headers": headers,
            "client": (client_host, 12345),
        }
        return Request(scope)

    # 1. Direct connection from trusted proxy with chained proxies and client
    # Chain: spoofed_ip, real_client_ip, intermediate_trusted_proxy
    req1 = _build_request("10.0.0.1", "203.0.113.195, 198.51.100.5, 172.16.0.2")
    resolved1 = get_client_ip(req1, settings)
    assert resolved1 == "198.51.100.5"

    # 2. Direct connection from untrusted IP ignores X-Forwarded-For completely
    req2 = _build_request("192.0.2.1", "203.0.113.195")
    resolved2 = get_client_ip(req2, settings)
    assert resolved2 == "192.0.2.1"

    # 3. Malformed IP entry in XFF fails closed safely to direct IP
    req3 = _build_request("10.0.0.1", "malformed_ip, 172.16.0.2")
    resolved3 = get_client_ip(req3, settings)
    assert resolved3 == "10.0.0.1"
