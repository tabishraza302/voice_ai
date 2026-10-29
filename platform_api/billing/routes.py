"""FastAPI routes for wallet, adjustments, and internal runtime metering.

Follows accounting-contract.md:
- GET /api/v1/wallet and /api/v1/wallet/entries are owner-only.
- POST /api/v1/admin/users/{id}/wallet-adjustments requires admin step-up, CSRF, and Idempotency-Key.
- POST /internal/sessions/{id}/budget/checkpoint and /close handle synchronous metering.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.dependencies import require_csrf, require_step_up, require_user
from platform_api.auth.errors import (
    DependencyUnavailableError,
    UnauthenticatedError,
    ValidationError,
)
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.billing.schemas import (
    AdminWalletAdjustmentRequest,
    BudgetPermit,
    BudgetView,
    CheckpointRequest,
    CloseBudgetRequest,
    WalletEntriesPage,
    WalletView,
)
from platform_api.billing.service import (
    RuntimePrincipal,
    checkpoint_and_reserve,
    close_budget,
    credit_wallet,
    get_wallet_entries,
    get_wallet_view,
)
from platform_api.db.session import get_db

# Owner wallet routes mounted under /api/v1
router = APIRouter(tags=["wallet"])

# Admin wallet routes mounted under /api/v1/admin
admin_router = APIRouter(prefix="/admin", tags=["admin_wallet"])

# Internal runtime metering routes mounted under /internal
internal_router = APIRouter(prefix="/sessions", tags=["internal_metering"])


# --- Owner Wallet Endpoints ---------------------------------------------------


@router.get("/wallet", response_model=WalletView)
async def get_wallet(
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> WalletView:
    """Return own verified wallet balance."""
    response.headers["Cache-Control"] = "no-store"
    return await get_wallet_view(db, user_id=principal.user_id)


@router.get("/wallet/entries", response_model=WalletEntriesPage)
async def list_wallet_entries(
    response: Response,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> WalletEntriesPage:
    """Return own wallet ledger entries with stable cursor pagination."""
    response.headers["Cache-Control"] = "no-store"
    return await get_wallet_entries(
        db,
        user_id=principal.user_id,
        cursor=cursor,
        limit=limit,
    )


# --- Admin Adjustment Endpoint ------------------------------------------------


@admin_router.post("/users/{user_id}/wallet-adjustments", response_model=WalletView)
async def adjust_user_wallet(
    user_id: uuid.UUID,
    payload: AdminWalletAdjustmentRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_step_up),
    ctx: AuthSessionContext = Depends(require_csrf),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
) -> WalletView:
    """Admin endpoint to credit or debit a user's wallet with step-up verification."""
    idempotency_key = idempotency_key.strip()
    if not idempotency_key:
        raise ValidationError("Idempotency-Key header cannot be empty")
    if len(idempotency_key) > 128:
        raise ValidationError("Idempotency-Key header cannot exceed 128 characters")
    view = await credit_wallet(
        db,
        actor=f"admin:{principal.user_id}",
        user_id=user_id,
        amount_minor=payload.amount_minor,
        reason=payload.reason,
        operation_id=idempotency_key,
        kind="adjustment",
        audit_actor_kind="admin",
        audit_actor_id=principal.user_id,
        audit_session_id=principal.session_id,
    )

    response.headers["Cache-Control"] = "no-store"
    return view


# --- Internal Runtime Metering Endpoints --------------------------------------


def get_runtime_principal(
    request: Request,
) -> RuntimePrincipal:
    """Fail-closed dependency: genuine runtime authentication belongs to Task 6 (transport_auth).

    Arbitrary caller-supplied headers are strictly forbidden from acting as an authenticated principal.
    """
    raise DependencyUnavailableError(
        "Internal runtime authentication is unavailable; genuine transport auth is a Task 6 prerequisite"
    )

@internal_router.post("/{session_id}/budget/checkpoint", response_model=BudgetPermit)
async def session_budget_checkpoint(
    session_id: uuid.UUID,
    payload: CheckpointRequest,
    request: Request,
    response: Response,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
    db: AsyncSession = Depends(get_db),
) -> BudgetPermit:
    """Synchronous checkpoint of proven billable time and renewal of cumulative permit."""
    response.headers["Cache-Control"] = "no-store"
    settings = getattr(request.app.state, "settings", None)
    slice_ms = getattr(settings, "billing_slice_ms", None) if settings else None
    ttl_ms = getattr(settings, "billing_permit_ttl_ms", None) if settings else None
    if slice_ms is None or ttl_ms is None:
        raise DependencyUnavailableError(
            "Billing deployment configuration missing: PLATFORM_BILLING_SLICE_MS and PLATFORM_BILLING_PERMIT_TTL_MS must be explicitly configured"
        )
    return await checkpoint_and_reserve(
        db,
        runtime_principal=principal,
        session_id=session_id,
        operation_id=payload.operation_id,
        expected_budget_version=payload.expected_budget_version,
        cumulative_billable_ms=payload.cumulative_billable_ms,
        requested_future_ms=payload.requested_future_ms,
        billing_slice_ms=slice_ms,
        permit_ttl_ms=ttl_ms,
    )


@internal_router.post("/{session_id}/budget/close", response_model=BudgetView)
async def session_budget_close(
    session_id: uuid.UUID,
    payload: CloseBudgetRequest,
    response: Response,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
    db: AsyncSession = Depends(get_db),
) -> BudgetView:
    """Synchronous settlement of final billable time and complete reservation release."""
    response.headers["Cache-Control"] = "no-store"
    return await close_budget(
        db,
        runtime_principal=principal,
        session_id=session_id,
        operation_id=payload.operation_id,
        expected_budget_version=payload.expected_budget_version,
        cumulative_billable_ms=payload.cumulative_billable_ms,
        stop_evidence=payload.stop_evidence,
    )
