"""Admin APIs with MFA, audited support access, and idempotent config changes."""

from __future__ import annotations

from datetime import datetime
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.admin.schemas import (
    AdminAgentDetail,
    AdminAgentPage,
    AdminAgentDisableRequest,
    AdminAgentItem,
    AdminAdmissionPauseItem,
    AdminAdmissionPausePage,
    AdminAdmissionPauseRequest,
    AdminTariffPage,
    AdminTariffVersionItem,
    AdminTariffVersionRequest,
    AdminSessionTerminationRequest,
    AdminTranscriptCorrectionItem,
    AdminTranscriptCorrectionRequest,
    AdminTranscriptPolicyView,
    AdminTranscriptVisibilityRequest,
    AdminAuditPage,
    AdminOverview,
    AdminPromptItem,
    AdminPromptPage,
    AdminSessionDetail,
    AdminSessionPage,
    AdminTemplatePage,
    AdminTemplateItem,
    PromptVersionRequest,
    TemplateVersionRequest,
    TranscriptAccessRequest,
)
from platform_api.admin.service import (
    create_prompt_version,
    create_template_version,
    get_admin_overview,
    get_admin_session,
    get_admin_agent_detail,
    get_admin_wallet,
    disable_admin_agent,
    get_prompt_version,
    get_template_version,
    list_admin_agents,
    list_admin_audit_logs,
    list_admin_prompts,
    list_admin_sessions,
    list_admin_templates,
    list_admin_wallet_entries,
)
from platform_api.admin.policies import (
    create_admin_tariff_version,
    create_admin_transcript_correction,
    list_admin_admission_pauses,
    list_admin_tariffs,
    set_admin_admission_pause,
    set_admin_transcript_visibility,
)
from platform_api.agents.service import (
    check_idempotency_record,
    compute_payload_hash,
    lock_idempotency_key,
    store_idempotency_record,
)
from platform_api.audit import record_audit
from platform_api.auth.dependencies import (
    get_client_ip,
    get_request_settings,
    require_admin,
    require_csrf,
    require_step_up,
)
from platform_api.auth.errors import ConflictError, ValidationError
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.billing.schemas import WalletEntriesPage, WalletView
from platform_api.db.session import get_db
from platform_api.sessions.history import (
    encode_transcript_api_cursor,
    get_admin_transcript_page,
)
from platform_api.sessions.history_schemas import TranscriptPageView
from platform_api.sessions.schemas import SessionStateView
from platform_api.sessions.service import terminate_admin_session


router = APIRouter(prefix="/admin", tags=["admin"])
IDEMPOTENCY_KEY_MAX_LENGTH = 128


def _normalize_idempotency_key(value: str | None) -> str:
    if value is None:
        raise ValidationError(
            "Idempotency-Key header is required",
            fields=[{"field": "Idempotency-Key", "code": "REQUIRED"}],
        )
    key = value.strip()
    if not key or len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValidationError(
            "Idempotency-Key must be between 1 and 128 characters",
            fields=[{"field": "Idempotency-Key", "code": "INVALID"}],
        )
    return key


async def _set_idempotency_transaction_isolation(
    db: AsyncSession = Depends(get_db),
) -> None:
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))


def _request_context(request: Request) -> tuple[str | None, uuid.UUID]:
    settings = get_request_settings(request)
    client_ip = get_client_ip(request, settings)
    request_id = getattr(request.state, "request_id", uuid.uuid4())
    return client_ip, request_id


