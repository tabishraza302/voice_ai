"""FastAPI HTTP routes for platform_api.agents.

Mounted under /api/v1:
- /agents (CRUD, duplicate, enable, disable)
- /agents/{id}/draft (GET, PATCH with CAS)
- /agents/{id}/validate (validation diagnostics)
- /agents/{id}/publish (immutable version creation)
- /agents/{id}/versions (history and detail)
- /agents/{id}/restore-draft (restore version to draft)
- /templates (list and detail)

Every route is owner-scoped through ``require_user``.  Mutations also require
CSRF and commit their service transaction before returning. Idempotency-Key
is required for publish and accepted on create/duplicate; publish replay is
the lost-response path required by the platform contract.
"""

from __future__ import annotations

from typing import Any
import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from bot.flow.qualification import AgentQualificationSettings, CapabilityReport
from platform_api.qualification import DeploymentIdentity
from platform_api.agents.schemas import (
    AgentCreate,
    AgentListResponse,
    AgentUpdate,
    AgentView,
    DraftUpdate,
    DraftView,
    DuplicateAgentRequest,
    PublishRequest,
    RestoreDraftRequest,
    TemplateDetailView,
    TemplateListResponse,
    ValidateRequest,
    ValidationResponse,
    VersionDetailView,
    VersionListResponse,
    VersionView,
)
from bot.flow.schemas import AdapterCapabilities
from platform_api.agents.service import (
    check_idempotency_record,
    compute_payload_hash,
    create_agent,
    delete_agent,
    duplicate_agent,
    get_agent,
    get_draft,
    get_template,
    get_version,
    list_agents,
    list_templates,
    list_versions,
    lock_idempotency_key,
    publish_agent,
    restore_draft,
    set_agent_enabled,
    store_idempotency_record,
    update_agent,
    update_draft,
    validate_draft,
)
from platform_api.auth.dependencies import (
    get_client_ip,
    get_request_settings,
    require_csrf,
    require_user,
)
from platform_api.auth.errors import ValidationError
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.db.session import get_db
from platform_api.catalog import ModelCatalog, load_catalog

router = APIRouter(tags=["agents"])

IDEMPOTENCY_KEY_MAX_LENGTH = 128


def _normalize_idempotency_key(value: str | None) -> str | None:
    """Validate and normalize an optional Idempotency-Key header."""
    if value is None:
        return None
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
    """Start idempotent mutations at READ COMMITTED before auth reads begin."""
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))


def _request_context(request: Request) -> tuple[str | None, uuid.UUID]:
    settings = get_request_settings(request)
    client_ip = get_client_ip(request, settings)
    req_id = getattr(request.state, "request_id", uuid.uuid4())
    return client_ip, req_id


def _request_catalog(request: Request) -> ModelCatalog:
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is not None:
        return catalog
    return load_catalog(get_request_settings(request).catalog_path)


def _publication_context(
    request: Request,
) -> tuple[AdapterCapabilities | None, AgentQualificationSettings | None]:
    """Project the lifespan-validated report into pinned runtime capabilities."""
    report = getattr(request.app.state, "adapter_capability_report", None)
    if report is None:
        return None, None
    if not isinstance(report, CapabilityReport):
        raise ValidationError(
            "The deployment adapter qualification is invalid",
            fields=[{"field": "capabilities", "code": "PLAYBACK_UNQUALIFIED"}],
        )

    settings = getattr(request.app.state, "settings", None)
    expected_report_hash = getattr(settings, "adapter_capability_report_hash", None)

    if (
        expected_report_hash is None
        or report.report_hash != expected_report_hash
        or report.compute_hash() != expected_report_hash
    ):
        raise ValidationError(
            "The deployment adapter qualification is not pinned by configuration",
            fields=[{"field": "capabilities.report_hash", "code": "PLAYBACK_UNQUALIFIED"}],
        )

    deployment_identity = getattr(request.app.state, "deployment_identity", None)
    if not isinstance(deployment_identity, DeploymentIdentity) or not (
        deployment_identity.matches_report(report)
    ):
        return None, None

    capabilities = AdapterCapabilities(
        adapter_id=report.adapter_id,
        adapter_version=report.adapter_version,
        model_revision=report.model_revision,
        metadata=report.metadata,
        speech_gating=report.speech_gating,
        cancel_generation=report.cancel_generation,
        playback=report.playback,
        locales=list(report.locales),
        report_hash=report.report_hash,
    )
    return capabilities, report.agent_settings



# --- Agent CRUD ---------------------------------------------------------------


