"""Service operations for public deployment links and session-scoped guests."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import secrets
import uuid
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.agents.models import Agent
from platform_api.agents.service import (
    check_idempotency_record,
    compute_payload_hash,
    lock_idempotency_key,
    store_idempotency_record,
)
from platform_api.auth.errors import (
    ConflictError,
    CsrfInvalidError,
    DependencyUnavailableError,
    ForbiddenError,
    NotFoundError,
    OriginForbiddenError,
    PlatformError,
    ValidationError,
)
from platform_api.auth.rate_limits import check_and_increment_rate_limit
from platform_api.auth.types import Principal
from platform_api.config import PlatformSettings
from platform_api.catalog import ModelCatalog
from platform_api.models.auth import User
from platform_api.models.session import Session
from platform_api.public.models import PublicDeployment, PublicGuestCapability
from platform_api.public.schemas import (
    GuestAdmissionRequest,
    GuestAdmissionView,
    GuestEndView,
    GuestReconnectView,
    GuestSessionStatus,
    PublicDeploymentBootstrap,
    PublicDeploymentView,
)
from platform_api.sessions.admission import public_session_admission_config
from platform_api.sessions.models import SessionParticipant
from platform_api.sessions.service import (
    end_guest_session as end_registered_guest_session,
    issue_guest_join,
    reserve_guest_session,
)


DISCLOSURE_VERSION = "public-guest-v1"
DISCLOSURE_TEXT = (
    "You are speaking with an AI voice agent. Your voice input is processed to "
    "provide the conversation. You can end the session at any time."
)
PUBLIC_DEPLOYMENT_CREATE_ACTION = "public.deployment.create"
PUBLIC_DEPLOYMENT_DISABLE_ACTION = "public.deployment.disable"
TERMINAL_SESSION_STATES = frozenset(
    {"CLOSED", "COMPLETED", "FAILED", "EXPIRED", "ENDED", "TERMINATED", "CANCELLED", "STOPPED"}
)


class GuestUnauthenticatedError(PlatformError):
    """A guest bearer is absent, expired, or has been revoked."""

    status_code = 401
    code = "UNAUTHENTICATED"
    message = "Guest capability is invalid or expired"



def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key_bytes(settings: PlatformSettings) -> bytes:
    value = settings.auth_csrf_key.strip()
    padded = value + ("=" * (-len(value) % 4))
    try:
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise DependencyUnavailableError("Public guest signing key is invalid") from exc
    if len(key) != 32:
        raise DependencyUnavailableError("Public guest signing key is invalid")
    return key


def _mac(settings: PlatformSettings, message: bytes) -> bytes:
    return hmac.new(_key_bytes(settings), message, hashlib.sha256).digest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _require_origin(origin: str | None, settings: PlatformSettings) -> str:
    expected = settings.web_origin
    if (
        not origin
        or expected is None
        or not hmac.compare_digest(origin.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise OriginForbiddenError("Origin header is not permitted")
    return origin


def _require_deployment_origin(
    origin: str | None,
    deployment: PublicDeployment,
    settings: PlatformSettings,
) -> str:
    resolved = _require_origin(origin, settings)
    if not hmac.compare_digest(resolved.encode("utf-8"), deployment.origin.encode("utf-8")):
        raise OriginForbiddenError("Origin header is not permitted for this deployment")
    return resolved


def _operation_id(value: str) -> str:
    if not value or value != value.strip() or len(value) > 128:
        raise ValidationError(
            "Idempotency-Key must be between 1 and 128 non-whitespace characters",
            fields=[{"field": "Idempotency-Key", "code": "INVALID"}],
        )
    return value


def _stored_response(model: type[Any], response_json: dict[str, Any]) -> Any:
    try:
        return model.model_validate(response_json)
    except PydanticValidationError as exc:
        raise DependencyUnavailableError("Stored public outcome is invalid") from exc


def generate_public_csrf_token(
    settings: PlatformSettings,
    deployment_id: uuid.UUID,
    origin: str,
) -> str:
    """Derive a deployment-scoped CSRF token for the public page bootstrap."""
    return _b64url(_mac(settings, b"public-csrf-v1\0" + deployment_id.bytes + origin.encode("utf-8")))


def _guest_csrf_token(
    settings: PlatformSettings,
    session_id: uuid.UUID,
    capability_digest: str,
    origin: str,
) -> str:
    return _b64url(
        _mac(
            settings,
            b"guest-csrf-v1\0"
            + session_id.bytes
            + bytes.fromhex(capability_digest)
            + origin.encode("utf-8"),
        )
    )


def guest_cookie_name(session_id: uuid.UUID) -> str:
    """Return the cookie name bound to exactly one guest session."""
    return f"platform_guest_{session_id.hex}"


def guest_cookie_path(session_id: uuid.UUID) -> str:
    """Limit the bearer cookie to its own guest-session routes."""
    return f"/api/v1/guest/sessions/{session_id}"

def _derive_capability(
    settings: PlatformSettings,
    *,
    owner_user_id: uuid.UUID,
    deployment_id: uuid.UUID,
    operation_id: str,
) -> tuple[str, str]:
    # The operation key and stable scope let a lost-response retry recover the
    # same bearer without ever storing its plaintext in the database.
    message = (
        b"public-guest-capability-v1\0"
        + owner_user_id.bytes
        + deployment_id.bytes
        + operation_id.encode("utf-8")
    )
    raw_token = _b64url(_mac(settings, message))
    digest = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
    return raw_token, digest


def _capability_digest(raw_token: str | None) -> str | None:
    if not raw_token or len(raw_token) > 128:
        return None
    try:
        encoded = raw_token.encode("ascii")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(encoded).hexdigest()


async def _load_deployment(
    db: AsyncSession,
    *,
    public_id: str,
) -> tuple[PublicDeployment, Agent, User]:
    if not 32 <= len(public_id) <= 64:
        raise NotFoundError("Public deployment is unavailable")
    deployment = (
        await db.execute(
            select(PublicDeployment).where(PublicDeployment.public_id == public_id)
        )
    ).scalar_one_or_none()
    if deployment is None or not deployment.enabled or deployment.disabled_at is not None:
        raise NotFoundError("Public deployment is unavailable")
    agent = (
        await db.execute(select(Agent).where(Agent.id == deployment.agent_id))
    ).scalar_one_or_none()
    owner = (
        await db.execute(select(User).where(User.id == deployment.owner_user_id))
    ).scalar_one_or_none()
    if (
        agent is None
        or not agent.enabled
        or agent.owner_user_id != deployment.owner_user_id
        or agent.current_published_version_id is None
        or owner is None
        or owner.status != "active"
    ):
        raise NotFoundError("Public deployment is unavailable")
    return deployment, agent, owner


async def create_public_deployment(
    db: AsyncSession,
    *,
    principal: Principal,
    agent_id: uuid.UUID,
    origin: str,
    operation_id: str,
    settings: PlatformSettings,
) -> PublicDeploymentView:
    """Create an opaque public link for an enabled, published owned agent."""
    public_session_admission_config(settings)
    origin = _require_origin(origin, settings)
    operation_id = _operation_id(operation_id)
    request_hash = compute_payload_hash({"agent_id": str(agent_id), "origin": origin})
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=PUBLIC_DEPLOYMENT_CREATE_ACTION,
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=PUBLIC_DEPLOYMENT_CREATE_ACTION,
        request_hash=request_hash,
    )
    if cached is not None:
        if cached.status_code != 201:
            raise ConflictError("Stored public deployment outcome is unavailable")
        await db.commit()
        return _stored_response(PublicDeploymentView, cached.response_json)

    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id).with_for_update()
        )
    ).scalar_one_or_none()
    owner = (
        await db.execute(
            select(User).where(User.id == principal.user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if (
        agent is None
        or agent.owner_user_id != principal.user_id
        or not agent.enabled
        or agent.current_published_version_id is None
    ):
        raise NotFoundError("Published enabled agent not found")
    if owner is None or owner.status != "active":
        raise ForbiddenError("Owner is not eligible for public calls")

    now = _now()
    deployment = PublicDeployment(
        id=uuid.uuid4(),
        public_id=secrets.token_urlsafe(32),
        owner_user_id=principal.user_id,
        agent_id=agent.id,
        origin=origin,
        disclosure_version=DISCLOSURE_VERSION,
        enabled=True,
        created_at=now,
        disabled_at=None,
    )
    db.add(deployment)
    view = PublicDeploymentView(
        public_id=deployment.public_id,
        enabled=True,
        created_at=now,
    )
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=PUBLIC_DEPLOYMENT_CREATE_ACTION,
        request_hash=request_hash,
        status_code=201,
        response_json=view.model_dump(mode="json"),
    )
    await db.commit()
    return view


async def disable_public_deployment(
    db: AsyncSession,
    *,
    principal: Principal,
    public_id: str,
    origin: str,
    operation_id: str,
    settings: PlatformSettings,
) -> PublicDeploymentView:
    """Disable one owned deployment without disturbing its live guest sessions."""
    origin = _require_origin(origin, settings)
    operation_id = _operation_id(operation_id)
    request_hash = compute_payload_hash({"origin": origin, "public_id": public_id})
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=PUBLIC_DEPLOYMENT_DISABLE_ACTION,
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=PUBLIC_DEPLOYMENT_DISABLE_ACTION,
        request_hash=request_hash,
    )
    if cached is not None:
        if cached.status_code != 200:
            raise ConflictError("Stored public deployment outcome is unavailable")
        await db.commit()
        return _stored_response(PublicDeploymentView, cached.response_json)

    deployment = (
        await db.execute(
            select(PublicDeployment)
            .where(
                PublicDeployment.public_id == public_id,
                PublicDeployment.owner_user_id == principal.user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if deployment is None:
        raise NotFoundError("Public deployment not found")
    if deployment.enabled:
        deployment.enabled = False
        deployment.disabled_at = _now()
    view = PublicDeploymentView(
        public_id=deployment.public_id,
        enabled=False,
        created_at=deployment.created_at,
    )
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=PUBLIC_DEPLOYMENT_DISABLE_ACTION,
        request_hash=request_hash,
        status_code=200,
        response_json=view.model_dump(mode="json"),
    )
    await db.commit()
    return view


async def get_public_deployment(
    db: AsyncSession,
    *,
    public_id: str,
    origin: str,
    settings: PlatformSettings,
) -> PublicDeploymentBootstrap:
    """Return public agent metadata and the disclosure page contract."""
    public_session_admission_config(settings)
    deployment, agent, _owner = await _load_deployment(db, public_id=public_id)
    origin = _require_deployment_origin(origin, deployment, settings)
    csrf_token = generate_public_csrf_token(settings, deployment.id, origin)
    await db.commit()
    return PublicDeploymentBootstrap(
        public_id=deployment.public_id,
        agent_name=agent.name,
        agent_description=agent.description,
        disclosure_version=deployment.disclosure_version,
        disclosure_text=DISCLOSURE_TEXT,
        csrf_token=csrf_token,
    )


async def admit_guest_session(
    db: AsyncSession,
    *,
    public_id: str,
    payload: GuestAdmissionRequest,
    origin: str,
    csrf_token: str | None,
    client_ip: str | None,
    operation_id: str,
    capabilities: Any,
    qualification_settings: Any,
    settings: PlatformSettings,
    catalog: ModelCatalog | None = None,
) -> tuple[GuestAdmissionView, str]:
    """Check disclosure and abuse limits before reserving a funded guest call."""
    public_session_admission_config(settings)
    operation_id = _operation_id(operation_id)
    deployment, agent, owner = await _load_deployment(db, public_id=public_id)
    origin = _require_deployment_origin(origin, deployment, settings)
    expected_csrf = generate_public_csrf_token(settings, deployment.id, origin)
    if not csrf_token or not hmac.compare_digest(expected_csrf, csrf_token):
        raise CsrfInvalidError("Invalid or missing public CSRF token")
    if not payload.disclosure_acknowledged:
        raise ValidationError(
            "The public disclosure must be acknowledged",
            fields=[{"field": "disclosure_acknowledged", "code": "REQUIRED"}],
        )
    if not hmac.compare_digest(
        payload.disclosure_version.encode("utf-8"),
        deployment.disclosure_version.encode("utf-8"),
    ):
        raise ConflictError("The public disclosure has changed")

    now = _now()
    await check_and_increment_rate_limit(
        db,
        settings,
        scope="public_admission_ip",
        identifier=client_ip or "unknown",
        limit=settings.public_admission_ip_limit,
        window_seconds=settings.public_admission_window_seconds,
        now=now,
    )
    await check_and_increment_rate_limit(
        db,
        settings,
        scope="public_admission_agent",
        identifier=str(agent.id),
        limit=settings.public_admission_agent_limit,
        window_seconds=settings.public_admission_window_seconds,
        now=now,
    )

    raw_capability, digest = _derive_capability(
        settings,
        owner_user_id=owner.id,
        deployment_id=deployment.id,
        operation_id=operation_id,
    )
    admission = await reserve_guest_session(
        db,
        owner_user_id=owner.id,
        deployment_id=deployment.id,
        origin=origin,
        agent_id=agent.id,
        agent_version_id=agent.current_published_version_id,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        operation_id=operation_id,
        capability_digest=digest,
        settings=settings,
        catalog=catalog,
    )
    await db.commit()
    return (
        GuestAdmissionView(
            session_id=admission.session_id,
            state="REGISTERED",
            signaling_url=admission.join_details.signaling_url,
            join_token=admission.join_details.join_token,
            join_expires_at=admission.join_details.expires_at,
            hard_end_deadline=admission.hard_end_deadline,
            csrf_token=_guest_csrf_token(
                settings, admission.session_id, digest, origin
            ),
        ),
        raw_capability,
    )


async def _load_guest_capability(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    raw_token: str | None,
    origin: str,
    settings: PlatformSettings,
    allow_inactive: bool = False,
) -> tuple[Session, SessionParticipant, PublicGuestCapability]:
    origin = _require_origin(origin, settings)
    digest = _capability_digest(raw_token)
    session = await db.get(Session, session_id)
    participant = (
        await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.kind == "guest",
            )
        )
    ).scalar_one_or_none()
    capability = await db.get(PublicGuestCapability, session_id)
    if (
        session is None
        or participant is None
        or capability is None
        or digest is None
        or not hmac.compare_digest(capability.capability_digest, digest)
        or session.owner_user_id != capability.owner_user_id
        or participant.owner_user_id != capability.owner_user_id
        or participant.participant_id != capability.participant_id
        or participant.auth_session_id is not None
        or session.kind != "voice"
        or not isinstance(session.tariff_snapshot, dict)
        or session.tariff_snapshot.get("public") is not True
        or session.tariff_snapshot.get("paid") is not False
        or not hmac.compare_digest(participant.origin.encode("utf-8"), origin.encode("utf-8"))
    ):
        raise NotFoundError("Guest session is unavailable")
    now = _now()
    if not allow_inactive and (
        capability.revoked_at is not None
        or participant.revoked_at is not None
        or capability.expires_at <= now
    ):
        if capability.expires_at <= now and capability.revoked_at is None:
            capability.revoked_at = now
            await db.commit()
        raise GuestUnauthenticatedError()
    if session.hard_end_deadline is None:
        raise DependencyUnavailableError("Stored guest session metadata is incomplete")
    return session, participant, capability


async def get_guest_session_status(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    raw_token: str | None,
    origin: str,
    settings: PlatformSettings,
) -> GuestSessionStatus:
    """Return only safe state for the session named by its guest bearer."""
    session, _participant, capability = await _load_guest_capability(
        db,
        session_id=session_id,
        raw_token=raw_token,
        origin=origin,
        settings=settings,
    )
    if session.state in TERMINAL_SESSION_STATES:
        capability.revoked_at = capability.revoked_at or _now()
        await db.commit()
    else:
        await db.commit()
    return GuestSessionStatus(
        session_id=session.id,
        state=session.state,
        session_version=session.version,
        created_at=session.created_at,
        hard_end_deadline=session.hard_end_deadline,
        ended_at=session.ended_at,
    )


async def reconnect_guest_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    raw_token: str | None,
    origin: str,
    csrf_token: str | None,
    operation_id: str,
    settings: PlatformSettings,
) -> GuestReconnectView:
    """Reissue a one-use join token only for the same live guest capability."""
    _operation_id(operation_id)
    session, _participant, capability = await _load_guest_capability(
        db,
        session_id=session_id,
        raw_token=raw_token,
        origin=origin,
        settings=settings,
    )
    expected_csrf = _guest_csrf_token(
        settings, session_id, capability.capability_digest, origin
    )
    if not csrf_token or not hmac.compare_digest(expected_csrf, csrf_token):
        raise CsrfInvalidError("Invalid or missing guest CSRF token")
    details = await issue_guest_join(
        db,
        session_id=session_id,
        owner_user_id=capability.owner_user_id,
        origin=origin,
        operation_id=operation_id,
        settings=settings,
    )
    return GuestReconnectView(
        session_id=session_id,
        state=session.state,
        signaling_url=details.signaling_url,
        join_token=details.join_token,
        expires_at=details.expires_at,
    )


async def end_guest_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    raw_token: str | None,
    origin: str,
    csrf_token: str | None,
    operation_id: str,
    settings: PlatformSettings,
) -> GuestEndView:
    """Request idempotent teardown with only the matching guest capability."""
    _operation_id(operation_id)
    _session, _participant, capability = await _load_guest_capability(
        db,
        session_id=session_id,
        raw_token=raw_token,
        origin=origin,
        settings=settings,
        allow_inactive=True,
    )
    expected_csrf = _guest_csrf_token(
        settings, session_id, capability.capability_digest, origin
    )
    if not csrf_token or not hmac.compare_digest(expected_csrf, csrf_token):
        raise CsrfInvalidError("Invalid or missing guest CSRF token")
    view = await end_registered_guest_session(
        db,
        session_id=session_id,
        owner_user_id=capability.owner_user_id,
        origin=origin,
        operation_id=operation_id,
        settings=settings,
    )
    return GuestEndView(
        session_id=view.session_id,
        state=view.state,
        session_version=view.session_version,
        ended_at=view.ended_at,
        failure_code=view.failure_code,
    )


__all__ = [
    "DISCLOSURE_TEXT",
    "DISCLOSURE_VERSION",
    "admit_guest_session",
    "create_public_deployment",
    "disable_public_deployment",
    "end_guest_session",
    "get_guest_session_status",
    "get_public_deployment",
    "guest_cookie_name",
    "guest_cookie_path",
    "reconnect_guest_session",
]
