"""Administrator API and catalog queries for operator-managed model entries."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.admin.model_providers import model_provider_metadata
from platform_api.audit import record_audit
from platform_api.auth.dependencies import (
    get_client_ip,
    get_request_settings,
    require_csrf,
    require_step_up,
)
from platform_api.auth.errors import ConflictError, NotFoundError
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.db.session import get_db
from platform_api.models.provider_model_entry import ProviderModelEntry


router = APIRouter(tags=["admin-models"])


class ModelEntryView(BaseModel):
    provider_id: str
    model_id: str
    name: str
    enabled: bool
    qualified: bool = Field(
        description=(
            "Explicit operator attestation only; it does not prove installation, "
            "probe success, or measured/deployment qualification."
        )
    )


class ModelEntriesResponse(BaseModel):
    models: list[ModelEntryView]


class ModelEntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    enabled: StrictBool = False
    qualified: StrictBool = Field(
        default=False,
        description=(
            "Explicit operator attestation only; it does not prove installation, "
            "probe success, or measured/deployment qualification."
        ),
    )

    @field_validator("model_id")
    @classmethod
    def model_id_is_exact_and_nonblank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError(
                "model_id must be nonblank without leading or trailing whitespace"
            )
        return value

    @field_validator("name")
    @classmethod
    def display_name_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class ModelEntryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: StrictBool | None = None
    qualified: StrictBool | None = Field(
        default=None,
        description=(
            "Explicit operator attestation only; it does not prove installation, "
            "probe success, or measured/deployment qualification."
        ),
    )

    @field_validator("name")
    @classmethod
    def display_name_is_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @model_validator(mode="after")
    def update_has_values(self) -> ModelEntryUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field must be supplied")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Update fields must not be null")
        return self


def _entry_view(row: ProviderModelEntry) -> ModelEntryView:
    return ModelEntryView(
        provider_id=row.provider_id,
        model_id=row.model_id,
        name=row.name,
        enabled=row.enabled,
        qualified=row.qualified,
    )


def _entry_snapshot(row: ProviderModelEntry | None) -> dict[str, str | bool] | None:
    if row is None:
        return None
    return {
        "provider_id": row.provider_id,
        "model_id": row.model_id,
        "name": row.name,
        "enabled": row.enabled,
        "qualified": row.qualified,
    }


def _require_model_provider(provider_id: str) -> None:
    if model_provider_metadata(provider_id) is None:
        raise NotFoundError()


async def list_model_entries(db: AsyncSession) -> list[ModelEntryView]:
    """Return every operator-managed provider/model row in stable identity order."""
    result = await db.execute(
        select(ProviderModelEntry).order_by(
            ProviderModelEntry.provider_id,
            ProviderModelEntry.model_id,
        ).execution_options(populate_existing=True)
    )
    return [_entry_view(row) for row in result.scalars().all()]


async def list_qualified_enabled_model_entries(
    db: AsyncSession,
) -> list[ModelEntryView]:
    """Return only entries both enabled and explicitly operator-attested as qualified."""
    result = await db.execute(
        select(ProviderModelEntry)
        .where(
            ProviderModelEntry.enabled.is_(True),
            ProviderModelEntry.qualified.is_(True),
        )
        .order_by(ProviderModelEntry.provider_id, ProviderModelEntry.model_id)
    )
    return [_entry_view(row) for row in result.scalars().all()]


async def _list_provider_model_entries(
    db: AsyncSession,
    provider_id: str,
) -> list[ModelEntryView]:
    result = await db.execute(
        select(ProviderModelEntry)
        .where(ProviderModelEntry.provider_id == provider_id)
        .order_by(ProviderModelEntry.model_id)
    )
    return [_entry_view(row) for row in result.scalars().all()]


async def _audit_model_change(
    db: AsyncSession,
    request: Request,
    principal: Principal,
    *,
    action: str,
    provider_id: str,
    model_id: str,
    before: ProviderModelEntry | None,
    after: ProviderModelEntry | None,
) -> None:
    settings = get_request_settings(request)
    await record_audit(
        db,
        actor_kind="user",
        actor_id=principal.user_id,
        session_id=principal.session_id,
        action=action,
        target_type="ollama_model",
        result="success",
        request_id=getattr(request.state, "request_id", uuid.uuid4()),
        source_ip=get_client_ip(request, settings),
        change_summary={
            "provider_id": provider_id,
            "model_id": model_id,
            "before": _entry_snapshot(before),
            "after": _entry_snapshot(after),
        },
    )


@router.get(
    "/{provider_id}/models",
    response_model=ModelEntriesResponse,
)
async def list_admin_model_entries(
    provider_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_step_up),
) -> ModelEntriesResponse:
    _require_model_provider(provider_id)
    response.headers["Cache-Control"] = "no-store"
    return ModelEntriesResponse(
        models=await _list_provider_model_entries(db, provider_id)
    )


@router.post(
    "/{provider_id}/models",
    response_model=ModelEntryView,
    status_code=status.HTTP_201_CREATED,
)
async def create_admin_model_entry(
    provider_id: str,
    payload: ModelEntryCreate,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> ModelEntryView:
    _require_model_provider(provider_id)
    response.headers["Cache-Control"] = "no-store"
    row = ProviderModelEntry(
        provider_id=provider_id,
        model_id=payload.model_id,
        name=payload.name,
        enabled=payload.enabled,
        qualified=payload.qualified,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise ConflictError() from None
    await _audit_model_change(
        db,
        request,
        principal,
        action="ollama_model_created",
        provider_id=provider_id,
        model_id=payload.model_id,
        before=None,
        after=row,
    )
    await db.commit()
    return _entry_view(row)


@router.patch(
    "/{provider_id}/models/{model_id:path}",
    response_model=ModelEntryView,
)
async def update_admin_model_entry(
    provider_id: str,
    model_id: str,
    payload: ModelEntryUpdate,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> ModelEntryView:
    _require_model_provider(provider_id)
    response.headers["Cache-Control"] = "no-store"
    row = await db.get(
        ProviderModelEntry,
        (provider_id, model_id),
        with_for_update=True,
    )
    if row is None:
        raise NotFoundError()

    before = ProviderModelEntry(
        provider_id=row.provider_id,
        model_id=row.model_id,
        name=row.name,
        enabled=row.enabled,
        qualified=row.qualified,
    )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.flush()
    await _audit_model_change(
        db,
        request,
        principal,
        action="ollama_model_updated",
        provider_id=provider_id,
        model_id=model_id,
        before=before,
        after=row,
    )
    await db.commit()
    return _entry_view(row)


@router.delete(
    "/{provider_id}/models/{model_id:path}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_admin_model_entry(
    provider_id: str,
    model_id: str,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_step_up),
    _csrf: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_model_provider(provider_id)
    response.headers["Cache-Control"] = "no-store"
    row = await db.get(
        ProviderModelEntry,
        (provider_id, model_id),
        with_for_update=True,
    )
    if row is None:
        raise NotFoundError()
    before = ProviderModelEntry(
        provider_id=row.provider_id,
        model_id=row.model_id,
        name=row.name,
        enabled=row.enabled,
        qualified=row.qualified,
    )
    await db.delete(row)
    await _audit_model_change(
        db,
        request,
        principal,
        action="ollama_model_deleted",
        provider_id=provider_id,
        model_id=model_id,
        before=before,
        after=None,
    )
    await db.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
