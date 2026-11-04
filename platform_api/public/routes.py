"""HTTP routes for owner-managed public links and anonymous guest sessions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.agents.routes import _publication_context, _request_catalog
from platform_api.auth.csrf import validate_content_type, validate_origin
from platform_api.auth.dependencies import (
    get_client_ip,
    get_request_settings,
    require_csrf,
    require_user,
)
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.config import PlatformSettings
from platform_api.db.session import get_db
from platform_api.public.schemas import (
    GuestAdmissionRequest,
    GuestAdmissionView,
    GuestEndView,
    GuestReconnectRequest,
    GuestReconnectView,
    GuestSessionStatus,
    PublicDeploymentBootstrap,
    PublicDeploymentView,
)
from platform_api.public.service import (
    admit_guest_session,
    create_public_deployment,
    disable_public_deployment,
    end_guest_session,
    get_guest_session_status,
    get_public_deployment,
    guest_cookie_name,
    guest_cookie_path,
    reconnect_guest_session,
)


router = APIRouter(tags=["public"])


class _EmptyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


async def _set_idempotency_transaction_isolation(
    db: AsyncSession = Depends(get_db),
) -> None:
    """Begin idempotent mutations at READ COMMITTED before auth reads."""
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))


@router.post(
    "/agents/{agent_id}/public-deployment",
    status_code=status.HTTP_201_CREATED,
    response_model=PublicDeploymentView,
)
async def post_public_deployment(
    agent_id: uuid.UUID,
    request: Request,
    response: Response,
    payload: _EmptyPayload | None = Body(default=None),
    origin: str = Header(..., alias="Origin"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_user),
    _auth_context: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> PublicDeploymentView:
    """Create a public link only for an enabled, published owned agent."""
    del payload
    response.headers["Cache-Control"] = "no-store"
    return await create_public_deployment(
        db,
        principal=principal,
        agent_id=agent_id,
        origin=origin,
        operation_id=idempotency_key,
        settings=get_request_settings(request),
    )


@router.delete(
    "/public/deployments/{public_id}",
    response_model=PublicDeploymentView,
)
async def delete_public_deployment(
    public_id: str,
    request: Request,
    response: Response,
    payload: _EmptyPayload | None = Body(default=None),
    origin: str = Header(..., alias="Origin"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_user),
    _auth_context: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> PublicDeploymentView:
    """Disable one owner's public link without ending already-running calls."""
    del payload
    response.headers["Cache-Control"] = "no-store"
    return await disable_public_deployment(
        db,
        principal=principal,
        public_id=public_id,
        origin=origin,
        operation_id=idempotency_key,
        settings=get_request_settings(request),
    )


@router.get(
    "/public/deployments/{public_id}",
    response_model=PublicDeploymentBootstrap,
)
async def get_public_deployment_bootstrap(
    public_id: str,
    request: Request,
    response: Response,
    origin: str | None = Header(None, alias="Origin"),
    db: AsyncSession = Depends(get_db),
) -> PublicDeploymentBootstrap:
    """Return the public agent description and admission disclosure."""
    response.headers["Cache-Control"] = "no-store"
    settings = get_request_settings(request)
    validate_origin(request, settings)
    return await get_public_deployment(
        db,
        public_id=public_id,
        origin=origin or settings.web_origin,
        settings=settings,
    )


@router.post(
    "/public/deployments/{public_id}/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=GuestAdmissionView,
)
async def post_public_guest_session(
    public_id: str,
    payload: GuestAdmissionRequest,
    request: Request,
    response: Response,
    origin: str | None = Header(None, alias="Origin"),
    public_csrf: str | None = Header(None, alias="X-Public-CSRF"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    db: AsyncSession = Depends(get_db),
) -> GuestAdmissionView:
    """Admit an acknowledged guest and set its opaque session-only cookie."""
    response.headers["Cache-Control"] = "no-store"
    settings = get_request_settings(request)
    validate_origin(request, settings)
    validate_content_type(request)
    capabilities, qualification_settings = _publication_context(request)
    view, raw_capability = await admit_guest_session(
        db,
        public_id=public_id,
        payload=payload,
        origin=origin or "",
        csrf_token=public_csrf,
        client_ip=get_client_ip(request, settings),
        operation_id=idempotency_key,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        catalog=await _request_catalog(request, db),
        settings=settings,
    )
    max_age = max(
        0,
        int((view.hard_end_deadline - datetime.now(timezone.utc)).total_seconds()),
    )
    response.set_cookie(
        key=guest_cookie_name(view.session_id),
        value=raw_capability,
        max_age=max_age,
        path=guest_cookie_path(view.session_id),
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return view


@router.get(
    "/guest/sessions/{session_id}/status",
    response_model=GuestSessionStatus,
)
async def get_guest_session(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    origin: str | None = Header(None, alias="Origin"),
    db: AsyncSession = Depends(get_db),
) -> GuestSessionStatus:
    """Read safe session state using only this session's guest cookie."""
    response.headers["Cache-Control"] = "no-store"
    settings = get_request_settings(request)
    validate_origin(request, settings)
    return await get_guest_session_status(
        db,
        session_id=session_id,
        raw_token=request.cookies.get(guest_cookie_name(session_id)),
        origin=origin or settings.web_origin,
        settings=settings,
    )


@router.post(
    "/guest/sessions/{session_id}/reconnect-token",
    response_model=GuestReconnectView,
)
async def post_guest_reconnect_token(
    session_id: uuid.UUID,
    payload: GuestReconnectRequest,
    request: Request,
    response: Response,
    origin: str | None = Header(None, alias="Origin"),
    guest_csrf: str | None = Header(None, alias="X-Guest-CSRF"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    db: AsyncSession = Depends(get_db),
) -> GuestReconnectView:
    """Issue a fresh join token for the same guest within its reconnect window."""
    del payload
    response.headers["Cache-Control"] = "no-store"
    settings = get_request_settings(request)
    validate_origin(request, settings)
    validate_content_type(request)
    return await reconnect_guest_session(
        db,
        session_id=session_id,
        raw_token=request.cookies.get(guest_cookie_name(session_id)),
        origin=origin or "",
        csrf_token=guest_csrf,
        operation_id=idempotency_key,
        settings=settings,
    )


@router.post(
    "/guest/sessions/{session_id}/end",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=GuestEndView,
)
async def post_guest_end(
    session_id: uuid.UUID,
    payload: GuestReconnectRequest,
    request: Request,
    response: Response,
    origin: str | None = Header(None, alias="Origin"),
    guest_csrf: str | None = Header(None, alias="X-Guest-CSRF"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    db: AsyncSession = Depends(get_db),
) -> GuestEndView:
    """Idempotently request guest-session teardown."""
    del payload
    response.headers["Cache-Control"] = "no-store"
    settings = get_request_settings(request)
    validate_origin(request, settings)
    validate_content_type(request)
    view = await end_guest_session(
        db,
        session_id=session_id,
        raw_token=request.cookies.get(guest_cookie_name(session_id)),
        origin=origin or "",
        csrf_token=guest_csrf,
        operation_id=idempotency_key,
        settings=settings,
    )
    if view.state != "ENDING":
        response.status_code = status.HTTP_200_OK
    return view


__all__ = ["router"]
