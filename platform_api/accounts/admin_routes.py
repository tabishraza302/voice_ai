"""Admin user management and suspension routes mounted at /api/v1.

Follows auth-contract.md:
- GET /admin/users: paginated with bounded cursor, ordered stably by (created_at, id).
- GET /admin/users/{user_id}: safe user metadata view.
- POST /admin/users/{user_id}/suspend: requires fresh step-up, validates reason (10-500 chars),
  protects last enrolled admin under PostgreSQL advisory lock, revokes sessions, audits in transaction.
- POST /admin/users/{user_id}/restore: requires fresh step-up, validates reason, audits in transaction.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.audit import record_audit
from platform_api.auth.dependencies import (
    get_client_ip,
    get_request_settings,
    require_admin,
    require_csrf,
    require_step_up,
)
from platform_api.auth.errors import (
    LastAdminError,
    NotFoundError,
    ValidationError,
)
from platform_api.auth.schemas import (
    AdminRestoreRequest,
    AdminSuspendRequest,
    UserPage,
    UserView,
)
from platform_api.auth.service import build_user_view
from platform_api.auth.sessions import revoke_all_user_sessions
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.db.session import get_db
from platform_api.models.auth import AuthMfa, User

PLATFORM_LAST_ADMIN_ADVISORY_LOCK_ID = 8100984920448107370

router = APIRouter(prefix="/admin", tags=["admin"])


def _validate_reason(reason_raw: str) -> str:
    """Validate admin reason string: trimmed 10–500 codepoints."""
    trimmed = reason_raw.strip()
    if len(trimmed) < 10:
        raise ValidationError(
            "Reason must be at least 10 characters",
            fields=[{"field": "reason", "code": "too_short"}],
        )
    if len(trimmed) > 500:
        raise ValidationError(
            "Reason cannot exceed 500 characters",
            fields=[{"field": "reason", "code": "too_long"}],
        )
    return trimmed


def _encode_cursor(created_at: datetime, user_id: uuid.UUID) -> str:
    payload = json.dumps({"t": created_at.isoformat(), "id": str(user_id)}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(raw_cursor: str) -> tuple[datetime, uuid.UUID]:
    if len(raw_cursor) > 256:
        raise ValidationError(
            "Cursor exceeds maximum length",
            fields=[{"field": "cursor", "code": "invalid_cursor"}],
        )
    pad = len(raw_cursor) % 4
    padded = raw_cursor + ("=" * (4 - pad) if pad else "")
    try:
        data = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(data.decode("utf-8"))
        created_at = datetime.fromisoformat(payload["t"])
        user_id = uuid.UUID(payload["id"])
        return created_at, user_id
    except Exception as e:
        raise ValidationError(
            "Malformed pagination cursor",
            fields=[{"field": "cursor", "code": "invalid_cursor"}],
        ) from e


@router.get("/users", response_model=UserPage)
async def list_users(
    response: Response,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=256),
    q: str | None = Query(default=None, max_length=80),
    status: Literal["active", "suspended"] | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UserPage:
    """List users ordered stably by (created_at, id) with cursor pagination."""
    query = select(User).order_by(User.created_at.asc(), User.id.asc())

    if status:
        query = query.where(User.status == status)

    if q:
        search = f"%{q.strip().lower()}%"
        query = query.where(
            or_(
                func.lower(User.email).like(search),
                func.lower(User.display_name).like(search),
            )
        )

    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                User.created_at > cursor_time,
                (User.created_at == cursor_time) & (User.id > cursor_id),
            )
        )

    # Fetch limit + 1 to detect next page
    query = query.limit(limit + 1)
    result = await db.execute(query)
    users = list(result.scalars().all())

    has_more = len(users) > limit
    page_users = users[:limit]

    # Preload MFA status for listed users
    user_ids = [u.id for u in page_users]
    mfa_map: dict[uuid.UUID, bool] = {}
    if user_ids:
        mfa_stmt = select(AuthMfa).where(AuthMfa.user_id.in_(user_ids))
        mfa_res = await db.execute(mfa_stmt)
        for mfa in mfa_res.scalars().all():
            if mfa.enabled_at is not None:
                mfa_map[mfa.user_id] = True

    items = [build_user_view(u, mfa_enabled=mfa_map.get(u.id, False)) for u in page_users]

    next_cursor = None
    if has_more and page_users:
        last = page_users[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    response.headers["Cache-Control"] = "no-store"
    return UserPage(items=items, next_cursor=next_cursor)


@router.get("/users/{user_id}", response_model=UserView)
async def get_user_by_id(
    user_id: uuid.UUID,
    response: Response,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UserView:
    """Return safe user metadata for administrative inspection."""
    stmt = select(User).where(User.id == user_id)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found")

    mfa_stmt = select(AuthMfa).where(AuthMfa.user_id == user.id)
    mfa_res = await db.execute(mfa_stmt)
    mfa = mfa_res.scalar_one_or_none()
    mfa_enabled = bool(mfa and mfa.enabled_at is not None)

    response.headers["Cache-Control"] = "no-store"
    return build_user_view(user, mfa_enabled=mfa_enabled)


@router.post("/users/{user_id}/suspend", response_model=UserView)
async def suspend_user(
    user_id: uuid.UUID,
    payload: AdminSuspendRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_step_up),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> UserView:
    """Suspend user account, revoke sessions, and record audit log."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)
    clean_reason = _validate_reason(payload.reason)

    # Acquire PostgreSQL transaction advisory lock for last-admin safety
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": PLATFORM_LAST_ADMIN_ADVISORY_LOCK_ID},
    )

    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user_res = await db.execute(user_stmt)
    target_user = user_res.scalar_one_or_none()
    if target_user is None:
        raise NotFoundError("User not found")

    # Check last active enrolled admin protection
    if target_user.role == "admin" and target_user.status == "active":
        admin_count_stmt = (
            select(func.count(User.id))
            .join(AuthMfa, AuthMfa.user_id == User.id)
            .where(
                User.role == "admin",
                User.status == "active",
                AuthMfa.enabled_at.is_not(None),
            )
        )
        admin_count = (await db.execute(admin_count_stmt)).scalar_one()
        if admin_count <= 1:
            raise LastAdminError("Cannot suspend the last active administrator")

    previous_status = target_user.status
    if previous_status == "active":
        target_user.status = "suspended"
        target_user.auth_version += 1
        target_user.updated_at = now
        await revoke_all_user_sessions(db, target_user.id, now)

    await record_audit(
        db,
        actor_kind="user",
        actor_id=principal.user_id,
        session_id=principal.session_id,
        action="admin.user.suspend",
        target_type="user",
        target_id=target_user.id,
        reason=clean_reason,
        source_ip=client_ip,
        result="success",
        change_summary={
            "previous_status": previous_status,
            "new_status": "suspended",
        },
        created_at=now,
    )

    await db.commit()

    mfa_stmt = select(AuthMfa).where(AuthMfa.user_id == target_user.id)
    mfa_res = await db.execute(mfa_stmt)
    mfa = mfa_res.scalar_one_or_none()
    mfa_enabled = bool(mfa and mfa.enabled_at is not None)

    response.headers["Cache-Control"] = "no-store"
    return build_user_view(target_user, mfa_enabled=mfa_enabled)


