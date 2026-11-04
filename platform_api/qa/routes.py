"""Owner-scoped Flow QA previews, jobs, and cancellation routes."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.agents.models import AgentDraft
from platform_api.agents.schemas import DraftUpdate
from platform_api.agents.service import update_draft
from platform_api.auth.dependencies import require_csrf, require_user
from platform_api.auth.errors import ConflictError, DependencyUnavailableError, NotFoundError
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.catalog import (
    ModelCatalog,
    catalog_with_managed_llms,
    load_catalog,
)
from platform_api.config import PlatformSettings
from platform_api.db.session import get_db
from platform_api.qa.models import FlowQAJob
from platform_api.qa.schemas import (
    QAJobCancel,
    QAJobCreate,
    QAJobQueued,
    QAJobView,
    QACatalogResponse,
    QASuggestionApplyRequest,
    QASuggestionApplied,
    QASuggestionDismissed,
)
from platform_api.qa.service import (
    cancel_qa_job,
    enqueue_qa_job,
    get_qa_job,
    job_view,
    preview_qa_scenarios,
)


router = APIRouter(prefix="/qa")


@router.get("/scenarios", response_model=QACatalogResponse)
async def get_qa_scenarios(
    request: Request,
    response: Response,
    agent_id: uuid.UUID,
    snapshot_hash: str = Query(pattern=r"^[0-9a-f]{64}$"),
    scenario_count: int = Query(default=8, ge=1, le=64),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> QACatalogResponse:
    settings = _settings(request)
    catalog = await _catalog(request, db)
    scenarios, flow_hash, available_count = await preview_qa_scenarios(
        db,
        settings=settings,
        catalog=catalog,
        owner_user_id=principal.user_id,
        agent_id=agent_id,
        snapshot_hash=snapshot_hash,
        scenario_count=scenario_count,
    )
    response.headers["Cache-Control"] = "no-store"
    return QACatalogResponse(
        scenarios=scenarios,
        scenario_count=len(scenarios),
        omitted_scenario_count=max(0, available_count - len(scenarios)),
        flow_hash=flow_hash,
    )


@router.post(
    "/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=QAJobQueued,
)
async def post_qa_job(
    payload: QAJobCreate,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    _ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> QAJobQueued:
    settings = _settings(request)
    catalog = await _catalog(request, db)
    _model_client(request)
    job = await enqueue_qa_job(
        db,
        settings=settings,
        catalog=catalog,
        owner_user_id=principal.user_id,
        payload=payload,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Retry-After"] = str(settings.qa_poll_after_seconds)
    return QAJobQueued(job_id=job.id, status="QUEUED")


@router.get("/jobs/{job_id}", response_model=QAJobView)
async def get_qa_job_route(
    job_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> QAJobView:
    settings = _settings(request)
    job = await get_qa_job(db, owner_user_id=principal.user_id, job_id=job_id)
    response.headers["Cache-Control"] = "no-store"
    if job.status in {"QUEUED", "RUNNING"}:
        response.headers["Retry-After"] = str(settings.qa_poll_after_seconds)
    return await job_view(job)


@router.post("/jobs/{job_id}/cancel", response_model=QAJobCancel)
async def post_cancel_qa_job(
    job_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_user),
    _ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> QAJobCancel:
    _settings(request)
    job = await cancel_qa_job(
        db,
        owner_user_id=principal.user_id,
        job_id=job_id,
    )
    await db.commit()
    return QAJobCancel(job_id=job.id, status=job.status)


@router.post(
    "/jobs/{job_id}/suggestions/{suggestion_id}/apply",
    response_model=QASuggestionApplied,
)
async def apply_qa_suggestion(
    job_id: uuid.UUID,
    suggestion_id: str,
    payload: QASuggestionApplyRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    _ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> QASuggestionApplied:
    _settings(request)
    job = await _locked_qa_job(db, owner_user_id=principal.user_id, job_id=job_id)
    if job.status != "COMPLETED" or job.snapshot_kind != "draft":
        raise ConflictError("Only completed draft QA suggestions can be applied")
    suggestion = _qa_suggestion(job, suggestion_id)
    if suggestion.get("status") != "DRAFT" or suggestion.get("draft_only") is not True:
        raise ConflictError("QA suggestion is no longer available for application")
    patch = suggestion.get("proposed_patch")
    target_step_id = suggestion.get("target_step_id")
    append_text = patch.get("append_text") if isinstance(patch, dict) else None
    if not isinstance(target_step_id, str) or not isinstance(append_text, str):
        raise ConflictError("This QA suggestion has no applicable draft edit")
    if job.draft_revision != payload.expected_revision:
        raise ConflictError("The QA suggestion applies only to its pinned draft revision")

    draft_result = await db.execute(
        select(AgentDraft).where(AgentDraft.agent_id == job.agent_id)
    )
    draft = draft_result.scalar_one_or_none()
    if draft is None:
        raise NotFoundError("Draft not found")
    config = deepcopy(draft.authoring_json)
    flow = config.get("flow") if isinstance(config.get("flow"), dict) else config
    steps = flow.get("steps") if isinstance(flow, dict) else None
    step = next(
        (
            item
            for item in steps or []
            if isinstance(item, dict) and item.get("id") == target_step_id
        ),
        None,
    )
    if step is None:
        raise ConflictError("The suggested target step is no longer in the draft")
    current_instruction = step.get("instruction", "")
    if not isinstance(current_instruction, str):
        raise ConflictError("The suggested target step has no editable instruction")
    if append_text in current_instruction:
        raise ConflictError("The suggested edit is already present in the draft")
    step["instruction"] = (
        f"{current_instruction.rstrip()}\n\n{append_text}".strip()
    )
    catalog = await _catalog(request, db)
    view = await update_draft(
        db,
        agent_id=job.agent_id,
        owner_user_id=principal.user_id,
        payload=DraftUpdate(expected_revision=payload.expected_revision, config=config),
        catalog=catalog,
        source_ip=request.client.host if request.client else None,
    )
    suggestions = [dict(item) for item in job.suggestions_json or []]
    for item in suggestions:
        if item.get("suggestion_id") == suggestion_id:
            item["status"] = "APPLIED_TO_DRAFT"
            item["applied_at"] = datetime.now(timezone.utc).isoformat()
            item["target_draft_revision"] = view.draft_revision
            break
    job.suggestions_json = suggestions
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return QASuggestionApplied(
        suggestion_id=suggestion_id,
        draft_revision=view.draft_revision,
        authoring_hash=view.authoring_hash,
        applied=True,
    )


@router.post(
    "/jobs/{job_id}/suggestions/{suggestion_id}/dismiss",
    response_model=QASuggestionDismissed,
)
async def dismiss_qa_suggestion(
    job_id: uuid.UUID,
    suggestion_id: str,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    _ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> QASuggestionDismissed:
    _settings(request)
    job = await _locked_qa_job(db, owner_user_id=principal.user_id, job_id=job_id)
    if job.status != "COMPLETED":
        raise ConflictError("QA suggestions are available only after a completed job")
    suggestion = _qa_suggestion(job, suggestion_id)
    if suggestion.get("status") != "DRAFT":
        raise ConflictError("QA suggestion is no longer available for dismissal")
    suggestions = [dict(item) for item in job.suggestions_json or []]
    for item in suggestions:
        if item.get("suggestion_id") == suggestion_id:
            item["status"] = "DISMISSED"
            item["dismissed_at"] = datetime.now(timezone.utc).isoformat()
            break
    job.suggestions_json = suggestions
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return QASuggestionDismissed(suggestion_id=suggestion_id, status="DISMISSED")


async def _locked_qa_job(
    db: AsyncSession, *, owner_user_id: uuid.UUID, job_id: uuid.UUID
) -> FlowQAJob:
    result = await db.execute(
        select(FlowQAJob)
        .where(FlowQAJob.id == job_id, FlowQAJob.owner_user_id == owner_user_id)
        .with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise NotFoundError("QA job not found")
    return job


def _qa_suggestion(job: FlowQAJob, suggestion_id: str) -> dict[str, object]:
    suggestion = next(
        (
            item
            for item in job.suggestions_json or []
            if isinstance(item, dict) and item.get("suggestion_id") == suggestion_id
        ),
        None,
    )
    if suggestion is None:
        raise NotFoundError("QA suggestion not found")
    return suggestion


def _settings(request: Request) -> PlatformSettings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None or not settings.enable_qa:
        raise DependencyUnavailableError("Flow QA is not enabled")
    return settings


async def _catalog(request: Request, db: AsyncSession) -> ModelCatalog:
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        catalog = load_catalog(_settings(request).catalog_path)
    return await catalog_with_managed_llms(catalog, db)

def _model_client(request: Request):
    client = getattr(request.app.state, "model_client", None)
    if client is None:
        raise DependencyUnavailableError("Flow QA model endpoints are unavailable")
    return client
