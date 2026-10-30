"""Owner-scoped conversation history backed by the existing session and event projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import base64
import binascii
import json
from typing import Literal
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.agents.models import Agent, AgentVersion
from platform_api.agents.security import find_secret_paths
from platform_api.auth.errors import ConflictError, NotFoundError, ValidationError
from platform_api.billing.models import CallBudget
from platform_api.billing.pricing import charge_minor
from platform_api.events.repository import (
    HistoryRepository,
    InvalidCursor,
    TranscriptChanged,
    TranscriptPage,
    decode_transcript_cursor,
    encode_transcript_cursor,
)
from platform_api.models.session import Session
from platform_api.models.transcript_policy import TranscriptCorrection
from platform_api.pagination import (
    MAX_CURSOR_LENGTH,
    PaginationError,
    decode_keyset_cursor,
    encode_keyset_cursor,
    normalize_date_range,
)
from platform_api.sessions.transcript_policy import (
    TranscriptPolicyState,
    lock_transcript_policy,
    read_transcript_policy,
)
from platform_api.sessions.history_schemas import (
    ConfigSnapshotView,
    OwnerSessionsPage,
    OwnerUsageItem,
    OwnerUsagePage,
    SessionBillingView,
    SessionHistoryItem,
    TranscriptGapView,
    TranscriptPageView,
    TranscriptSegmentView,
)


SESSION_PAGE_MAX = 100
TRANSCRIPT_PAGE_MAX = 200


@dataclass(frozen=True)
class AdminSessionRecord:
    """Metadata and stored budget facts for an admin-only conversation view."""

    session: Session
    budget: CallBudget | None


@dataclass(frozen=True)
class SessionRecordsPage:
    items: tuple[AdminSessionRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SessionFilters:
    state: str | None
    kind: str | None
    agent_version_id: uuid.UUID | None
    from_at: datetime | None
    to_at: datetime | None

    def as_cursor_filters(self, *, owner_user_id: uuid.UUID | None) -> dict[str, str | None]:
        return {
            "agent_version_id": str(self.agent_version_id) if self.agent_version_id else None,
            "from_at": self.from_at.isoformat() if self.from_at else None,
            "kind": self.kind,
            "owner_user_id": str(owner_user_id) if owner_user_id else None,
            "state": self.state,
            "to_at": self.to_at.isoformat() if self.to_at else None,
        }


def _filters(
    *,
    state: str | None,
    kind: str | None,
    agent_version_id: uuid.UUID | None,
    from_at: datetime | None,
    to_at: datetime | None,
) -> SessionFilters:
    if state is not None and (not state or len(state) > 32):
        raise ValidationError("state filter must be between 1 and 32 characters")
    if kind is not None and kind not in {"voice", "chat", "voice_test"}:
        raise ValidationError("kind filter is invalid")
    try:
        start, end = normalize_date_range(from_at, to_at)
    except PaginationError as exc:
        raise ValidationError(str(exc)) from exc
    return SessionFilters(
        state=state,
        kind=kind,
        agent_version_id=agent_version_id,
        from_at=start,
        to_at=end,
    )


def _session_item(session: Session) -> SessionHistoryItem:
    return SessionHistoryItem(
        session_id=session.id,
        agent_version_id=session.agent_version_id,
        kind=session.kind,
        state=session.state,
        session_version=session.version,
        connect_deadline=session.connect_deadline,
        hard_end_deadline=session.hard_end_deadline,
        created_at=session.created_at,
        ended_at=session.ended_at,
        failure_code=session.failure_code,
    )


def _billing_view(budget: CallBudget | None) -> SessionBillingView | None:
    if budget is None:
        return None
    if budget.status not in {"OPEN", "CLOSED"}:
        raise ConflictError("Stored call budget status is invalid")
    estimate_minor = None
    if budget.status == "OPEN":
        try:
            estimate_minor = charge_minor(
                budget.pinned_rate,
                budget.cumulative_authorized_ms,
            )
        except ValueError as exc:
            raise ConflictError("Stored call budget has an invalid authorization ceiling") from exc
    return SessionBillingView(
        budget_status=budget.status,
        charge_status="final" if budget.status == "CLOSED" else "provisional",
        pinned_rate_minor_per_minute=budget.pinned_rate,
        billed_ms=budget.cumulative_proven_ms,
        authorized_ms=budget.cumulative_authorized_ms,
        authorized_estimate_minor=estimate_minor,
        charged_minor=budget.cumulative_charged_minor,
        reserved_minor=budget.reserved_minor,
        budget_version=budget.version,
        waived_unknown_usage=budget.waived_unknown_usage,
    )


async def _load_budgets(
    db: AsyncSession,
    sessions: list[Session],
) -> dict[uuid.UUID, CallBudget]:
    if not sessions:
        return {}
    owners = {row.id: row.owner_user_id for row in sessions}
    rows = (
        await db.execute(
            select(CallBudget).where(
                CallBudget.session_id.in_(tuple(owners.keys()))
            )
        )
    ).scalars().all()
    budgets: dict[uuid.UUID, CallBudget] = {}
    for budget in rows:
        if budget.owner_user_id != owners.get(budget.session_id):
            raise ConflictError("Session billing owner does not match session owner")
        budgets[budget.session_id] = budget
    return budgets


async def _query_session_page(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID | None,
    scope: str,
    filters: SessionFilters,
    limit: int,
    cursor: str | None,
) -> tuple[list[Session], str | None]:
    if type(limit) is not int or limit < 1 or limit > SESSION_PAGE_MAX:
        raise ValidationError(f"limit must be between 1 and {SESSION_PAGE_MAX}")
    cursor_filters = filters.as_cursor_filters(owner_user_id=owner_user_id)
    stmt = select(Session)
    if owner_user_id is not None:
        stmt = stmt.where(Session.owner_user_id == owner_user_id)
    if filters.state is not None:
        stmt = stmt.where(Session.state == filters.state)
    if filters.kind is not None:
        stmt = stmt.where(Session.kind == filters.kind)
    if filters.agent_version_id is not None:
        stmt = stmt.where(Session.agent_version_id == filters.agent_version_id)
    if filters.from_at is not None and filters.to_at is not None:
        stmt = stmt.where(
            Session.created_at >= filters.from_at,
            Session.created_at < filters.to_at,
        )
    if cursor is not None:
        try:
            position = decode_keyset_cursor(
                cursor,
                scope=scope,
                filters=cursor_filters,
            )
        except PaginationError as exc:
            raise ValidationError("Session cursor is invalid for this query") from exc
        stmt = stmt.where(
            (Session.created_at < position.created_at)
            | (
                (Session.created_at == position.created_at)
                & (Session.id < position.record_id)
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(Session.created_at.desc(), Session.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars().all()
    )
    next_cursor = None
    if len(rows) > limit:
        last_item = rows[limit - 1]
        next_cursor = encode_keyset_cursor(
            scope=scope,
            filters=cursor_filters,
            created_at=last_item.created_at,
            record_id=last_item.id,
        )
        rows = rows[:limit]
    return rows, next_cursor


async def list_owner_sessions(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    state: str | None = None,
    kind: Literal["voice", "chat", "voice_test"] | None = None,
    agent_version_id: uuid.UUID | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> OwnerSessionsPage:
    filters = _filters(
        state=state,
        kind=kind,
        agent_version_id=agent_version_id,
        from_at=from_at,
        to_at=to_at,
    )
    rows, next_cursor = await _query_session_page(
        db,
        owner_user_id=owner_user_id,
        scope=f"owner.sessions:{owner_user_id}",
        filters=filters,
        limit=limit,
        cursor=cursor,
    )
    return OwnerSessionsPage(
        items=[_session_item(row) for row in rows],
        next_cursor=next_cursor,
    )


async def list_owner_usage(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    state: str | None = None,
    kind: Literal["voice", "chat", "voice_test"] | None = None,
    agent_version_id: uuid.UUID | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> OwnerUsagePage:
    filters = _filters(
        state=state,
        kind=kind,
        agent_version_id=agent_version_id,
        from_at=from_at,
        to_at=to_at,
    )
    rows, next_cursor = await _query_session_page(
        db,
        owner_user_id=owner_user_id,
        scope=f"owner.usage:{owner_user_id}",
        filters=filters,
        limit=limit,
        cursor=cursor,
    )
    budgets = await _load_budgets(db, rows)
    return OwnerUsagePage(
        items=[
            OwnerUsageItem(
                session=_session_item(row),
                billing=_billing_view(budgets.get(row.id)),
            )
            for row in rows
        ],
        next_cursor=next_cursor,
    )


async def list_admin_session_records(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID | None = None,
    state: str | None = None,
    kind: Literal["voice", "chat", "voice_test"] | None = None,
    agent_version_id: uuid.UUID | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> SessionRecordsPage:
    filters = _filters(
        state=state,
        kind=kind,
        agent_version_id=agent_version_id,
        from_at=from_at,
        to_at=to_at,
    )
    rows, next_cursor = await _query_session_page(
        db,
        owner_user_id=owner_user_id,
        scope="admin.sessions",
        filters=filters,
        limit=limit,
        cursor=cursor,
    )
    budgets = await _load_budgets(db, rows)
    return SessionRecordsPage(
        items=tuple(
            AdminSessionRecord(session=row, budget=budgets.get(row.id))
            for row in rows
        ),
        next_cursor=next_cursor,
    )


async def _get_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID | None,
) -> Session:
    stmt = select(Session).where(Session.id == session_id)
    if owner_user_id is not None:
        stmt = stmt.where(Session.owner_user_id == owner_user_id)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        raise NotFoundError("Session not found")
    return session


async def get_admin_session_record(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
) -> AdminSessionRecord:
    session = await _get_session(db, session_id=session_id, owner_user_id=None)
    budgets = await _load_budgets(db, [session])
    return AdminSessionRecord(session=session, budget=budgets.get(session.id))


async def get_owner_config_snapshot(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    session_id: uuid.UUID,
) -> ConfigSnapshotView:
    session = await _get_session(
        db,
        session_id=session_id,
        owner_user_id=owner_user_id,
    )
    if session.agent_version_id is None:
        raise NotFoundError("Pinned configuration snapshot not found")
    result = await db.execute(
        select(AgentVersion, Agent)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .where(
            AgentVersion.id == session.agent_version_id,
            Agent.owner_user_id == owner_user_id,
        )
    )
    pinned = result.one_or_none()
    if pinned is None:
        raise NotFoundError("Pinned configuration snapshot not found")
    version, agent = pinned
    if find_secret_paths(version.immutable_config_json):
        raise ConflictError("Pinned configuration contains protected fields")
    if find_secret_paths(session.tariff_snapshot):
        raise ConflictError("Pinned tariff snapshot contains protected fields")
    return ConfigSnapshotView(
        session_id=session.id,
        agent_id=agent.id,
        agent_name=agent.name,
        agent_version_id=version.id,
        agent_version=version.version,
        flow_hash=version.flow_hash,
        artifact_hash=version.artifact_hash,
        authoring_hash=version.authoring_hash,
        immutable_config=version.immutable_config_json,
        tariff_snapshot=session.tariff_snapshot,
        published_at=version.created_at,
    )

async def get_admin_config_snapshot(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
) -> ConfigSnapshotView | None:
    """Return a sanitized exact version pinned to a session for operator inspection."""
    session = await _get_session(db, session_id=session_id, owner_user_id=None)
    if session.agent_version_id is None:
        return None
    pinned = (
        await db.execute(
            select(AgentVersion, Agent)
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(AgentVersion.id == session.agent_version_id)
        )
    ).one_or_none()
    if pinned is None:
        raise ConflictError("Pinned agent version is missing")
    version, agent = pinned
    if find_secret_paths(version.immutable_config_json):
        raise ConflictError("Pinned configuration contains protected fields")
    if find_secret_paths(session.tariff_snapshot):
        raise ConflictError("Pinned tariff snapshot contains protected fields")
    return ConfigSnapshotView(
        session_id=session.id,
        agent_id=agent.id,
        agent_name=agent.name,
        agent_version_id=version.id,
        agent_version=version.version,
        flow_hash=version.flow_hash,
        artifact_hash=version.artifact_hash,
        authoring_hash=version.authoring_hash,
        immutable_config=version.immutable_config_json,
        tariff_snapshot=session.tariff_snapshot,
        published_at=version.created_at,
    )


async def _transcript_page(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    cursor: str | None,
    limit: int,
) -> TranscriptPage:
    if type(limit) is not int or limit < 1 or limit > TRANSCRIPT_PAGE_MAX:
        raise ValidationError(f"limit must be between 1 and {TRANSCRIPT_PAGE_MAX}")
    try:
        return await HistoryRepository().transcript_page(
            db,
            session_id,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursor as exc:
        raise ValidationError("Transcript cursor is invalid") from exc
    except TranscriptChanged as exc:
        raise ConflictError("Transcript changed; reload the first page") from exc
    except ValueError as exc:
        raise ValidationError("Transcript page request is invalid") from exc


def encode_transcript_api_cursor(
    *,
    transcript_revision: int,
    policy_revision: int,
    after_sequence: int,
) -> str:
    """Bind a public transcript cursor to both source and admin-policy revisions."""
    raw_cursor = encode_transcript_cursor(
        revision=transcript_revision,
        after_sequence=after_sequence,
    )
    payload = {
        "event_cursor": raw_cursor,
        "policy_revision": policy_revision,
        "transcript_revision": transcript_revision,
        "v": 1,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_transcript_api_cursor(
    cursor: str | None,
    *,
    policy_revision: int,
) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise ValidationError("Transcript cursor is invalid")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("Transcript cursor is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "event_cursor",
        "policy_revision",
        "transcript_revision",
        "v",
    }:
        raise ValidationError("Transcript cursor is invalid")
    if (
        type(payload["v"]) is not int
        or payload["v"] != 1
        or type(payload["policy_revision"]) is not int
        or type(payload["transcript_revision"]) is not int
        or not isinstance(payload["event_cursor"], str)
    ):
        raise ValidationError("Transcript cursor is invalid")
    if payload["policy_revision"] != policy_revision:
        raise ConflictError("Transcript policy changed; reload the first page")
    try:
        event_cursor = decode_transcript_cursor(payload["event_cursor"])
    except InvalidCursor as exc:
        raise ValidationError("Transcript cursor is invalid") from exc
    if event_cursor.revision != payload["transcript_revision"]:
        raise ValidationError("Transcript cursor is invalid")
    return payload["event_cursor"]


async def _transcript_view(
    db: AsyncSession,
    page: TranscriptPage,
    policy: TranscriptPolicyState,
) -> TranscriptPageView:
    corrections: dict[uuid.UUID, str] = {}
    event_ids = tuple(segment.event_id for segment in page.segments)
    if event_ids:
        rows = (
            await db.execute(
                select(TranscriptCorrection)
                .where(
                    TranscriptCorrection.session_id == page.session_id,
                    TranscriptCorrection.event_id.in_(event_ids),
                    TranscriptCorrection.policy_revision <= policy.revision,
                )
                .order_by(TranscriptCorrection.policy_revision.desc())
            )
        ).scalars().all()
        for correction in rows:
            corrections.setdefault(correction.event_id, correction.replacement_text)
    next_cursor = None
    if page.next_cursor is not None:
        raw_position = decode_transcript_cursor(page.next_cursor)
        next_cursor = encode_transcript_api_cursor(
            transcript_revision=page.transcript_revision,
            policy_revision=policy.revision,
            after_sequence=raw_position.after_sequence,
        )
    return TranscriptPageView(
        session_id=page.session_id,
        transcript_revision=page.transcript_revision,
        policy_revision=policy.revision,
        visible_to_owner=policy.visible_to_owner,
        segments=[
            TranscriptSegmentView(
                sequence=segment.sequence,
                event_id=segment.event_id,
                speaker=segment.speaker,
                final_text=corrections.get(segment.event_id, segment.final_text),
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                kind=segment.kind,
                corrected=segment.event_id in corrections,
            )
            for segment in page.segments
        ],
        has_gaps=page.has_gaps,
        gaps=[
            TranscriptGapView(
                after_sequence=gap.after_sequence,
                before_sequence=gap.before_sequence,
                reason=gap.reason,
            )
            for gap in page.gaps
        ],
        processing=page.processing,
        next_cursor=next_cursor,
    )


async def get_owner_transcript_page(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    session_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> TranscriptPageView:
    await _get_session(
        db,
        session_id=session_id,
        owner_user_id=owner_user_id,
    )
    await lock_transcript_policy(db, session_id, shared=True)
    policy = await read_transcript_policy(db, session_id)
    raw_cursor = _decode_transcript_api_cursor(
        cursor,
        policy_revision=policy.revision,
    )
    page = await _transcript_page(
        db,
        session_id=session_id,
        cursor=raw_cursor,
        limit=limit,
    )
    if not policy.visible_to_owner:
        return TranscriptPageView(
            session_id=page.session_id,
            transcript_revision=page.transcript_revision,
            policy_revision=policy.revision,
            visible_to_owner=False,
            segments=[],
            has_gaps=False,
            gaps=[],
            processing=page.processing,
            next_cursor=None,
        )
    return await _transcript_view(db, page, policy)


async def get_admin_transcript_page(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> TranscriptPageView:
    await _get_session(db, session_id=session_id, owner_user_id=None)
    await lock_transcript_policy(db, session_id, shared=True)
    policy = await read_transcript_policy(db, session_id)
    raw_cursor = _decode_transcript_api_cursor(
        cursor,
        policy_revision=policy.revision,
    )
    page = await _transcript_page(
        db,
        session_id=session_id,
        cursor=raw_cursor,
        limit=limit,
    )
    return await _transcript_view(db, page, policy)

def billing_view_for_admin(budget: CallBudget | None) -> SessionBillingView | None:
    return _billing_view(budget)
