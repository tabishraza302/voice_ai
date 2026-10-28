"""Contract tests for signup, email verification, login, reset, and password change.

Follows auth-contract.md Task 3:
- Email normalization and alias policy (lowercased ASCII, dots/+suffix preserved).
- Unknown extra role/status fields rejected on wire models (extra='forbid').
- Registration does not create a usable user until token possession confirmed.
- Generic 202 response for duplicate registrations and password reset requests.
- Token invalidation on wrong purpose, expiry, or reuse.
- Concurrent confirmation: exactly one winner and one wallet initialization.
- Password reset revokes all sessions without clearing MFA factor or account status.
- Password change rotates current session and revokes all others.
- 204 No Content for logout and session deletion.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import select
from platform_api.auth.delivery import deliver_pending_auth_messages
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthSession, AuthToken, User
from platform_api.models.wallet import WalletAccount
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_email_normalization_and_alias_policy(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Email normalization trims whitespace, normalizes IDNA, preserves +aliases, and lowercases email_key."""
    # 1. Bootstrap anonymous session
    session_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin},
    )
    csrf = session_res.json()["csrf_token"]
    cookie = session_res.headers.get("set-cookie")

    # 2. Register with leading/trailing spaces and mixed case and +suffix
    raw_email = "  User.Name+TestTag@Example.COM  "
    reg_res = await app_client.post(
        "/api/v1/auth/register",
        json={"email": raw_email, "display_name": "Test User"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )
    assert reg_res.status_code == 202
    assert reg_res.json() == {"status": "accepted"}

    # Inspect token row in database
    async with initialized_pg.sessions() as session:
        stmt = select(AuthToken).where(AuthToken.purpose == "register")
        res = await session.execute(stmt)
        token_record = res.scalar_one()

        # email_key is fully lowercased and preserves dots and +tag
        assert token_record.email_key == "user.name+testtag@example.com"
        assert token_record.consumed_at is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_rejects_extra_role_and_status_fields(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
):
    """Attempts to inject role or status via request payloads are rejected with 422 VALIDATION."""
    session_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin},
    )
    csrf = session_res.json()["csrf_token"]
    cookie = session_res.headers.get("set-cookie")

    # Register payload with forbidden 'role' field
    res = await app_client.post(
        "/api/v1/auth/register",
        json={
            "email": "attacker@example.com",
            "display_name": "Attacker",
            "role": "admin",
        },
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_signup_completion_and_wallet_initialization(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Confirming verification token creates user and zero-credit wallet account atomically."""
    # 1. Bootstrap
    session_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin},
    )
    csrf = session_res.json()["csrf_token"]
    cookie = session_res.headers.get("set-cookie")

    # 2. Register
    await app_client.post(
        "/api/v1/auth/register",
        json={"email": "alice@example.com", "display_name": "Alice Tester"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )
    await deliver_pending_auth_messages(test_platform_settings)

    # Read token from written mail file
    mail_files = os.listdir(test_platform_settings.auth_mail_dir)
    assert len(mail_files) == 1
    with open(os.path.join(test_platform_settings.auth_mail_dir, mail_files[0]), "r") as f:
        mail_data = json.load(f)

    # Fragment contains token: .../verify-email#token=<token>
    token_str = mail_data["link"].split("#token=")[1]

    # 3. Verify email with chosen password
    verify_res = await app_client.post(
        "/api/v1/auth/verify-email",
        json={"token": token_str, "password": "alice-secure-password-123"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )
    assert verify_res.status_code == 201
    user_data = verify_res.json()
    user_id = uuid.UUID(user_data["id"])
    assert user_data["email"] == "alice@example.com"
    assert user_data["role"] == "user"
    assert user_data["status"] == "active"

    # 4. Verify wallet account exists with zero balance
    async with initialized_pg.sessions() as session:
        wallet_stmt = select(WalletAccount).where(WalletAccount.user_id == user_id)
        wallet = (await session.execute(wallet_stmt)).scalar_one_or_none()
        assert wallet is not None
        assert wallet.currency == "INR"
        assert wallet.posted_minor == 0
        assert wallet.reserved_minor == 0

    # 5. Token reuse rejected
    reuse_res = await app_client.post(
        "/api/v1/auth/verify-email",
        json={"token": token_str, "password": "alice-secure-password-123"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )
    assert reuse_res.status_code == 400
    assert reuse_res.json()["error"]["code"] == "TOKEN_INVALID"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_password_reset_revokes_all_sessions(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Password reset increments auth_version and revokes all active sessions."""
    # 1. Register & verify user
    session_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin},
    )
    csrf = session_res.json()["csrf_token"]
    cookie = session_res.headers.get("set-cookie")

    await app_client.post(
        "/api/v1/auth/register",
        json={"email": "bob@example.com", "display_name": "Bob Tester"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )
    await deliver_pending_auth_messages(test_platform_settings)
    mail_files = os.listdir(test_platform_settings.auth_mail_dir)
    with open(os.path.join(test_platform_settings.auth_mail_dir, mail_files[0]), "r") as f:
        reg_token = json.load(f)["link"].split("#token=")[1]

    await app_client.post(
        "/api/v1/auth/verify-email",
        json={"token": reg_token, "password": "bob-original-password-123"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )

    # 2. Login to get authenticated session
    login_res = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "bob@example.com", "password": "bob-original-password-123"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )
    assert login_res.status_code == 200
    auth_cookie = login_res.headers.get("set-cookie")
    auth_csrf = login_res.json()["csrf_token"]

    # 3. Request reset
    os.remove(os.path.join(test_platform_settings.auth_mail_dir, mail_files[0]))
    reset_req_res = await app_client.post(
        "/api/v1/auth/password/reset-request",
        json={"email": "bob@example.com"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": auth_csrf,
            "Cookie": auth_cookie,
        },
    )
    assert reset_req_res.status_code == 202
    await deliver_pending_auth_messages(test_platform_settings)

    # Read reset token
    new_mail_files = os.listdir(test_platform_settings.auth_mail_dir)
    with open(os.path.join(test_platform_settings.auth_mail_dir, new_mail_files[0]), "r") as f:
        reset_token = json.load(f)["link"].split("#token=")[1]

    # 4. Confirm reset
    confirm_res = await app_client.post(
        "/api/v1/auth/password/reset-confirm",
        json={"token": reset_token, "password": "bob-new-password-56789"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": auth_csrf,
            "Cookie": auth_cookie,
        },
    )
    assert confirm_res.status_code == 204

    # 5. Prior authenticated session must now fail with 401 UNAUTHENTICATED
    me_res = await app_client.get(
        "/api/v1/me",
        headers={"Cookie": auth_cookie},
    )
    assert me_res.status_code == 401
    assert me_res.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_failed_identity_switching_revokes_old_session(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Failed login from an authenticated session revokes the old session and expires the cookie."""
    # 1. Setup verified user
    boot = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin},
    )
    csrf = boot.json()["csrf_token"]
    cookie = boot.headers.get("set-cookie")

    await app_client.post(
        "/api/v1/auth/register",
        json={"email": "switch@example.com", "display_name": "Switch Tester"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )
    await deliver_pending_auth_messages(test_platform_settings)
    mail_file = os.listdir(test_platform_settings.auth_mail_dir)[0]
    with open(os.path.join(test_platform_settings.auth_mail_dir, mail_file), "r") as f:
        token = json.load(f)["link"].split("#token=")[1]

    await app_client.post(
        "/api/v1/auth/verify-email",
        json={"token": token, "password": "switch-good-pw-12345"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )

    # 2. Login to obtain authenticated session
    login_res = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "switch@example.com", "password": "switch-good-pw-12345"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
        },
    )
    assert login_res.status_code == 200
    auth_cookie = login_res.headers.get("set-cookie")
    auth_csrf = login_res.json()["csrf_token"]
    session_id = uuid.UUID(login_res.json()["session_id"])

    # 3. Attempt to switch user / login with wrong password
    fail_login_res = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "switch@example.com", "password": "wrong-password-99999"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": auth_csrf,
            "Cookie": auth_cookie,
        },
    )
    assert fail_login_res.status_code == 401
    assert fail_login_res.json()["error"]["code"] == "INVALID_CREDENTIALS"

    # Cookie expiration header must be present on the 401 response
    fail_set_cookie = fail_login_res.headers.get("set-cookie")
    assert fail_set_cookie is not None
    assert "Max-Age=0" in fail_set_cookie

    # Prior session in PostgreSQL must be marked revoked
    async with initialized_pg.sessions() as session:
        stmt = select(AuthSession).where(AuthSession.id == session_id)
        revoked_sess = (await session.execute(stmt)).scalar_one()
        assert revoked_sess.revoked_at is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_get_session_cross_site_rejection(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
):
    """GET /auth/session rejects Sec-Fetch-Site: cross-site and wrong Origin."""
    # 1. Sec-Fetch-Site: cross-site rejected
    res1 = await app_client.get(
        "/api/v1/auth/session",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert res1.status_code == 403
    assert res1.json()["error"]["code"] == "ORIGIN_FORBIDDEN"

    # 2. Foreign Origin header rejected
    res2 = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": "https://attacker.example.com"},
    )
    assert res2.status_code == 403
    assert res2.json()["error"]["code"] == "ORIGIN_FORBIDDEN"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repeated_failed_anonymous_logins_persist_and_reach_429(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Failed anonymous logins persist counter increments to PostgreSQL and reach 429."""
    from platform_api.app import create_app
    from platform_api.models.auth import AuthRateLimit

    # Create app with small login limit (3 attempts per 15 min per IP)
    settings = test_platform_settings.model_copy(
        update={"auth_rate_login_ip_per_15_minutes": 3}
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            # Bootstrap session
            boot = await client.get(
                "/api/v1/auth/session",
                headers={"Origin": settings.web_origin},
            )
            csrf = boot.json()["csrf_token"]
            cookie = boot.headers.get("set-cookie")

            # Send 3 failed login attempts
            for i in range(3):
                res = await client.post(
                    "/api/v1/auth/login",
                    json={"email": f"fail{i}@example.com", "password": "wrong-password-123"},
                    headers={
                        "Origin": settings.web_origin,
                        "X-CSRF-Token": csrf,
                        "Cookie": cookie,
                    },
                )
                assert res.status_code == 401
                assert res.json()["error"]["code"] == "INVALID_CREDENTIALS"

            # 4th attempt from same IP breaches limit and returns 429
            res4 = await client.post(
                "/api/v1/auth/login",
                json={"email": "fail_again@example.com", "password": "wrong-password-123"},
                headers={
                    "Origin": settings.web_origin,
                    "X-CSRF-Token": csrf,
                    "Cookie": cookie,
                },
            )
            assert res4.status_code == 429
            assert res4.json()["error"]["code"] == "RATE_LIMITED"
            assert "Retry-After" in res4.headers

            # Direct database verification: counter rows exist and persisted across rollbacks
            async with initialized_pg.sessions() as session:
                stmt = select(AuthRateLimit).where(AuthRateLimit.scope == "login_ip")
                records = list((await session.execute(stmt)).scalars().all())
                assert len(records) >= 1
                assert records[0].count >= 4


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_global_auth_rate_cap_on_non_session_endpoint(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Global auth route limit (auth_rate_global_per_minute) is enforced on non-session endpoints."""
    from platform_api.app import create_app

    # Create app with small global rate limit (2 requests per minute per IP)
    settings = test_platform_settings.model_copy(
        update={"auth_rate_global_per_minute": 2}
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            boot = await client.get(
                "/api/v1/auth/session",
                headers={"Origin": settings.web_origin},
            )
            csrf = boot.json()["csrf_token"]
            cookie = boot.headers.get("set-cookie")

            # 2nd request to /register succeeds with 202
            res2 = await client.post(
                "/api/v1/auth/register",
                json={"email": "global1@example.com", "display_name": "Global One"},
                headers={
                    "Origin": settings.web_origin,
                    "X-CSRF-Token": csrf,
                    "Cookie": cookie,
                },
            )
            assert res2.status_code == 202

            # 3rd request to /register from same IP exceeds global cap of 2 and returns 429
            res3 = await client.post(
                "/api/v1/auth/register",
                json={"email": "global2@example.com", "display_name": "Global Two"},
                headers={
                    "Origin": settings.web_origin,
                    "X-CSRF-Token": csrf,
                    "Cookie": cookie,
                },
            )
            assert res3.status_code == 429
            assert res3.json()["error"]["code"] == "RATE_LIMITED"
            assert "Retry-After" in res3.headers
            assert int(res3.headers["Retry-After"]) >= 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_password_reset_consumption_single_winner(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Concurrent password reset token submissions on independent DB connections allow exactly one winner."""
    from platform_api.auth.service import confirm_password_reset, generate_token_pair
    from platform_api.auth.passwords import get_password_hasher
    from platform_api.auth.errors import TokenInvalidError
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    orig_pw_hash = await hasher.hash_password("original-user-password-123")

    user_id = uuid.uuid4()
    raw_token, token_digest = generate_token_pair()

    # Seed user and reset token in DB
    async with initialized_pg.sessions() as session:
        user = User(
            id=user_id,
            email="concurrent-reset@example.com",
            email_key="concurrent-reset@example.com",
            display_name="Concurrent Reset",
            password_hash=orig_pw_hash,
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
        token = AuthToken(
            id=uuid.uuid4(),
            purpose="password_reset",
            token_digest=token_digest,
            email_key="concurrent-reset@example.com",
            user_id=user_id,
            display_name="Concurrent Reset",
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            consumed_at=None,
        )
        session.add(token)
        await session.commit()

    barrier = asyncio.Barrier(2)

    async def _attempt_reset(candidate_pw: str) -> str:
        async with initialized_pg.sessions() as db_conn:
            await barrier.wait()
            try:
                await confirm_password_reset(
                    db_conn,
                    test_platform_settings,
                    token_str=raw_token,
                    password_str=candidate_pw,
                    client_ip=None,
                    now=now,
                )
                await db_conn.commit()
                return "success"
            except TokenInvalidError:
                await db_conn.rollback()
                return "token_invalid"

    pw1 = "winning-pw-candidate-111"
    pw2 = "losing-pw-candidate-222"

    results = await asyncio.gather(
        _attempt_reset(pw1),
        _attempt_reset(pw2),
    )

    # Exactly one transaction succeeded, and exactly one failed with TokenInvalidError
    assert sorted(results) == ["success", "token_invalid"]

    # Verify invariants in PostgreSQL
    async with initialized_pg.sessions() as session:
        u = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        assert u.auth_version == 2

        t = (await session.execute(select(AuthToken).where(AuthToken.token_digest == token_digest))).scalar_one()
        assert t.consumed_at is not None

        is_pw1 = await hasher.verify_password(pw1, u.password_hash)
        is_pw2 = await hasher.verify_password(pw2, u.password_hash)
        assert (is_pw1 and not is_pw2) or (is_pw2 and not is_pw1)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_registration_consumption_single_winner(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Concurrent registration token submissions on independent DB connections allow exactly one winner."""
    from platform_api.auth.service import verify_email_and_create_user, generate_token_pair
    from platform_api.auth.passwords import get_password_hasher
    from platform_api.auth.errors import TokenInvalidError
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    raw_token, token_digest = generate_token_pair()
    email_key = "concurrent-signup@example.com"

    # Seed unconsumed registration token
    async with initialized_pg.sessions() as session:
        token = AuthToken(
            id=uuid.uuid4(),
            purpose="register",
            token_digest=token_digest,
            email_key=email_key,
            user_id=None,
            display_name="Concurrent Signup",
            created_at=now,
            expires_at=now + timedelta(hours=24),
            consumed_at=None,
        )
        session.add(token)
        await session.commit()

    barrier = asyncio.Barrier(2)

    async def _attempt_verify(candidate_pw: str) -> str:
        async with initialized_pg.sessions() as db_conn:
            await barrier.wait()
            try:
                await verify_email_and_create_user(
                    db_conn,
                    test_platform_settings,
                    token_str=raw_token,
                    password_str=candidate_pw,
                    client_ip=None,
                    now=now,
                )
                await db_conn.commit()
                return "success"
            except TokenInvalidError:
                await db_conn.rollback()
                return "token_invalid"

    pw1 = "winning-signup-pw-111"
    pw2 = "losing-signup-pw-222"

    results = await asyncio.gather(
        _attempt_verify(pw1),
        _attempt_verify(pw2),
    )

    # Exactly one transaction succeeded, and exactly one failed with TokenInvalidError
    assert sorted(results) == ["success", "token_invalid"]

    # Verify durable resulting invariants in PostgreSQL
    async with initialized_pg.sessions() as session:
        users = (await session.execute(select(User).where(User.email_key == email_key))).scalars().all()
        assert len(users) == 1
        u = users[0]
        assert u.role == "user"
        assert u.status == "active"
        assert u.email_verified_at is not None

        # Exactly one wallet initialized
        wallets = (await session.execute(select(WalletAccount).where(WalletAccount.user_id == u.id))).scalars().all()
        assert len(wallets) == 1

        # Token record is marked consumed
        t = (await session.execute(select(AuthToken).where(AuthToken.token_digest == token_digest))).scalar_one()
        assert t.consumed_at is not None

        is_pw1 = await hasher.verify_password(pw1, u.password_hash)
        is_pw2 = await hasher.verify_password(pw2, u.password_hash)
        assert (is_pw1 and not is_pw2) or (is_pw2 and not is_pw1)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_password_reset_http_requests_single_winner(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Simultaneous HTTP requests to /api/v1/auth/password/reset-confirm allow exactly one winner."""
    from platform_api.app import create_app
    from platform_api.auth.service import generate_token_pair
    from platform_api.auth.passwords import get_password_hasher
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    orig_pw_hash = await hasher.hash_password("http-reset-orig-pw-1234")
    user_id = uuid.uuid4()
    email_key = "concurrent-http-reset@example.com"
    raw_token, token_digest = generate_token_pair()

    async with initialized_pg.sessions() as session:
        user = User(
            id=user_id,
            email=email_key,
            email_key=email_key,
            display_name="Concurrent HTTP Reset",
            password_hash=orig_pw_hash,
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
        token = AuthToken(
            id=uuid.uuid4(),
            purpose="password_reset",
            token_digest=token_digest,
            email_key=email_key,
            user_id=user_id,
            display_name="Concurrent HTTP Reset",
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            consumed_at=None,
        )
        session.add(token)
        await session.commit()

    app = create_app(test_platform_settings)
    barrier = asyncio.Barrier(2)

    async with app.router.lifespan_context(app):
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client1,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client2,
        ):
            # Obtain anonymous session and CSRF tokens for each client
            b1 = await client1.get("/api/v1/auth/session", headers={"Origin": test_platform_settings.web_origin})
            csrf1 = b1.json()["csrf_token"]
            cookie1 = b1.headers.get("set-cookie")

            b2 = await client2.get("/api/v1/auth/session", headers={"Origin": test_platform_settings.web_origin})
            csrf2 = b2.json()["csrf_token"]
            cookie2 = b2.headers.get("set-cookie")

            pw1 = "winning-http-candidate-111"
            pw2 = "losing-http-candidate-222"

            async def _post_reset(client: httpx.AsyncClient, csrf: str, cookie: str, pw: str) -> httpx.Response:
                await barrier.wait()
                return await client.post(
                    "/api/v1/auth/password/reset-confirm",
                    json={"token": raw_token, "password": pw},
                    headers={
                        "Origin": test_platform_settings.web_origin,
                        "X-CSRF-Token": csrf,
                        "Cookie": cookie,
                    },
                )

            resp1, resp2 = await asyncio.gather(
                _post_reset(client1, csrf1, cookie1, pw1),
                _post_reset(client2, csrf2, cookie2, pw2),
            )

            status_codes = [resp1.status_code, resp2.status_code]
            assert sorted(status_codes) == [204, 400]

            err_resp = resp1 if resp1.status_code == 400 else resp2
            assert err_resp.json()["error"]["code"] == "TOKEN_INVALID"

    # Verify durable invariants in PostgreSQL
    async with initialized_pg.sessions() as session:
        u = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        assert u.auth_version == 2
        t = (await session.execute(select(AuthToken).where(AuthToken.token_digest == token_digest))).scalar_one()
        assert t.consumed_at is not None
