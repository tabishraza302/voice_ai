"""Owner-scoped HTTP routes for generation catalog, jobs, and cancellation."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.dependencies import require_csrf, require_user
from platform_api.auth.errors import DependencyUnavailableError, NotFoundError
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.catalog import ModelCatalog
from platform_api.config import PlatformSettings
from platform_api.db.session import get_db
from platform_api.generation.models import GenerationJob
from platform_api.generation.schemas import (
    GenerationCancelRequest,
    GenerationCancelResponse,
    GenerationJobView,
    GenerationModelsResponse,
    GenerationQueuedResponse,
    GenerationRequest,
)
from platform_api.generation.service import (
    cancel_generation_job,
    enqueue_generation_job,
)


router = APIRouter()


@router.get("/generation-models", response_model=GenerationModelsResponse)
async def get_generation_models(
    request: Request,
    response: Response,
    _principal: Principal = Depends(require_user),
) -> GenerationModelsResponse:
    settings = _settings(request)
    _require_generation(settings)
    catalog = _catalog(request)
    response.headers["Cache-Control"] = "no-store"
    return GenerationModelsResponse(items=catalog.generation_models)


@router.post(
    "/agents/generate",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=GenerationQueuedResponse,
)
async def post_generate_agent(
    payload: GenerationRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    _ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> GenerationQueuedResponse:
    settings = _settings(request)
    _require_generation(settings)
    catalog = _catalog(request)
    _model_client(request)
    job = await enqueue_generation_job(
        db,
        settings=settings,
        catalog=catalog,
        owner_user_id=principal.user_id,
        requirement=payload.requirement,
        generation_model_id=payload.generation_model_id,
        language=payload.language,
        token_budget=payload.token_budget,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Retry-After"] = str(settings.generation_poll_after_seconds)
    return GenerationQueuedResponse(job_id=job.id, status="QUEUED")


@router.get("/generation-jobs/{job_id}", response_model=GenerationJobView)
async def get_generation_job(
    job_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> GenerationJobView:
    settings = _settings(request)
    result = await db.execute(
        select(GenerationJob).where(
            GenerationJob.id == job_id,
            GenerationJob.owner_user_id == principal.user_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise NotFoundError()
    response.headers["Cache-Control"] = "no-store"
    if job.status in {"QUEUED", "RUNNING"}:
        response.headers["Retry-After"] = str(settings.generation_poll_after_seconds)
    return GenerationJobView(
        status=job.status,
        proposal=job.proposal_json,
        unknown_business_facts=job.unknown_business_facts_json,
        validation_warnings=job.validation_warnings_json,
        usage=job.usage_json,
    )


@router.post(
    "/generation-jobs/{job_id}/cancel",
    response_model=GenerationCancelResponse,
)
async def post_cancel_generation_job(
    job_id: uuid.UUID,
    _payload: GenerationCancelRequest,
    principal: Principal = Depends(require_user),
    _ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> GenerationCancelResponse:
    job = await cancel_generation_job(
        db,
        owner_user_id=principal.user_id,
        job_id=job_id,
    )
    await db.commit()
    return GenerationCancelResponse(job_id=job.id, status=job.status)


def _settings(request: Request) -> PlatformSettings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise DependencyUnavailableError("Generation is not configured")
    return settings


def _catalog(request: Request) -> ModelCatalog:
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        raise DependencyUnavailableError("Generation catalog is unavailable")
    return catalog


def _model_client(request: Request):
    client = getattr(request.app.state, "model_client", None)
    if client is None:
        raise DependencyUnavailableError("Generation model endpoint is unavailable")
    return client


def _require_generation(settings: PlatformSettings) -> None:
    if not settings.enable_generation or settings.generation_llm_base_url is None:
        raise DependencyUnavailableError("Agent generation is not enabled")