def _resource_id(cached: object) -> uuid.UUID:
    response_json = getattr(cached, "response_json", None)
    raw_id = response_json.get("version_id") if isinstance(response_json, dict) else None
    try:
        return uuid.UUID(raw_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConflictError("Stored idempotency result is invalid") from exc


def _cached_response(cached: object, view: object) -> JSONResponse:
    status_code = getattr(cached, "status_code", None)
    model_dump = getattr(view, "model_dump", None)
    if not isinstance(status_code, int) or model_dump is None:
        raise ConflictError("Stored idempotency result is invalid")
    return JSONResponse(
        status_code=status_code,
        content=model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )

def _cached_model_response(cached: object, model_type: object, label: str) -> JSONResponse:
    response_json = getattr(cached, "response_json", None)
    validator = getattr(model_type, "model_validate", None)
    if not isinstance(response_json, dict) or not callable(validator):
        raise ConflictError(f"Stored {label} result is invalid")
    try:
        view = validator(response_json)
    except (TypeError, ValueError) as exc:
        raise ConflictError(f"Stored {label} result is invalid") from exc
    return _cached_response(cached, view)


def _cached_admin_agent_response(cached: object) -> JSONResponse:
    response_json = getattr(cached, "response_json", None)
    if not isinstance(response_json, dict):
        raise ConflictError("Stored agent disable result is invalid")
    try:
        view = AdminAgentItem.model_validate_json(json.dumps(response_json))
    except (TypeError, ValueError) as exc:
        raise ConflictError("Stored agent disable result is invalid") from exc
    return _cached_response(cached, view)


@router.get("/overview", response_model=AdminOverview)
async def get_admin_dashboard_overview(
    response: Response,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminOverview:
    response.headers["Cache-Control"] = "no-store"
    return await get_admin_overview(db)


@router.get("/sessions", response_model=AdminSessionPage)
async def get_admin_conversations(
    response: Response,
    owner_user_id: uuid.UUID | None = Query(default=None),
    state: str | None = Query(default=None, min_length=1, max_length=32),
    kind: Literal["voice", "chat", "voice_test"] | None = Query(default=None),
    agent_version_id: uuid.UUID | None = Query(default=None),
    from_at: datetime | None = Query(default=None),
    to_at: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminSessionPage:
    response.headers["Cache-Control"] = "no-store"
    return await list_admin_sessions(
        db,
        owner_user_id=owner_user_id,
        state=state,
        kind=kind,
        agent_version_id=agent_version_id,
        from_at=from_at,
        to_at=to_at,
        cursor=cursor,
        limit=limit,
    )


@router.get("/sessions/{session_id}", response_model=AdminSessionDetail)
async def get_admin_conversation(
    session_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminSessionDetail:
    """Return session metadata and stored billing facts, never transcript text."""
    response.headers["Cache-Control"] = "no-store"
    return await get_admin_session(db, session_id=session_id)


@router.post(
    "/sessions/{session_id}/transcript-access",
    response_model=TranscriptPageView,
)
async def access_admin_conversation_transcript(
    session_id: uuid.UUID,
    payload: TranscriptAccessRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> TranscriptPageView | JSONResponse:
    """Read transcript content only with a step-up-verified, audited support reason."""
    key = _normalize_idempotency_key(idempotency_key)
    action = "admin.transcript.access"
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
    )
    request_hash = compute_payload_hash(
        {
            "session_id": str(session_id),
            "request": payload.model_dump(mode="json"),
        }
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
    )
    response.headers["Cache-Control"] = "no-store"
    if cached is not None:
        marker = cached.response_json
        if not isinstance(marker, dict) or set(marker) != {
            "cursor",
            "limit",
            "session_id",
            "transcript_revision",
            "policy_revision",
        }:
            raise ConflictError("Stored transcript access result is invalid")
        if (
            marker["session_id"] != str(session_id)
            or marker["limit"] != payload.limit
            or not isinstance(marker["cursor"], str)
            or type(marker["transcript_revision"]) is not int
            or type(marker["policy_revision"]) is not int
        ):
            raise ConflictError("Stored transcript access result is invalid")
        page = await get_admin_transcript_page(
            db,
            session_id=session_id,
            cursor=marker["cursor"],
            limit=payload.limit,
        )
        if (
            page.transcript_revision != marker["transcript_revision"]
            or page.policy_revision != marker["policy_revision"]
        ):
            raise ConflictError("Transcript changed; repeat support access with a new key")
        await db.commit()
        return page

    page = await get_admin_transcript_page(
        db,
        session_id=session_id,
        cursor=payload.cursor,
        limit=payload.limit,
    )
    stable_cursor = payload.cursor or encode_transcript_api_cursor(
        transcript_revision=page.transcript_revision,
        policy_revision=page.policy_revision,
        after_sequence=0,
    )
    source_ip, request_id = _request_context(request)
    await record_audit(
        db,
        actor_kind="admin",
        actor_id=principal.user_id,
        session_id=principal.session_id,
        action="admin.conversation.transcript.view",
        target_type="session",
        target_id=session_id,
        reason=payload.reason,
        request_id=request_id,
        source_ip=source_ip,
        result="success",
        change_summary={
            "transcript_revision": page.transcript_revision,
            "policy_revision": page.policy_revision,
            "page_limit": payload.limit,
        },
    )
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
        status_code=status.HTTP_200_OK,
        response_json={
            "cursor": stable_cursor,
            "limit": payload.limit,
            "session_id": str(session_id),
            "transcript_revision": page.transcript_revision,
            "policy_revision": page.policy_revision,
        },
    )
    await db.commit()
    return page


@router.get("/users/{user_id}/wallet", response_model=WalletView)
async def get_admin_user_wallet(
    user_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> WalletView:
    response.headers["Cache-Control"] = "no-store"
    return await get_admin_wallet(db, user_id=user_id)


@router.get("/users/{user_id}/wallet/entries", response_model=WalletEntriesPage)
async def get_admin_user_wallet_entries(
    user_id: uuid.UUID,
    response: Response,
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> WalletEntriesPage:
    response.headers["Cache-Control"] = "no-store"
    return await list_admin_wallet_entries(
        db,
        user_id=user_id,
        cursor=cursor,
        limit=limit,
    )


@router.get("/agents", response_model=AdminAgentPage)
async def get_admin_agent_inventory(
    response: Response,
    owner_user_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, max_length=128),
    enabled: bool | None = Query(default=None),
    from_at: datetime | None = Query(default=None),
    to_at: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentPage:
    response.headers["Cache-Control"] = "no-store"
    return await list_admin_agents(
        db,
        owner_user_id=owner_user_id,
        q=q,
        enabled=enabled,
        from_at=from_at,
        to_at=to_at,
        cursor=cursor,
        limit=limit,
    )


@router.get("/agents/{agent_id}", response_model=AdminAgentDetail)
async def get_admin_agent_record(
    agent_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentDetail:
    response.headers["Cache-Control"] = "no-store"
    return await get_admin_agent_detail(db, agent_id=agent_id)


@router.post(
    "/agents/{agent_id}/disable",
    response_model=AdminAgentItem,
)
async def disable_admin_agent_route(
    agent_id: uuid.UUID,
    payload: AdminAgentDisableRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentItem | JSONResponse:
    key = _normalize_idempotency_key(idempotency_key)
    action = "admin.agent.disable"
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
    )
    request_hash = compute_payload_hash(
        {
            "agent_id": str(agent_id),
            "request": payload.model_dump(mode="json"),
        }
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
    )
    response.headers["Cache-Control"] = "no-store"
    if cached is not None:
        result = _cached_admin_agent_response(cached)
        await db.commit()
        return result
    source_ip, request_id = _request_context(request)
    view = await disable_admin_agent(
        db,
        agent_id=agent_id,
        actor_id=principal.user_id,
        auth_session_id=principal.session_id,
        payload=payload,
        request_id=request_id,
        source_ip=source_ip,
    )
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
        status_code=status.HTTP_200_OK,
        response_json=view.model_dump(mode="json"),
    )
    await db.commit()
    return view


@router.get("/templates", response_model=AdminTemplatePage)
async def get_admin_templates(
    response: Response,
    slug: str | None = Query(default=None, max_length=64),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminTemplatePage:
    response.headers["Cache-Control"] = "no-store"
    page = await list_admin_templates(db, slug=slug, cursor=cursor, limit=limit)
    await db.commit()
    return page


@router.post(
    "/templates/{slug}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=AdminTemplateItem,
)
async def post_admin_template_version(
    slug: str,
    payload: TemplateVersionRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AdminTemplateItem | JSONResponse:
    key = _normalize_idempotency_key(idempotency_key)
    action = "admin.template.version.create"
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
    )
    request_hash = compute_payload_hash(
        {"slug": slug, "request": payload.model_dump(mode="json")}
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
    )
    response.headers["Cache-Control"] = "no-store"
    if cached is not None:
        view = await get_template_version(db, version_id=_resource_id(cached))
        await db.commit()
        return _cached_response(cached, view)
    source_ip, request_id = _request_context(request)
    view = await create_template_version(
        db,
        actor_id=principal.user_id,
        auth_session_id=principal.session_id,
        slug=slug,
        payload=payload,
        request_id=request_id,
        source_ip=source_ip,
    )
    response_json = view.model_dump(mode="json")
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
        status_code=status.HTTP_201_CREATED,
        response_json={"version_id": response_json["id"]},
    )
    await db.commit()
    return view


@router.get("/prompts", response_model=AdminPromptPage)
async def get_admin_prompts(
    response: Response,
    slug: str | None = Query(default=None, max_length=64),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminPromptPage:
    response.headers["Cache-Control"] = "no-store"
    page = await list_admin_prompts(db, slug=slug, cursor=cursor, limit=limit)
    await db.commit()
    return page


@router.post(
    "/prompts/{slug}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=AdminPromptItem,
)
async def post_admin_prompt_version(
    slug: str,
    payload: PromptVersionRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AdminPromptItem | JSONResponse:
    key = _normalize_idempotency_key(idempotency_key)
    action = "admin.prompt.version.create"
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
    )
    request_hash = compute_payload_hash(
        {"slug": slug, "request": payload.model_dump(mode="json")}
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
    )
    response.headers["Cache-Control"] = "no-store"
    if cached is not None:
        view = await get_prompt_version(db, version_id=_resource_id(cached))
        await db.commit()
        return _cached_response(cached, view)
    source_ip, request_id = _request_context(request)
    view = await create_prompt_version(
        db,
        actor_id=principal.user_id,
        auth_session_id=principal.session_id,
        slug=slug,
        payload=payload,
        request_id=request_id,
        source_ip=source_ip,
    )
    response_json = view.model_dump(mode="json")
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
        status_code=status.HTTP_201_CREATED,
        response_json={"version_id": response_json["id"]},
    )
    await db.commit()
    return view


@router.get("/audit-logs", response_model=AdminAuditPage)
async def get_admin_audit_logs(
    response: Response,
    actor_id: uuid.UUID | None = Query(default=None),
    action: str | None = Query(default=None, min_length=1, max_length=64),
    target_type: str | None = Query(default=None, min_length=1, max_length=32),
    target_id: uuid.UUID | None = Query(default=None),
    from_at: datetime | None = Query(default=None),
    to_at: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAuditPage:
    response.headers["Cache-Control"] = "no-store"
    return await list_admin_audit_logs(
        db,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        from_at=from_at,
        to_at=to_at,
        cursor=cursor,
        limit=limit,
    )


@router.get("/admission-pauses", response_model=AdminAdmissionPausePage)
async def get_admin_admission_pause_policies(
    response: Response,
    scope: Literal["global", "user", "agent"] | None = Query(default=None),
    owner_user_id: uuid.UUID | None = Query(default=None),
    agent_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAdmissionPausePage:
    response.headers["Cache-Control"] = "no-store"
    return await list_admin_admission_pauses(
        db,
        scope=scope,
        owner_user_id=owner_user_id,
        agent_id=agent_id,
        cursor=cursor,
        limit=limit,
    )


@router.post(
    "/admission-pauses",
    response_model=AdminAdmissionPauseItem,
)
async def set_admin_admission_pause_route(
    payload: AdminAdmissionPauseRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AdminAdmissionPauseItem | JSONResponse:
    key = _normalize_idempotency_key(idempotency_key)
    action = "admin.admission.pause.set"
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
    )
    request_hash = compute_payload_hash({"request": payload.model_dump(mode="json")})
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
    )
    response.headers["Cache-Control"] = "no-store"
    if cached is not None:
        result = _cached_model_response(cached, AdminAdmissionPauseItem, "admission pause")
        await db.commit()
        return result
    source_ip, request_id = _request_context(request)
    view = await set_admin_admission_pause(
        db,
        actor_id=principal.user_id,
        auth_session_id=principal.session_id,
        payload=payload,
        request_id=request_id,
        source_ip=source_ip,
    )
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
        status_code=status.HTTP_200_OK,
        response_json=view.model_dump(mode="json"),
    )
    await db.commit()
    return view


@router.get("/tariffs", response_model=AdminTariffPage)
async def get_admin_tariff_versions(
    response: Response,
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminTariffPage:
    response.headers["Cache-Control"] = "no-store"
    page = await list_admin_tariffs(db, cursor=cursor, limit=limit)
    await db.commit()
    return page


@router.post(
    "/tariffs/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=AdminTariffVersionItem,
)
async def post_admin_tariff_version(
    payload: AdminTariffVersionRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AdminTariffVersionItem | JSONResponse:
    key = _normalize_idempotency_key(idempotency_key)
    action = "admin.tariff.version.create"
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
    )
    request_hash = compute_payload_hash({"request": payload.model_dump(mode="json")})
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
    )
    response.headers["Cache-Control"] = "no-store"
    if cached is not None:
        result = _cached_model_response(cached, AdminTariffVersionItem, "tariff")
        await db.commit()
        return result
    source_ip, request_id = _request_context(request)
    view = await create_admin_tariff_version(
        db,
        actor_id=principal.user_id,
        auth_session_id=principal.session_id,
        payload=payload,
        request_id=request_id,
        source_ip=source_ip,
    )
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
        status_code=status.HTTP_201_CREATED,
        response_json=view.model_dump(mode="json"),
    )
    await db.commit()
    return view


@router.post(
    "/sessions/{session_id}/terminate",
    response_model=SessionStateView,
)
async def terminate_admin_conversation(
    session_id: uuid.UUID,
    payload: AdminSessionTerminationRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> SessionStateView:
    key = _normalize_idempotency_key(idempotency_key)
    source_ip, request_id = _request_context(request)
    settings = get_request_settings(request)
    request_hash = compute_payload_hash(
        {
            "admin_user_id": str(principal.user_id),
            "reason": payload.reason.strip(),
            "session_id": str(session_id),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    view = await terminate_admin_session(
        db,
        session_id=session_id,
        admin_user_id=principal.user_id,
        auth_session_id=principal.session_id,
        operation_id=key,
        request_hash=request_hash,
        reason=payload.reason,
        request_id=request_id,
        source_ip=source_ip,
        settings=settings,
    )
    response.status_code = (
        status.HTTP_202_ACCEPTED if view.state == "ENDING" else status.HTTP_200_OK
    )
    return view


@router.post(
    "/sessions/{session_id}/transcript-corrections",
    status_code=status.HTTP_201_CREATED,
    response_model=AdminTranscriptCorrectionItem,
)
async def create_admin_transcript_correction_route(
    session_id: uuid.UUID,
    payload: AdminTranscriptCorrectionRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AdminTranscriptCorrectionItem | JSONResponse:
    key = _normalize_idempotency_key(idempotency_key)
    action = "admin.transcript.correction.create"
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
    )
    request_hash = compute_payload_hash(
        {"session_id": str(session_id), "request": payload.model_dump(mode="json")}
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
    )
    response.headers["Cache-Control"] = "no-store"
    if cached is not None:
        result = _cached_model_response(cached, AdminTranscriptCorrectionItem, "transcript correction")
        await db.commit()
        return result
    source_ip, request_id = _request_context(request)
    view = await create_admin_transcript_correction(
        db,
        session_id=session_id,
        actor_id=principal.user_id,
        auth_session_id=principal.session_id,
        payload=payload,
        request_id=request_id,
        source_ip=source_ip,
    )
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
        status_code=status.HTTP_201_CREATED,
        response_json=view.model_dump(mode="json"),
    )
    await db.commit()
    return view


@router.post(
    "/sessions/{session_id}/transcript-visibility",
    response_model=AdminTranscriptPolicyView,
)
async def set_admin_transcript_visibility_route(
    session_id: uuid.UUID,
    payload: AdminTranscriptVisibilityRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(_set_idempotency_transaction_isolation),
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AdminTranscriptPolicyView | JSONResponse:
    key = _normalize_idempotency_key(idempotency_key)
    action = "admin.transcript.visibility.set"
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
    )
    request_hash = compute_payload_hash(
        {"session_id": str(session_id), "request": payload.model_dump(mode="json")}
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
    )
    response.headers["Cache-Control"] = "no-store"
    if cached is not None:
        result = _cached_model_response(cached, AdminTranscriptPolicyView, "transcript policy")
        await db.commit()
        return result
    source_ip, request_id = _request_context(request)
    view = await set_admin_transcript_visibility(
        db,
        session_id=session_id,
        actor_id=principal.user_id,
        auth_session_id=principal.session_id,
        payload=payload,
        request_id=request_id,
        source_ip=source_ip,
    )
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action,
        request_hash=request_hash,
        status_code=status.HTTP_200_OK,
        response_json=view.model_dump(mode="json"),
    )
    await db.commit()
    return view
