"""Core authentication, registration, password lifecycle, and session orchestration.

Follows auth-contract.md:
- Strict email normalization via email-validator (no DNS deliverability checks).
- Generic Accepted responses for anti-enumeration.
- Password selection only upon proof of token possession.
- Atomic token consumption and user creation with initialize_user_account.
- Password reset increments auth_version and revokes all sessions across all tabs.
- Bounded scrypt verification and fixed dummy hash.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Literal
import uuid

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.accounts.service import initialize_user_account
from platform_api.audit import record_audit
from platform_api.auth.csrf import generate_csrf_token
from platform_api.auth.delivery import enqueue_auth_message
from platform_api.auth.errors import (
    InvalidCredentialsError,
    TokenInvalidError,
    ValidationError,
)
from platform_api.auth.passwords import (
    get_password_hasher,
    validate_password_rules,
)
from platform_api.auth.rate_limits import check_and_increment_rate_limit
from platform_api.auth.schemas import (
    Accepted,
    SessionResponse,
    UserView,
)
from platform_api.auth.sessions import (
    create_authenticated_session,
    generate_session_secret,
    revoke_all_user_sessions,
    revoke_session,
)
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthSession, AuthToken, User


def normalize_email_address(raw_email: str) -> tuple[str, str]:
    """Normalize email address without performing DNS lookups.

    Returns (display_email, email_key) where email_key is fully lowercased ASCII.
    """
    trimmed = raw_email.strip()
    try:
        info = validate_email(
            trimmed,
            check_deliverability=False,
            allow_smtputf8=False,
        )
        display_email = info.normalized
        email_key = display_email.lower()
        return display_email, email_key
    except EmailNotValidError as e:
        raise ValidationError(
            "Invalid email address format",
            fields=[{"field": "email", "code": "invalid_email"}],
        ) from e


def validate_display_name(raw_name: str) -> str:
    """Validate display name rules: 1–80 codepoints, no control characters."""
    trimmed = raw_name.strip()
    if len(trimmed) < 1:
        raise ValidationError(
            "Display name cannot be empty",
            fields=[{"field": "display_name", "code": "too_short"}],
        )
    if len(trimmed) > 80:
        raise ValidationError(
            "Display name cannot exceed 80 characters",
            fields=[{"field": "display_name", "code": "too_long"}],
        )
    for c in trimmed:
        if ord(c) < 32 or ord(c) == 127:
            raise ValidationError(
                "Display name cannot contain control characters",
                fields=[{"field": "display_name", "code": "control_characters"}],
            )
    return trimmed


def build_user_view(user: User, mfa_enabled: bool = False) -> UserView:
    """Construct safe UserView from User model."""
    return UserView(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,  # type: ignore[arg-type]
        status=user.status,  # type: ignore[arg-type]
        email_verified_at=user.email_verified_at,
        preferred_language=user.preferred_language,  # type: ignore[arg-type]
        timezone=user.timezone,
        low_balance_notifications=user.low_balance_notifications,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def generate_token_pair() -> tuple[str, bytes]:
    """Generate 32 random bytes token and its SHA-256 digest."""
    raw_bytes = secrets.token_bytes(32)
    b64_token = base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")
    token_digest = hashlib.sha256(raw_bytes).digest()
    return b64_token, token_digest


def hash_token(raw_token: str) -> bytes | None:
    """Decode unpadded base64url token and compute SHA-256 digest."""
    if not raw_token:
        return None
    raw = raw_token.strip()
    pad = len(raw) % 4
    if pad:
        raw += "=" * (4 - pad)
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception:
        return None
    if len(decoded) != 32:
        return None
    return hashlib.sha256(decoded).digest()


async def register_account(
    db: AsyncSession,
    settings: PlatformSettings,
    *,
    email_raw: str,
    display_name_raw: str,
    client_ip: str | None,
    now: datetime,
) -> Accepted:
    """Initiate registration. Returns 202 Accepted unconditionally."""
    display_email, email_key = normalize_email_address(email_raw)
    display_name = validate_display_name(display_name_raw)

    # 1. Enforce rate limits
    if client_ip:
        await check_and_increment_rate_limit(
            db,
            settings,
            scope="register_ip",
            identifier=client_ip,
            limit=settings.auth_rate_mail_ip_per_hour,
            window_seconds=3600,
            now=now,
        )
    await check_and_increment_rate_limit(
        db,
        settings,
        scope="register_email",
        identifier=email_key,
        limit=settings.auth_rate_mail_email_per_hour,
        window_seconds=3600,
        now=now,
    )
    # Mail resend rate limit (1 / minute)
    await check_and_increment_rate_limit(
        db,
        settings,
        scope="mail_resend_register",
        identifier=email_key,
        limit=1,
        window_seconds=settings.auth_mail_resend_seconds,
        now=now,
    )

    # 2. Check if user already exists
    user_stmt = select(User).where(User.email_key == email_key)
    user_res = await db.execute(user_stmt)
    existing_user = user_res.scalar_one_or_none()
    if existing_user is not None:
        # Existing user: do nothing, return 202 to prevent enumeration
        return Accepted()

    # 3. Invalidate previous pending register tokens for this email
    invalidate_stmt = (
        update(AuthToken)
        .where(
            AuthToken.email_key == email_key,
            AuthToken.purpose == "register",
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    await db.execute(invalidate_stmt)

    # 4. Generate fresh registration token
    raw_token, token_digest = generate_token_pair()
    expires_at = now + timedelta(seconds=settings.auth_register_ttl_seconds)

    token_record = AuthToken(
        id=uuid.uuid4(),
        purpose="register",
        token_digest=token_digest,
        email_key=email_key,
        user_id=None,
        display_name=display_name,
        created_at=now,
        expires_at=expires_at,
        consumed_at=None,
    )
    db.add(token_record)
    await db.flush()

    # 5. Enqueue durable mail
    await enqueue_auth_message(
        db,
        settings,
        token_id=token_record.id,
        email=display_email,
        purpose="register",
        token=raw_token,
        expires_at=expires_at,
        display_name=display_name,
        now=now,
    )

    return Accepted()


async def verify_email_and_create_user(
    db: AsyncSession,
    settings: PlatformSettings,
    *,
    token_str: str,
    password_str: str,
    client_ip: str | None,
    now: datetime,
) -> UserView:
    """Verify registration token, choose password, and initialize user + wallet."""
    # Enforce confirm rate limit
    if client_ip:
        await check_and_increment_rate_limit(
            db,
            settings,
            scope="confirm_ip",
            identifier=client_ip,
            limit=settings.auth_rate_confirm_ip_per_15_minutes,
            window_seconds=900,
            now=now,
        )

    # Validate password rules before token consumption
    validate_password_rules(password_str)

    token_digest = hash_token(token_str)
    if token_digest is None:
        raise TokenInvalidError("Invalid or expired registration token")

    # Lock token row to prevent concurrent consumption races
    token_stmt = (
        select(AuthToken)
        .where(
            AuthToken.token_digest == token_digest,
            AuthToken.purpose == "register",
            AuthToken.consumed_at.is_(None),
            AuthToken.expires_at > now,
        )
        .with_for_update()
    )
    result = await db.execute(token_stmt)
    token_record = result.scalar_one_or_none()
    if token_record is None:
        raise TokenInvalidError("Invalid or expired registration token")

    # Check if a user with this email_key was created in the meantime
    user_stmt = select(User).where(User.email_key == token_record.email_key).with_for_update()
    user_res = await db.execute(user_stmt)
    if user_res.scalar_one_or_none() is not None:
        token_record.consumed_at = now
        await db.flush()
        raise TokenInvalidError("An account with this email already exists")

    # Mark token consumed
    token_record.consumed_at = now

    # Hash chosen password
    hasher = get_password_hasher(settings.auth_hash_concurrency)
    password_hash = await hasher.hash_password(password_str)

    # Create user
    new_user = User(
        id=uuid.uuid4(),
        email=token_record.email_key,
        email_key=token_record.email_key,
        display_name=token_record.display_name or "User",
        password_hash=password_hash,
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
    db.add(new_user)
    await db.flush()

    # Link user to token record
    token_record.user_id = new_user.id

    # Initialize wallet account (zero credit)
    await initialize_user_account(db, user_id=new_user.id, now=now)

    # Record security audit
    await record_audit(
        db,
        actor_kind="user",
        actor_id=new_user.id,
        action="user.registered",
        target_type="user",
        target_id=new_user.id,
        source_ip=client_ip,
        result="success",
        created_at=now,
    )

    return build_user_view(new_user, mfa_enabled=False)


async def request_password_reset(
    db: AsyncSession,
    settings: PlatformSettings,
    *,
    email_raw: str,
    client_ip: str | None,
    now: datetime,
) -> Accepted:
    """Initiate password reset. Always returns 202 Accepted."""
    display_email, email_key = normalize_email_address(email_raw)

    if client_ip:
        await check_and_increment_rate_limit(
            db,
            settings,
            scope="reset_ip",
            identifier=client_ip,
            limit=settings.auth_rate_mail_ip_per_hour,
            window_seconds=3600,
            now=now,
        )
    await check_and_increment_rate_limit(
        db,
        settings,
        scope="reset_email",
        identifier=email_key,
        limit=settings.auth_rate_mail_email_per_hour,
        window_seconds=3600,
        now=now,
    )
    await check_and_increment_rate_limit(
        db,
        settings,
        scope="mail_resend_reset",
        identifier=email_key,
        limit=1,
        window_seconds=settings.auth_mail_resend_seconds,
        now=now,
    )

    user_stmt = select(User).where(User.email_key == email_key)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()

    # If unknown or suspended: do not generate token; return accepted
    if user is None or user.status != "active":
        return Accepted()

    # Invalidate existing reset tokens for this user
    invalidate_stmt = (
        update(AuthToken)
        .where(
            AuthToken.email_key == email_key,
            AuthToken.purpose == "password_reset",
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    await db.execute(invalidate_stmt)

    raw_token, token_digest = generate_token_pair()
    expires_at = now + timedelta(seconds=settings.auth_reset_ttl_seconds)

    token_record = AuthToken(
        id=uuid.uuid4(),
        purpose="password_reset",
        token_digest=token_digest,
        email_key=email_key,
        user_id=user.id,
        display_name=user.display_name,
        created_at=now,
        expires_at=expires_at,
        consumed_at=None,
    )
    db.add(token_record)
    await db.flush()

    await enqueue_auth_message(
        db,
        settings,
        token_id=token_record.id,
        email=user.email,
        purpose="password_reset",
        token=raw_token,
        expires_at=expires_at,
        display_name=user.display_name,
        now=now,
    )

    return Accepted()


async def confirm_password_reset(
    db: AsyncSession,
    settings: PlatformSettings,
    *,
    token_str: str,
    password_str: str,
    client_ip: str | None,
    now: datetime,
) -> None:
    """Consume reset token, set new hash, bump auth_version, and revoke all sessions."""
    if client_ip:
        await check_and_increment_rate_limit(
            db,
            settings,
            scope="confirm_ip",
            identifier=client_ip,
            limit=settings.auth_rate_confirm_ip_per_15_minutes,
            window_seconds=900,
            now=now,
        )

    validate_password_rules(password_str)

    token_digest = hash_token(token_str)
    if token_digest is None:
        raise TokenInvalidError("Invalid or expired password reset token")

    token_stmt = (
        select(AuthToken)
        .where(
            AuthToken.token_digest == token_digest,
            AuthToken.purpose == "password_reset",
            AuthToken.consumed_at.is_(None),
            AuthToken.expires_at > now,
        )
        .with_for_update()
    )
    result = await db.execute(token_stmt)
    token_record = result.scalar_one_or_none()
    if token_record is None or token_record.user_id is None:
        raise TokenInvalidError("Invalid or expired password reset token")

    user_stmt = select(User).where(User.id == token_record.user_id).with_for_update()
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if user is None:
        token_record.consumed_at = now
        await db.flush()
        raise TokenInvalidError("Account not found")

    # Consume token
    token_record.consumed_at = now

    # Invalidate all remaining reset tokens for this user
    await db.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user.id,
            AuthToken.purpose == "password_reset",
            AuthToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )

    # Hash new password
    hasher = get_password_hasher(settings.auth_hash_concurrency)
    password_hash = await hasher.hash_password(password_str)

    # Update user: bump auth_version to invalidate all existing sessions
    user.password_hash = password_hash
    user.auth_version += 1
    user.updated_at = now

    # Revoke all sessions across all tabs and devices
    await revoke_all_user_sessions(db, user.id, now)

    # Record security audit
    await record_audit(
        db,
        actor_kind="user",
        actor_id=user.id,
        action="password.reset",
        target_type="user",
        target_id=user.id,
        source_ip=client_ip,
        result="success",
        created_at=now,
    )
