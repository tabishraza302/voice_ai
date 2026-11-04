"""Owner and local-runtime HTTP routes for first-release voice-test sessions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.agents.routes import _publication_context, _request_catalog
from platform_api.auth.dependencies import (
    get_request_settings,
    require_csrf,
    require_user,
)
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.config import PlatformSettings
from platform_api.billing.service import RuntimePrincipal
from platform_api.db.session import get_db
from platform_api.sessions.admission import get_runtime_principal
from platform_api.sessions.schemas import (
    JoinConsumptionRequest,
    JoinConsumptionView,
    JoinDetails,
    OwnerSessionView,
    PeerReleaseRequest,
    PeerReleaseView,
    RegisteredSession,
    RuntimeConfigView,
    SessionAdmissionRequest,
    SessionStateView,
    SessionTransitionRequest,
)
from platform_api.sessions.service import (
    consume_join,
    end_owner_session,
    get_owner_session,
    get_runtime_config,
    issue_join,
    release_peer,
    reserve_session,
    transition_runtime_session,
)

owner_router = APIRouter(tags=["sessions"])
internal_router = APIRouter(prefix="/sessions", tags=["internal_sessions"])


async def _set_idempotency_transaction_isolation(
    db: AsyncSession = Depends(get_db),
) -> None:
    """Start idempotent owner mutations at READ COMMITTED before auth reads."""
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))


@owner_router.post(
    "/agents/{agent_id}/test/voice-sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisteredSession,
)
async def post_test_voice_session(
    agent_id: uuid.UUID,
    payload: SessionAdmissionRequest,
    request: Request,
    response: Response,
    origin: str = Header(..., alias="Origin"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_user),
    auth_context: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> RegisteredSession:
    """Reserve, register, fund, and return one owner-only immutable voice test."""
    response.headers["Cache-Control"] = "no-store"
    capabilities, qualification_settings = _publication_context(request)
    return await reserve_session(
        db,
        principal=principal,
        auth_session_id=auth_context.session.id,
        origin=origin,
        agent_id=agent_id,
        agent_version_id=payload.agent_version_id,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        operation_id=idempotency_key,
        settings=get_request_settings(request),
        catalog=await _request_catalog(request, db),
    )


@owner_router.get(
    "/sessions/{session_id}",
    response_model=OwnerSessionView,
)
async def get_test_voice_session(
    session_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> OwnerSessionView:
    """Return only a safe session state to its authenticated owner."""
    response.headers["Cache-Control"] = "no-store"
    return await get_owner_session(
        db,
        session_id=session_id,
        owner_user_id=principal.user_id,
    )


@owner_router.post(
    "/sessions/{session_id}/join",
    response_model=JoinDetails,
)
async def post_test_voice_session_join(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    origin: str = Header(..., alias="Origin"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_user),
    auth_context: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> JoinDetails:
    """Issue a one-use signed join token bound to owner, origin, runner, and JTI."""
    response.headers["Cache-Control"] = "no-store"
    return await issue_join(
        db,
        session_id=session_id,
        principal=principal,
        auth_session_id=auth_context.session.id,
        origin=origin,
        operation_id=idempotency_key,
        settings=get_request_settings(request),
    )


@owner_router.post(
    "/sessions/{session_id}/end",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SessionStateView,
)
async def post_end_test_voice_session(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    origin: str = Header(..., alias="Origin"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_user),
    auth_context: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> SessionStateView:
    """Idempotently request stop and release runner capacity after its acknowledgement."""
    response.headers["Cache-Control"] = "no-store"
    view = await end_owner_session(
        db,
        session_id=session_id,
        principal=principal,
        auth_session_id=auth_context.session.id,
        origin=origin,
        operation_id=idempotency_key,
        settings=get_request_settings(request),
    )
    if view.state != "ENDING":
        response.status_code = status.HTTP_200_OK
    return view


@internal_router.get(
    "/{session_id}/runtime-config",
    response_model=RuntimeConfigView,
)
async def get_registered_runtime_config(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    runtime_principal: RuntimePrincipal = Depends(get_runtime_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeConfigView:
    """Expose only the immutable registered snapshot and its current funded permit."""
    response.headers["Cache-Control"] = "no-store"
    return await get_runtime_config(
        db,
        session_id=session_id,
        runtime_principal=runtime_principal,
        settings=get_request_settings(request),
        catalog=await _request_catalog(request, db),
    )


@internal_router.post(
    "/{session_id}/join/consume",
    response_model=JoinConsumptionView,
)
async def post_consume_join(
    session_id: uuid.UUID,
    payload: JoinConsumptionRequest,
    request: Request,
    response: Response,
    runtime_principal: RuntimePrincipal = Depends(get_runtime_principal),
    db: AsyncSession = Depends(get_db),
) -> JoinConsumptionView:
    """Verify and atomically consume one signed browser JTI for one runner peer."""
    response.headers["Cache-Control"] = "no-store"
    return await consume_join(
        db,
        session_id=session_id,
        runtime_principal=runtime_principal,
        payload=payload,
        settings=get_request_settings(request),
    )


@internal_router.post(
    "/{session_id}/peer/release",
    response_model=PeerReleaseView,
)
async def post_release_peer(
    session_id: uuid.UUID,
    payload: PeerReleaseRequest,
    request: Request,
    response: Response,
    runtime_principal: RuntimePrincipal = Depends(get_runtime_principal),
    db: AsyncSession = Depends(get_db),
) -> PeerReleaseView:
    """Report a peer disconnect and begin reconnect grace or confirmed teardown."""
    response.headers["Cache-Control"] = "no-store"
    return await release_peer(
        db,
        session_id=session_id,
        runtime_principal=runtime_principal,
        payload=payload,
        settings=get_request_settings(request),
    )


@internal_router.post(
    "/{session_id}/state",
    response_model=SessionStateView,
)
async def post_session_transition(
    session_id: uuid.UUID,
    payload: SessionTransitionRequest,
    request: Request,
    response: Response,
    runtime_principal: RuntimePrincipal = Depends(get_runtime_principal),
    db: AsyncSession = Depends(get_db),
) -> SessionStateView:
    """Apply a compare-and-swap runtime lifecycle transition."""
    response.headers["Cache-Control"] = "no-store"
    return await transition_runtime_session(
        db,
        session_id=session_id,
        runtime_principal=runtime_principal,
        payload=payload,
        settings=get_request_settings(request),
    )


__all__ = ["internal_router", "owner_router"]