@router.get("/agents", response_model=AgentListResponse)
async def get_agents(
    response: Response,
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> AgentListResponse:
    """List agents owned by current authenticated user."""
    response.headers["Cache-Control"] = "no-store"
    return await list_agents(
        db,
        owner_user_id=principal.user_id,
        cursor=cursor,
        limit=limit,
    )


@router.post("/agents", status_code=status.HTTP_201_CREATED, response_model=AgentView)
async def post_agent(
    payload: AgentCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(
        _set_idempotency_transaction_isolation
    ),
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Create a new agent and initial draft for owner."""
    client_ip, req_id = _request_context(request)
    key = _normalize_idempotency_key(idempotency_key)

    req_hash = None
    if key:
        await lock_idempotency_key(
            db,
            user_id=principal.user_id,
            idempotency_key=key,
            action="agent.create",
        )
        req_hash = compute_payload_hash(payload.model_dump(mode="json"))
        cached = await check_idempotency_record(
            db,
            user_id=principal.user_id,
            idempotency_key=key,
            action="agent.create",
            request_hash=req_hash,
        )
        if cached is not None:
            return JSONResponse(
                status_code=cached.status_code,
                content=cached.response_json,
                headers={"Cache-Control": "no-store"},
            )

    view = await create_agent(
        db,
        owner_user_id=principal.user_id,
        payload=payload,
        session_id=principal.session_id,
        request_id=req_id,
        source_ip=client_ip,
    )
    resp_dict = view.model_dump(mode="json")

    if key and req_hash:
        await store_idempotency_record(
            db,
            user_id=principal.user_id,
            idempotency_key=key,
            action="agent.create",
            request_hash=req_hash,
            status_code=status.HTTP_201_CREATED,
            response_json=resp_dict,
        )

    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return view


@router.get("/agents/{agent_id}", response_model=AgentView)
async def get_agent_by_id(
    agent_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> AgentView:
    """Get single owned agent."""
    response.headers["Cache-Control"] = "no-store"
    agent = await get_agent(db, agent_id=agent_id, owner_user_id=principal.user_id)
    return AgentView.model_validate(agent)


@router.patch("/agents/{agent_id}", response_model=AgentView)
async def patch_agent_by_id(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AgentView:
    """Update agent metadata (name, description)."""
    client_ip, req_id = _request_context(request)

    view = await update_agent(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        payload=payload,
        session_id=principal.session_id,
        request_id=req_id,
        source_ip=client_ip,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return view


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_by_id(
    agent_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete owned agent and cascading drafts/versions."""
    client_ip, req_id = _request_context(request)

    await delete_agent(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        session_id=principal.session_id,
        request_id=req_id,
        source_ip=client_ip,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.status_code = status.HTTP_204_NO_CONTENT


@router.post("/agents/{agent_id}/enable", response_model=AgentView)
async def enable_agent(
    agent_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AgentView:
    """Enable agent for admission."""
    client_ip, req_id = _request_context(request)

    view = await set_agent_enabled(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        enabled=True,
        session_id=principal.session_id,
        request_id=req_id,
        source_ip=client_ip,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return view


@router.post("/agents/{agent_id}/disable", response_model=AgentView)
async def disable_agent(
    agent_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> AgentView:
    """Disable agent from admission."""
    client_ip, req_id = _request_context(request)

    view = await set_agent_enabled(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        enabled=False,
        session_id=principal.session_id,
        request_id=req_id,
        source_ip=client_ip,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return view


# --- Draft Routes -------------------------------------------------------------


@router.get("/agents/{agent_id}/draft", response_model=DraftView)
async def get_agent_draft(
    agent_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> DraftView:
    """Get mutable draft with live executable validation summary."""
    response.headers["Cache-Control"] = "no-store"
    capabilities, qualification_settings = _publication_context(request)
    return await get_draft(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        catalog=_request_catalog(request),
        capabilities=capabilities,
        qualification_settings=qualification_settings,
    )


@router.patch("/agents/{agent_id}/draft", response_model=DraftView)
async def patch_agent_draft(
    agent_id: uuid.UUID,
    payload: DraftUpdate,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> DraftView:
    """CAS update of agent draft. Enforces revision match & authoring structure."""
    client_ip, req_id = _request_context(request)
    capabilities, qualification_settings = _publication_context(request)

    view = await update_draft(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        payload=payload,
        catalog=_request_catalog(request),
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        session_id=principal.session_id,
        request_id=req_id,
        source_ip=client_ip,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return view


@router.post("/agents/{agent_id}/validate", response_model=ValidationResponse)
async def post_validate_agent_draft(
    agent_id: uuid.UUID,
    request: Request,
    response: Response,
    payload: ValidateRequest = ValidateRequest(),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ValidationResponse:
    """Validate saved or candidate config against structural and runtime checks."""
    response.headers["Cache-Control"] = "no-store"
    capabilities, qualification_settings = _publication_context(request)
    return await validate_draft(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        payload=payload,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        catalog=_request_catalog(request),
    )


# --- Publish & Versions -------------------------------------------------------


@router.post(
    "/agents/{agent_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=VersionView,
)
async def post_publish_agent(
    agent_id: uuid.UUID,
    payload: PublishRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(
        _set_idempotency_transaction_isolation
    ),
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Compile and publish an immutable version artifact in active transaction."""
    client_ip, req_id = _request_context(request)
    key = _normalize_idempotency_key(idempotency_key)
    if key is None:
        raise ValidationError(
            "Idempotency-Key is required for publish",
            fields=[{"field": "Idempotency-Key", "code": "REQUIRED"}],
        )

    action_key = f"agent.publish:{agent_id}"
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action_key,
    )
    req_hash = compute_payload_hash(
        {"agent_id": str(agent_id), **payload.model_dump(mode="json")}
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action_key,
        request_hash=req_hash,
    )
    if cached is not None:
        return JSONResponse(
            status_code=cached.status_code,
            content=cached.response_json,
            headers={"Cache-Control": "no-store"},
        )

    capabilities, qualification_settings = _publication_context(request)
    view = await publish_agent(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        payload=payload,
        catalog=_request_catalog(request),
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        session_id=principal.session_id,
        request_id=req_id,
        source_ip=client_ip,
    )
    resp_dict = view.model_dump(mode="json")

    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=key,
        action=action_key,
        request_hash=req_hash,
        status_code=status.HTTP_201_CREATED,
        response_json=resp_dict,
    )

    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return view


@router.get("/agents/{agent_id}/versions", response_model=VersionListResponse)
async def get_agent_versions(
    agent_id: uuid.UUID,
    response: Response,
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> VersionListResponse:
    """List immutable versions for an owned agent."""
    response.headers["Cache-Control"] = "no-store"
    return await list_versions(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        cursor=cursor,
        limit=limit,
    )


@router.get("/agents/{agent_id}/versions/{version_ref}", response_model=VersionDetailView)
async def get_agent_version_detail(
    agent_id: uuid.UUID,
    version_ref: str,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> VersionDetailView:
    """Get immutable version details including pinned config."""
    response.headers["Cache-Control"] = "no-store"
    return await get_version(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        version_ref=version_ref,
    )


@router.post("/agents/{agent_id}/restore-draft", response_model=DraftView)
async def post_restore_draft(
    agent_id: uuid.UUID,
    payload: RestoreDraftRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> DraftView:
    """Restore mutable draft from an immutable published version."""
    client_ip, req_id = _request_context(request)
    capabilities, qualification_settings = _publication_context(request)

    view = await restore_draft(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        payload=payload,
        catalog=_request_catalog(request),
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        session_id=principal.session_id,
        request_id=req_id,
        source_ip=client_ip,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return view


@router.post(
    "/agents/{agent_id}/duplicate",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentView,
)
async def post_duplicate_agent(
    agent_id: uuid.UUID,
    payload: DuplicateAgentRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    _transaction_isolation: None = Depends(
        _set_idempotency_transaction_isolation
    ),
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Duplicate an owned agent and its draft/version flow."""
    client_ip, req_id = _request_context(request)
    key = _normalize_idempotency_key(idempotency_key)

    req_hash = None
    action_key = f"agent.duplicate:{agent_id}"
    if key:
        await lock_idempotency_key(
            db,
            user_id=principal.user_id,
            idempotency_key=key,
            action=action_key,
        )
        req_hash = compute_payload_hash(
            {
                "source_agent_id": str(agent_id),
                **payload.model_dump(mode="json"),
            }
        )
        cached = await check_idempotency_record(
            db,
            user_id=principal.user_id,
            idempotency_key=key,
            action=action_key,
            request_hash=req_hash,
        )
        if cached is not None:
            return JSONResponse(
                status_code=cached.status_code,
                content=cached.response_json,
                headers={"Cache-Control": "no-store"},
            )

    view = await duplicate_agent(
        db,
        agent_id=agent_id,
        owner_user_id=principal.user_id,
        payload=payload,
        session_id=principal.session_id,
        request_id=req_id,
        source_ip=client_ip,
    )
    resp_dict = view.model_dump(mode="json")

    if key and req_hash:
        await store_idempotency_record(
            db,
            user_id=principal.user_id,
            idempotency_key=key,
            action=action_key,
            request_hash=req_hash,
            status_code=status.HTTP_201_CREATED,
            response_json=resp_dict,
        )

    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return view


# --- Templates Routes ---------------------------------------------------------


@router.get("/templates", response_model=TemplateListResponse)
async def get_templates(
    response: Response,
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateListResponse:
    """List available active agent templates."""
    result = await list_templates(db, cursor=cursor, limit=limit)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/templates/{template_id}", response_model=TemplateDetailView)
async def get_template_detail(
    template_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateDetailView:
    """Get active template definition with full flow JSON."""
    result = await get_template(db, template_id=template_id)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result
