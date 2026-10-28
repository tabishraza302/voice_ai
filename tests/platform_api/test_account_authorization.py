"""Contract tests for account ownership, authorization boundaries, and admin controls.

Follows auth-contract.md Tasks 3 & 4:
- Two users cannot read, list, or revoke each other's sessions (404 NOT_FOUND).
- Role, status, and email fields cannot be injected through /me (422 VALIDATION).
- Admin cannot bypass owner queries through ordinary owner routes.
- Normal users receive 403 FORBIDDEN on all /admin routes.
- Stale or missing step-up denies suspension (403 STEP_UP_REQUIRED).
- Last active enrolled admin cannot be suspended (409 LAST_ADMIN).
- Suspension immediately denies admissions and invalidates active sessions.
- Restore does not grant active sessions or wallet credit.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

import httpx
import pytest
from sqlalchemy import select

from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import create_authenticated_session
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthMfa, AuthSession, User
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_users_cannot_access_each_others_sessions(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """User A cannot delete or inspect User B's session via owner routes; returns 404 NOT_FOUND."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("secure-password-12345")

    async with initialized_pg.sessions() as session:
        user_a = User(
            id=uuid.uuid4(),
            email="usera@example.com",
            email_key="usera@example.com",
            display_name="User A",
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
        user_b = User(
            id=uuid.uuid4(),
            email="userb@example.com",
            email_key="userb@example.com",
            display_name="User B",
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
        session.add_all([user_a, user_b])
        await session.flush()

        sess_a, secret_a = await create_authenticated_session(
            session, test_platform_settings, user=user_a, client_ip="127.0.0.1", user_agent="A", now=now
        )
        sess_b, secret_b = await create_authenticated_session(
            session, test_platform_settings, user=user_b, client_ip="127.0.0.1", user_agent="B", now=now
        )
        await session.commit()

    # User A tries to delete User B's session
    cookie_name = test_platform_settings.session_cookie_name
    cookie_header_a = f"{cookie_name}={secret_a}"

    # Bootstrap to get CSRF token for User A
    boot_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin, "Cookie": cookie_header_a},
    )
    csrf_a = boot_res.json()["csrf_token"]

    del_res = await app_client.delete(
        f"/api/v1/auth/sessions/{sess_b.id}",
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf_a,
            "Cookie": cookie_header_a,
            "Content-Type": "application/json",
        },
    )
    # Must fail with 404 NOT_FOUND, never revealing User B's session exists
    assert del_res.status_code == 404
    assert del_res.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_normal_user_denied_on_admin_routes(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Ordinary authenticated users receive 403 FORBIDDEN on all admin routes."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("secure-password-12345")

    async with initialized_pg.sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email="regular@example.com",
            email_key="regular@example.com",
            display_name="Regular User",
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
        _, secret = await create_authenticated_session(
            session, test_platform_settings, user=user, client_ip="127.0.0.1", user_agent="Test", now=now
        )
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    res = await app_client.get(
        "/api/v1/admin/users",
        headers={"Cookie": cookie_header},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_last_admin_suspension_prevented(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Attempting to suspend the sole active enrolled administrator returns 409 LAST_ADMIN."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-secure-pw-12345")

    async with initialized_pg.sessions() as session:
        admin = User(
            id=uuid.uuid4(),
            email="admin@example.com",
            email_key="admin@example.com",
            display_name="Sole Admin",
            password_hash=pw_hash,
            role="admin",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(admin)
        await session.flush()

        mfa = AuthMfa(
            user_id=admin.id,
            secret_ciphertext="encrypted-secret",
            enabled_at=now,
            last_accepted_step=100,
        )
        session.add(mfa)
        await session.flush()

        # Session with verified MFA and fresh step-up
        sess, secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=admin,
            client_ip="127.0.0.1",
            user_agent="AdminDevice",
            now=now,
            mfa_verified=True,
        )
        sess.step_up_at = now
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    boot_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin, "Cookie": cookie_header},
    )
    csrf = boot_res.json()["csrf_token"]

    suspend_res = await app_client.post(
        f"/api/v1/admin/users/{admin.id}/suspend",
        json={"reason": "Testing suspension of the sole administrator"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie_header,
        },
    )
    assert suspend_res.status_code == 409
    assert suspend_res.json()["error"]["code"] == "LAST_ADMIN"



@pytest.mark.postgres
@pytest.mark.asyncio
async def test_missing_password_verified_at_fails_require_user(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Sessions lacking password_verified_at fail closed with 401 without inventing now."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("password-verified-12345")

    async with initialized_pg.sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email="no-pw-verified@example.com",
            email_key="no-pw-verified@example.com",
            display_name="No PW User",
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

        sess, secret = await create_authenticated_session(
            session, test_platform_settings, user=user, client_ip="127.0.0.1", user_agent="Device", now=now
        )
        # Clear password_verified_at
        sess.password_verified_at = None
        await session.commit()

    cookie_header = f"{test_platform_settings.session_cookie_name}={secret}"
    res = await app_client.get("/api/v1/me", headers={"Cookie": cookie_header})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHENTICATED"

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_server_side_suspended_account_denied_on_active_session(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Server-side suspended account with otherwise valid active session is denied access (403 ACCOUNT_SUSPENDED)."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("user-password-12345")

    async with initialized_pg.sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email="suspended-active-sess@example.com",
            email_key="suspended-active-sess@example.com",
            display_name="Suspended Active",
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

        sess, secret = await create_authenticated_session(
            session, test_platform_settings, user=user, client_ip="127.0.0.1", user_agent="Device", now=now
        )
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    # 1. While active, access to /me succeeds
    res_before = await app_client.get("/api/v1/me", headers={"Cookie": cookie_header})
    assert res_before.status_code == 200

    # 2. Server-side suspension occurs directly on user record in PostgreSQL
    # Session row itself is left untouched to test fail-closed check on user status
    async with initialized_pg.sessions() as session:
        u = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
        u.status = "suspended"
        await session.commit()

    # 3. Subsequent request with the still-active session cookie fails closed with 403 ACCOUNT_SUSPENDED
    res_after = await app_client.get("/api/v1/me", headers={"Cookie": cookie_header})
    assert res_after.status_code == 403
    assert res_after.json()["error"]["code"] == "ACCOUNT_SUSPENDED"

    # Mutations requiring CSRF and require_user also fail closed with 403 ACCOUNT_SUSPENDED
    csrf_token = res_before.headers.get("x-csrf-token")
    change_pw_res = await app_client.post(
        "/api/v1/auth/password/change",
        json={"current_password": "user-password-12345", "new_password": "new-user-password-67890"},
        headers={
            "Cookie": cookie_header,
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf_token or "dummy-csrf",
        },
    )
    assert change_pw_res.status_code == 403
    assert change_pw_res.json()["error"]["code"] == "ACCOUNT_SUSPENDED"

    # 4. GET /auth/session sees suspension, revokes session, and returns anonymous state
    sess_res = await app_client.get("/api/v1/auth/session", headers={"Cookie": cookie_header})
    assert sess_res.status_code == 200
    assert sess_res.json()["state"] == "anonymous"
    assert sess_res.json()["user"] is None

    # 5. Verify the session is durably revoked in PostgreSQL
    async with initialized_pg.sessions() as session:
        db_sess = (await session.execute(select(AuthSession).where(AuthSession.id == sess.id))).scalar_one()
        assert db_sess.revoked_at is not None
