"""Serialized admission-policy checks shared by owner and guest calls."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.errors import ConflictError
from platform_api.models.admission_pause import AdmissionPause


class AdmissionPausedError(ConflictError):
    code = "ADMISSION_PAUSED"
    message = "New sessions are temporarily paused."


def _admission_pause_lock_id(scope: str, scope_id: uuid.UUID | None) -> int:
    if scope == "global":
        if scope_id is not None:
            raise ValueError("Global admission-pause scope cannot have an ID.")
        target = b"global"
    elif scope in {"user", "agent"}:
        if scope_id is None:
            raise ValueError(f"{scope} admission-pause scope requires an ID.")
        target = scope_id.bytes
    else:
        raise ValueError("Admission-pause scope must be global, user, or agent.")
    digest = hashlib.sha256(b"voice-ai/admission-pause/v1\0" + scope.encode() + b"\0" + target).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def lock_admission_pause_scope(
    db: AsyncSession,
    *,
    scope: str,
    scope_id: uuid.UUID | None,
    shared: bool = False,
) -> None:
    """Lock one policy scope, including when the corresponding row is absent."""
    lock_id = _admission_pause_lock_id(scope, scope_id)
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await db.execute(text(f"SELECT {function}(:lock_id)"), {"lock_id": lock_id})


async def check_admission_pause(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """Block admission if a global, owner, or selected-agent policy is paused.

    Shared transaction locks let admissions proceed concurrently while making a
    pause mutation wait for every in-flight admission check on the same scope.
    """
    await lock_admission_pause_scope(db, scope="global", scope_id=None, shared=True)
    await lock_admission_pause_scope(
        db, scope="user", scope_id=owner_user_id, shared=True
    )
    await lock_admission_pause_scope(
        db, scope="agent", scope_id=agent_id, shared=True
    )

    paused_id = await db.scalar(
        select(AdmissionPause.id)
        .where(
            AdmissionPause.paused.is_(True),
            or_(
                AdmissionPause.scope == "global",
                and_(
                    AdmissionPause.scope == "user",
                    AdmissionPause.owner_user_id == owner_user_id,
                ),
                and_(
                    AdmissionPause.scope == "agent",
                    AdmissionPause.agent_id == agent_id,
                ),
            ),
        )
        .limit(1)
    )
    if paused_id is not None:
        raise AdmissionPausedError()
