"""Authentication HTTP endpoints mounted at /api/v1.

Follows auth-contract.md:
- Exact wire schemas and routes.
- Strict Origin, CSRF, and media-type enforcement on mutations.
- Cache-Control: no-store on all auth responses.
- Anonymous bootstrap on GET /auth/session.
- 204 No Content for empty operations without JSON bodies.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.audit import record_audit
from platform_api.auth.csrf import (
    generate_csrf_token,
    validate_origin,
)
from platform_api.auth.dependencies import (
    get_client_ip,
    get_request_settings,
    require_csrf,
    require_step_up,
    require_user,
)
from platform_api.auth.errors import (
    ForbiddenError,
    InvalidCredentialsError,
    NotFoundError,
    TokenInvalidError,
    UnauthenticatedError,
    ValidationError,
)
from platform_api.auth.passwords import get_password_hasher, validate_password_rules
from platform_api.auth.rate_limits import (
    check_and_increment_rate_limit,
    record_rate_limit_failure,
)
from platform_api.auth.schemas import (
    Accepted,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordConfirmRequest,
    ResetPasswordRequest,
    SessionItem,
    SessionList,
    SessionResponse,
    StepUpRequest,
    UserView,
    VerifyEmailRequest,
)
from platform_api.auth.service import (
    build_user_view,
    confirm_password_reset,
    normalize_email_address,
    register_account,
    request_password_reset,
    verify_email_and_create_user,
)
from platform_api.auth.sessions import (
    create_anonymous_session,
    create_authenticated_session,
    delete_session_cookie,
    get_cookie_name,
    hash_session_secret,
    revoke_all_user_sessions,
    revoke_session,
    set_session_cookie,
)
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.config import PlatformSettings
from platform_api.db.session import get_db
from platform_api.models.auth import AuthSession, User

async def enforce_global_auth_rate_limit(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Enforce contract global auth limit across all auth endpoints: 120/min/IP."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)
    if client_ip:
        await check_and_increment_rate_limit(
            db,
            settings,
            scope="auth_global_ip",
            identifier=client_ip,
            limit=settings.auth_rate_global_per_minute,
            window_seconds=60,
            now=now,
        )


router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    dependencies=[Depends(enforce_global_auth_rate_limit)],
)


# --- Helper: build SessionResponse and set cookie -----------------------------


def _build_session_response(
    settings: PlatformSettings,
    session: AuthSession,
    raw_secret: str,
    user_view: UserView | None,
    response: Response,
    now: datetime,
) -> SessionResponse:
    """Derive CSRF token, set session cookie, and return SessionResponse."""
    csrf_token = generate_csrf_token(settings.auth_csrf_key, session.id, session.token_digest)
    remaining_seconds = max(0, int((session.absolute_expires_at - now).total_seconds()))
    set_session_cookie(response, settings, raw_secret, remaining_seconds)
    response.headers["Cache-Control"] = "no-store"

    step_up_exp = None
    if session.step_up_at is not None:
        step_up_exp = session.step_up_at + (
            session.absolute_expires_at - session.absolute_expires_at  # placeholder type
        )
        step_up_exp = session.step_up_at + (
            datetime.fromtimestamp(settings.auth_step_up_seconds, tz=timezone.utc)
            - datetime.fromtimestamp(0, tz=timezone.utc)
        )

    return SessionResponse(
        state=session.state,  # type: ignore[arg-type]
        user=user_view,
        session_id=session.id,
        expires_at=session.absolute_expires_at,
        csrf_token=csrf_token,
        step_up_expires_at=step_up_exp,
    )


# 1. GET /auth/session ---------------------------------------------------------


@router.get("/session", response_model=SessionResponse)
async def get_session(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    """Bootstrap anonymous session or return current valid session state."""
    settings = get_request_settings(request)
    validate_origin(request, settings)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)


    cookie_name = get_cookie_name(settings)
    raw_cookie = request.cookies.get(cookie_name)
    token_digest = hash_session_secret(raw_cookie) if raw_cookie else None

    # Try loading existing active session
    if token_digest is not None:
        stmt = (
            select(AuthSession)
            .where(
                AuthSession.token_digest == token_digest,
                AuthSession.revoked_at.is_(None),
                AuthSession.idle_expires_at > now,
                AuthSession.absolute_expires_at > now,
            )
        )
        res = await db.execute(stmt)
        session = res.scalar_one_or_none()

        if session is not None:
            user_view: UserView | None = None
            if session.user_id is not None:
                user_stmt = select(User).where(User.id == session.user_id)
                user_res = await db.execute(user_stmt)
                user = user_res.scalar_one_or_none()
                if (
                    user is not None
                    and user.status == "active"
                    and session.auth_version == user.auth_version
                ):
                    user_view = build_user_view(user)
                else:
                    # User suspended or version invalid: revoke session
                    session.revoked_at = now
                    await db.commit()
                    session = None
                if session is not None:
                    csrf_token = generate_csrf_token(settings.auth_csrf_key, session.id, session.token_digest)
                    response.headers["Cache-Control"] = "no-store"
                    step_up_exp = None
                    if session.step_up_at is not None:
                        step_up_exp = session.step_up_at + (
                            datetime.fromtimestamp(settings.auth_step_up_seconds, tz=timezone.utc)
                            - datetime.fromtimestamp(0, tz=timezone.utc)
                        )
                    return SessionResponse(
                        state=session.state,  # type: ignore[arg-type]
                        user=user_view,
                        session_id=session.id,
                        expires_at=session.absolute_expires_at,
                        csrf_token=csrf_token,
                        step_up_expires_at=step_up_exp,
                    )

    # If cookie missing, expired, revoked, or invalid: create new anonymous session
    if client_ip:
        await check_and_increment_rate_limit(
            db,
            settings,
            scope="bootstrap_anonymous_ip",
            identifier=client_ip,
            limit=settings.auth_rate_bootstrap_per_minute,
            window_seconds=60,
            now=now,
        )

    user_agent = request.headers.get("user-agent")
    new_session, raw_secret = await create_anonymous_session(
        db, settings, client_ip=client_ip, user_agent=user_agent, now=now
    )
    await db.commit()

    return _build_session_response(
        settings, new_session, raw_secret, None, response, now
    )


# 2. POST /auth/register -------------------------------------------------------


@router.post("/register", status_code=status.HTTP_202_ACCEPTED, response_model=Accepted)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Accepted:
    """Initiate user registration. Always returns 202 Accepted."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)

    result = await register_account(
        db,
        settings,
        email_raw=payload.email,
        display_name_raw=payload.display_name,
        client_ip=client_ip,
        now=now,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


# 3. POST /auth/verify-email ---------------------------------------------------


@router.post("/verify-email", status_code=status.HTTP_201_CREATED, response_model=UserView)
async def verify_email(
    payload: VerifyEmailRequest,
    request: Request,
    response: Response,
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> UserView:
    """Confirm registration token, select password, and initialize user."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)

    user_view = await verify_email_and_create_user(
        db,
        settings,
        token_str=payload.token,
        password_str=payload.password,
        client_ip=client_ip,
        now=now,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return user_view


# 4. POST /auth/login ----------------------------------------------------------


@router.post("/login", response_model=SessionResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    """Verify credentials and rotate session."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)
    user_agent = request.headers.get("user-agent")

    display_email, email_key = normalize_email_address(payload.email)

    # 1. Rate limits
    if client_ip:
        await check_and_increment_rate_limit(
            db,
            settings,
            scope="login_ip",
            identifier=client_ip,
            limit=settings.auth_rate_login_ip_per_15_minutes,
            window_seconds=900,
            now=now,
        )
    await check_and_increment_rate_limit(
        db,
        settings,
        scope="login_email",
        identifier=email_key,
        limit=settings.auth_rate_login_email_per_15_minutes,
        window_seconds=900,
        now=now,
    )

    # 2. Look up user
    user_stmt = select(User).where(User.email_key == email_key)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()

    hasher = get_password_hasher(settings.auth_hash_concurrency)

    async def _handle_credential_failure():
        # Transactionally revoke prior authenticated session on failed identity switch
        should_expire = False
        if ctx.session.user_id is not None or ctx.session.state == "authenticated":
            ctx.session.revoked_at = now
            await db.commit()
            should_expire = True
        delete_session_cookie(response, settings)
        raise InvalidCredentialsError(expire_cookie=should_expire)
    # 3. Unknown account dummy verification
    if user is None:
        await hasher.verify_dummy()
        await _handle_credential_failure()

    # 4. Verify password
    is_valid = await hasher.verify_password(payload.password, user.password_hash)
    if not is_valid:
        await _handle_credential_failure()

    # 5. Check suspension (anti-enumeration: same 401 response)
    if user.status != "active":
        await _handle_credential_failure()

    # 6. Revalidate and lock old session in PostgreSQL before rotation
    old_session_stmt = (
        select(AuthSession)
        .where(
            AuthSession.id == ctx.session.id,
            AuthSession.revoked_at.is_(None),
            AuthSession.idle_expires_at > now,
            AuthSession.absolute_expires_at > now,
        )
        .with_for_update()
    )
    old_session = (await db.execute(old_session_stmt)).scalar_one_or_none()
    if old_session is None:
        raise CsrfInvalidError("Session is expired or has already been rotated")

    old_session.revoked_at = now
    new_session, raw_secret = await create_authenticated_session(
        db,
        settings,
        user=user,
        client_ip=client_ip,
        user_agent=user_agent,
        now=now,
    )
    user.last_login_at = now

    await db.commit()

    user_view = build_user_view(user)
    return _build_session_response(
        settings, new_session, raw_secret, user_view, response, now
    )


# 5. POST /auth/logout ---------------------------------------------------------


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke current session and expire its cookie."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)

    ctx.session.revoked_at = now
    await db.commit()

    delete_session_cookie(response, settings)
    response.headers["Cache-Control"] = "no-store"


# 6. POST /auth/password/reset-request -----------------------------------------


@router.post(
    "/password/reset-request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Accepted,
)
async def reset_request(
    payload: ResetPasswordRequest,
    request: Request,
    response: Response,
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Accepted:
    """Initiate password reset. Always returns 202 Accepted."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)

    result = await request_password_reset(
        db,
        settings,
        email_raw=payload.email,
        client_ip=client_ip,
        now=now,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


# 7. POST /auth/password/reset-confirm -----------------------------------------


@router.post("/password/reset-confirm", status_code=status.HTTP_204_NO_CONTENT)
async def reset_confirm(
    payload: ResetPasswordConfirmRequest,
    request: Request,
    response: Response,
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Confirm password reset with token. Revokes all sessions."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)

    await confirm_password_reset(
        db,
        settings,
        token_str=payload.token,
        password_str=payload.password,
        client_ip=client_ip,
        now=now,
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"


# 8. POST /auth/password/change ------------------------------------------------


@router.post("/password/change", response_model=SessionResponse)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    """Change password for authenticated user. Requires step-up for admin."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)
    user_agent = request.headers.get("user-agent")

    # If admin, require fresh step-up
    if principal.role == "admin":
        await require_step_up(principal, request)

    # Validate new password rules before checking current password
    validate_password_rules(payload.new_password)

    # Verify current password
    user_stmt = select(User).where(User.id == principal.user_id).with_for_update()
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if user is None:
        raise UnauthenticatedError()

    hasher = get_password_hasher(settings.auth_hash_concurrency)
    is_valid = await hasher.verify_password(payload.current_password, user.password_hash)
    if not is_valid:
        raise InvalidCredentialsError()

    # Hash new password
    new_hash = await hasher.hash_password(payload.new_password)

    # Bump auth_version and set new hash
    user.password_hash = new_hash
    user.auth_version += 1
    user.updated_at = now

    # Revoke all existing sessions for this user (including current)
    await revoke_all_user_sessions(db, user.id, now)

    # Create fresh rotated session with new auth_version
    new_session, raw_secret = await create_authenticated_session(
        db,
        settings,
        user=user,
        client_ip=client_ip,
        user_agent=user_agent,
        now=now,
    )

    await record_audit(
        db,
        actor_kind="user",
        actor_id=user.id,
        session_id=new_session.id,
        action="password.change",
        target_type="user",
        target_id=user.id,
        source_ip=client_ip,
        result="success",
        created_at=now,
    )

    await db.commit()

    user_view = build_user_view(user)
    return _build_session_response(
        settings, new_session, raw_secret, user_view, response, now
    )


# 9. GET /auth/sessions --------------------------------------------------------


@router.get("/sessions", response_model=SessionList)
async def list_sessions(
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SessionList:
    """List own active authenticated sessions, newest first, max 5."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(AuthSession)
        .where(
            AuthSession.user_id == principal.user_id,
            AuthSession.state == "authenticated",
            AuthSession.revoked_at.is_(None),
            AuthSession.idle_expires_at > now,
            AuthSession.absolute_expires_at > now,
        )
        .order_by(desc(AuthSession.created_at))
        .limit(5)
    )
    result = await db.execute(stmt)
    sessions = list(result.scalars().all())

    items = [
        SessionItem(
            id=s.id,
            current=(s.id == principal.session_id),
            user_agent_label=s.user_agent_label or "Unknown Device",
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            expires_at=s.idle_expires_at,
        )
        for s in sessions
    ]
    response.headers["Cache-Control"] = "no-store"
    return SessionList(items=items)


# 10. DELETE /auth/sessions/{session_id} ---------------------------------------


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    ctx: AuthSessionContext = Depends(require_csrf),
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke an active session belonging to current user."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)

    stmt = select(AuthSession).where(
        AuthSession.id == session_id,
        AuthSession.user_id == principal.user_id,
        AuthSession.revoked_at.is_(None),
    )
    res = await db.execute(stmt)
    target_session = res.scalar_one_or_none()
    if target_session is None:
        raise NotFoundError("Session not found")

    target_session.revoked_at = now
    await db.commit()

    if session_id == principal.session_id:
        delete_session_cookie(response, settings)

    response.headers["Cache-Control"] = "no-store"


# 11. POST /auth/logout-all ----------------------------------------------------


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke all own sessions and bump auth_version without requiring password."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)

    user_stmt = select(User).where(User.id == principal.user_id).with_for_update()
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if user is not None:
        user.auth_version += 1
        user.updated_at = now

    await revoke_all_user_sessions(db, principal.user_id, now)
    await db.commit()

    delete_session_cookie(response, settings)
    response.headers["Cache-Control"] = "no-store"


# 12. POST /auth/step-up -------------------------------------------------------


@router.post("/step-up", response_model=SessionResponse)
async def step_up(
    payload: StepUpRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    ctx: AuthSessionContext = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    """Fresh password step-up verification for privileged admin actions."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)
    client_ip = get_client_ip(request, settings)

    if principal.role != "admin":
        raise ForbiddenError("Only administrators require step-up authentication")

    # Rate limit attempts
    if client_ip:
        await check_and_increment_rate_limit(
            db,
            settings,
            scope="mfa_ip",
            identifier=client_ip,
            limit=settings.auth_rate_mfa_ip_per_15_minutes,
            window_seconds=900,
            now=now,
        )
    await check_and_increment_rate_limit(
        db,
        settings,
        scope="mfa_session",
        identifier=str(principal.session_id),
        limit=settings.auth_rate_mfa_session_per_5_minutes,
        window_seconds=300,
        now=now,
    )

    # 1. Verify password
    user_stmt = select(User).where(User.id == principal.user_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if user is None:
        raise UnauthenticatedError()

    hasher = get_password_hasher(settings.auth_hash_concurrency)
    is_valid_pw = await hasher.verify_password(payload.password, user.password_hash)
    if not is_valid_pw:
        raise InvalidCredentialsError()

    # 2. Update step_up_at on existing session
    ctx.session.step_up_at = now
    await db.commit()

    user_view = build_user_view(user)
    csrf_token = generate_csrf_token(settings.auth_csrf_key, ctx.session.id, ctx.session.token_digest)
    response.headers["Cache-Control"] = "no-store"

    step_up_exp = now + (
        datetime.fromtimestamp(settings.auth_step_up_seconds, tz=timezone.utc)
        - datetime.fromtimestamp(0, tz=timezone.utc)
    )

    return SessionResponse(
        state="authenticated",
        user=user_view,
        session_id=ctx.session.id,
        expires_at=ctx.session.absolute_expires_at,
        csrf_token=csrf_token,
        step_up_expires_at=step_up_exp,
    )
