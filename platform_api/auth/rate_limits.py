"""PostgreSQL-backed atomic rate limit counters.

Follows auth-contract.md:
- Fixed-window counters with atomic upsert in auth_rate_limits table.
- Keys are HMAC-SHA256 digests under PLATFORM_AUTH_RATE_KEY.
- Never stores raw emails or unmasked identifiers in metrics or database rows.
- Separate dimensions apply independently (IP, email, session).
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from typing import Literal
import uuid

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.errors import RateLimitedError
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthRateLimit


def compute_rate_key_digest(rate_key_b64: str, identifier: str) -> bytes:
    """Compute HMAC-SHA256 digest of normalized rate limit identifier."""
    raw = rate_key_b64.strip()
    pad = len(raw) % 4
    if pad:
        raw += "=" * (4 - pad)
    key_bytes = base64.urlsafe_b64decode(raw.encode("ascii"))
    return hmac.new(key_bytes, identifier.encode("utf-8"), hashlib.sha256).digest()


def _get_window_start(now: datetime, window_seconds: int) -> datetime:
    """Calculate the aligned start time for a fixed window."""
    now_epoch = int(now.timestamp())
    window_epoch = (now_epoch // window_seconds) * window_seconds
    return datetime.fromtimestamp(window_epoch, tz=timezone.utc)


async def check_and_increment_rate_limit(
    db: AsyncSession,
    settings: PlatformSettings,
    *,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int,
    now: datetime,
) -> None:
    """Atomically record attempt in PostgreSQL and raise RateLimitedError on breach.

    Returns seconds remaining in the current window when limit is exceeded.
    """
    key_digest = compute_rate_key_digest(settings.auth_rate_key, identifier)
    window_start = _get_window_start(now, window_seconds)
    window_expires = window_start + timedelta(seconds=window_seconds)
    retry_after = max(1, int((window_expires - now).total_seconds()))

    stmt = (
        insert(AuthRateLimit)
        .values(
            id=uuid.uuid4(),
            scope=scope,
            key_digest=key_digest,
            window_start=window_start,
            count=1,
            expires_at=window_expires,
        )
        .on_conflict_do_update(
            constraint="uq_auth_rate_limits_window",
            set_={"count": AuthRateLimit.count + 1},
        )
        .returning(AuthRateLimit.count)
    )

    bind = getattr(db, "bind", None) or db.get_bind()
    async with AsyncSession(bind, expire_on_commit=False) as rate_session:
        result = await rate_session.execute(stmt)
        current_count = result.scalar_one()
        await rate_session.commit()

    if current_count > limit:
        raise RateLimitedError(
            f"Rate limit exceeded for {scope}; please retry later",
            retry_after=retry_after,
        )


async def record_rate_limit_failure(
    db: AsyncSession,
    settings: PlatformSettings,
    *,
    scope: str,
    identifier: str,
    window_seconds: int,
    now: datetime,
) -> int:
    """Atomically record a failed attempt in PostgreSQL in an independent transaction.

    Returns the updated failure count in the current window.
    """
    key_digest = compute_rate_key_digest(settings.auth_rate_key, identifier)
    window_start = _get_window_start(now, window_seconds)
    window_expires = window_start + timedelta(seconds=window_seconds)

    stmt = (
        insert(AuthRateLimit)
        .values(
            id=uuid.uuid4(),
            scope=scope,
            key_digest=key_digest,
            window_start=window_start,
            count=1,
            expires_at=window_expires,
        )
        .on_conflict_do_update(
            constraint="uq_auth_rate_limits_window",
            set_={"count": AuthRateLimit.count + 1},
        )
        .returning(AuthRateLimit.count)
    )

    bind = getattr(db, "bind", None) or db.get_bind()
    async with AsyncSession(bind, expire_on_commit=False) as rate_session:
        result = await rate_session.execute(stmt)
        current_count = result.scalar_one()
        await rate_session.commit()

    return current_count