@router.post("/users/{user_id}/restore", response_model=UserView)
async def restore_user(
    user_id: uuid.UUID,
    payload: AdminRestoreRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_step_up),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> UserView:
    """Restore suspended user account and record audit log."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)
    clean_reason = _validate_reason(payload.reason)

    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user_res = await db.execute(user_stmt)
    target_user = user_res.scalar_one_or_none()
    if target_user is None:
        raise NotFoundError("User not found")

    previous_status = target_user.status
    if previous_status == "suspended":
        target_user.status = "active"
        target_user.updated_at = now

    await record_audit(
        db,
        actor_kind="user",
        actor_id=principal.user_id,
        session_id=principal.session_id,
        action="admin.user.restore",
        target_type="user",
        target_id=target_user.id,
        reason=clean_reason,
        source_ip=client_ip,
        result="success",
        change_summary={
            "previous_status": previous_status,
            "new_status": "active",
        },
        created_at=now,
    )

    await db.commit()

    mfa_stmt = select(AuthMfa).where(AuthMfa.user_id == target_user.id)
    mfa_res = await db.execute(mfa_stmt)
    mfa = mfa_res.scalar_one_or_none()
    mfa_enabled = bool(mfa and mfa.enabled_at is not None)

    response.headers["Cache-Control"] = "no-store"
    return build_user_view(target_user, mfa_enabled=mfa_enabled)
