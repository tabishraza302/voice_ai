"""Account profile management routes (/me) mounted at /api/v1.

Follows auth-contract.md:
- GET /me: full active session returns UserView.
- PATCH /me: accepts nonempty subset of {display_name, preferred_language, timezone, low_balance_notifications}.
- Strict validation: email, role, status, and owner fields return 422 VALIDATION.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.dependencies import require_csrf, require_user
from platform_api.auth.errors import UnauthenticatedError, ValidationError
from platform_api.auth.schemas import UpdateProfileRequest, UserView
from platform_api.auth.service import build_user_view, validate_display_name
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.db.session import get_db
from platform_api.models.auth import AuthMfa, User

router = APIRouter(tags=["account"])


@router.get("/me", response_model=UserView)
async def get_me(
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> UserView:
    """Return own verified account metadata."""
    stmt = select(User).where(User.id == principal.user_id)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    if user is None:
        raise UnauthenticatedError("User record not found")

    mfa_stmt = select(AuthMfa).where(AuthMfa.user_id == user.id)
    mfa_res = await db.execute(mfa_stmt)
    mfa = mfa_res.scalar_one_or_none()
    mfa_enabled = bool(mfa and mfa.enabled_at is not None)

    response.headers["Cache-Control"] = "no-store"
    return build_user_view(user, mfa_enabled=mfa_enabled)


@router.patch("/me", response_model=UserView)
async def update_me(
    payload: UpdateProfileRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> UserView:
    """Update profile preferences. Requires nonempty subset of allowed fields."""
    now = datetime.now(timezone.utc)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise ValidationError(
            "At least one profile field must be updated",
            fields=[{"field": "body", "code": "empty_update"}],
        )

    stmt = select(User).where(User.id == principal.user_id).with_for_update()
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    if user is None:
        raise UnauthenticatedError("User record not found")

    if "display_name" in updates and updates["display_name"] is not None:
        user.display_name = validate_display_name(updates["display_name"])
    if "preferred_language" in updates and updates["preferred_language"] is not None:
        user.preferred_language = updates["preferred_language"]
    if "timezone" in updates and updates["timezone"] is not None:
        user.timezone = updates["timezone"]
    if "low_balance_notifications" in updates and updates["low_balance_notifications"] is not None:
        user.low_balance_notifications = updates["low_balance_notifications"]

    user.updated_at = now
    await db.commit()

    mfa_stmt = select(AuthMfa).where(AuthMfa.user_id == user.id)
    mfa_res = await db.execute(mfa_stmt)
    mfa = mfa_res.scalar_one_or_none()
    mfa_enabled = bool(mfa and mfa.enabled_at is not None)

    response.headers["Cache-Control"] = "no-store"
    return build_user_view(user, mfa_enabled=mfa_enabled)
