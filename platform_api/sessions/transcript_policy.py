"""Transaction locks for revisioned transcript visibility and correction reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.models.transcript_policy import TranscriptPolicy


@dataclass(frozen=True, slots=True)
class TranscriptPolicyState:
    visible_to_owner: bool
    revision: int
    updated_at: datetime | None


def _transcript_policy_lock_id(session_id: uuid.UUID) -> int:
    digest = hashlib.sha256(
        b"voice-ai/transcript-policy/v1\0" + session_id.bytes
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def lock_transcript_policy(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    shared: bool,
) -> None:
    """Serialize reads and writes even before a policy row exists."""
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await db.execute(
        text(f"SELECT {function}(:lock_id)"),
        {"lock_id": _transcript_policy_lock_id(session_id)},
    )


async def read_transcript_policy(
    db: AsyncSession,
    session_id: uuid.UUID,
) -> TranscriptPolicyState:
    row = await db.get(TranscriptPolicy, session_id)
    if row is None:
        return TranscriptPolicyState(
            visible_to_owner=True,
            revision=0,
            updated_at=None,
        )
    return TranscriptPolicyState(
        visible_to_owner=row.visible_to_owner,
        revision=row.revision,
        updated_at=row.updated_at,
    )
