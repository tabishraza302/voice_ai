"""Multi-factor authentication (TOTP) and Fernet secret encryption.

Follows auth-contract.md:
- 160-bit random TOTP secrets encrypted with Fernet and bound to user_id.
- RFC 6238 TOTP with interval=30s, digits=6, HMAC-SHA1, accepting step ±1.
- Mandatory replay prevention: matching step must strictly exceed last_accepted_step.
- Concurrent submissions serialize on auth_mfa row lock.
- Plaintext secrets only in one-time MfaSetup response; never logged or audited.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import secrets
import uuid

from cryptography.fernet import Fernet
import pyotp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.errors import MfaInvalidError, MfaStateConflictError
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthMfa, User

TOTP_INTERVAL = 30
TOTP_DIGITS = 6
TOTP_SECRET_BYTES = 20  # 160 bits


def generate_totp_secret() -> str:
    """Generate 160-bit random secret formatted as unpadded Base32 string."""
    raw = secrets.token_bytes(TOTP_SECRET_BYTES)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def encrypt_totp_secret(fernet_key: str, user_id: uuid.UUID, secret_b32: str) -> str:
    """Encrypt TOTP secret bound to user_id using Fernet."""
    f = Fernet(fernet_key.encode("ascii"))
    payload = json.dumps({"user_id": str(user_id), "secret": secret_b32}).encode("utf-8")
    return f.encrypt(payload).decode("ascii")


def decrypt_totp_secret(fernet_key: str, user_id: uuid.UUID, ciphertext: str) -> str:
    """Decrypt TOTP secret and verify embedded user_id binding."""
    f = Fernet(fernet_key.encode("ascii"))
    try:
        decrypted = f.decrypt(ciphertext.encode("ascii"))
        payload = json.loads(decrypted.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Failed to decrypt TOTP secret: {e}") from e

    if payload.get("user_id") != str(user_id):
        raise ValueError("Decrypted TOTP secret does not match expected user binding")

    secret = payload.get("secret")
    if not secret:
        raise ValueError("Missing secret in decrypted TOTP payload")
    return secret


def get_provisioning_uri(secret_b32: str, email: str, issuer: str = "Voice AI") -> str:
    """Create otpauth:// URI for offline authenticators."""
    totp = pyotp.TOTP(secret_b32, digits=TOTP_DIGITS, interval=TOTP_INTERVAL)
    return totp.provisioning_uri(name=email, issuer_name=issuer)


async def get_or_create_mfa_record(db: AsyncSession, user_id: uuid.UUID) -> AuthMfa:
    """Load or initialize AuthMfa record under row lock."""
    stmt = select(AuthMfa).where(AuthMfa.user_id == user_id).with_for_update()
    result = await db.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        record = AuthMfa(
            user_id=user_id,
            secret_ciphertext=None,
            pending_secret_ciphertext=None,
            pending_expires_at=None,
            enabled_at=None,
            last_accepted_step=None,
        )
        db.add(record)
        await db.flush()
    return record


def _verify_totp_step(
    secret_b32: str,
    code: str,
    now: datetime,
    last_accepted_step: int | None,
) -> int:
    """Verify code against time window ±1 step.

    Returns the strictly newer matched step, or raises MfaInvalidError.
    """
    clean_code = code.strip()
    if len(clean_code) != 6 or not clean_code.isdigit():
        raise MfaInvalidError("Code must be 6 digits")

    totp = pyotp.TOTP(secret_b32, digits=TOTP_DIGITS, interval=TOTP_INTERVAL)
    now_ts = int(now.timestamp())
    current_step = now_ts // TOTP_INTERVAL

    matched_step: int | None = None
    # Check steps: current, current-1, current+1
    for step in (current_step, current_step - 1, current_step + 1):
        step_time = step * TOTP_INTERVAL
        expected_code = totp.at(step_time)
        if secrets.compare_digest(expected_code, clean_code):
            matched_step = step
            break

    if matched_step is None:
        raise MfaInvalidError("Invalid authentication code")

    # Replay protection: matching step must strictly exceed last accepted step
    if last_accepted_step is not None and matched_step <= last_accepted_step:
        raise MfaInvalidError("Authentication code has already been used")

    return matched_step


async def start_mfa_enrollment(
    db: AsyncSession,
    settings: PlatformSettings,
    user: User,
    now: datetime,
) -> tuple[str, str, datetime]:
    """Generate pending TOTP secret for admin enrollment.

    Returns (secret_b32, provisioning_uri, expires_at).
    """
    mfa = await get_or_create_mfa_record(db, user.id)
    if mfa.enabled_at is not None:
        raise MfaStateConflictError("MFA is already enabled on this account")

    secret_b32 = generate_totp_secret()
    ciphertext = encrypt_totp_secret(settings.auth_mfa_key, user.id, secret_b32)
    expires_at = now + timedelta(seconds=settings.auth_mfa_ttl_seconds)

    mfa.pending_secret_ciphertext = ciphertext
    mfa.pending_expires_at = expires_at
    await db.flush()

    uri = get_provisioning_uri(secret_b32, user.email)
    return secret_b32, uri, expires_at


async def confirm_mfa_enrollment(
    db: AsyncSession,
    settings: PlatformSettings,
    user: User,
    code: str,
    now: datetime,
) -> int:
    """Confirm pending TOTP enrollment under row lock.

    Returns the consumed step.
    """
    mfa = await get_or_create_mfa_record(db, user.id)
    if mfa.pending_secret_ciphertext is None or mfa.pending_expires_at is None:
        raise MfaInvalidError("No pending MFA enrollment found")

    if now > mfa.pending_expires_at:
        mfa.pending_secret_ciphertext = None
        mfa.pending_expires_at = None
        await db.flush()
        raise MfaInvalidError("Pending MFA enrollment has expired")

    secret_b32 = decrypt_totp_secret(
        settings.auth_mfa_key, user.id, mfa.pending_secret_ciphertext
    )

    matched_step = _verify_totp_step(
        secret_b32, code, now, mfa.last_accepted_step
    )

    # Enable factor and clear pending state
    mfa.secret_ciphertext = mfa.pending_secret_ciphertext
    mfa.pending_secret_ciphertext = None
    mfa.pending_expires_at = None
    mfa.enabled_at = now
    mfa.last_accepted_step = matched_step
    await db.flush()
    return matched_step


async def verify_mfa_login(
    db: AsyncSession,
    settings: PlatformSettings,
    user: User,
    code: str,
    now: datetime,
) -> int:
    """Verify code for an active admin login session under row lock.

    Returns the consumed step.
    """
    mfa = await get_or_create_mfa_record(db, user.id)
    if mfa.enabled_at is None or mfa.secret_ciphertext is None:
        raise MfaStateConflictError("MFA is not enabled for this user")

    secret_b32 = decrypt_totp_secret(
        settings.auth_mfa_key, user.id, mfa.secret_ciphertext
    )

    matched_step = _verify_totp_step(
        secret_b32, code, now, mfa.last_accepted_step
    )

    mfa.last_accepted_step = matched_step
    await db.flush()
    return matched_step
