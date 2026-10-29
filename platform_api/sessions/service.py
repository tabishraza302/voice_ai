"""Durable owner admission, immutable runtime config, and single-peer join control."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import uuid
from typing import Any

import jwt
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import ValidationError as PydanticValidationError

from bot.flow.qualification import AgentQualificationSettings
from bot.flow.schemas import AdapterCapabilities
from bot.flow.canonical import (
    compute_artifact_hash,
    compute_authoring_hash,
    compute_flow_hash,
)
from platform_api.agents.validation import validate_voice_capability
from platform_api.agents.config import (
    compute_model_hash,
    compute_settings_hash,
    compute_voice_hash,
)
from platform_api.agents.models import (
    Agent,
    AgentVersion,
    IdempotencyRecord,
    PlatformPromptVersion,
)
from platform_api.public.models import PublicDeployment, PublicGuestCapability
from platform_api.agents.service import (
    check_idempotency_record,
    compute_payload_hash,
    lock_idempotency_key,
    store_idempotency_record,
)
from platform_api.auth.errors import (
    ConflictError,
    DependencyUnavailableError,
    ForbiddenError,
    IdempotencyConflictError,
    NotFoundError,
    UnauthenticatedError,
    ValidationError,
)
from platform_api.auth.types import Principal
from platform_api.billing.models import BillingOperation, CallBudget, MeterCheckpoint
from platform_api.billing.schemas import (
    BudgetPermit,
    RuntimeStopAttestation,
    decode_stored_response,
)
from platform_api.billing.service import (
    RuntimePrincipal,
    checkpoint_and_reserve,
    close_budget,
    create_call_budget,
)
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthSession, User
from platform_api.models.session import Session
from platform_api.sessions.admission import (
    RuntimeRegistrationRejectedError,
    SessionAdmissionConfig,
    register_runtime as register_with_runtime,
    public_session_admission_config,
    release_runtime as release_from_runtime,
    session_admission_config,
    session_runtime_config,
)
from platform_api.sessions.models import (
    RunnerCapacity,
    RuntimeRegistration,
    SessionJoinJti,
    SessionParticipant,
    SessionPeer,
    SessionSlot,
)
from platform_api.sessions.schemas import (
    JoinConsumptionRequest,
    JoinConsumptionView,
    JoinDetails,
    OwnerSessionView,
    PeerReleaseRequest,
    PeerReleaseView,
    RegisteredSession,
    RuntimeBudgetSnapshot,
    RuntimeConfigView,
    RuntimeRegistrationRequest,
    RuntimeReleaseRequest,
    RuntimeReservation,
    SessionStateView,
    SessionTransitionRequest,
)


SESSION_ADMISSION_ACTION = "session.admit"
PUBLIC_SESSION_ADMISSION_ACTION = "public.session.admit"
PUBLIC_SESSION_JOIN_ACTION = "public.session.join"
PUBLIC_SESSION_END_ACTION = "public.session.end"
SESSION_JOIN_ACTION = "session.join"
SESSION_END_ACTION = "session.end"
LIVE_SESSION_STATES = frozenset(
    {"RESERVED", "REGISTERED", "CONNECTING", "INITIALIZING", "READY", "ACTIVE", "RECONNECTING", "ENDING"}
)
TERMINAL_SESSION_STATES = frozenset(
    {"CLOSED", "COMPLETED", "FAILED", "EXPIRED", "ENDED", "TERMINATED", "CANCELLED", "STOPPED"}
)
ALLOWED_RUNTIME_TRANSITIONS = {
    "REGISTERED": frozenset({"ENDING"}),
    "CONNECTING": frozenset({"INITIALIZING", "ENDING"}),
    "INITIALIZING": frozenset({"READY", "ENDING"}),
    "READY": frozenset({"ACTIVE", "ENDING"}),
    "ACTIVE": frozenset({"RECONNECTING", "ENDING"}),
    "RECONNECTING": frozenset({"ACTIVE", "ENDING"}),
    "ENDING": frozenset({"COMPLETED", "FAILED", "EXPIRED"}),
}


@dataclass(frozen=True)
class GuestSessionAdmission:
    session_id: uuid.UUID
    hard_end_deadline: datetime
    join_details: JoinDetails


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _check_operation_id(value: str) -> str:
    key = value.strip()
    if not key or key != value or len(key) > 128:
        raise ValidationError(
            "Idempotency-Key must be between 1 and 128 non-whitespace characters",
            fields=[{"field": "Idempotency-Key", "code": "INVALID"}],
        )
    return key


def _check_origin(origin: str, settings: PlatformSettings) -> None:
    if (
        not origin
        or len(origin) > 512
        or settings.web_origin is None
        or not hmac.compare_digest(origin, settings.web_origin)
    ):
        raise ForbiddenError("Session origin does not match the configured web origin")


def _budget_create_operation_id(session_id: uuid.UUID) -> str:
    return f"session_admit:{session_id}:budget"


def _first_permit_operation_id(session_id: uuid.UUID) -> str:
    return f"session_admit:{session_id}:permit:initial"


def _request_hash(
    *,
    agent_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    auth_session_id: uuid.UUID | None,
    origin: str,
    kind: str = "voice_test",
    public_deployment_id: uuid.UUID | None = None,
) -> str:
    payload: dict[str, Any] = {
        "agent_id": str(agent_id),
        "agent_version_id": str(agent_version_id),
        "auth_session_id": (
            str(auth_session_id) if auth_session_id is not None else None
        ),
        "kind": kind,
        "origin": origin,
    }
    if public_deployment_id is not None:
        payload["public_deployment_id"] = str(public_deployment_id)
    return compute_payload_hash(payload)


async def _verify_owner_session(
    db: AsyncSession,
    *,
    principal: Principal,
    auth_session_id: uuid.UUID,
    now: datetime,
) -> User:
    if (
        principal.role != "user"
        or principal.session_id != auth_session_id
        or principal.email_verified_at is None
        or principal.password_verified_at is None
    ):
        raise UnauthenticatedError("A verified owner session is required")

    auth_session = (
        await db.execute(
            select(AuthSession)
            .where(AuthSession.id == auth_session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        auth_session is None
        or auth_session.user_id != principal.user_id
        or auth_session.state != "authenticated"
        or auth_session.revoked_at is not None
        or auth_session.idle_expires_at <= now
        or auth_session.absolute_expires_at <= now
        or auth_session.password_verified_at != principal.password_verified_at
    ):
        raise UnauthenticatedError("Verified owner session is expired or revoked")

    owner = (
        await db.execute(
            select(User).where(User.id == principal.user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if (
        owner is None
        or owner.status != "active"
        or owner.role != "user"
        or owner.email_verified_at != principal.email_verified_at
        or owner.auth_version != auth_session.auth_version
    ):
        raise ForbiddenError("Account is not eligible for voice-test admission")
    return owner


def _validate_immutable_version(
    version: AgentVersion,
    policy_version: PlatformPromptVersion,
) -> dict[str, Any]:
    """Recompute every published pin needed by the runtime; never use mutable defaults."""
    snapshot = version.immutable_config_json
    if not isinstance(snapshot, dict):
        raise ConflictError("Published agent version has no immutable runtime snapshot")
    source = snapshot.get("authoring_config")
    normalized = snapshot.get("normalized_flow")
    manifest = snapshot.get("manifest")
    hashes = snapshot.get("hashes")
    policy = snapshot.get("policy")
    pins = snapshot.get("model_pins")
    if not all(
        isinstance(value, dict)
        for value in (source, normalized, manifest, hashes, policy, pins)
    ):
        raise ConflictError("Published agent version is missing required immutable fields")
    if version.policy_version_id is None:
        raise ConflictError("Published agent version has no pinned policy version")
    try:
        authoring_hash = compute_authoring_hash(source)
        flow_hash = compute_flow_hash(normalized)
        artifact_hash = compute_artifact_hash(manifest)
        settings = {
            key: value
            for key, value in pins.items()
            if key not in {"model_hash", "voice_hash", "settings_hash"}
        }
        if not settings or any(not isinstance(key, str) or not isinstance(value, str) for key, value in settings.items()):
            raise ValueError("Invalid immutable setting pins")
        model_hash = compute_model_hash(settings)
        voice_hash = compute_voice_hash(settings)
        settings_hash = compute_settings_hash(settings)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConflictError("Published agent version hashes cannot be verified") from exc

    expected_hashes = {
        "authoring_hash": authoring_hash,
        "flow_hash": flow_hash,
        "artifact_hash": artifact_hash,
        "model_hash": model_hash,
        "voice_hash": voice_hash,
        "settings_hash": settings_hash,
        "policy_hash": policy_version.policy_hash,
    }
    stored_hashes = {
        "authoring_hash": version.authoring_hash,
        "flow_hash": version.flow_hash,
        "artifact_hash": version.artifact_hash,
        "model_hash": pins.get("model_hash"),
        "voice_hash": pins.get("voice_hash"),
        "settings_hash": pins.get("settings_hash"),
        "policy_hash": policy_version.policy_hash,
    }
    if any(stored_hashes[name] != digest for name, digest in expected_hashes.items()):
        raise ConflictError("Published agent version hash mismatch")
    if (
        policy.get("version_id") != str(policy_version.id)
        or policy.get("policy_hash") != policy_version.policy_hash
        or hashes.get("policy_hash") != policy_version.policy_hash
        or hashes.get("authoring_hash") != authoring_hash
        or hashes.get("flow_hash") != flow_hash
        or hashes.get("artifact_hash") != artifact_hash
        or hashes.get("model_hash") != model_hash
        or hashes.get("voice_hash") != voice_hash
        or hashes.get("settings_hash") != settings_hash
    ):
        raise ConflictError("Published agent snapshot does not match its pinned hashes")
    return snapshot


async def _load_owned_version(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_version_id: uuid.UUID,
) -> tuple[AgentVersion, PlatformPromptVersion, dict[str, Any]]:
    result = await db.execute(
        select(AgentVersion, PlatformPromptVersion)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .join(
            PlatformPromptVersion,
            PlatformPromptVersion.id == AgentVersion.policy_version_id,
        )
        .where(
            AgentVersion.id == agent_version_id,
            Agent.id == agent_id,
            Agent.owner_user_id == owner_user_id,
            Agent.enabled.is_(True),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise NotFoundError("Enabled immutable agent version not found")
    version, policy = row
    snapshot = _validate_immutable_version(version, policy)
    return version, policy, snapshot


def _validate_voice_qualification(
    snapshot: dict[str, Any],
    *,
    capabilities: AdapterCapabilities | None,
    qualification_settings: AgentQualificationSettings | None,
) -> None:
    pins = snapshot["model_pins"]
    if not pins.get("tts_voice_en") and not pins.get("tts_voice_hi"):
        raise ValidationError(
            "Voice-test sessions require a published voice setting",
            fields=[{"field": "model_pins.tts_voice_en", "code": "PLAYBACK_UNQUALIFIED"}],
        )
    diagnostics = validate_voice_capability(
        pins,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
    )
    if diagnostics:
        raise ValidationError(
            "The published voice settings do not match the pinned deployment qualification",
            fields=[
                {"field": diagnostic.path, "code": diagnostic.code}
                for diagnostic in diagnostics
            ],
        )



async def _lock_runner_capacity(
    db: AsyncSession,
    *,
    runner_id: str,
    configured_capacity: int,
    now: datetime,
) -> RunnerCapacity:
    statement = pg_insert(RunnerCapacity).values(
        runner_id=runner_id,
        max_active_sessions=configured_capacity,
        updated_at=now,
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[RunnerCapacity.runner_id],
            set_={
                "max_active_sessions": configured_capacity,
                "updated_at": now,
            },
        )
    )
    row = (
        await db.execute(
            select(RunnerCapacity)
            .where(RunnerCapacity.runner_id == runner_id)
            .with_for_update()
        )
    ).scalar_one()
    return row


async def _verify_public_deployment(
    db: AsyncSession,
    *,
    deployment_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    origin: str,
) -> User:
    deployment = (
        await db.execute(
            select(PublicDeployment)
            .where(PublicDeployment.id == deployment_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id).with_for_update()
        )
    ).scalar_one_or_none()
    owner = (
        await db.execute(
            select(User).where(User.id == owner_user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if (
        deployment is None
        or not deployment.enabled
        or deployment.owner_user_id != owner_user_id
        or deployment.agent_id != agent_id
        or not hmac.compare_digest(deployment.origin, origin)
        or agent is None
        or not agent.enabled
        or agent.owner_user_id != owner_user_id
        or agent.current_published_version_id != agent_version_id
    ):
        raise NotFoundError("Public deployment is unavailable")
    if owner is None or owner.status != "active":
        raise ForbiddenError("Deployment owner is not eligible for public calls")
    return owner


async def _create_or_replay_reservation(
    db: AsyncSession,
    *,
    principal: Principal | None,
    owner_user_id: uuid.UUID,
    auth_session_id: uuid.UUID | None,
    origin: str,
    agent_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    capabilities: AdapterCapabilities | None,
    qualification_settings: AgentQualificationSettings | None,
    operation_id: str,
    request_hash: str,
    config: SessionAdmissionConfig,
    settings: PlatformSettings,
    now: datetime,
    participant_kind: str = "owner",
    public_deployment_id: uuid.UUID | None = None,
    capability_digest: str | None = None,
) -> tuple[uuid.UUID, str, str | None]:
    if participant_kind not in {"owner", "guest"}:
        raise DependencyUnavailableError("Session participant kind is invalid")
    action = (
        SESSION_ADMISSION_ACTION
        if participant_kind == "owner"
        else PUBLIC_SESSION_ADMISSION_ACTION
    )
    if participant_kind == "owner":
        if principal is None or auth_session_id is None or public_deployment_id is not None:
            raise UnauthenticatedError("A verified owner session is required")
    elif (
        principal is not None
        or auth_session_id is not None
        or public_deployment_id is None
        or capability_digest is None
    ):
        raise UnauthenticatedError("A public guest admission is required")

    await lock_idempotency_key(
        db,
        user_id=owner_user_id,
        idempotency_key=operation_id,
        action=action,
    )
    cached = await check_idempotency_record(
        db,
        user_id=owner_user_id,
        idempotency_key=operation_id,
        action=action,
        request_hash=request_hash,
    )
    if participant_kind == "owner":
        assert principal is not None and auth_session_id is not None
        owner = await _verify_owner_session(
            db,
            principal=principal,
            auth_session_id=auth_session_id,
            now=now,
        )
    else:
        assert public_deployment_id is not None
        owner = await _verify_public_deployment(
            db,
            deployment_id=public_deployment_id,
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            origin=origin,
        )
    if cached is not None:
        raw_session_id = cached.response_json.get("session_id")
        try:
            session_id = uuid.UUID(str(raw_session_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise DependencyUnavailableError("Stored admission outcome is invalid") from exc
        if cached.status_code == 201:
            jti = cached.response_json.get("jti")
            if not isinstance(jti, str):
                raise DependencyUnavailableError("Stored admission join outcome is invalid")
            return session_id, "registered", jti
        if cached.status_code == 202:
            return session_id, "pending", None
        raise ConflictError(
            "This admission operation already failed; use a new Idempotency-Key"
        )

    if participant_kind == "owner":
        session_admission_config(settings)
    else:
        public_session_admission_config(settings)
    _, _, snapshot = await _load_owned_version(
        db,
        owner_user_id=owner.id,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
    )
    _validate_voice_qualification(
        snapshot,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
    )
    runner_capacity = await _lock_runner_capacity(
        db,
        runner_id=config.runner_id,
        configured_capacity=config.runner_capacity,
        now=now,
    )
    owner_active = await db.execute(
        select(func.count())
        .select_from(SessionSlot)
        .where(
            SessionSlot.owner_user_id == owner.id,
            SessionSlot.released_at.is_(None),
        )
    )
    if int(owner_active.scalar_one()) >= config.owner_capacity:
        raise ConflictError("Owner session capacity is full")
    runner_active = await db.execute(
        select(func.count())
        .select_from(SessionSlot)
        .where(
            SessionSlot.runner_id == runner_capacity.runner_id,
            SessionSlot.released_at.is_(None),
        )
    )
    if int(runner_active.scalar_one()) >= runner_capacity.max_active_sessions:
        raise ConflictError("Configured runner capacity is full")

    session_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    connect_deadline = now + timedelta(seconds=config.setup_timeout_seconds)
    hard_end_deadline = now + timedelta(milliseconds=config.max_duration_ms)
    session_kind = "voice_test" if participant_kind == "owner" else "voice"
    is_public = participant_kind == "guest"
    tariff_snapshot = {
        "kind": session_kind,
        "currency": "INR",
        "rate_minor_per_minute": config.rate_minor_per_minute,
        "tariff_version": config.tariff_version,
        "billing_slice_ms": config.billing_slice_ms,
        "permit_ttl_ms": config.billing_permit_ttl_ms,
        "maximum_session_duration_ms": config.max_duration_ms,
        "setup_timeout_seconds": config.setup_timeout_seconds,
        "reconnect_grace_seconds": config.reconnect_grace_seconds,
        "public": is_public,
        "paid": False,
    }
    session = Session(
        id=session_id,
        owner_user_id=owner.id,
        participant_id=str(participant_id),
        agent_version_id=agent_version_id,
        tariff_snapshot=tariff_snapshot,
        kind=session_kind,
        state="CREATED",
        version=1,
        runner_id=config.runner_id,
        connect_deadline=connect_deadline,
        hard_end_deadline=hard_end_deadline,
        created_at=now,
    )
    db.add(session)
    await db.flush()
    if session.state != "CREATED" or session.version != 1:
        raise ConflictError("New session state changed before reservation")
    session.state = "RESERVED"
    session.version += 1
    db.add(
        SessionSlot(
            session_id=session_id,
            owner_user_id=owner.id,
            runner_id=config.runner_id,
            reserved_at=now,
        )
    )
    registration_id = uuid.uuid4()
    db.add(
        RuntimeRegistration(
            session_id=session_id,
            runner_id=config.runner_id,
            registration_id=registration_id,
            status="PENDING",
            expires_at=connect_deadline,
            created_at=now,
        )
    )
    db.add(
        SessionParticipant(
            participant_id=participant_id,
            session_id=session_id,
            owner_user_id=owner.id,
            auth_session_id=auth_session_id,
            kind=participant_kind,
            origin=origin,
            created_at=now,
        )
    )
    await db.flush()
    if is_public:
        assert public_deployment_id is not None and capability_digest is not None
        db.add(
            PublicGuestCapability(
                session_id=session_id,
                deployment_id=public_deployment_id,
                owner_user_id=owner.id,
                participant_id=participant_id,
                capability_digest=capability_digest,
                expires_at=hard_end_deadline,
                created_at=now,
            )
        )
    await store_idempotency_record(
        db,
        user_id=owner.id,
        idempotency_key=operation_id,
        action=action,
        request_hash=request_hash,
        status_code=202,
        response_json={"session_id": str(session_id)},
    )
    await db.commit()
    return session_id, "pending", None


async def _load_reservation(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
) -> tuple[Session, RuntimeRegistration, SessionParticipant]:
    session = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration).where(
                RuntimeRegistration.session_id == session_id
            )
        )
    ).scalar_one_or_none()
    participant = (
        await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session_id,
            )
        )
    ).scalar_one_or_none()
    if session is None or registration is None or participant is None:
        raise DependencyUnavailableError("Persisted session reservation is incomplete")
    return session, registration, participant


async def _persist_runner_reservation(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    reservation: RuntimeReservation,
    now: datetime,
) -> None:
    session = (
        await db.execute(
            select(Session).where(Session.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        session is None
        or registration is None
        or session.state != "RESERVED"
        or session.runner_id != reservation.runner_id
        or registration.runner_id != reservation.runner_id
        or registration.registration_id != reservation.registration_id
    ):
        raise ConflictError("Session reservation changed during runner registration")
    if registration.status == "RELEASE_PENDING" or registration.status == "RELEASED":
        raise ConflictError("Runner registration is already being released")
    registration.status = "REGISTERED"
    registration.expires_at = reservation.expires_at
    registration.registered_at = registration.registered_at or now
    registration.last_error_code = None
    await db.commit()


async def _mark_release_pending(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    failure_code: str,
    now: datetime,
) -> None:
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if registration is None:
        raise DependencyUnavailableError("Session registration is missing")
    if registration.status != "RELEASED":
        registration.status = "RELEASE_PENDING"
        registration.last_error_code = failure_code
    await db.commit()


async def _mark_admission_failure(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    operation_id: str,
    failure_code: str,
    now: datetime,
    action: str = SESSION_ADMISSION_ACTION,
) -> None:
    session = (
        await db.execute(
            select(Session).where(Session.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    slot = (
        await db.execute(
            select(SessionSlot)
            .where(SessionSlot.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    idem = (
        await db.execute(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.user_id == user_id,
                IdempotencyRecord.idempotency_key == operation_id,
                IdempotencyRecord.action == action,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None or registration is None or slot is None or idem is None:
        raise DependencyUnavailableError("Admission compensation records are incomplete")
    if session.state == "RESERVED":
        session.state = "FAILED"
        session.version += 1
        session.ended_at = now
    elif session.state != "CLOSED" and session.state not in TERMINAL_SESSION_STATES:
        raise ConflictError("Session left the admission state before compensation")
    session.failure_code = failure_code
    registration.status = "RELEASED"
    registration.released_at = registration.released_at or now
    registration.last_error_code = failure_code
    slot.released_at = slot.released_at or now
    idem.status_code = 409
    idem.response_json = {
        "session_id": str(session_id),
        "failure_code": failure_code,
    }
    capability = (
        await db.execute(
            select(PublicGuestCapability)
            .where(PublicGuestCapability.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if capability is not None:
        capability.revoked_at = capability.revoked_at or now
    await db.commit()


async def _complete_release_compensation(
    db: AsyncSession,
    *,
    config: SessionAdmissionConfig,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    operation_id: str,
    failure_code: str,
    now: datetime,
    action: str = SESSION_ADMISSION_ACTION,
) -> None:
    session, registration, _ = await _load_reservation(db, session_id=session_id)
    await db.commit()
    acknowledgement = await release_from_runtime(
        config,
        RuntimeReleaseRequest(
            session_id=session_id,
            runner_id=registration.runner_id,
            registration_id=registration.registration_id,
            session_version=session.version,
        ),
    )
    if not acknowledgement.released:
        raise DependencyUnavailableError(
            "Runtime did not confirm admission compensation",
        )

    stopped_at = _utc_now()
    budget = (
        await db.execute(
            select(CallBudget).where(CallBudget.session_id == session_id)
        )
    ).scalar_one_or_none()
    await db.commit()
    if budget is not None and budget.status == "OPEN":
        stop_attestation = RuntimeStopAttestation(
            source="platform-admission",
            stop_id=registration.registration_id,
            session_id=session_id,
            runner_id=registration.runner_id,
            registration_id=registration.registration_id,
            session_version=session.version,
            stopped_at=stopped_at,
            reason=failure_code[:64],
        )
        await close_budget(
            db,
            runtime_principal=RuntimePrincipal(
                runner_id=registration.runner_id,
                service_id="platform-admission",
            ),
            session_id=session_id,
            operation_id=f"session_admit:{session_id}:compensate",
            expected_budget_version=budget.version,
            cumulative_billable_ms=budget.cumulative_proven_ms,
            stop_evidence=stop_attestation.model_dump(mode="json"),
            stop_attestation=stop_attestation,
            now=stopped_at,
        )
    await _mark_admission_failure(
        db,
        session_id=session_id,
        user_id=user_id,
        operation_id=operation_id,
        failure_code=failure_code,
        now=stopped_at,
        action=action,
    )
async def _new_first_permit(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    config: SessionAdmissionConfig,
    now: datetime,
) -> BudgetPermit:
    budget = await create_call_budget(
        db,
        session_id=session_id,
        operation_id=_budget_create_operation_id(session_id),
        pinned_rate_minor_per_minute=config.rate_minor_per_minute,
        now=now,
    )
    requested_ms = min(config.billing_slice_ms, config.max_duration_ms)
    runtime = RuntimePrincipal(runner_id=config.runner_id, service_id="platform-admission")
    permit = await checkpoint_and_reserve(
        db,
        runtime_principal=runtime,
        session_id=session_id,
        operation_id=_first_permit_operation_id(session_id),
        expected_budget_version=budget.budget_version,
        cumulative_billable_ms=0,
        requested_future_ms=requested_ms,
        billing_slice_ms=config.billing_slice_ms,
        permit_ttl_ms=config.billing_permit_ttl_ms,
        now=now,
    )
    if permit.expires_at <= now:
        current = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session_id)
            )
        ).scalar_one_or_none()
        await db.commit()
        if current is None or current.status != "OPEN":
            raise ConflictError("First funded budget is unavailable")
        operation_id = f"session_admit:{session_id}:renew:{current.version}"
        permit = await checkpoint_and_reserve(
            db,
            runtime_principal=runtime,
            session_id=session_id,
            operation_id=operation_id,
            expected_budget_version=current.version,
            cumulative_billable_ms=current.cumulative_proven_ms,
            requested_future_ms=requested_ms,
            billing_slice_ms=config.billing_slice_ms,
            permit_ttl_ms=config.billing_permit_ttl_ms,
            now=_utc_now(),
        )
    return permit


async def _finalize_registered_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    operation_id: str,
    config: SessionAdmissionConfig,
    permit: BudgetPermit,
    now: datetime,
    participant_kind: str = "owner",
    action: str = SESSION_ADMISSION_ACTION,
) -> str:
    session = (
        await db.execute(
            select(Session)
            .where(Session.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    participant = (
        await db.execute(
            select(SessionParticipant)
            .where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.kind == participant_kind,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    idem = (
        await db.execute(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.user_id == owner_user_id,
                IdempotencyRecord.idempotency_key == operation_id,
                IdempotencyRecord.action == action,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        session is None
        or registration is None
        or participant is None
        or idem is None
        or session.owner_user_id != owner_user_id
        or participant.owner_user_id != owner_user_id
        or registration.runner_id != config.runner_id
        or session.runner_id != config.runner_id
        or session.agent_version_id is None
        or session.kind != ("voice_test" if participant_kind == "owner" else "voice")
        or not isinstance(session.tariff_snapshot, dict)
        or session.tariff_snapshot.get("public") is not (participant_kind == "guest")
        or session.tariff_snapshot.get("paid") is not False
    ):
        raise ConflictError("Session is not ready for registration finalization")

    if session.state == "REGISTERED":
        result = idem.response_json
        existing_jti = result.get("jti") if isinstance(result, dict) else None
        stored_join = None
        if isinstance(existing_jti, str):
            stored_join = (
                await db.execute(
                    select(SessionJoinJti)
                    .where(SessionJoinJti.jti == existing_jti)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if (
            not isinstance(result, dict)
            or idem.status_code != 201
            or result.get("session_id") != str(session_id)
            or registration.status != "REGISTERED"
            or stored_join is None
            or stored_join.session_id != session_id
            or stored_join.participant_id != participant.participant_id
            or stored_join.owner_user_id != owner_user_id
            or stored_join.auth_session_id != participant.auth_session_id
            or stored_join.participant_kind != participant_kind
            or stored_join.runner_id != registration.runner_id
            or stored_join.config_version_id != session.agent_version_id
            or stored_join.registration_id != registration.registration_id
            or stored_join.session_version != session.version
            or stored_join.origin != participant.origin
        ):
            raise DependencyUnavailableError(
                "Registered admission outcome is incomplete or inconsistent"
            )
        await db.commit()
        return existing_jti

    if (
        session.state != "RESERVED"
        or registration.status != "REGISTERED"
    ):
        raise ConflictError("Session is not ready for registration finalization")
    expiry = min(
        now + timedelta(seconds=config.join_ttl_seconds),
        session.connect_deadline,
        registration.expires_at,
        permit.expires_at,
        session.hard_end_deadline,
    )
    expiry = datetime.fromtimestamp(int(expiry.timestamp()), tz=timezone.utc)
    issued_at = datetime.fromtimestamp(int(now.timestamp()), tz=timezone.utc)
    if expiry <= issued_at or permit.authorized_through_ms <= 0 or permit.funds_exhausted:
        raise ConflictError("No live funded join window remains")
    session.state = "REGISTERED"
    session.version += 1
    jti = str(uuid.uuid4())
    db.add(
        SessionJoinJti(
            jti=jti,
            session_id=session_id,
            participant_id=participant.participant_id,
            owner_user_id=session.owner_user_id,
            auth_session_id=participant.auth_session_id,
            participant_kind=participant_kind,
            runner_id=registration.runner_id,
            config_version_id=session.agent_version_id,
            registration_id=registration.registration_id,
            session_version=session.version,
            signaling_url=config.signaling_url,
            origin=participant.origin,
            issued_at=issued_at,
            expires_at=expiry,
            key_fingerprint=config.join_key_fingerprint,
        )
    )
    idem.status_code = 201
    idem.response_json = {"session_id": str(session_id), "jti": jti}
    await db.commit()
    return jti


def _jwt_for_jti(
    row: SessionJoinJti,
    config: SessionAdmissionConfig,
) -> str:
    if not hmac.compare_digest(row.key_fingerprint, config.join_key_fingerprint):
        raise DependencyUnavailableError(
            "Stored join token uses a different signing key; re-admission is required"
        )
    claims = {
        "iss": config.join_issuer,
        "aud": "voice-runtime",
        "scope": "webrtc:join",
        "sub": str(row.participant_id),
        "session_id": str(row.session_id),
        "session_version": row.session_version,
        "config_version_id": str(row.config_version_id),
        "runner_id": row.runner_id,
        "registration_id": str(row.registration_id),
        "origin": row.origin,
        "participant_kind": row.participant_kind,
        "jti": row.jti,
        "iat": int(row.issued_at.timestamp()),
        "nbf": int(row.issued_at.timestamp()),
        "exp": int(row.expires_at.timestamp()),
    }
    if row.auth_session_id is not None:
        claims["auth_session_id"] = str(row.auth_session_id)
    return jwt.encode(
        claims,
        config.join_private_key,
        algorithm="EdDSA",
        headers={"kid": row.key_fingerprint, "typ": "JWT"},
    )


async def _registered_result(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    jti: str,
    config: SessionAdmissionConfig,
) -> RegisteredSession:
    session = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    participant = (
        await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.kind == "owner",
            )
        )
    ).scalar_one_or_none()
    join = (
        await db.execute(select(SessionJoinJti).where(SessionJoinJti.jti == jti))
    ).scalar_one_or_none()
    if (
        session is None
        or participant is None
        or join is None
        or session.state not in LIVE_SESSION_STATES
        or session.owner_user_id != participant.owner_user_id
        or session.agent_version_id != join.config_version_id
        or session.runner_id != join.runner_id
        or join.session_id != session_id
        or join.participant_id != participant.participant_id
    ):
        raise ConflictError("Stored session admission result is no longer valid")
    token = _jwt_for_jti(join, config)
    return RegisteredSession(
        session_id=session.id,
        owner_user_id=session.owner_user_id,
        participant_id=participant.participant_id,
        config_version_id=join.config_version_id,
        kind="voice_test",
        state="REGISTERED",
        session_version=join.session_version,
        runner_id=join.runner_id,
        connect_deadline=session.connect_deadline,
        hard_end_deadline=session.hard_end_deadline,
        join_details=JoinDetails(
            session_id=session.id,
            runner_id=join.runner_id,
            config_version_id=join.config_version_id,
            signaling_url=join.signaling_url,
            join_token=token,
            expires_at=join.expires_at,
        ),
    )


async def _registered_guest_result(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    jti: str,
    config: SessionAdmissionConfig,
) -> GuestSessionAdmission:
    session = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    participant = (
        await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.kind == "guest",
            )
        )
    ).scalar_one_or_none()
    join = (
        await db.execute(select(SessionJoinJti).where(SessionJoinJti.jti == jti))
    ).scalar_one_or_none()
    capability = (
        await db.execute(
            select(PublicGuestCapability).where(
                PublicGuestCapability.session_id == session_id
            )
        )
    ).scalar_one_or_none()
    now = _utc_now()
    if (
        session is None
        or participant is None
        or join is None
        or capability is None
        or session.state not in LIVE_SESSION_STATES
        or session.kind != "voice"
        or not isinstance(session.tariff_snapshot, dict)
        or session.tariff_snapshot.get("public") is not True
        or session.tariff_snapshot.get("paid") is not False
        or session.owner_user_id != participant.owner_user_id
        or participant.auth_session_id is not None
        or join.participant_kind != "guest"
        or join.auth_session_id is not None
        or join.session_id != session_id
        or join.participant_id != participant.participant_id
        or join.owner_user_id != session.owner_user_id
        or join.config_version_id != session.agent_version_id
        or join.runner_id != session.runner_id
        or join.origin != participant.origin
        or capability.owner_user_id != session.owner_user_id
        or capability.participant_id != participant.participant_id
        or capability.revoked_at is not None
        or capability.expires_at <= now
        or join.expires_at <= now
    ):
        raise ConflictError("Stored guest admission result is no longer valid")
    return GuestSessionAdmission(
        session_id=session.id,
        hard_end_deadline=session.hard_end_deadline,
        join_details=JoinDetails(
            session_id=session.id,
            runner_id=join.runner_id,
            config_version_id=join.config_version_id,
            signaling_url=join.signaling_url,
            join_token=_jwt_for_jti(join, config),
            expires_at=join.expires_at,
        ),
    )


async def register_runtime(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    runner_id: str,
    registration_id: uuid.UUID,
    settings: PlatformSettings,
) -> RuntimeReservation:
    """Call the configured local runner using the already-persisted registration ID."""
    config = session_runtime_config(settings)
    session, registration, _ = await _load_reservation(db, session_id=session_id)
    await db.commit()
    if (
        runner_id != config.runner_id
        or runner_id != session.runner_id
        or registration.runner_id != runner_id
        or registration.registration_id != registration_id
        or session.state != "RESERVED"
    ):
        raise ForbiddenError("Runtime registration identity does not match reservation")
    command = RuntimeRegistrationRequest(
        session_id=session.id,
        runner_id=runner_id,
        registration_id=registration_id,
        config_version_id=session.agent_version_id,
        session_version=session.version,
        connect_deadline=session.connect_deadline,
        hard_end_deadline=session.hard_end_deadline,
    )
    return await register_with_runtime(config, command)


async def reserve_session(
    db: AsyncSession,
    *,
    principal: Principal,
    auth_session_id: uuid.UUID,
    origin: str,
    agent_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    capabilities: AdapterCapabilities | None,
    qualification_settings: AgentQualificationSettings | None,
    operation_id: str,
    settings: PlatformSettings,
) -> RegisteredSession:
    result = await _reserve_session(
        db,
        principal=principal,
        owner_user_id=principal.user_id,
        auth_session_id=auth_session_id,
        origin=origin,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        operation_id=operation_id,
        settings=settings,
    )
    if not isinstance(result, RegisteredSession):
        raise DependencyUnavailableError("Owner admission returned a guest result")
    return result


async def reserve_guest_session(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    deployment_id: uuid.UUID,
    origin: str,
    agent_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    capabilities: AdapterCapabilities | None,
    qualification_settings: AgentQualificationSettings | None,
    operation_id: str,
    capability_digest: str,
    settings: PlatformSettings,
) -> GuestSessionAdmission:
    result = await _reserve_session(
        db,
        principal=None,
        owner_user_id=owner_user_id,
        auth_session_id=None,
        origin=origin,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        operation_id=operation_id,
        settings=settings,
        participant_kind="guest",
        public_deployment_id=deployment_id,
        capability_digest=capability_digest,
    )
    if not isinstance(result, GuestSessionAdmission):
        raise DependencyUnavailableError("Guest admission returned an owner result")
    return result


async def _reserve_session(
    db: AsyncSession,
    *,
    principal: Principal | None,
    owner_user_id: uuid.UUID,
    auth_session_id: uuid.UUID | None,
    origin: str,
    agent_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    capabilities: AdapterCapabilities | None,
    qualification_settings: AgentQualificationSettings | None,
    operation_id: str,
    settings: PlatformSettings,
    participant_kind: str = "owner",
    public_deployment_id: uuid.UUID | None = None,
    capability_digest: str | None = None,
) -> RegisteredSession | GuestSessionAdmission:
    """Reserve and fund a session using owner or server-resolved guest authority."""
    if participant_kind == "owner":
        if principal is None or auth_session_id is None:
            raise UnauthenticatedError("A verified owner session is required")
        config = session_runtime_config(settings)
        kind = "voice_test"
    elif participant_kind == "guest":
        if principal is not None or auth_session_id is not None:
            raise UnauthenticatedError("A guest cannot use owner credentials")
        config = public_session_admission_config(settings)
        kind = "voice"
    else:
        raise ValidationError("Unsupported session participant kind")
    _check_origin(origin, settings)
    operation_id = _check_operation_id(operation_id)
    action = (
        SESSION_ADMISSION_ACTION
        if participant_kind == "owner"
        else PUBLIC_SESSION_ADMISSION_ACTION
    )
    request_hash = _request_hash(
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        auth_session_id=auth_session_id,
        origin=origin,
        kind=kind,
        public_deployment_id=public_deployment_id,
    )
    now = _utc_now()
    session_id, outcome, stored_jti = await _create_or_replay_reservation(
        db,
        principal=principal,
        owner_user_id=owner_user_id,
        auth_session_id=auth_session_id,
        origin=origin,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        capabilities=capabilities,
        qualification_settings=qualification_settings,
        operation_id=operation_id,
        request_hash=request_hash,
        config=config,
        settings=settings,
        now=now,
        participant_kind=participant_kind,
        public_deployment_id=public_deployment_id,
        capability_digest=capability_digest,
    )
    if outcome == "registered":
        await db.commit()
        if participant_kind == "owner":
            return await _registered_result(
                db,
                session_id=session_id,
                jti=stored_jti or "",
                config=config,
            )
        return await _registered_guest_result(
            db,
            session_id=session_id,
            jti=stored_jti or "",
            config=config,
        )
    session, registration, _participant = await _load_reservation(
        db, session_id=session_id
    )
    await db.commit()
    if session.state == "REGISTERED":
        idem = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.user_id == owner_user_id,
                    IdempotencyRecord.idempotency_key == operation_id,
                    IdempotencyRecord.action == action,
                )
            )
        ).scalar_one_or_none()
        await db.commit()
        if idem is None or not isinstance(idem.response_json.get("jti"), str):
            raise DependencyUnavailableError("Registered admission outcome is incomplete")
        if participant_kind == "owner":
            return await _registered_result(
                db,
                session_id=session_id,
                jti=idem.response_json["jti"],
                config=config,
            )
        return await _registered_guest_result(
            db,
            session_id=session_id,
            jti=idem.response_json["jti"],
            config=config,
        )
    if session.state != "RESERVED":
        raise ConflictError("Session reservation is not active")
    if registration.status == "RELEASE_PENDING":
        await _complete_release_compensation(
            db,
            config=config,
            session_id=session_id,
            user_id=owner_user_id,
            operation_id=operation_id,
            failure_code=registration.last_error_code or "ADMISSION_COMPENSATION",
            now=_utc_now(),
            action=action,
        )
        raise ConflictError("Session admission failed and was compensated")

    expired_at = _utc_now()
    if (
        session.connect_deadline is None
        or session.connect_deadline <= expired_at
    ):
        await _mark_release_pending(
            db,
            session_id=session_id,
            failure_code="CONNECT_TIMEOUT",
            now=expired_at,
        )
        await _complete_release_compensation(
            db,
            config=config,
            session_id=session_id,
            user_id=owner_user_id,
            operation_id=operation_id,
            failure_code="CONNECT_TIMEOUT",
            now=expired_at,
            action=action,
        )
        raise ConflictError("Session setup deadline expired before registration")


    try:
        reservation = await register_runtime(
            db,
            session_id=session_id,
            runner_id=registration.runner_id,
            registration_id=registration.registration_id,
            settings=settings,
        )
    except RuntimeRegistrationRejectedError:
        await _mark_admission_failure(
            db,
            session_id=session_id,
            user_id=owner_user_id,
            operation_id=operation_id,
            failure_code="RUNTIME_REJECTED",
            now=_utc_now(),
            action=action,
        )
        raise

    now = _utc_now()
    await _persist_runner_reservation(
        db,
        session_id=session_id,
        reservation=reservation,
        now=now,
    )
    if reservation.expires_at <= now:
        await _mark_release_pending(
            db,
            session_id=session_id,
            failure_code="RUNTIME_RESERVATION_EXPIRED",
            now=now,
        )
        await _complete_release_compensation(
            db,
            config=config,
            session_id=session_id,
            user_id=owner_user_id,
            operation_id=operation_id,
            failure_code="RUNTIME_RESERVATION_EXPIRED",
            now=now,
            action=action,
        )
        raise ConflictError("Runtime registration expired before admission completed")

    try:
        permit = await _new_first_permit(
            db,
            session_id=session_id,
            config=config,
            now=now,
        )
    except Exception as exc:
        await db.rollback()
        failure_code = (
            "BUDGET_UNAVAILABLE"
            if isinstance(exc, NotFoundError)
            else "FUNDING_FAILED"
        )
        stopped_at = _utc_now()
        await _mark_release_pending(
            db,
            session_id=session_id,
            failure_code=failure_code,
            now=stopped_at,
        )
        await _complete_release_compensation(
            db,
            config=config,
            session_id=session_id,
            user_id=owner_user_id,
            operation_id=operation_id,
            failure_code=failure_code,
            now=stopped_at,
            action=action,
        )
        raise ConflictError(
            "Session funding failed; the runtime reservation was compensated"
        ) from exc

    if permit.funds_exhausted or permit.authorized_through_ms <= 0 or permit.expires_at <= _utc_now():
        failure_code = (
            "INSUFFICIENT_FUNDS"
            if permit.funds_exhausted or permit.authorized_through_ms <= 0
            else "FUNDING_PERMIT_EXPIRED"
        )
        await _mark_release_pending(
            db,
            session_id=session_id,
            failure_code=failure_code,
            now=_utc_now(),
        )
        await _complete_release_compensation(
            db,
            config=config,
            session_id=session_id,
            user_id=owner_user_id,
            operation_id=operation_id,
            failure_code=failure_code,
            now=_utc_now(),
            action=action,
        )
        raise ConflictError("Session funding was denied or expired")

    jti = await _finalize_registered_session(
        db,
        session_id=session_id,
        owner_user_id=owner_user_id,
        operation_id=operation_id,
        config=config,
        permit=permit,
        now=_utc_now(),
        participant_kind=participant_kind,
        action=action,
    )
    if participant_kind == "owner":
        return await _registered_result(
            db,
            session_id=session_id,
            jti=jti,
            config=config,
        )
    return await _registered_guest_result(
        db,
        session_id=session_id,
        jti=jti,
        config=config,
    )


def _signing_claims(token: str, config: SessionAdmissionConfig) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
        if (
            header.get("alg") != "EdDSA"
            or header.get("kid") != config.join_key_fingerprint
        ):
            raise jwt.InvalidTokenError("unexpected join signing key")
        claims = jwt.decode(
            token,
            config.join_public_key,
            algorithms=["EdDSA"],
            audience="voice-runtime",
            issuer=config.join_issuer,
            options={
                "verify_exp": False,
                "require": [
                    "iss",
                    "aud",
                    "sub",
                    "iat",
                    "nbf",
                    "exp",
                    "jti",
                    "scope",
                    "session_id",
                    "session_version",
                    "config_version_id",
                    "runner_id",
                    "registration_id",
                    "origin",
                    "participant_kind",
                ],
            },
        )
    except jwt.PyJWTError as exc:
        raise UnauthenticatedError("Join token is invalid or expired") from exc
    if (
        claims.get("aud") != "voice-runtime"
        or claims.get("scope") != "webrtc:join"
        or type(claims.get("session_version")) is not int
        or type(claims.get("exp")) is not int
        or type(claims.get("nbf")) is not int
        or type(claims.get("iat")) is not int
        or claims["nbf"] > claims["iat"]
        or claims["iat"] >= claims["exp"]
        or any(
            type(claims.get(name)) is not str
            for name in (
                "sub",
                "session_id",
                "config_version_id",
                "runner_id",
                "registration_id",
                "origin",
                "participant_kind",
            )
        )
        or claims.get("participant_kind") not in {"owner", "guest"}
        or (
            claims["participant_kind"] == "owner"
            and type(claims.get("auth_session_id")) is not str
        )
        or (
            claims["participant_kind"] == "guest"
            and "auth_session_id" in claims
        )
    ):
        raise UnauthenticatedError("Join token claims are invalid")
    return claims


async def get_runtime_config(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    runtime_principal: RuntimePrincipal,
    settings: PlatformSettings,
) -> RuntimeConfigView:
    """Return only the registered immutable version and its current funded permit."""
    now = _utc_now()
    config = session_runtime_config(settings)
    if runtime_principal.service_id != "voice-runtime" or runtime_principal.runner_id != config.runner_id:
        raise ForbiddenError("Runtime principal does not match configured runner")
    session = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration).where(
                RuntimeRegistration.session_id == session_id
            )
        )
    ).scalar_one_or_none()
    participant_kind = (
        "guest" if session is not None and session.kind == "voice" else "owner"
    )
    participant = (
        await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.kind == participant_kind,
            )
        )
    ).scalar_one_or_none()
    if (
        session is None
        or registration is None
        or participant is None
        or session.runner_id != runtime_principal.runner_id
        or registration.runner_id != runtime_principal.runner_id
        or registration.status != "REGISTERED"
        or session.agent_version_id is None
        or session.state
        not in {
            "REGISTERED",
            "CONNECTING",
            "INITIALIZING",
            "READY",
            "ACTIVE",
            "RECONNECTING",
        }
        or session.hard_end_deadline is None
        or session.hard_end_deadline <= now
        or (
            session.state == "REGISTERED"
            and (
                session.connect_deadline is None
                or session.connect_deadline <= now
            )
        )
    ):
        raise NotFoundError("Registered session runtime configuration not found")
    auth_session = None
    if participant.auth_session_id is not None:
        auth_session = (
            await db.execute(
                select(AuthSession).where(
                    AuthSession.id == participant.auth_session_id
                )
            )
        ).scalar_one_or_none()
    owner = (
        await db.execute(select(User).where(User.id == session.owner_user_id))
    ).scalar_one_or_none()
    capability = None
    if participant.kind == "guest":
        capability = (
            await db.execute(
                select(PublicGuestCapability).where(
                    PublicGuestCapability.session_id == session_id
                )
            )
        ).scalar_one_or_none()
    if (
        owner is None
        or owner.status != "active"
        or participant.revoked_at is not None
        or participant.kind not in {"owner", "guest"}
        or session.kind != ("voice_test" if participant.kind == "owner" else "voice")
        or not isinstance(session.tariff_snapshot, dict)
        or session.tariff_snapshot.get("public") is not (participant.kind == "guest")
        or session.tariff_snapshot.get("paid") is not False
        or (
            participant.kind == "owner"
            and (
                auth_session is None
                or auth_session.user_id != session.owner_user_id
                or auth_session.state != "authenticated"
                or auth_session.revoked_at is not None
                or auth_session.idle_expires_at <= now
                or auth_session.absolute_expires_at <= now
                or auth_session.auth_version != owner.auth_version
            )
        )
        or (
            participant.kind == "guest"
            and (
                participant.auth_session_id is not None
                or capability is None
                or capability.owner_user_id != session.owner_user_id
                or capability.participant_id != participant.participant_id
                or capability.revoked_at is not None
                or capability.expires_at <= now
            )
        )
    ):
        raise ForbiddenError("Registered session participant is no longer valid")

    version_row = (
        await db.execute(
            select(AgentVersion, PlatformPromptVersion)
            .join(
                PlatformPromptVersion,
                PlatformPromptVersion.id == AgentVersion.policy_version_id,
            )
            .where(
                AgentVersion.id == session.agent_version_id,
                AgentVersion.agent_id.in_(
                    select(Agent.id).where(Agent.owner_user_id == session.owner_user_id)
                ),
            )
        )
    ).one_or_none()
    if version_row is None:
        raise ConflictError("Registered immutable agent version is unavailable")
    version, policy = version_row
    snapshot = _validate_immutable_version(version, policy)
    budget = (
        await db.execute(select(CallBudget).where(CallBudget.session_id == session_id))
    ).scalar_one_or_none()
    if (
        budget is None
        or budget.status != "OPEN"
        or budget.permit_expiry is None
        or budget.cumulative_authorized_ms <= budget.cumulative_proven_ms
    ):
        raise DependencyUnavailableError("Registered session has no open funded budget")
    checkpoint = (
        await db.execute(
            select(MeterCheckpoint)
            .where(
                MeterCheckpoint.session_id == session_id,
                MeterCheckpoint.budget_version == budget.version - 1,
            )
            .order_by(MeterCheckpoint.received_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if checkpoint is None:
        raise DependencyUnavailableError("Current funded permit checkpoint is missing")
    operation = (
        await db.execute(
            select(BillingOperation).where(
                BillingOperation.session_id == session_id,
                BillingOperation.operation_id == checkpoint.operation_id,
            )
        )
    ).scalar_one_or_none()
    if operation is None:
        raise DependencyUnavailableError("Current funded permit operation is missing")
    try:
        permit = decode_stored_response(BudgetPermit, operation.response_body)
    except PydanticValidationError as exc:
        raise DependencyUnavailableError(
            "Current budget operation is not a permit"
        ) from exc
    if (
        permit.session_id != session_id
        or permit.budget_version != budget.version
        or permit.expires_at != budget.permit_expiry
        or permit.authorized_through_ms != budget.cumulative_authorized_ms
        or permit.reserved_minor != budget.reserved_minor
        or permit.expires_at <= now
        or permit.funds_exhausted
    ):
        raise DependencyUnavailableError("Registered session has no live funded permit")
    pins = snapshot["model_pins"]
    await db.commit()
    return RuntimeConfigView(
        session_id=session.id,
        owner_user_id=session.owner_user_id,
        participant_id=participant.participant_id,
        auth_session_id=participant.auth_session_id,
        participant_kind=participant.kind,
        origin=participant.origin,
        runner_id=registration.runner_id,
        registration_id=registration.registration_id,
        config_version_id=version.id,
        session_version=session.version,
        state=session.state,
        connect_deadline=session.connect_deadline,
        hard_end_deadline=session.hard_end_deadline,
        tariff_snapshot=session.tariff_snapshot or {},
        budget=RuntimeBudgetSnapshot(
            pinned_rate_minor_per_minute=budget.pinned_rate,
            budget_version=budget.version,
            reserved_minor=budget.reserved_minor,
            authorized_through_ms=budget.cumulative_authorized_ms,
            permit=permit,
        ),
        immutable_config=snapshot,
        authoring_hash=version.authoring_hash,
        flow_hash=version.flow_hash,
        artifact_hash=version.artifact_hash,
        model_hash=pins["model_hash"],
        voice_hash=pins["voice_hash"],
        settings_hash=pins["settings_hash"],
    )


async def issue_join(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    principal: Principal,
    auth_session_id: uuid.UUID,
    origin: str,
    operation_id: str,
    settings: PlatformSettings,
) -> JoinDetails:
    """Issue a new one-use join JTI for an unconnected or reconnecting owner."""
    config = session_runtime_config(settings)
    _check_origin(origin, settings)
    operation_id = _check_operation_id(operation_id)
    now = _utc_now()
    request_hash = compute_payload_hash(
        {
            "session_id": str(session_id),
            "auth_session_id": str(auth_session_id),
            "origin": origin,
        }
    )
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=SESSION_JOIN_ACTION,
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=SESSION_JOIN_ACTION,
        request_hash=request_hash,
    )
    if cached is not None:
        if (
            cached.status_code != 201
            or not isinstance(cached.response_json.get("jti"), str)
        ):
            raise ConflictError("Stored join outcome is not available")
        await _verify_owner_session(
            db,
            principal=principal,
            auth_session_id=auth_session_id,
            now=now,
        )
        jti = cached.response_json["jti"]
        await db.commit()
        row = (
            await db.execute(select(SessionJoinJti).where(SessionJoinJti.jti == jti))
        ).scalar_one_or_none()
        if (
            row is None
            or row.session_id != session_id
            or row.owner_user_id != principal.user_id
            or row.auth_session_id != auth_session_id
            or not hmac.compare_digest(row.origin, origin)
        ):
            raise DependencyUnavailableError("Stored join JTI binding is invalid")
        return JoinDetails(
            session_id=row.session_id,
            runner_id=row.runner_id,
            config_version_id=row.config_version_id,
            signaling_url=row.signaling_url,
            join_token=_jwt_for_jti(row, config),
            expires_at=row.expires_at,
        )

    await _verify_owner_session(
        db,
        principal=principal,
        auth_session_id=auth_session_id,
        now=now,
    )
    session = (
        await db.execute(
            select(Session).where(Session.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    participant = (
        await db.execute(
            select(SessionParticipant)
            .where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.kind == "owner",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    budget = (
        await db.execute(
            select(CallBudget).where(CallBudget.session_id == session_id)
        )
    ).scalar_one_or_none()
    peer = (
        await db.execute(
            select(SessionPeer)
            .where(SessionPeer.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        session is None
        or participant is None
        or registration is None
        or session.owner_user_id != principal.user_id
        or participant.owner_user_id != principal.user_id
        or participant.auth_session_id != auth_session_id
        or not hmac.compare_digest(participant.origin, origin)
        or session.runner_id != config.runner_id
        or registration.runner_id != config.runner_id
        or registration.status != "REGISTERED"
        or session.state not in {"REGISTERED", "RECONNECTING"}
        or session.agent_version_id is None
        or session.hard_end_deadline is None
        or session.hard_end_deadline <= now
        or (
            session.state == "REGISTERED"
            and (
                session.connect_deadline is None
                or session.connect_deadline <= now
            )
        )
        or budget is None
        or budget.status != "OPEN"
        or budget.permit_expiry is None
        or budget.permit_expiry <= now
        or budget.cumulative_authorized_ms <= budget.cumulative_proven_ms
        or (peer is not None and peer.status == "ACTIVE")
    ):
        raise ConflictError("Session is not eligible for a new join credential")
    reconnect_until: datetime | None = None
    if session.state == "RECONNECTING":
        if peer is None or peer.reconnect_until is None or peer.reconnect_until <= now:
            raise ConflictError("Session reconnect window has expired")
        reconnect_until = peer.reconnect_until
    expiry = min(
        now + timedelta(seconds=config.join_ttl_seconds),
        session.hard_end_deadline,
        budget.permit_expiry,
        reconnect_until or session.hard_end_deadline,
    )
    expiry = datetime.fromtimestamp(int(expiry.timestamp()), tz=timezone.utc)
    issued_at = datetime.fromtimestamp(int(now.timestamp()), tz=timezone.utc)
    if expiry <= issued_at:
        raise ConflictError("No live join window remains")
    jti = str(uuid.uuid4())
    db.add(
        SessionJoinJti(
            jti=jti,
            session_id=session_id,
            participant_id=participant.participant_id,
            owner_user_id=principal.user_id,
            auth_session_id=auth_session_id,
            runner_id=registration.runner_id,
            config_version_id=session.agent_version_id,
            registration_id=registration.registration_id,
            session_version=session.version,
            signaling_url=config.signaling_url,
            origin=participant.origin,
            issued_at=issued_at,
            expires_at=expiry,
            key_fingerprint=config.join_key_fingerprint,
        )
    )
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=SESSION_JOIN_ACTION,
        request_hash=request_hash,
        status_code=201,
        response_json={"session_id": str(session_id), "jti": jti},
    )
    await db.commit()
    row = (
        await db.execute(select(SessionJoinJti).where(SessionJoinJti.jti == jti))
    ).scalar_one()
    await db.commit()
    return JoinDetails(
        session_id=session_id,
        runner_id=row.runner_id,
        config_version_id=row.config_version_id,
        signaling_url=row.signaling_url,
        join_token=_jwt_for_jti(row, config),
        expires_at=row.expires_at,
    )


async def issue_guest_join(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    origin: str,
    operation_id: str,
    settings: PlatformSettings,
) -> JoinDetails:
    """Issue a capability-bound, single-use guest reconnect token."""
    config = public_session_admission_config(settings)
    _check_origin(origin, settings)
    operation_id = _check_operation_id(operation_id)
    now = _utc_now()
    request_hash = compute_payload_hash(
        {"session_id": str(session_id), "owner_user_id": str(owner_user_id), "origin": origin}
    )
    await lock_idempotency_key(
        db,
        user_id=owner_user_id,
        idempotency_key=operation_id,
        action=PUBLIC_SESSION_JOIN_ACTION,
    )
    cached = await check_idempotency_record(
        db,
        user_id=owner_user_id,
        idempotency_key=operation_id,
        action=PUBLIC_SESSION_JOIN_ACTION,
        request_hash=request_hash,
    )
    if cached is not None:
        jti = cached.response_json.get("jti")
        if cached.status_code != 201 or not isinstance(jti, str):
            raise ConflictError("Stored guest reconnect outcome is not available")
        await db.commit()
        row = (
            await db.execute(select(SessionJoinJti).where(SessionJoinJti.jti == jti))
        ).scalar_one_or_none()
        if (
            row is None
            or row.session_id != session_id
            or row.owner_user_id != owner_user_id
            or row.participant_kind != "guest"
            or row.auth_session_id is not None
            or not hmac.compare_digest(row.origin, origin)
            or row.revoked_at is not None
            or row.consumed_at is not None
            or row.expires_at <= now
        ):
            raise ConflictError("Stored guest reconnect token is no longer usable")
        return JoinDetails(
            session_id=row.session_id,
            runner_id=row.runner_id,
            config_version_id=row.config_version_id,
            signaling_url=row.signaling_url,
            join_token=_jwt_for_jti(row, config),
            expires_at=row.expires_at,
        )

    session = (
        await db.execute(
            select(Session).where(Session.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    participant = (
        await db.execute(
            select(SessionParticipant)
            .where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.kind == "guest",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    budget = (
        await db.execute(
            select(CallBudget).where(CallBudget.session_id == session_id)
        )
    ).scalar_one_or_none()
    peer = (
        await db.execute(
            select(SessionPeer)
            .where(SessionPeer.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    capability = (
        await db.execute(
            select(PublicGuestCapability)
            .where(PublicGuestCapability.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    owner = None
    deployment = None
    if session is not None and capability is not None:
        owner = (
            await db.execute(
                select(User).where(User.id == session.owner_user_id)
            )
        ).scalar_one_or_none()
        deployment = (
            await db.execute(
                select(PublicDeployment)
                .where(PublicDeployment.id == capability.deployment_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    version_agent_id = None
    if session is not None and session.agent_version_id is not None:
        version_agent_id = (
            await db.execute(
                select(AgentVersion.agent_id).where(
                    AgentVersion.id == session.agent_version_id
                )
            )
        ).scalar_one_or_none()
    if (
        session is None
        or participant is None
        or registration is None
        or budget is None
        or capability is None
        or owner is None
        or deployment is None
        or owner.id != owner_user_id
        or owner.status != "active"
        or session.owner_user_id != owner_user_id
        or participant.owner_user_id != owner_user_id
        or participant.auth_session_id is not None
        or participant.revoked_at is not None
        or not hmac.compare_digest(participant.origin, origin)
        or session.kind != "voice"
        or session.agent_version_id is None
        or not isinstance(session.tariff_snapshot, dict)
        or session.tariff_snapshot.get("public") is not True
        or session.tariff_snapshot.get("paid") is not False
        or session.runner_id != config.runner_id
        or registration.runner_id != config.runner_id
        or registration.status != "REGISTERED"
        or session.state not in {"REGISTERED", "RECONNECTING"}
        or session.hard_end_deadline is None
        or session.hard_end_deadline <= now
        or (
            session.state == "REGISTERED"
            and (session.connect_deadline is None or session.connect_deadline <= now)
        )
        or budget.status != "OPEN"
        or budget.permit_expiry is None
        or budget.permit_expiry <= now
        or budget.cumulative_authorized_ms <= budget.cumulative_proven_ms
        or (peer is not None and peer.status == "ACTIVE")
        or (session.state == "RECONNECTING" and (
            peer is None or peer.status != "RELEASED"
            or peer.reconnect_until is None or peer.reconnect_until <= now
        ))
        or capability.owner_user_id != owner_user_id
        or capability.participant_id != participant.participant_id
        or capability.revoked_at is not None
        or capability.expires_at <= now
        or deployment.owner_user_id != owner_user_id
        or deployment.agent_id != version_agent_id
        or not deployment.enabled
        or deployment.disabled_at is not None
        or not hmac.compare_digest(deployment.origin, origin)
    ):
        raise ConflictError("Guest session is not eligible for reconnect")
    reconnect_until = peer.reconnect_until if peer is not None else None
    expiry = min(
        now + timedelta(seconds=config.join_ttl_seconds),
        session.hard_end_deadline,
        budget.permit_expiry,
        capability.expires_at,
        reconnect_until or session.hard_end_deadline,
    )
    expiry = datetime.fromtimestamp(int(expiry.timestamp()), tz=timezone.utc)
    issued_at = datetime.fromtimestamp(int(now.timestamp()), tz=timezone.utc)
    if expiry <= issued_at:
        raise ConflictError("No live guest reconnect window remains")
    stale_jtis = (
        await db.execute(
            select(SessionJoinJti)
            .where(
                SessionJoinJti.session_id == session_id,
                SessionJoinJti.participant_id == participant.participant_id,
                SessionJoinJti.participant_kind == "guest",
                SessionJoinJti.revoked_at.is_(None),
                SessionJoinJti.consumed_at.is_(None),
            )
            .with_for_update()
        )
    ).scalars().all()
    for stale_jti in stale_jtis:
        stale_jti.revoked_at = now
    jti = str(uuid.uuid4())
    db.add(
        SessionJoinJti(
            jti=jti,
            session_id=session_id,
            participant_id=participant.participant_id,
            owner_user_id=owner_user_id,
            auth_session_id=None,
            participant_kind="guest",
            runner_id=registration.runner_id,
            config_version_id=session.agent_version_id,
            registration_id=registration.registration_id,
            session_version=session.version,
            signaling_url=config.signaling_url,
            origin=participant.origin,
            issued_at=issued_at,
            expires_at=expiry,
            key_fingerprint=config.join_key_fingerprint,
        )
    )
    await store_idempotency_record(
        db,
        user_id=owner_user_id,
        idempotency_key=operation_id,
        action=PUBLIC_SESSION_JOIN_ACTION,
        request_hash=request_hash,
        status_code=201,
        response_json={"session_id": str(session_id), "jti": jti},
    )
    await db.commit()
    row = (
        await db.execute(select(SessionJoinJti).where(SessionJoinJti.jti == jti))
    ).scalar_one()
    await db.commit()
    return JoinDetails(
        session_id=session_id,
        runner_id=row.runner_id,
        config_version_id=row.config_version_id,
        signaling_url=row.signaling_url,
        join_token=_jwt_for_jti(row, config),
        expires_at=row.expires_at,
    )


async def consume_join(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    runtime_principal: RuntimePrincipal,
    payload: JoinConsumptionRequest,
    settings: PlatformSettings,
) -> JoinConsumptionView:
    """Verify bindings and atomically consume a JTI for the sole active peer."""
    config = session_runtime_config(settings)
    if (
        runtime_principal.service_id != "voice-runtime"
        or runtime_principal.runner_id != config.runner_id
    ):
        raise ForbiddenError("Runtime principal does not match configured runner")
    claims = _signing_claims(payload.join_token, config)
    try:
        token_session_id = uuid.UUID(claims["session_id"])
        participant_id = uuid.UUID(claims["sub"])
        participant_kind = claims["participant_kind"]
        token_auth_session_id = (
            uuid.UUID(claims["auth_session_id"])
            if participant_kind == "owner"
            else None
        )
        config_version_id = uuid.UUID(claims["config_version_id"])
        registration_id = uuid.UUID(claims["registration_id"])
        uuid.UUID(claims["jti"])
    except (ValueError, TypeError, KeyError) as exc:
        raise UnauthenticatedError("Join token identity claims are invalid") from exc
    if token_session_id != session_id:
        raise ForbiddenError("Join token is bound to a different session")
    if not hmac.compare_digest(payload.origin, claims["origin"]):
        raise ForbiddenError("Join token origin does not match the browser handshake")

    now = _utc_now()
    session = (
        await db.execute(
            select(Session).where(Session.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    participant = (
        await db.execute(
            select(SessionParticipant)
            .where(
                SessionParticipant.session_id == session_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    jti = (
        await db.execute(
            select(SessionJoinJti)
            .where(SessionJoinJti.jti == claims["jti"])
            .with_for_update()
        )
    ).scalar_one_or_none()
    peer = (
        await db.execute(
            select(SessionPeer)
            .where(SessionPeer.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    guest_capability = None
    deployment = None
    if participant_kind == "guest":
        guest_capability = (
            await db.execute(
                select(PublicGuestCapability)
                .where(PublicGuestCapability.session_id == session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if guest_capability is not None:
            deployment = (
                await db.execute(
                    select(PublicDeployment).where(
                        PublicDeployment.id == guest_capability.deployment_id
                    )
                )
            ).scalar_one_or_none()
    if (
        session is None
        or registration is None
        or participant is None
        or jti is None
        or registration.status != "REGISTERED"
        or registration.runner_id != runtime_principal.runner_id
        or claims["runner_id"] != runtime_principal.runner_id
        or registration.registration_id != registration_id
        or session.runner_id != runtime_principal.runner_id
        or session.agent_version_id != config_version_id
        or session.kind != ("voice_test" if participant_kind == "owner" else "voice")
        or not isinstance(session.tariff_snapshot, dict)
        or session.tariff_snapshot.get("public") is not (participant_kind == "guest")
        or session.tariff_snapshot.get("paid") is not False
        or session.state not in LIVE_SESSION_STATES - {"RESERVED"}
        or session.hard_end_deadline is None
        or session.hard_end_deadline <= now
        or (
            session.state == "REGISTERED"
            and (
                session.connect_deadline is None
                or session.connect_deadline <= now
            )
        )
        or participant.participant_id != participant_id
        or participant.kind != participant_kind
        or participant.owner_user_id != session.owner_user_id
        or participant.auth_session_id != token_auth_session_id
        or participant.revoked_at is not None
        or not hmac.compare_digest(participant.origin, payload.origin)
        or jti.session_id != session_id
        or jti.participant_id != participant_id
        or jti.owner_user_id != session.owner_user_id
        or jti.auth_session_id != token_auth_session_id
        or jti.participant_kind != participant_kind
        or jti.config_version_id != config_version_id
        or jti.registration_id != registration_id
        or jti.runner_id != runtime_principal.runner_id
        or jti.session_version != claims["session_version"]
        or int(jti.issued_at.timestamp()) != claims["iat"]
        or int(jti.expires_at.timestamp()) != claims["exp"]
        or not hmac.compare_digest(jti.origin, payload.origin)
        or not hmac.compare_digest(jti.key_fingerprint, config.join_key_fingerprint)
        or jti.revoked_at is not None
    ):
        raise ForbiddenError("Join token is not bound to this registered session")

    if jti.consumed_at is not None:
        if (
            jti.consumed_peer_id == payload.peer_id
            and peer is not None
            and peer.peer_id == payload.peer_id
            and peer.status == "ACTIVE"
            and session.state in {
                "CONNECTING",
                "INITIALIZING",
                "READY",
                "ACTIVE",
                "RECONNECTING",
            }
        ):
            await db.commit()
            return JoinConsumptionView(
                session_id=session_id,
                peer_id=payload.peer_id,
                runner_id=runtime_principal.runner_id,
                session_version=session.version,
                state=session.state,
                reconnect=session.state == "RECONNECTING",
            )
        raise ConflictError("Join JTI has already been consumed")

    if (
        session.version != claims["session_version"]
        or session.state not in {"REGISTERED", "RECONNECTING"}
        or claims["exp"] <= int(now.timestamp())
        or jti.expires_at <= now
    ):
        raise ConflictError("Join token is expired or its session version changed")
    if peer is not None and peer.status == "ACTIVE":
        raise ConflictError("Session already has an active peer")
    reconnect = session.state == "RECONNECTING"
    if reconnect and (
        peer is None
        or peer.reconnect_until is None
        or peer.reconnect_until <= now
    ):
        raise ConflictError("Session reconnect window has expired")

    budget = (
        await db.execute(
            select(CallBudget).where(CallBudget.session_id == session_id)
        )
    ).scalar_one_or_none()
    if (
        budget is None
        or budget.status != "OPEN"
        or budget.permit_expiry is None
        or budget.permit_expiry <= now
        or budget.cumulative_authorized_ms <= budget.cumulative_proven_ms
    ):
        raise ConflictError("Session has no live funded permit")
    auth_session = None
    if token_auth_session_id is not None:
        auth_session = (
            await db.execute(
                select(AuthSession).where(AuthSession.id == token_auth_session_id)
            )
        ).scalar_one_or_none()
    owner = (
        await db.execute(select(User).where(User.id == session.owner_user_id))
    ).scalar_one_or_none()
    if owner is None or owner.status != "active":
        raise ForbiddenError("Join owner is no longer active")
    if participant_kind == "owner" and (
        auth_session is None
        or auth_session.user_id != session.owner_user_id
        or auth_session.state != "authenticated"
        or auth_session.revoked_at is not None
        or auth_session.idle_expires_at <= now
        or auth_session.absolute_expires_at <= now
        or auth_session.auth_version != owner.auth_version
    ):
        raise ForbiddenError("Join owner authentication is no longer valid")
    if participant_kind == "guest":
        version_agent_id = (
            await db.execute(
                select(AgentVersion.agent_id).where(
                    AgentVersion.id == session.agent_version_id
                )
            )
        ).scalar_one_or_none()
        if (
            token_auth_session_id is not None
            or guest_capability is None
            or deployment is None
            or guest_capability.owner_user_id != session.owner_user_id
            or guest_capability.participant_id != participant.participant_id
            or guest_capability.revoked_at is not None
            or guest_capability.expires_at <= now
            or deployment.owner_user_id != session.owner_user_id
            or deployment.agent_id != version_agent_id
            or not deployment.enabled
            or deployment.disabled_at is not None
            or not hmac.compare_digest(deployment.origin, payload.origin)
        ):
            raise ForbiddenError("Guest deployment or capability is no longer valid")

    jti.consumed_at = now
    jti.consumed_peer_id = payload.peer_id
    session.version += 1
    if not reconnect:
        session.state = "CONNECTING"
        peer = SessionPeer(
            session_id=session_id,
            peer_id=payload.peer_id,
            participant_id=participant_id,
            runner_id=runtime_principal.runner_id,
            status="ACTIVE",
            joined_at=now,
        )
        db.add(peer)
    else:
        assert peer is not None
        peer.peer_id = payload.peer_id
        peer.participant_id = participant_id
        peer.runner_id = runtime_principal.runner_id
        peer.status = "ACTIVE"
        peer.joined_at = now
        peer.released_at = None
        peer.reconnect_until = None
        peer.release_reason = None
    await db.commit()
    return JoinConsumptionView(
        session_id=session_id,
        peer_id=payload.peer_id,
        runner_id=runtime_principal.runner_id,
        session_version=session.version,
        state=session.state,
        reconnect=reconnect,
    )


def _session_state_view(session: Session) -> SessionStateView:
    return SessionStateView(
        session_id=session.id,
        state=session.state,
        session_version=session.version,
        ended_at=session.ended_at,
        failure_code=session.failure_code,
    )


async def get_owner_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> OwnerSessionView:
    """Return the safe state of one session owned by the authenticated user."""
    session = (
        await db.execute(
            select(Session).where(
                Session.id == session_id,
                Session.owner_user_id == owner_user_id,
                Session.kind == "voice_test",
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise NotFoundError("Session not found")
    if (
        session.agent_version_id is None
        or session.connect_deadline is None
        or session.hard_end_deadline is None
    ):
        raise DependencyUnavailableError("Stored session metadata is incomplete")
    await db.commit()
    return OwnerSessionView(
        session_id=session.id,
        agent_version_id=session.agent_version_id,
        kind="voice_test",
        state=session.state,
        session_version=session.version,
        connect_deadline=session.connect_deadline,
        hard_end_deadline=session.hard_end_deadline,
        created_at=session.created_at,
        ended_at=session.ended_at,
        failure_code=session.failure_code,
    )


async def _release_execution_slot(
    db: AsyncSession,
    *,
    config: SessionAdmissionConfig,
    session_id: uuid.UUID,
    runner_id: str,
    now: datetime | None = None,
) -> None:
    """Stop the assigned runtime and free capacity after its bound acknowledgement."""
    session = (
        await db.execute(
            select(Session).where(Session.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    slot = (
        await db.execute(
            select(SessionSlot)
            .where(SessionSlot.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None or registration is None or slot is None:
        raise DependencyUnavailableError("Session release records are incomplete")
    if (
        session.runner_id != runner_id
        or registration.runner_id != runner_id
        or slot.runner_id != runner_id
        or session.state not in {"ENDING", "COMPLETED", "FAILED", "EXPIRED"}
    ):
        raise ConflictError("Session is not assigned for runtime release")
    if registration.status == "RELEASED":
        slot.released_at = slot.released_at or registration.released_at or now or _utc_now()
        await db.commit()
        return
    if registration.status != "REGISTERED":
        raise ConflictError("Session registration is not active for runtime release")
    if registration.released_at is not None and slot.released_at is not None:
        await db.commit()
        return

    command = RuntimeReleaseRequest(
        session_id=session_id,
        runner_id=runner_id,
        registration_id=registration.registration_id,
        session_version=session.version,
    )
    await db.commit()
    acknowledgement = await release_from_runtime(config, command)
    if not acknowledgement.released:
        raise DependencyUnavailableError(
            "Runtime did not confirm session teardown",
        )

    stopped_at = now or _utc_now()
    session = (
        await db.execute(
            select(Session).where(Session.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    slot = (
        await db.execute(
            select(SessionSlot)
            .where(SessionSlot.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    peer = (
        await db.execute(
            select(SessionPeer)
            .where(SessionPeer.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        session is None
        or registration is None
        or slot is None
        or session.runner_id != runner_id
        or registration.runner_id != runner_id
        or registration.registration_id != command.registration_id
        or session.version != command.session_version
        or session.state not in {"ENDING", "COMPLETED", "FAILED", "EXPIRED"}
        or registration.status != "REGISTERED"
    ):
        raise ConflictError("Session changed during runtime release")
    registration.released_at = registration.released_at or stopped_at
    slot.released_at = slot.released_at or stopped_at
    if peer is not None and peer.status == "ACTIVE":
        peer.status = "RELEASED"
        peer.released_at = peer.released_at or stopped_at
        peer.reconnect_until = None
        peer.release_reason = "SESSION_ENDING"
    await db.commit()


async def end_owner_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    principal: Principal,
    auth_session_id: uuid.UUID,
    origin: str,
    operation_id: str,
    settings: PlatformSettings,
) -> SessionStateView:
    """Idempotently request owner teardown, freeing execution capacity after stop ack."""
    config = session_runtime_config(settings)
    _check_origin(origin, settings)
    operation_id = _check_operation_id(operation_id)
    now = _utc_now()
    request_hash = compute_payload_hash(
        {
            "auth_session_id": str(auth_session_id),
            "origin": origin,
            "session_id": str(session_id),
        }
    )
    await lock_idempotency_key(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=SESSION_END_ACTION,
    )
    cached = await check_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=SESSION_END_ACTION,
        request_hash=request_hash,
    )
    if cached is not None:
        if cached.status_code not in {200, 202}:
            raise ConflictError("Stored session end outcome is unavailable")
        await _verify_owner_session(
            db,
            principal=principal,
            auth_session_id=auth_session_id,
            now=now,
        )
        try:
            view = decode_stored_response(SessionStateView, cached.response_json)
        except PydanticValidationError as exc:
            raise DependencyUnavailableError(
                "Stored session end outcome is invalid"
            ) from exc
        await db.commit()
        if view.state == "ENDING":
            await _release_execution_slot(
                db,
                config=config,
                session_id=session_id,
                runner_id=config.runner_id,
            )
        return view

    await _verify_owner_session(
        db,
        principal=principal,
        auth_session_id=auth_session_id,
        now=now,
    )
    session = (
        await db.execute(
            select(Session)
            .where(
                Session.id == session_id,
                Session.owner_user_id == principal.user_id,
                Session.kind == "voice_test",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise NotFoundError("Session not found")
    if session.runner_id != config.runner_id:
        raise DependencyUnavailableError(
            "Configured runner does not match the existing session assignment"
        )
    if session.state in LIVE_SESSION_STATES:
        registration = (
            await db.execute(
                select(RuntimeRegistration)
                .where(RuntimeRegistration.session_id == session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        slot = (
            await db.execute(
                select(SessionSlot)
                .where(SessionSlot.session_id == session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            registration is None
            or registration.runner_id != config.runner_id
            or registration.status not in {"REGISTERED", "RELEASED"}
            or slot is None
            or slot.runner_id != config.runner_id
        ):
            raise DependencyUnavailableError(
                "Session runtime release records are incomplete"
            )
    if session.state in LIVE_SESSION_STATES:
        if session.state == "RESERVED":
            raise ConflictError("Session admission is still being resolved")
        if session.state != "ENDING":
            session.state = "ENDING"
            session.version += 1
            session.failure_code = None
    elif session.state not in TERMINAL_SESSION_STATES:
        raise ConflictError("Session cannot be ended from its current state")
    view = _session_state_view(session)
    await store_idempotency_record(
        db,
        user_id=principal.user_id,
        idempotency_key=operation_id,
        action=SESSION_END_ACTION,
        request_hash=request_hash,
        status_code=202 if view.state == "ENDING" else 200,
        response_json=view.model_dump(mode="json"),
    )
    await db.commit()
    if view.state == "ENDING":
        await _release_execution_slot(
            db,
            config=config,
            session_id=session_id,
            runner_id=config.runner_id,
        )
    return view


async def end_guest_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    origin: str,
    operation_id: str,
    settings: PlatformSettings,
) -> SessionStateView:
    """End a public guest session using only its session-scoped capability."""
    config = session_runtime_config(settings)
    _check_origin(origin, settings)
    operation_id = _check_operation_id(operation_id)
    now = _utc_now()
    request_hash = compute_payload_hash(
        {"session_id": str(session_id), "owner_user_id": str(owner_user_id), "origin": origin}
    )
    await lock_idempotency_key(
        db,
        user_id=owner_user_id,
        idempotency_key=operation_id,
        action=PUBLIC_SESSION_END_ACTION,
    )
    cached = await check_idempotency_record(
        db,
        user_id=owner_user_id,
        idempotency_key=operation_id,
        action=PUBLIC_SESSION_END_ACTION,
        request_hash=request_hash,
    )
    if cached is not None:
        if cached.status_code not in {200, 202}:
            raise ConflictError("Stored guest session end outcome is unavailable")
        try:
            view = decode_stored_response(SessionStateView, cached.response_json)
        except PydanticValidationError as exc:
            raise DependencyUnavailableError(
                "Stored guest session end outcome is invalid"
            ) from exc
        await db.commit()
        if view.state == "ENDING":
            await _release_execution_slot(
                db,
                config=config,
                session_id=session_id,
                runner_id=config.runner_id,
            )
        return view

    session = (
        await db.execute(
            select(Session)
            .where(
                Session.id == session_id,
                Session.owner_user_id == owner_user_id,
                Session.kind == "voice",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    participant = (
        await db.execute(
            select(SessionParticipant)
            .where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.kind == "guest",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    capability = (
        await db.execute(
            select(PublicGuestCapability)
            .where(PublicGuestCapability.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        session is None
        or participant is None
        or capability is None
        or participant.owner_user_id != owner_user_id
        or participant.auth_session_id is not None
        or participant.revoked_at is not None
        or not hmac.compare_digest(participant.origin, origin)
        or capability.owner_user_id != owner_user_id
        or capability.participant_id != participant.participant_id
        or capability.revoked_at is not None
        or capability.expires_at <= now
    ):
        raise NotFoundError("Guest session is not available to this capability")
    if session.runner_id != config.runner_id:
        raise DependencyUnavailableError(
            "Configured runner does not match the existing session assignment"
        )
    if session.state in LIVE_SESSION_STATES:
        registration = (
            await db.execute(
                select(RuntimeRegistration)
                .where(RuntimeRegistration.session_id == session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        slot = (
            await db.execute(
                select(SessionSlot)
                .where(SessionSlot.session_id == session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            registration is None
            or registration.runner_id != config.runner_id
            or registration.status not in {"REGISTERED", "RELEASED"}
            or slot is None
            or slot.runner_id != config.runner_id
        ):
            raise DependencyUnavailableError(
                "Guest session runtime release records are incomplete"
            )
        if session.state == "RESERVED":
            raise ConflictError("Guest session admission is still being resolved")
        if session.state != "ENDING":
            session.state = "ENDING"
            session.version += 1
            session.failure_code = None
    elif session.state not in TERMINAL_SESSION_STATES:
        raise ConflictError("Guest session cannot be ended from its current state")
    else:
        capability.revoked_at = capability.revoked_at or now
    view = _session_state_view(session)
    await store_idempotency_record(
        db,
        user_id=owner_user_id,
        idempotency_key=operation_id,
        action=PUBLIC_SESSION_END_ACTION,
        request_hash=request_hash,
        status_code=202 if view.state == "ENDING" else 200,
        response_json=view.model_dump(mode="json"),
    )
    await db.commit()
    if view.state == "ENDING":
        await _release_execution_slot(
            db,
            config=config,
            session_id=session_id,
            runner_id=config.runner_id,
        )
    return view


async def release_peer(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    runtime_principal: RuntimePrincipal,
    payload: PeerReleaseRequest,
    settings: PlatformSettings,
) -> PeerReleaseView:
    """Release one peer and retain capacity only through the reconnect grace."""
    config = session_runtime_config(settings)
    if runtime_principal.service_id != "voice-runtime" or runtime_principal.runner_id != config.runner_id:
        raise ForbiddenError("Runtime principal does not match configured runner")
    now = _utc_now()
    session = (
        await db.execute(
            select(Session).where(Session.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    peer = (
        await db.execute(
            select(SessionPeer)
            .where(SessionPeer.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        session is None
        or peer is None
        or session.runner_id != runtime_principal.runner_id
        or peer.runner_id != runtime_principal.runner_id
        or peer.peer_id != payload.peer_id
    ):
        raise NotFoundError("Active session peer not found")
    if peer.status == "RELEASED":
        reconnect_until = peer.reconnect_until
        await db.commit()
        if session.state == "ENDING":
            await _release_execution_slot(
                db,
                config=config,
                session_id=session_id,
                runner_id=runtime_principal.runner_id,
            )
        return PeerReleaseView(
            session_id=session_id,
            peer_id=payload.peer_id,
            released=True,
            reconnect_until=reconnect_until,
        )
    if peer.status != "ACTIVE":
        raise ConflictError("Session peer is not active")
    peer.status = "RELEASED"
    peer.released_at = now
    peer.release_reason = payload.reason
    if (
        session.state == "ACTIVE"
        and session.hard_end_deadline is not None
        and session.hard_end_deadline > now
    ):
        reconnect_until = min(
            now + timedelta(seconds=config.reconnect_grace_seconds),
            session.hard_end_deadline,
        )
        if reconnect_until > now:
            peer.reconnect_until = reconnect_until
            session.state = "RECONNECTING"
        else:
            peer.reconnect_until = None
            session.state = "ENDING"
    else:
        peer.reconnect_until = None
        session.state = "ENDING"
    session.version += 1
    reconnect_until = peer.reconnect_until
    await db.commit()
    if session.state == "ENDING":
        await _release_execution_slot(
            db,
            config=config,
            session_id=session_id,
            runner_id=runtime_principal.runner_id,
        )
    return PeerReleaseView(
        session_id=session_id,
        peer_id=payload.peer_id,
        released=True,
        reconnect_until=reconnect_until,
    )


async def transition_runtime_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    runtime_principal: RuntimePrincipal,
    payload: SessionTransitionRequest,
    settings: PlatformSettings,
) -> SessionStateView:
    """Apply runtime lifecycle transitions with version checks and stop confirmation."""
    config = session_runtime_config(settings)
    if runtime_principal.service_id != "voice-runtime" or runtime_principal.runner_id != config.runner_id:
        raise ForbiddenError("Runtime principal does not match configured runner")
    session = (
        await db.execute(
            select(Session).where(Session.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    registration = (
        await db.execute(
            select(RuntimeRegistration)
            .where(RuntimeRegistration.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        session is None
        or registration is None
        or session.runner_id != runtime_principal.runner_id
        or registration.runner_id != runtime_principal.runner_id
        or registration.registration_id != payload.registration_id
        or registration.status not in {"REGISTERED", "RELEASED"}
    ):
        raise NotFoundError("Registered session not found")
    if (
        session.state == payload.target_state
        and session.version == payload.expected_version + 1
    ) or (
        session.state == "ENDING"
        and payload.target_state in {"FAILED", "EXPIRED"}
        and session.version == payload.expected_version + 1
        and session.failure_code == payload.failure_code
    ):
        view = _session_state_view(session)
        await db.commit()
        if view.state == "ENDING":
            await _release_execution_slot(
                db,
                config=config,
                session_id=session_id,
                runner_id=runtime_principal.runner_id,
            )
        return view
    if session.version != payload.expected_version:
        raise ConflictError("Session state version changed")
    if registration.status != "REGISTERED":
        raise ConflictError("Terminal session registration cannot be reopened")
    if (
        payload.target_state in {"FAILED", "EXPIRED"}
        and session.state != "ENDING"
    ):
        if session.state not in LIVE_SESSION_STATES:
            raise ConflictError("Session cannot fail from its current state")
        session.state = "ENDING"
        session.version += 1
        session.failure_code = payload.failure_code
        view = _session_state_view(session)
        await db.commit()
        await _release_execution_slot(
            db,
            config=config,
            session_id=session_id,
            runner_id=runtime_principal.runner_id,
        )
        return view
    if payload.target_state not in ALLOWED_RUNTIME_TRANSITIONS.get(session.state, frozenset()):
        raise ConflictError("Session state transition is not allowed")

    now = _utc_now()
    if payload.target_state == "ACTIVE":
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session_id)
            )
        ).scalar_one_or_none()
        if (
            budget is None
            or budget.status != "OPEN"
            or budget.permit_expiry is None
            or budget.permit_expiry <= now
            or budget.cumulative_authorized_ms <= budget.cumulative_proven_ms
            or session.hard_end_deadline is None
            or session.hard_end_deadline <= now
        ):
            raise ConflictError("Session cannot become active without a live funded permit")

    if payload.target_state in {"COMPLETED", "FAILED", "EXPIRED"}:
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session_id)
            )
        ).scalar_one_or_none()
        slot = (
            await db.execute(
                select(SessionSlot)
                .where(SessionSlot.session_id == session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            budget is None
            or budget.status != "CLOSED"
            or registration.released_at is None
            or slot is None
            or slot.released_at is None
        ):
            raise ConflictError(
                "Terminal transition requires confirmed stop and closed budget"
            )
        if payload.target_state == "COMPLETED" and session.failure_code is not None:
            raise ConflictError("A failed session cannot complete successfully")
        registration.status = "RELEASED"
        session.ended_at = now
        session.failure_code = (
            payload.failure_code
            if payload.target_state in {"FAILED", "EXPIRED"}
            else None
        )
    elif payload.target_state == "ENDING":
        session.failure_code = payload.failure_code

    if payload.target_state in TERMINAL_SESSION_STATES:
        capability = (
            await db.execute(
                select(PublicGuestCapability)
                .where(PublicGuestCapability.session_id == session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if capability is not None:
            capability.revoked_at = capability.revoked_at or now
    session.state = payload.target_state
    session.version += 1
    view = _session_state_view(session)
    await db.commit()
    if view.state == "ENDING":
        await _release_execution_slot(
            db,
            config=config,
            session_id=session_id,
            runner_id=runtime_principal.runner_id,
        )
    return view


__all__ = [
    "consume_join",
    "end_guest_session",
    "end_owner_session",
    "get_owner_session",
    "get_runtime_config",
    "issue_guest_join",
    "issue_join",
    "release_peer",
    "register_runtime",
    "reserve_guest_session",
    "reserve_session",
    "transition_runtime_session",
]


