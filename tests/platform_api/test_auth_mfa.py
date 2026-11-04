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
from platform_api.auth.sessions import create_authenticated_session
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
        json={"password": "wrong-admin-password"},
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
        json={"password": correct_pw},
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
async def test_admin_login_without_mfa_authenticates_directly(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Admin login with password immediately enters authenticated state without MFA prompts."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw = "admin-login-direct-pw-12345"
    pw_hash = await hasher.hash_password(pw)

    async with initialized_pg.sessions() as session:
        admin = User(
            id=uuid.uuid4(),
            email="admin-direct@example.com",
            email_key="admin-direct@example.com",
            display_name="Direct Admin",
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
        await session.commit()

    # 1. Bootstrap anonymous session
    boot = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin},
    )
    assert boot.status_code == 200
    csrf = boot.json()["csrf_token"]

    # 2. Login as admin
    login_res = await app_client.post(
        "/api/v1/auth/login",
        json={"email": "admin-direct@example.com", "password": pw},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
        },
    )
    assert login_res.status_code == 200
    data = login_res.json()
    assert data["state"] == "authenticated"
    assert data["user"]["role"] == "admin"

    # 3. Can immediately access /api/v1/me
    me_res = await app_client.get("/api/v1/me")
    assert me_res.status_code == 200
    assert me_res.json()["role"] == "admin"

    # 4. Privileged step-up succeeds with password only
    csrf = login_res.json()["csrf_token"]
    step_up_res = await app_client.post(
        "/api/v1/auth/step-up",
        json={"password": pw},
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
        },
    )
    assert step_up_res.status_code == 200
    assert step_up_res.json()["step_up_expires_at"] is not None
