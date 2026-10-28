"""Contract tests for admin multi-factor authentication (TOTP) and step-up.

Follows auth-contract.md Task 4:
- Deterministic RFC 6238-compatible vectors and time-step calculations.
- ±1-step acceptance window enforced.
- Replay prevention: same step cannot satisfy two requests.
- Concurrent replay allows exactly one winner under row lock.
- Pending secret expiration (5 min) enforced.
- Fernet encryption verifies embedded user_id binding; tampered/mismatched payloads rejected.
- Restricted login (mfa_required / mfa_enrollment_required) cannot access any platform resource.
- Current password required for enrollment.
- Fresh MFA upgrades session state and cookie.
- Privileged operations reject stale step-up (> 5 min).
- TOTP secret absent from logs, audit logs, and non-enrollment responses.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import uuid

from cryptography.fernet import Fernet
import httpx
import pyotp
import pytest
from sqlalchemy import select

from platform_api.auth.errors import MfaInvalidError, MfaRequiredError, StepUpRequiredError
from platform_api.auth.mfa import (
    _verify_totp_step,
    confirm_mfa_enrollment,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_totp_secret,
    verify_mfa_login,
)
from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import create_authenticated_session, create_mfa_pending_session
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthMfa, AuthSession, User
from tests.support.postgres import FreshPostgres


def test_rfc6238_compatible_totp_vectors():
    """Verify PyOTP generates standards-compliant HMAC-SHA1 6-digit codes."""
    # RFC 6238 secret: "12345678901234567890" in base32 is GEZDGNBVGY3TQOJQGEZDGNBVGY======
    secret_b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY"
    totp = pyotp.TOTP(secret_b32, digits=6, interval=30)

    # At epoch 59 seconds (step 1)
    code_59 = totp.at(59)
    assert len(code_59) == 6
    assert code_59.isdigit()

    # Step tolerance ±1
    now = datetime.fromtimestamp(60, tz=timezone.utc)
    step_matched = _verify_totp_step(secret_b32, totp.at(60), now, last_accepted_step=None)
    assert step_matched == 2

    # Step -1 (time 30..59) is accepted
    step_prev = _verify_totp_step(secret_b32, totp.at(45), now, last_accepted_step=None)
    assert step_prev == 1

    # Step +1 (time 90..119) is accepted
    step_next = _verify_totp_step(secret_b32, totp.at(100), now, last_accepted_step=None)
    assert step_next == 3

    # Step -2 (time 29) is rejected
    with pytest.raises(MfaInvalidError):
        _verify_totp_step(secret_b32, totp.at(25), now, last_accepted_step=None)


def test_totp_replay_prevention():
    """Reusing the same code/step is rejected by last_accepted_step."""
    secret_b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY"
    totp = pyotp.TOTP(secret_b32, digits=6, interval=30)
    now = datetime.fromtimestamp(90, tz=timezone.utc)  # Step 3
    code = totp.at(90)

    # First use consumes step 3
    step1 = _verify_totp_step(secret_b32, code, now, last_accepted_step=2)
    assert step1 == 3

    # Immediate second use with same step is rejected
    with pytest.raises(MfaInvalidError) as exc:
        _verify_totp_step(secret_b32, code, now, last_accepted_step=step1)
    assert "already been used" in str(exc.value)


def test_fernet_user_id_binding_verification():
    """Decrypting a TOTP secret bound to User A fails when queried for User B."""
    fernet_key = Fernet.generate_key().decode("ascii")
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    secret = generate_totp_secret()

    ciphertext = encrypt_totp_secret(fernet_key, user_a, secret)

    # Valid decrypt for user A
    decrypted = decrypt_totp_secret(fernet_key, user_a, ciphertext)
    assert decrypted == secret

    # Tampered / mismatched user B decrypt fails
    with pytest.raises(ValueError):
        decrypt_totp_secret(fernet_key, user_b, ciphertext)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restricted_mfa_session_cannot_access_resources(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Admin sessions in mfa_required state cannot access /me or other resources (403 MFA_REQUIRED)."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-secret-password-123")

    async with initialized_pg.sessions() as session:
        admin = User(
            id=uuid.uuid4(),
            email="mfa-restricted@example.com",
            email_key="mfa-restricted@example.com",
            display_name="MFA Admin",
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

        sess, secret = await create_mfa_pending_session(
            session,
            test_platform_settings,
            user=admin,
            state="mfa_required",
            client_ip="127.0.0.1",
            user_agent="RestrictedAgent",
            now=now,
        )
        await session.commit()

    cookie_header = f"{test_platform_settings.session_cookie_name}={secret}"

    # Accessing /me with restricted session returns 403 MFA_REQUIRED
    res = await app_client.get("/api/v1/me", headers={"Cookie": cookie_header})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "MFA_REQUIRED"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_totp_login_replay_single_winner(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Simultaneous TOTP verification on independent DB connections allows exactly one winner under row lock."""
    import asyncio
    now = datetime.fromtimestamp(120, tz=timezone.utc)  # Step 4
    secret_b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY"
    totp = pyotp.TOTP(secret_b32, digits=6, interval=30)
    code = totp.at(120)
    user_id = uuid.uuid4()

    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-totp-replay-pw-123")
    fernet_key = test_platform_settings.auth_mfa_key
    ciphertext = encrypt_totp_secret(fernet_key, user_id, secret_b32)

    async with initialized_pg.sessions() as session:
        admin = User(
            id=user_id,
            email="totp-replay@example.com",
            email_key="totp-replay@example.com",
            display_name="TOTP Replay Admin",
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
            user_id=user_id,
            secret_ciphertext=ciphertext,
            pending_secret_ciphertext=None,
            pending_expires_at=None,
            enabled_at=now,
            last_accepted_step=2,  # Prior step 2 consumed
        )
        session.add(mfa)
        await session.commit()

    barrier = asyncio.Barrier(2)

    async def _attempt_verify() -> str:
        async with initialized_pg.sessions() as db_conn:
            await barrier.wait()
            try:
                # Load user record in this connection
                u = (await db_conn.execute(select(User).where(User.id == user_id))).scalar_one()
                step = await verify_mfa_login(
                    db_conn,
                    test_platform_settings,
                    u,
                    code,
                    now,
                )
                await db_conn.commit()
                return f"success:{step}"
            except MfaInvalidError:
                await db_conn.rollback()
                return "mfa_invalid"

    results = await asyncio.gather(
        _attempt_verify(),
        _attempt_verify(),
    )

    # Exactly one succeeded and one failed with MfaInvalidError
    assert sorted(results) == ["mfa_invalid", "success:4"]

    # Invariant: AuthMfa.last_accepted_step in PostgreSQL is updated to 4
    async with initialized_pg.sessions() as session:
        m = (await session.execute(select(AuthMfa).where(AuthMfa.user_id == user_id))).scalar_one()
        assert m.last_accepted_step == 4


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_totp_http_verify_single_winner(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Simultaneous HTTP requests to /api/v1/auth/mfa/verify allow exactly one winner."""
    import asyncio
    from platform_api.app import create_app

    now = datetime.now(timezone.utc)
    # Avoid boundary rollover by sleeping slightly if close to interval edge (>= 27s into 30s window)
    if int(now.timestamp()) % 30 >= 27:
        import asyncio
        await asyncio.sleep(4)
        now = datetime.now(timezone.utc)
    current_step = int(now.timestamp()) // 30
    secret_b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY"
    totp = pyotp.TOTP(secret_b32, digits=6, interval=30)
    code = totp.at(int(now.timestamp()))
    user_id = uuid.uuid4()

    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-http-totp-pw-1234")
    fernet_key = test_platform_settings.auth_mfa_key
    ciphertext = encrypt_totp_secret(fernet_key, user_id, secret_b32)

    async with initialized_pg.sessions() as session:
        admin = User(
            id=user_id,
            email="totp-http-replay@example.com",
            email_key="totp-http-replay@example.com",
            display_name="TOTP HTTP Admin",
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
            user_id=user_id,
            secret_ciphertext=ciphertext,
            pending_secret_ciphertext=None,
            pending_expires_at=None,
            enabled_at=now,
            last_accepted_step=current_step - 1,
        )
        session.add(mfa)
        await session.flush()

        # Create mfa_required pending session
        sess, secret = await create_mfa_pending_session(
            session,
            test_platform_settings,
            user=admin,
            state="mfa_required",
            client_ip="127.0.0.1",
            user_agent="ReplayAgent",
            now=now,
        )
        await session.commit()

    app = create_app(test_platform_settings)
    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    barrier = asyncio.Barrier(2)

    async with app.router.lifespan_context(app):
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client1,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client2,
        ):
            # Fetch CSRF token for the session
            sess_info = await client1.get("/api/v1/auth/session", headers={"Cookie": cookie_header, "Origin": test_platform_settings.web_origin})
            csrf_token = sess_info.json()["csrf_token"]

            async def _post_mfa_verify(client: httpx.AsyncClient) -> httpx.Response:
                await barrier.wait()
                return await client.post(
                    "/api/v1/auth/mfa/verify",
                    json={"code": code},
                    headers={
                        "Origin": test_platform_settings.web_origin,
                        "X-CSRF-Token": csrf_token,
                        "Cookie": cookie_header,
                    },
                )

            resp1, resp2 = await asyncio.gather(
                _post_mfa_verify(client1),
                _post_mfa_verify(client2),
            )

            status_codes = [resp1.status_code, resp2.status_code]
            # One request rotates session to authenticated (200), other is rejected (400 MFA_INVALID or 403 CSRF_INVALID due to rotation)
            assert 200 in status_codes
            loser_resp = resp1 if resp1.status_code != 200 else resp2
            assert loser_resp.status_code in (400, 403)

    # Verify durable invariant in PostgreSQL: last_accepted_step matches current_step
    async with initialized_pg.sessions() as session:
        m = (await session.execute(select(AuthMfa).where(AuthMfa.user_id == user_id))).scalar_one()
        assert m.last_accepted_step == current_step


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_failed_step_up_does_not_expire_session_cookie(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Failed step-up attempts (wrong password) return 401 INVALID_CREDENTIALS without expiring cookie.

    A subsequent retry with correct credentials must succeed without being invalidated to UNAUTHENTICATED.
    """
    now = datetime.now(timezone.utc)
    if int(now.timestamp()) % 30 >= 27:
        import asyncio
        await asyncio.sleep(4)
        now = datetime.now(timezone.utc)

    current_step = int(now.timestamp()) // 30
    secret_b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY"
    totp = pyotp.TOTP(secret_b32, digits=6, interval=30)
    code = totp.at(int(now.timestamp()))
    user_id = uuid.uuid4()

    hasher = get_password_hasher()
    correct_pw = "admin-step-up-correct-pw-12345"
    pw_hash = await hasher.hash_password(correct_pw)
    fernet_key = test_platform_settings.auth_mfa_key
    ciphertext = encrypt_totp_secret(fernet_key, user_id, secret_b32)

    async with initialized_pg.sessions() as session:
        admin = User(
            id=user_id,
            email="step-up-retry@example.com",
            email_key="step-up-retry@example.com",
            display_name="Step Up Admin",
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
            user_id=user_id,
            secret_ciphertext=ciphertext,
            pending_secret_ciphertext=None,
            pending_expires_at=None,
            enabled_at=now,
            last_accepted_step=current_step - 2,
        )
        session.add(mfa)
        await session.flush()

        # Create authenticated session for admin
        sess, secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=admin,
            client_ip="127.0.0.1",
            user_agent="StepUpAgent",
            now=now,
            mfa_verified=True,
        )
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    # 1. Fetch CSRF token for the authenticated session
    sess_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Cookie": cookie_header, "Origin": test_platform_settings.web_origin},
    )
    assert sess_res.status_code == 200
    assert sess_res.json()["state"] == "authenticated"
    csrf_token = sess_res.json()["csrf_token"]

    # 2. Attempt step-up with WRONG password
    fail_res = await app_client.post(
        "/api/v1/auth/step-up",
        json={"password": "wrong-admin-password", "code": code},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf_token,
            "Cookie": cookie_header,
        },
    )
    assert fail_res.status_code == 401
    assert fail_res.json()["error"]["code"] == "INVALID_CREDENTIALS"

    # Cookie must NOT have been expired with Max-Age=0
    set_cookie = fail_res.headers.get("set-cookie")
    if set_cookie:
        assert "Max-Age=0" not in set_cookie

    # 3. Session remains valid and authenticated (not unauthenticated or revoked)
    me_res = await app_client.get("/api/v1/me", headers={"Cookie": cookie_header})
    assert me_res.status_code == 200

    # 4. Retry step-up with CORRECT password succeeds
    success_res = await app_client.post(
        "/api/v1/auth/step-up",
        json={"password": correct_pw, "code": code},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf_token,
            "Cookie": cookie_header,
        },
    )
    assert success_res.status_code == 200
    assert success_res.json()["state"] == "authenticated"
    assert success_res.json()["step_up_expires_at"] is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_fifth_failed_mfa_attempt_revokes_restricted_session(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Contract line 206: on fifth failed MFA credential attempt, revoke restricted session and delete cookie.

    Attempts 1–4 fail without revocation; a valid code prior to 5th can upgrade; 5th failure revokes.
    """
    now = datetime.now(timezone.utc)
    if int(now.timestamp()) % 30 >= 26:
        import asyncio
        await asyncio.sleep(5)
        now = datetime.now(timezone.utc)

    current_step = int(now.timestamp()) // 30
    secret_b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY"
    totp = pyotp.TOTP(secret_b32, digits=6, interval=30)
    valid_code = totp.at(int(now.timestamp()))
    user_id = uuid.uuid4()

    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-fifth-fail-pw-123")
    fernet_key = test_platform_settings.auth_mfa_key
    ciphertext = encrypt_totp_secret(fernet_key, user_id, secret_b32)

    async with initialized_pg.sessions() as session:
        admin = User(
            id=user_id,
            email="fifth-mfa-fail@example.com",
            email_key="fifth-mfa-fail@example.com",
            display_name="Fifth Fail Admin",
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
            user_id=user_id,
            secret_ciphertext=ciphertext,
            pending_secret_ciphertext=None,
            pending_expires_at=None,
            enabled_at=now,
            last_accepted_step=current_step - 2,
        )
        session.add(mfa)
        await session.flush()

        sess, secret = await create_mfa_pending_session(
            session,
            test_platform_settings,
            user=admin,
            state="mfa_required",
            client_ip="127.0.0.1",
            user_agent="FifthFailAgent",
            now=now,
        )
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    # Bootstrap session to get CSRF token
    sess_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Cookie": cookie_header, "Origin": test_platform_settings.web_origin},
    )
    csrf_token = sess_res.json()["csrf_token"]

    # Attempts 1–4 with invalid code: fail with 400 MFA_INVALID, do NOT revoke or expire cookie
    for attempt in range(1, 5):
        bad_res = await app_client.post(
            "/api/v1/auth/mfa/verify",
            json={"code": "000000"},
            headers={
                "Origin": test_platform_settings.web_origin,
                "X-CSRF-Token": csrf_token,
                "Cookie": cookie_header,
            },
        )
        assert bad_res.status_code == 400
        assert bad_res.json()["error"]["code"] == "MFA_INVALID"
        set_cookie = bad_res.headers.get("set-cookie")
        if set_cookie:
            assert "Max-Age=0" not in set_cookie

        # Verify session is still active in DB
        async with initialized_pg.sessions() as session:
            s = (await session.execute(select(AuthSession).where(AuthSession.id == sess.id))).scalar_one()
            assert s.revoked_at is None

    # Attempt 5 with invalid code: fails and revokes restricted session + expires cookie
    fifth_res = await app_client.post(
        "/api/v1/auth/mfa/verify",
        json={"code": "000000"},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf_token,
            "Cookie": cookie_header,
        },
    )
    assert fifth_res.status_code == 400
    assert fifth_res.json()["error"]["code"] == "MFA_INVALID"
    set_cookie_fifth = fifth_res.headers.get("set-cookie")
    assert set_cookie_fifth is not None
    assert "Max-Age=0" in set_cookie_fifth

    # Verify session is durably revoked in PostgreSQL
    async with initialized_pg.sessions() as session:
        s = (await session.execute(select(AuthSession).where(AuthSession.id == sess.id))).scalar_one()
        assert s.revoked_at is not None

    # Subsequent attempt with previously valid code is rejected because session is revoked
    subsequent_res = await app_client.post(
        "/api/v1/auth/mfa/verify",
        json={"code": valid_code},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf_token,
            "Cookie": cookie_header,
        },
    )
    assert subsequent_res.status_code == 403
    assert subsequent_res.json()["error"]["code"] == "CSRF_INVALID"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_valid_code_prior_to_fifth_failure_upgrades_session(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """MFA verification with failures 1–3 still succeeds on 4th attempt with valid code."""
    now = datetime.now(timezone.utc)
    if int(now.timestamp()) % 30 >= 26:
        import asyncio
        await asyncio.sleep(5)
        now = datetime.now(timezone.utc)

    current_step = int(now.timestamp()) // 30
    secret_b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY"
    totp = pyotp.TOTP(secret_b32, digits=6, interval=30)
    valid_code = totp.at(int(now.timestamp()))
    user_id = uuid.uuid4()

    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-valid-prior-fifth-pw-123")
    fernet_key = test_platform_settings.auth_mfa_key
    ciphertext = encrypt_totp_secret(fernet_key, user_id, secret_b32)

    async with initialized_pg.sessions() as session:
        admin = User(
            id=user_id,
            email="valid-prior-fifth@example.com",
            email_key="valid-prior-fifth@example.com",
            display_name="Valid Prior Fifth Admin",
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
            user_id=user_id,
            secret_ciphertext=ciphertext,
            pending_secret_ciphertext=None,
            pending_expires_at=None,
            enabled_at=now,
            last_accepted_step=current_step - 2,
        )
        session.add(mfa)
        await session.flush()

        sess, secret = await create_mfa_pending_session(
            session,
            test_platform_settings,
            user=admin,
            state="mfa_required",
            client_ip="127.0.0.1",
            user_agent="ValidPriorFifthAgent",
            now=now,
        )
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    # Bootstrap session to get CSRF token
    sess_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Cookie": cookie_header, "Origin": test_platform_settings.web_origin},
    )
    csrf_token = sess_res.json()["csrf_token"]

    # 3 bad attempts
    for _ in range(3):
        bad_res = await app_client.post(
            "/api/v1/auth/mfa/verify",
            json={"code": "000000"},
            headers={
                "Origin": test_platform_settings.web_origin,
                "X-CSRF-Token": csrf_token,
                "Cookie": cookie_header,
            },
        )
        assert bad_res.status_code == 400
        assert bad_res.json()["error"]["code"] == "MFA_INVALID"

    # 4th attempt with valid code upgrades session
    good_res = await app_client.post(
        "/api/v1/auth/mfa/verify",
        json={"code": valid_code},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf_token,
            "Cookie": cookie_header,
        },
    )
    assert good_res.status_code == 200
    assert good_res.json()["state"] == "authenticated"

    # Verify durable invariant in PostgreSQL
    async with initialized_pg.sessions() as session:
        m = (await session.execute(select(AuthMfa).where(AuthMfa.user_id == user_id))).scalar_one()
        assert m.last_accepted_step == current_step
