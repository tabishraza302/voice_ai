"""Transactional admin mutations for admission, tariffs, and transcript policy."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.admin.schemas import (
    AdminAdmissionPauseItem,
    AdminAdmissionPausePage,
    AdminAdmissionPauseRequest,
    AdminTariffPage,
    AdminTariffVersionItem,
    AdminTariffVersionRequest,
    AdminTranscriptCorrectionItem,
    AdminTranscriptCorrectionRequest,
    AdminTranscriptPolicyView,
    AdminTranscriptVisibilityRequest,
)
from platform_api.audit import record_audit
from platform_api.auth.errors import (
    ConflictError,
    NotFoundError,
    RevisionConflictError,
    ValidationError,
)
from platform_api.billing.tariffs import (
    append_tariff_version,
    lock_active_tariff,
    resolve_active_tariff,
)
from platform_api.models.admission_pause import AdmissionPause
from platform_api.models.auth import User
from platform_api.models.tariff import SessionTariffRecord
from platform_api.models.transcript_policy import TranscriptCorrection, TranscriptPolicy
from platform_api.pagination import (
    PaginationError,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from platform_api.sessions.admission_policy import lock_admission_pause_scope
from platform_api.sessions.transcript_policy import (
    lock_transcript_policy,
)
from platform_api.agents.models import Agent
from platform_api.events.models import TranscriptSegment
from platform_api.models.session import Session


_ADMIN_PAGE_MAX = 100
_MAX_BIGINT = (1 << 63) - 1


def _pause_item(row: AdmissionPause) -> AdminAdmissionPauseItem:
    return AdminAdmissionPauseItem(
        id=row.id,
        scope=row.scope,
        owner_user_id=row.owner_user_id,
        agent_id=row.agent_id,
        paused=row.paused,
        revision=row.revision,
        reason=row.reason,
        updated_by_user_id=row.updated_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_admin_admission_pauses(
    db: AsyncSession,
    *,
    scope: Literal["global", "user", "agent"] | None = None,
    owner_user_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> AdminAdmissionPausePage:
    if type(limit) is not int or not 1 <= limit <= _ADMIN_PAGE_MAX:
        raise ValidationError(f"limit must be between 1 and {_ADMIN_PAGE_MAX}")
    filters = {
        "agent_id": str(agent_id) if agent_id else None,
        "owner_user_id": str(owner_user_id) if owner_user_id else None,
        "scope": scope,
    }
    position = None
    if cursor is not None:
        try:
            position = decode_keyset_cursor(
                cursor,
                scope="admin.admission-pauses",
                filters=filters,
            )
        except PaginationError as exc:
            raise ValidationError("Admission-pause cursor is invalid for this query") from exc
    stmt = select(AdmissionPause)
    if scope is not None:
        stmt = stmt.where(AdmissionPause.scope == scope)
    if owner_user_id is not None:
        stmt = stmt.where(AdmissionPause.owner_user_id == owner_user_id)
    if agent_id is not None:
        stmt = stmt.where(AdmissionPause.agent_id == agent_id)
    if position is not None:
        stmt = stmt.where(
            (AdmissionPause.updated_at < position.created_at)
            | (
                (AdmissionPause.updated_at == position.created_at)
                & (AdmissionPause.id < position.record_id)
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(AdmissionPause.updated_at.desc(), AdmissionPause.id.desc())
                .limit(limit + 1)
            )
        ).scalars().all()
    )
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_keyset_cursor(
            scope="admin.admission-pauses",
            filters=filters,
            created_at=last.updated_at,
            record_id=last.id,
        )
        rows = rows[:limit]
    return AdminAdmissionPausePage(
        items=[_pause_item(row) for row in rows],
        next_cursor=next_cursor,
    )


async def set_admin_admission_pause(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    auth_session_id: uuid.UUID,
    payload: AdminAdmissionPauseRequest,
    request_id: uuid.UUID,
    source_ip: str | None,
) -> AdminAdmissionPauseItem:
    if payload.scope == "global":
        scope_id = None
        owner_user_id = None
        agent_id = None
    elif payload.scope == "user":
        scope_id = payload.owner_user_id
        owner_user_id = payload.owner_user_id
        agent_id = None
    else:
        scope_id = payload.agent_id
        owner_user_id = None
        agent_id = payload.agent_id
    await lock_admission_pause_scope(
        db,
        scope=payload.scope,
        scope_id=scope_id,
    )

    if payload.scope == "user":
        if await db.get(User, owner_user_id) is None:
            raise NotFoundError("User not found")
    elif payload.scope == "agent":
        agent = await db.get(Agent, agent_id)
        if agent is None:
            raise NotFoundError("Agent not found")
        owner_user_id = agent.owner_user_id

    statement = select(AdmissionPause).where(AdmissionPause.scope == payload.scope)
    if payload.scope == "user":
        statement = statement.where(AdmissionPause.owner_user_id == owner_user_id)
    elif payload.scope == "agent":
        statement = statement.where(AdmissionPause.agent_id == agent_id)
    row = (await db.execute(statement.with_for_update())).scalar_one_or_none()
    current_revision = row.revision if row is not None else 0
    if current_revision != payload.expected_revision:
        raise RevisionConflictError("Admission-pause policy changed; reload before applying a change")
    if current_revision >= _MAX_BIGINT:
        raise ValidationError("Admission-pause revision limit has been reached")

    now = datetime.now(timezone.utc)
    before = {
        "paused": row.paused if row is not None else False,
        "revision": current_revision,
    }
    if row is None:
        row = AdmissionPause(
            id=uuid.uuid4(),
            scope=payload.scope,
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            paused=payload.paused,
            revision=1,
            reason=payload.reason,
            updated_by_user_id=actor_id,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.paused = payload.paused
        row.revision = current_revision + 1
        row.reason = payload.reason
        row.updated_by_user_id = actor_id
        row.updated_at = now
    await db.flush()
    await record_audit(
        db,
        actor_kind="admin",
        actor_id=actor_id,
        session_id=auth_session_id,
        action="admin.admission.pause.set",
        target_type="admission_pause",
        target_id=row.id,
        reason=payload.reason,
        request_id=request_id,
        source_ip=source_ip,
        result="success",
        change_summary={
            "scope": row.scope,
            "owner_user_id": str(row.owner_user_id) if row.owner_user_id else None,
            "agent_id": str(row.agent_id) if row.agent_id else None,
            "before": before,
            "after": {"paused": row.paused, "revision": row.revision},
        },
        created_at=now,
    )
    return _pause_item(row)


def _tariff_item(record: SessionTariffRecord, *, current_id: uuid.UUID) -> AdminTariffVersionItem:
    return AdminTariffVersionItem(
        id=record.id,
        revision=record.revision,
        tariff_version=f"v{record.revision}",
        rate_minor_per_minute=record.rate_minor_per_minute,
        billing_slice_ms=record.billing_slice_ms,
        permit_ttl_ms=record.permit_ttl_ms,
        is_current=record.id == current_id,
        created_at=record.created_at,
    )


async def list_admin_tariffs(
    db: AsyncSession,
    *,
    cursor: str | None = None,
    limit: int = 50,
) -> AdminTariffPage:
    if type(limit) is not int or not 1 <= limit <= _ADMIN_PAGE_MAX:
        raise ValidationError(f"limit must be between 1 and {_ADMIN_PAGE_MAX}")
    active = await resolve_active_tariff(db)
    current_record = await db.get(SessionTariffRecord, active.tariff_id)
    if current_record is None:
        raise ConflictError("Active tariff record is missing")
    filters: dict[str, Any] = {}
    position = None
    if cursor is not None:
        try:
            position = decode_keyset_cursor(
                cursor,
                scope="admin.tariffs",
                filters=filters,
            )
        except PaginationError as exc:
            raise ValidationError("Tariff cursor is invalid") from exc
    stmt = select(SessionTariffRecord)
    if position is not None:
        stmt = stmt.where(
            (SessionTariffRecord.created_at < position.created_at)
            | (
                (SessionTariffRecord.created_at == position.created_at)
                & (SessionTariffRecord.id < position.record_id)
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(SessionTariffRecord.created_at.desc(), SessionTariffRecord.id.desc())
                .limit(limit + 1)
            )
        ).scalars().all()
    )
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_keyset_cursor(
            scope="admin.tariffs",
            filters=filters,
            created_at=last.created_at,
            record_id=last.id,
        )
        rows = rows[:limit]
    return AdminTariffPage(
        current=_tariff_item(current_record, current_id=active.tariff_id),
        items=[_tariff_item(row, current_id=active.tariff_id) for row in rows],
        next_cursor=next_cursor,
    )


async def create_admin_tariff_version(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    auth_session_id: uuid.UUID,
    payload: AdminTariffVersionRequest,
    request_id: uuid.UUID,
    source_ip: str | None,
) -> AdminTariffVersionItem:
    _, previous = await lock_active_tariff(db)
    if previous.revision != payload.expected_revision:
        raise RevisionConflictError("The active tariff changed; reload before applying a new rate")
    record = await append_tariff_version(
        db,
        expected_revision=payload.expected_revision,
        rate_minor_per_minute=payload.rate_minor_per_minute,
    )
    previous_summary = {
        "revision": previous.revision,
        "rate_minor_per_minute": previous.rate_minor_per_minute,
    }
    current_summary = {
        "revision": record.revision,
        "rate_minor_per_minute": record.rate_minor_per_minute,
        "billing_slice_ms": record.billing_slice_ms,
        "permit_ttl_ms": record.permit_ttl_ms,
    }
    await record_audit(
        db,
        actor_kind="admin",
        actor_id=actor_id,
        session_id=auth_session_id,
        action="admin.tariff.version.create",
        target_type="session_tariff_version",
        target_id=record.id,
        reason=payload.reason,
        request_id=request_id,
        source_ip=source_ip,
        result="success",
        change_summary={"previous": previous_summary, "current": current_summary},
        created_at=record.created_at,
    )
    return _tariff_item(record, current_id=record.id)


async def _lock_session_policy(
    db: AsyncSession,
    session_id: uuid.UUID,
) -> tuple[Session, TranscriptPolicy | None]:
    await lock_transcript_policy(db, session_id, shared=False)
    session = await db.get(Session, session_id)
    if session is None:
        raise NotFoundError("Session not found")
    policy = await db.get(TranscriptPolicy, session_id)
    return session, policy


def _policy_view(session_id: uuid.UUID, policy: TranscriptPolicy | None) -> AdminTranscriptPolicyView:
    return AdminTranscriptPolicyView(
        session_id=session_id,
        visible_to_owner=policy.visible_to_owner if policy is not None else True,
        policy_revision=policy.revision if policy is not None else 0,
        updated_at=policy.updated_at if policy is not None else None,
    )


async def set_admin_transcript_visibility(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
    auth_session_id: uuid.UUID,
    payload: AdminTranscriptVisibilityRequest,
    request_id: uuid.UUID,
    source_ip: str | None,
) -> AdminTranscriptPolicyView:
    _, policy = await _lock_session_policy(db, session_id)
    current_revision = policy.revision if policy is not None else 0
    if current_revision != payload.expected_revision:
        raise RevisionConflictError("Transcript policy changed; reload before applying a change")
    if current_revision >= _MAX_BIGINT:
        raise ValidationError("Transcript policy revision limit has been reached")
    before_visible = policy.visible_to_owner if policy is not None else True
    now = datetime.now(timezone.utc)
    if policy is None:
        policy = TranscriptPolicy(
            session_id=session_id,
            visible_to_owner=payload.visible_to_owner,
            revision=1,
            updated_by_user_id=actor_id,
            updated_at=now,
        )
        db.add(policy)
    else:
        policy.visible_to_owner = payload.visible_to_owner
        policy.revision = current_revision + 1
        policy.updated_by_user_id = actor_id
        policy.updated_at = now
    await db.flush()
    await record_audit(
        db,
        actor_kind="admin",
        actor_id=actor_id,
        session_id=auth_session_id,
        action="admin.transcript.visibility.set",
        target_type="session",
        target_id=session_id,
        reason=payload.reason,
        request_id=request_id,
        source_ip=source_ip,
        result="success",
        change_summary={
            "policy_revision_before": current_revision,
            "policy_revision_after": policy.revision,
            "visible_to_owner_before": before_visible,
            "visible_to_owner_after": policy.visible_to_owner,
        },
        created_at=now,
    )
    return _policy_view(session_id, policy)


async def create_admin_transcript_correction(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
    auth_session_id: uuid.UUID,
    payload: AdminTranscriptCorrectionRequest,
    request_id: uuid.UUID,
    source_ip: str | None,
) -> AdminTranscriptCorrectionItem:
    _, policy = await _lock_session_policy(db, session_id)
    current_revision = policy.revision if policy is not None else 0
    if current_revision != payload.expected_revision:
        raise RevisionConflictError("Transcript policy changed; reload before correcting text")
    if current_revision >= _MAX_BIGINT:
        raise ValidationError("Transcript policy revision limit has been reached")
    segment = (
        await db.execute(
            select(TranscriptSegment).where(
                TranscriptSegment.event_id == payload.event_id,
                TranscriptSegment.session_id == session_id,
            )
        )
    ).scalar_one_or_none()
    if segment is None:
        raise NotFoundError("Transcript segment not found")

    now = datetime.now(timezone.utc)
    next_revision = current_revision + 1
    correction = TranscriptCorrection(
        id=uuid.uuid4(),
        session_id=session_id,
        event_id=segment.event_id,
        policy_revision=next_revision,
        replacement_text=payload.replacement_text,
        created_by_user_id=actor_id,
        created_at=now,
    )
    if policy is None:
        policy = TranscriptPolicy(
            session_id=session_id,
            visible_to_owner=True,
            revision=next_revision,
            updated_by_user_id=actor_id,
            updated_at=now,
        )
        db.add(policy)
    else:
        policy.revision = next_revision
        policy.updated_by_user_id = actor_id
        policy.updated_at = now
    db.add(correction)
    await db.flush()
    await record_audit(
        db,
        actor_kind="admin",
        actor_id=actor_id,
        session_id=auth_session_id,
        action="admin.transcript.correction.create",
        target_type="transcript_segment",
        target_id=segment.event_id,
        reason=payload.reason,
        request_id=request_id,
        source_ip=source_ip,
        result="success",
        change_summary={
            "session_id": str(session_id),
            "event_id": str(segment.event_id),
            "policy_revision_before": current_revision,
            "policy_revision_after": next_revision,
            "original_text_length": len(segment.final_text),
            "replacement_text_length": len(payload.replacement_text),
            "original_text_sha256": hashlib.sha256(segment.final_text.encode("utf-8")).hexdigest(),
            "replacement_text_sha256": hashlib.sha256(payload.replacement_text.encode("utf-8")).hexdigest(),
        },
        created_at=now,
    )
    return AdminTranscriptCorrectionItem(
        id=correction.id,
        session_id=session_id,
        event_id=segment.event_id,
        policy_revision=next_revision,
        created_at=now,
    )
