"""Owner-scoped HTTP routes for snapshot-pinned Chat Test sessions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.dependencies import get_request_settings, require_csrf, require_user
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.catalog import ModelCatalog, catalog_with_managed_llms, load_catalog
from platform_api.config import PlatformSettings
from platform_api.db.session import get_db
from platform_api.testing.chat import get_chat_recovery, post_chat_turn
from platform_api.testing.schemas import (
    ChatRecoveryResponse,
    ChatTurnRequest,
    ChatTurnResponse,
)

router = APIRouter(tags=["agents"])


async def _set_chat_transaction_isolation(
    db: AsyncSession = Depends(get_db),
) -> None:
    """Ensure concurrent test turns use the documented PostgreSQL isolation."""
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))


async def _request_catalog(request: Request, settings: PlatformSettings, db: AsyncSession) -> ModelCatalog:
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        catalog = load_catalog(settings.catalog_path)
    return await catalog_with_managed_llms(catalog, db)


@router.post("/agents/{agent_id}/test/chat", response_model=ChatTurnResponse)
async def create_chat_turn(
    agent_id: uuid.UUID,
    payload: ChatTurnRequest,
    request: Request,
    response: Response,
    _transaction_isolation: None = Depends(_set_chat_transaction_isolation),
    principal: Principal = Depends(require_user),
    _ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> ChatTurnResponse:
    """Run one bounded text turn on a saved draft or immutable published snapshot."""
    settings = get_request_settings(request)
    response.headers["Cache-Control"] = "no-store"
    return await post_chat_turn(
        db,
        settings=settings,
        catalog=await _request_catalog(request, settings, db),
        model_client=getattr(request.app.state, "model_client", None),
        owner_user_id=principal.user_id,
        agent_id=agent_id,
        payload=payload,
    )


@router.get(
    "/agents/{agent_id}/test/chat/{test_id}",
    response_model=ChatRecoveryResponse,
)
async def get_chat_turns(
    agent_id: uuid.UUID,
    test_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ChatRecoveryResponse:
    """Recover a test's pinned snapshot identity, durable messages, state, and trace."""
    response.headers["Cache-Control"] = "no-store"
    return await get_chat_recovery(
        db,
        owner_user_id=principal.user_id,
        agent_id=agent_id,
        test_id=test_id,
    )
