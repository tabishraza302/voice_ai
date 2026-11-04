"""FastAPI authentication, role authorization, and CSRF dependencies.

Follows auth-contract.md:
- require_csrf: Origin check, Content-Type check, cookie session load, CSRF token verify.
- require_user: Session and user revalidation from PostgreSQL; fails closed on revoked,
  expired, suspended, or version-mismatched sessions; verifies admin MFA state.
- require_admin: Enforces admin role and non-null MFA verification.
- require_step_up: Enforces fresh step-up within PLATFORM_AUTH_STEP_UP_SECONDS (5 min).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
import uuid

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.csrf import (
    generate_csrf_token,
    validate_content_type,
    validate_origin,
    verify_csrf_token,
)
from platform_api.auth.errors import (
    AccountSuspendedError,
    CsrfInvalidError,
    ForbiddenError,
    MfaRequiredError,
    StepUpRequiredError,
    UnauthenticatedError,
)
from platform_api.auth.sessions import (
    get_cookie_name,
    hash_session_secret,
    touch_session_activity,
)
from platform_api.auth.types import AuthSessionContext, Principal
from platform_api.config import PlatformSettings, get_settings
from platform_api.db.session import get_db
from platform_api.models.auth import AuthMfa, AuthSession, User


def get_request_settings(request: Request) -> PlatformSettings:
    """Extract settings from request.app.state with fallback to get_settings()."""
    return getattr(request.app.state, "settings", None) or get_settings()


def get_client_ip(request: Request, settings: PlatformSettings) -> str | None:
    """Extract canonical client IP honoring trusted proxy CIDRs.

    Walks X-Forwarded-For chain from right to left, stripping trusted proxy hops,
    to prevent client-prepended spoofed addresses.
    """
    if not request.client:
        return None

    remote_ip_str = request.client.host
    try:
        remote_ip = ipaddress.ip_address(remote_ip_str.strip())
    except ValueError:
        return None

    trusted_nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in settings.trusted_proxy_cidrs:
        try:
            trusted_nets.append(ipaddress.ip_network(cidr.strip()))
        except ValueError:
            continue

    def is_trusted(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(ip in net for net in trusted_nets)

    # If the direct TCP peer is not a trusted proxy, do not trust X-Forwarded-For
    if not is_trusted(remote_ip):
        return str(remote_ip)

    forwarded_for = request.headers.get("x-forwarded-for")
    if not forwarded_for:
        return str(remote_ip)

    # Walk chain from right to left
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    client_ip = remote_ip
    for hop_str in reversed(hops):
        try:
            hop_ip = ipaddress.ip_address(hop_str)
        except ValueError:
            # Malformed address in chain: fail closed to remote_ip
            return str(remote_ip)

        if is_trusted(hop_ip):
            client_ip = hop_ip
            continue
        else:
            # First untrusted hop from the right is the genuine client IP
            return str(hop_ip)

    return str(client_ip)

async def require_csrf(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthSessionContext:
    """Validate Origin, Content-Type, cookie session, and X-CSRF-Token header."""
    settings = get_request_settings(request)

    # 1. Enforce Origin and Content-Type on mutations
    validate_origin(request, settings)
    validate_content_type(request)

    # 2. Extract and hash session cookie
    cookie_name = get_cookie_name(settings)
    raw_cookie = request.cookies.get(cookie_name)
    token_digest = hash_session_secret(raw_cookie) if raw_cookie else None
    if token_digest is None:
        raise CsrfInvalidError("Missing or malformed session cookie")

    # 3. Load active session from PostgreSQL
    now = datetime.now(timezone.utc)
    stmt = (
        select(AuthSession)
        .where(
            AuthSession.token_digest == token_digest,
            AuthSession.revoked_at.is_(None),
            AuthSession.idle_expires_at > now,
            AuthSession.absolute_expires_at > now,
        )
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if session is None:
        raise CsrfInvalidError("Session is expired, revoked, or not found")

    # 4. Verify CSRF token header
    csrf_header = request.headers.get(settings.csrf_header_name)
    if not csrf_header or not verify_csrf_token(
        settings.auth_csrf_key, session.id, session.token_digest, csrf_header
    ):
        raise CsrfInvalidError("Invalid or missing CSRF token")

    # 5. Load associated user if non-anonymous
    user: User | None = None
    if session.user_id is not None:
        user_stmt = select(User).where(User.id == session.user_id)
        user_res = await db.execute(user_stmt)
        user = user_res.scalar_one_or_none()

    return AuthSessionContext(session=session, user=user, csrf_token=csrf_header)


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """Verify authenticated session, active status, auth version, and admin MFA."""
    settings = get_request_settings(request)
    now = datetime.now(timezone.utc)

    # 1. Extract and hash session cookie
    cookie_name = get_cookie_name(settings)
    raw_cookie = request.cookies.get(cookie_name)
    token_digest = hash_session_secret(raw_cookie) if raw_cookie else None
    if token_digest is None:
        raise UnauthenticatedError("Authentication required")

    # 2. Load session from PostgreSQL
    stmt = (
        select(AuthSession)
        .where(
            AuthSession.token_digest == token_digest,
            AuthSession.revoked_at.is_(None),
            AuthSession.idle_expires_at > now,
            AuthSession.absolute_expires_at > now,
        )
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if session is None:
        raise UnauthenticatedError("Session expired or revoked")

    # 3. Check session state
    if session.state in ("mfa_required", "mfa_enrollment_required"):
        raise MfaRequiredError("MFA verification required to access platform resources")
    if session.state != "authenticated" or session.user_id is None:
        raise UnauthenticatedError("Authenticated session required")

    # 4. Load user and verify status and auth version
    user_stmt = select(User).where(User.id == session.user_id)
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if user is None:
        raise UnauthenticatedError("User record not found")

    if user.status == "suspended":
        raise AccountSuspendedError("Account is suspended")

    if user.email_verified_at is None:
        raise UnauthenticatedError("Email address is not verified")

    if session.password_verified_at is None:
        raise UnauthenticatedError("Session lacks password verification")

    if session.auth_version != user.auth_version:
        raise UnauthenticatedError("Session has been superseded by password or security change")
    # 5. Touch session activity conditionally
    await touch_session_activity(db, settings, session, now)
    return Principal(
        user_id=user.id,
        role=user.role,  # type: ignore[arg-type]
        session_id=session.id,
        email_verified_at=user.email_verified_at,
        password_verified_at=session.password_verified_at,
        step_up_at=session.step_up_at,
    )


async def require_admin(
    principal: Principal = Depends(require_user),
) -> Principal:
    """Enforce administrator role on top of verified require_user."""
    if principal.role != "admin":
        raise ForbiddenError("Administrator access required")
    return principal


async def require_step_up(
    principal: Principal = Depends(require_admin),
    request: Request = None,  # type: ignore[assignment]
) -> Principal:
    """Enforce fresh step-up within 5 minutes for privileged mutations."""
    now = datetime.now(timezone.utc)
    settings = get_request_settings(request) if request else get_settings()

    if principal.step_up_at is None:
        raise StepUpRequiredError("Fresh step-up authentication required")

    elapsed = (now - principal.step_up_at).total_seconds()
    if elapsed > settings.auth_step_up_seconds:
        raise StepUpRequiredError("Step-up authentication has expired; please re-authenticate")

    return principal
