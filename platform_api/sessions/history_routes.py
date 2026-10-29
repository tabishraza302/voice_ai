"""Owner-only APIs for conversation lists, pinned config, transcripts, and usage."""

from __future__ import annotations
from datetime import datetime
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.dependencies import require_user
from platform_api.auth.types import Principal
from platform_api.db.session import get_db
from platform_api.sessions.history import (
    get_owner_config_snapshot,
    get_owner_transcript_page,
    list_owner_sessions,
    list_owner_usage,
)
from platform_api.sessions.history_schemas import (
    ConfigSnapshotView,
    OwnerSessionsPage,
    OwnerUsagePage,
    TranscriptPageView,
)


router = APIRouter(tags=["session_history"])


@router.get("/sessions", response_model=OwnerSessionsPage)
async def get_owner_sessions(
    response: Response,
    state: str | None = Query(default=None, min_length=1, max_length=32),
    kind: Literal["voice", "chat", "voice_test"] | None = Query(default=None),
    agent_version_id: uuid.UUID | None = Query(default=None),
    from_at: datetime | None = Query(default=None),
    to_at: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> OwnerSessionsPage:
    """List only the authenticated owner's sessions with stable pagination."""
    response.headers["Cache-Control"] = "no-store"
    return await list_owner_sessions(
        db,
        owner_user_id=principal.user_id,
        state=state,
        kind=kind,
        agent_version_id=agent_version_id,
        from_at=from_at,
        to_at=to_at,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/sessions/{session_id}/config-snapshot",
    response_model=ConfigSnapshotView,
)
async def get_owner_session_config_snapshot(
    session_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ConfigSnapshotView:
    """Return the exact immutable agent and tariff snapshots pinned to a session."""
    response.headers["Cache-Control"] = "no-store"
    return await get_owner_config_snapshot(
        db,
        owner_user_id=principal.user_id,
        session_id=session_id,
    )


@router.get(
    "/sessions/{session_id}/transcript",
    response_model=TranscriptPageView,
)
async def get_owner_session_transcript(
    session_id: uuid.UUID,
    response: Response,
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> TranscriptPageView:
    """Read the owner's revision-bound Task 8 transcript projection."""
    response.headers["Cache-Control"] = "no-store"
    return await get_owner_transcript_page(
        db,
        owner_user_id=principal.user_id,
        session_id=session_id,
        cursor=cursor,
        limit=limit,
    )


@router.get("/usage", response_model=OwnerUsagePage)
async def get_owner_usage(
    response: Response,
    state: str | None = Query(default=None, min_length=1, max_length=32),
    kind: Literal["voice", "chat", "voice_test"] | None = Query(default=None),
    agent_version_id: uuid.UUID | None = Query(default=None),
    from_at: datetime | None = Query(default=None),
    to_at: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> OwnerUsagePage:
    """List the owner's actual billed and reserved budget facts per session."""
    response.headers["Cache-Control"] = "no-store"
    return await list_owner_usage(
        db,
        owner_user_id=principal.user_id,
        state=state,
        kind=kind,
        agent_version_id=agent_version_id,
        from_at=from_at,
        to_at=to_at,
        cursor=cursor,
        limit=limit,
    )
