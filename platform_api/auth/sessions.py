"""Session lifecycle, opaque cookie management, and state transitions.

Follows auth-contract.md:
- 32-byte random secrets stored only as SHA-256 digests in PostgreSQL.
- Unpadded base64url cookie encoding (43 characters).
- Cookie name: __Host-platform_session (production) or platform_dev_session (insecure dev).
- Authenticated session capped at 5 per user via locked eviction of oldest.
- Activity touch renews idle deadline at most once per touch interval.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import uuid

from fastapi import Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthSession, User


def generate_session_secret() -> tuple[str, bytes]:
    """Generate 32 random bytes.

    Returns (unpadded_b64url_secret, sha256_digest_bytes).
    """
    raw_bytes = secrets.token_bytes(32)
    b64_secret = base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")
    token_digest = hashlib.sha256(raw_bytes).digest()
    return b64_secret, token_digest


def hash_session_secret(raw_cookie_secret: str) -> bytes | None:
    """Decode raw cookie secret and compute SHA-256 digest.

    Returns None if malformed (never raises).
    """
    if not raw_cookie_secret:
        return None
    raw = raw_cookie_secret.strip()
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


def get_cookie_name(settings: PlatformSettings) -> str:
    """Return appropriate session cookie name based on configuration."""
    if settings.cookie_secure:
        return "__Host-platform_session"
    return settings.session_cookie_name


def set_session_cookie(
    response: Response,
    settings: PlatformSettings,
    raw_secret: str,
    max_age_seconds: int,
) -> None:
    """Set the session cookie with exact security attributes."""
    cookie_name = get_cookie_name(settings)
    max_age = max(0, int(max_age_seconds))

    response.set_cookie(
        key=cookie_name,
        value=raw_secret,
        max_age=max_age,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def delete_session_cookie(response: Response, settings: PlatformSettings) -> None:
    """Expire the session cookie immediately."""
    cookie_name = get_cookie_name(settings)
    response.set_cookie(
        key=cookie_name,
        value="",
        max_age=0,
        expires="Thu, 01 Jan 1970 00:00:00 GMT",
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def sanitize_user_agent(user_agent: str | None) -> str:
    """Sanitize and bound User-Agent header for audit/device list."""
    if not user_agent:
        return "Unknown Device"
    # Take first 160 characters and remove control characters
    clean = "".join(c for c in user_agent if c.isprintable()).strip()
    return clean[:160] or "Unknown Device"


async def create_anonymous_session(
    db: AsyncSession,
    settings: PlatformSettings,
    *,
    client_ip: str | None,
    user_agent: str | None,
    now: datetime,
) -> tuple[AuthSession, str]:
    """Create a new anonymous session with anonymous TTL."""
    raw_secret, token_digest = generate_session_secret()
    ttl = timedelta(seconds=settings.auth_anonymous_ttl_seconds)

    session = AuthSession(
        id=uuid.uuid4(),
        token_digest=token_digest,
        user_id=None,
        auth_version=None,
        state="anonymous",
        user_agent_label=sanitize_user_agent(user_agent),
        created_ip=client_ip,
        created_at=now,
        last_seen_at=now,
        idle_expires_at=now + ttl,
        absolute_expires_at=now + ttl,
        revoked_at=None,
    )
    db.add(session)
    await db.flush()
    return session, raw_secret



async def create_authenticated_session(
    db: AsyncSession,
    settings: PlatformSettings,
    *,
    user: User,
    client_ip: str | None,
    user_agent: str | None,
    now: datetime,
    mfa_verified: bool = False,
) -> tuple[AuthSession, str]:
    """Create full authenticated session, evicting oldest if >= max_sessions."""
    # User-row lock to prevent concurrent sessions exceeding cap
    await db.execute(
        select(User.id).where(User.id == user.id).with_for_update()
    )

    # Find active authenticated sessions for this user
    active_stmt = (
        select(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.state == "authenticated",
            AuthSession.revoked_at.is_(None),
            AuthSession.idle_expires_at > now,
            AuthSession.absolute_expires_at > now,
        )
        .order_by(AuthSession.created_at.asc())
    )
    active_result = await db.execute(active_stmt)
    active_sessions = list(active_result.scalars().all())

    # If at or exceeding cap, revoke oldest to keep room for the new session
    max_allowed = settings.auth_max_sessions
    if len(active_sessions) >= max_allowed:
        num_to_revoke = len(active_sessions) - max_allowed + 1
        for s in active_sessions[:num_to_revoke]:
            s.revoked_at = now

    raw_secret, token_digest = generate_session_secret()
    idle_ttl = timedelta(seconds=settings.auth_idle_ttl_seconds)
    abs_ttl = timedelta(seconds=settings.auth_session_ttl_seconds)


    session = AuthSession(
        id=uuid.uuid4(),
        token_digest=token_digest,
        user_id=user.id,
        auth_version=user.auth_version,
        state="authenticated",
        user_agent_label=sanitize_user_agent(user_agent),
        created_ip=client_ip,
        created_at=now,
        last_seen_at=now,
        idle_expires_at=now + idle_ttl,
        absolute_expires_at=now + abs_ttl,
        password_verified_at=now,
        mfa_verified_at=None,
        step_up_at=None,
        revoked_at=None,
    )
    db.add(session)
    await db.flush()
    return session, raw_secret


async def touch_session_activity(
    db: AsyncSession,
    settings: PlatformSettings,
    session: AuthSession,
    now: datetime,
) -> None:
    """Conditionally extend idle expiry if touch interval has elapsed."""
    if session.state != "authenticated" or session.revoked_at is not None:
        return

    # Only touch if touch interval elapsed
    if (now - session.last_seen_at).total_seconds() < settings.auth_idle_touch_seconds:
        return

    # Bounded by absolute expiry
    new_idle = min(
        now + timedelta(seconds=settings.auth_idle_ttl_seconds),
        session.absolute_expires_at,
    )
    session.last_seen_at = now
    session.idle_expires_at = new_idle
    await db.flush()


async def revoke_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    now: datetime,
) -> None:
    """Revoke a single session by ID."""
    stmt = (
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.execute(stmt)


async def revoke_all_user_sessions(
    db: AsyncSession,
    user_id: uuid.UUID,
    now: datetime,
) -> None:
    """Revoke all active sessions belonging to user_id."""
    stmt = (
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.execute(stmt)
