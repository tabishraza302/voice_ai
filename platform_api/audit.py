"""Append-only transaction audit service for security and operator events.

Follows auth-contract.md:
- Participates in the caller's transaction. Audit failure aborts the mutation.
- Enforces append-only semantics. Never copies secrets, tokens, or bodies into audit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.models.auth import AuditLog

REDACTED_KEYS = {
    "password",
    "current_password",
    "new_password",
    "token",
    "secret",
    "secret_ciphertext",
    "pending_secret_ciphertext",
    "code",
    "csrf_token",
    "cookie",
}


def redact_change_summary(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """Recursively redact sensitive keys from audit summaries."""
    if summary is None:
        return None
    redacted: dict[str, Any] = {}
    for k, v in summary.items():
        if k.lower() in REDACTED_KEYS:
            redacted[k] = "[REDACTED]"
        elif isinstance(v, dict):
            redacted[k] = redact_change_summary(v)
        elif isinstance(v, list):
            redacted[k] = [
                redact_change_summary(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            redacted[k] = v
    return redacted


async def record_audit(
    db: AsyncSession,
    *,
    actor_kind: str,
    action: str,
    target_type: str,
    result: str,
    actor_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    reason: str | None = None,
    request_id: uuid.UUID | None = None,
    source_ip: str | None = None,
    change_summary: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> AuditLog:
    """Record an append-only audit event within the active transaction."""
    entry = AuditLog(
        id=uuid.uuid4(),
        actor_kind=actor_kind,
        actor_id=actor_id,
        session_id=session_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        request_id=request_id,
        source_ip=source_ip,
        result=result,
        change_summary=redact_change_summary(change_summary),
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(entry)
    await db.flush()
    return entry
