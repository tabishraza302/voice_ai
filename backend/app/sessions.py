import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import time
import uuid
from typing import Annotated, Any, Literal, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from .agents import Agent
from .auth import csrf, current_user
from .coordination import CallClaim, Coordination, LeaseObservation
from .db import Base, Session as DbSession, get_db
from .models import User
from .observability import TraceUnavailableError, fetch_session_trace

CallType = Literal["test", "public", "chat_test"]
SessionStatus = Literal["created", "active", "completed", "failed", "cancelled", "expired"]

logger = logging.getLogger(__name__)

_COORDINATION_ADVISORY_LOCK_ID = 42000001


async def acquire_coordination_lock(
    db: AsyncSession,
    *,
    lock_timeout_ms: int = 3000,
    statement_timeout_ms: int = 5000,
) -> None:
    await db.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout_ms}ms'"))
    await db.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout_ms}ms'"))
    await db.execute(text(f"SELECT pg_advisory_xact_lock({_COORDINATION_ADVISORY_LOCK_ID})"))


_acquire_coordination_lock = acquire_coordination_lock
SIGNALING_TOKEN_TTL_SECONDS = 120
_SESSION_WRITE_QUEUE_SIZE = 256
_ACTIVE_STATUSES = frozenset({"created", "active"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})
_TRANSITIONS = {
    "created": frozenset({"active", "failed", "cancelled", "expired"}),
    "active": frozenset({"completed", "failed", "cancelled", "expired"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}


class FrozenCallSnapshot(TypedDict):
    global_prompt: dict[str, Any]
    policy: dict[str, int]
    language: str
    agent: dict[str, Any]

class SignalingContext(TypedDict, total=False):
    snapshot: FrozenCallSnapshot
    agent_status: str
    session_status: SessionStatus
    call_type: CallType
    worker_id: str | None
    claim_generation: str | None
    lease_expires_at: datetime | None


class CallSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_agent_status", "agent_id", "status"),
        Index("ix_sessions_owner_created", "owner_id", "created_at"),
        Index(
            "ix_sessions_coordination_recovery",
            "status",
            "lease_expires_at",
            "id",
            postgresql_where=text("call_type IN ('test', 'public')"),
        ),
        CheckConstraint("call_type IN ('test', 'public', 'chat_test')", name="sessions_call_type_check"),
        CheckConstraint(
            "status IN ('created', 'active', 'completed', 'failed', 'cancelled', 'expired')",
            name="sessions_status_check",
        ),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="sessions_snapshot_object_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    call_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'created'"))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signaling_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_generation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CallMessage(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="messages_session_sequence_key"),
        CheckConstraint("sequence > 0", name="messages_sequence_positive_check"),
        CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())



class SessionCreated(BaseModel):
    session_id: uuid.UUID
    signaling_token: str


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str | None = None

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("language must not be empty")
        return normalized


class PublicAgentRead(BaseModel):
    name: str
    slug: str
    description: str | None
    languages: list[str]
    default_language: str | None


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    call_type: CallType
    status: SessionStatus
    outcome: list[dict[str, Any]] | None = None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime



class TraceSpanRead(BaseModel):
    name: str
    started_at: str | None
    ended_at: str | None
    duration_ms: int | None
    status: str
    error_type: str | None


class SessionTraceRead(BaseModel):
    session_id: uuid.UUID
    spans: list[TraceSpanRead]


router = APIRouter(tags=["sessions"])
public_router = APIRouter(prefix="/public", tags=["public"])
Db = Annotated[AsyncSession, Depends(get_db)]
Reader = Annotated[User, Depends(current_user)]
Writer = Annotated[User, Depends(csrf)]
_call_creation_lock = asyncio.Lock()


class SessionUnavailableError(RuntimeError):
    pass


class SessionWriteError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _MessageWrite:
    role: str
    content: str



_SessionWrite = _MessageWrite
_STOP = object()


def _agent_snapshot(agent: Agent) -> dict[str, Any]:
    return {
        "id": str(agent.id),
        "name": agent.name,
        "slug": agent.slug,
        "description": agent.description,
        "instructions": agent.instructions,
        "languages": agent.languages,
        "default_language": agent.default_language,
        "stt_provider": agent.stt_provider,
        "stt_model": agent.stt_model,
        "azure_stt_english_locale": agent.azure_stt_english_locale,
        "llm_provider": agent.llm_provider,
        "llm_model": agent.llm_model,
        "tts_provider": agent.tts_provider,
        "tts_model": agent.tts_model,
        "voice_id": agent.voice_id,
        "status": agent.status,
    }


async def _frozen_call_snapshot(
    db: AsyncSession, agent: Agent, language: str
) -> FrozenCallSnapshot:
    from .admin import get_frozen_settings

    settings = await get_frozen_settings(db)
    return {
        "global_prompt": settings["global_prompt"],
        "policy": settings["policy"],
        "language": language,
        "agent": _agent_snapshot(agent),
    }


def _open_call_predicate(now: datetime):
    return or_(
        CallSession.status == "active",
        and_(
            CallSession.status == "created",
            or_(
                CallSession.worker_id.is_not(None),
                CallSession.signaling_expires_at.is_(None),
                CallSession.signaling_expires_at > now,
            ),
        ),
    )


async def _open_call_count(db: AsyncSession, now: datetime, *, agent_id: uuid.UUID | None = None) -> int:
    statement = select(func.count(CallSession.id)).where(_open_call_predicate(now))
    if agent_id is not None:
        statement = statement.where(CallSession.agent_id == agent_id)
    return int(await db.scalar(statement) or 0)


async def _locked_agent(
    db: AsyncSession, *, agent_id: uuid.UUID | None = None, owner_id: uuid.UUID | None = None, slug: str | None = None
) -> Agent:
    statement = select(Agent).with_for_update()
    if slug is not None:
        statement = statement.where(Agent.slug == slug)
    else:
        statement = statement.where(Agent.id == agent_id, Agent.owner_id == owner_id)
    agent = await db.scalar(statement)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    return agent


async def _create_call(
    db: AsyncSession,
    *,
    call_type: CallType,
    client_ip: str | None,
    owner_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    slug: str | None = None,
    language: str | None = None,
    coordination: Coordination | None = None,
) -> SessionCreated:
    # Acquire global admission advisory lock across all workers with bounded wait
    await _acquire_coordination_lock(db)
    agent = await _locked_agent(db, agent_id=agent_id, owner_id=owner_id, slug=slug)
    if call_type == "public" and agent.status != "published":
        raise HTTPException(404, "Agent not found")
    if call_type == "test" and agent.status == "suspended":
        raise HTTPException(409, "Suspended agents cannot start calls")

    selected_language = language or agent.default_language
    if selected_language is None and agent.languages:
        selected_language = agent.languages[0]
    if selected_language not in agent.languages:
        raise HTTPException(422, {"language": ["Choose an enabled call language"]})

    # Charge rate limits: IP first, then agent
    if coordination is None:
        raise HTTPException(status_code=503, detail="Coordination service unavailable.")

    await coordination.check_ip(client_ip)
    await coordination.check_agent(agent.id)

    snapshot = await _frozen_call_snapshot(db, agent, selected_language)
    db_now = await db.scalar(text("SELECT clock_timestamp()"))
    policy = snapshot["policy"]
    per_agent_limit = policy["max_concurrent_calls_per_agent"]
    overall_limit = policy["max_concurrent_calls_overall"]
    per_agent_count = await _open_call_count(db, db_now, agent_id=agent.id)
    overall_count = await _open_call_count(db, db_now)
    if per_agent_count >= per_agent_limit or overall_count >= overall_limit:
        raise HTTPException(429, "Concurrent call limit reached")

    session_id = uuid.uuid4()
    obs = await coordination.reserve_created(
        session_id=session_id,
        agent_id=agent.id,
        call_type=call_type,  # type: ignore[arg-type]
        per_agent_limit=per_agent_limit,
        overall_limit=overall_limit,
    )
    claim = obs.claim

    t_before_db = time.monotonic()
    db_now = await db.scalar(text("SELECT clock_timestamp()"))
    rem = max(0.0, obs.remaining_seconds - (time.monotonic() - t_before_db))
    db_signaling_deadline = db_now + timedelta(seconds=rem)

    call = CallSession(
        id=session_id,
        owner_id=agent.owner_id,
        agent_id=agent.id,
        call_type=call_type,
        status="created",
        snapshot=snapshot,
        signaling_expires_at=db_signaling_deadline,
        worker_id=None,
        claim_generation=claim.generation,
        lease_expires_at=None,
    )
    db.add(call)

    try:
        await db.commit()
    except Exception as commit_exc:
        try:
            await db.rollback()
        except Exception:
            try:
                await db.close()
            except Exception:
                pass
        try:
            async with DbSession() as verify_db:
                await _acquire_coordination_lock(verify_db)
                committed = await verify_db.scalar(
                    select(CallSession.id).where(CallSession.id == session_id)
                )
                if committed is None:
                    await coordination.release(claim, None)
                else:
                    logger.warning("Session %s committed despite commit exception; retaining for recovery", session_id)
        except Exception as verify_exc:
            logger.warning("Ambiguous outcome verification failed for session %s: %s; retaining for recovery", session_id, verify_exc)
        raise

    token_remaining = min(obs.remaining_seconds, max(0.0, (t_before_db + rem) - time.monotonic()))
    if token_remaining <= 0:
        async with DbSession() as rollback_db:
            await _acquire_coordination_lock(rollback_db)
            db_now = await rollback_db.scalar(text("SELECT clock_timestamp()"))
            await rollback_db.execute(
                update(CallSession)
                .where(
                    CallSession.id == session_id,
                    CallSession.claim_generation == claim.generation,
                    CallSession.status == "created",
                )
                .values(
                    status="failed",
                    ended_at=db_now,
                    updated_at=db_now,
                )
            )
            await rollback_db.commit()
            try:
                await coordination.release(claim, None)
            except Exception:
                pass
        raise HTTPException(status_code=503, detail="Could not issue single-use signaling grant.")

    try:
        signaling_token = await coordination.issue_token(
            claim, max_remaining_seconds=token_remaining
        )
    except Exception:
        async with DbSession() as rollback_db:
            await _acquire_coordination_lock(rollback_db)
            db_now = await rollback_db.scalar(text("SELECT clock_timestamp()"))
            await rollback_db.execute(
                update(CallSession)
                .where(
                    CallSession.id == session_id,
                    CallSession.claim_generation == claim.generation,
                    CallSession.status == "created",
                )
                .values(
                    status="failed",
                    ended_at=db_now,
                    updated_at=db_now,
                )
            )
            await rollback_db.commit()
            try:
                await coordination.release(claim, None)
            except Exception:
                pass
        raise HTTPException(status_code=503, detail="Could not issue single-use signaling grant.")
    return SessionCreated(session_id=call.id, signaling_token=signaling_token)


async def _owned_session(db: AsyncSession, owner_id: uuid.UUID, session_id: uuid.UUID) -> CallSession:
    session = await db.scalar(
        select(CallSession)
        .join(Agent, CallSession.agent_id == Agent.id)
        .where(
            CallSession.id == session_id,
            CallSession.owner_id == owner_id,
            Agent.owner_id == owner_id,
        )
    )
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


@router.post("/agents/{agent_id}/test-calls", status_code=201, response_model=SessionCreated)
async def create_test_call(
    agent_id: uuid.UUID, request: Request, db: Db, user: Writer, payload: CallRequest | None = None
):
    coordination = getattr(request.app.state, "coordination", None)
    return await _create_call(
        db,
        call_type="test",
        client_ip=request.client.host if request.client is not None else None,
        agent_id=agent_id,
        owner_id=user.id,
        language=payload.language if payload else None,
        coordination=coordination,
    )


@router.get("/agents/{agent_id}/sessions", response_model=list[SessionRead])
async def list_agent_sessions(agent_id: uuid.UUID, db: Db, user: Reader):
    agent = await db.scalar(select(Agent).where(Agent.id == agent_id, Agent.owner_id == user.id))
    if agent is None:
        raise HTTPException(404, "Agent not found")
    return list(
        await db.scalars(
            select(CallSession)
            .where(CallSession.agent_id == agent.id, CallSession.owner_id == user.id)
            .order_by(CallSession.created_at.desc(), CallSession.id)
        )
    )


@router.get("/sessions/{session_id}", response_model=SessionRead)
async def get_session(session_id: uuid.UUID, db: Db, user: Reader):
    return await _owned_session(db, user.id, session_id)


@router.get("/sessions/{session_id}/trace", response_model=SessionTraceRead)
async def get_session_trace(session_id: uuid.UUID, db: Db, user: Reader):
    await _owned_session(db, user.id, session_id)
    try:
        spans = await fetch_session_trace(str(session_id))
    except TraceUnavailableError:
        raise HTTPException(503, "Phoenix is not available right now.")
    return {"session_id": session_id, "spans": spans}


@router.get("/sessions/{session_id}/messages", response_model=list[MessageRead])
async def list_session_messages(session_id: uuid.UUID, db: Db, user: Reader):
    await _owned_session(db, user.id, session_id)
    return list(
        await db.scalars(
            select(CallMessage).where(CallMessage.session_id == session_id).order_by(CallMessage.sequence)
        )
    )


@public_router.get("/agents/{slug}", response_model=PublicAgentRead)
async def get_public_agent(slug: str, db: Db):
    agent = await db.scalar(select(Agent).where(Agent.slug == slug, Agent.status == "published"))
    if agent is None:
        raise HTTPException(404, "Agent not found")
    return agent


@public_router.post("/agents/{slug}/calls", status_code=201, response_model=SessionCreated)
async def create_public_call(slug: str, request: Request, db: Db, payload: CallRequest | None = None):
    coordination = getattr(request.app.state, "coordination", None)
    return await _create_call(
        db,
        call_type="public",
        client_ip=request.client.host if request.client is not None else None,
        slug=slug,
        language=payload.language if payload else None,
        coordination=coordination,
    )


class SessionWriter:
    def __init__(self, session_id: uuid.UUID):
        self.session_id = session_id
        self._queue: asyncio.Queue[_SessionWrite | object] = asyncio.Queue(maxsize=_SESSION_WRITE_QUEUE_SIZE)
        self._next_sequence: int | None = None
        self._failure: Exception | None = None
        self._closed = False
        self._task = asyncio.create_task(self._run())

    def enqueue_message(self, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        if not isinstance(content, str):
            raise TypeError("message content must be text")
        self._enqueue(_MessageWrite(role=role, content=content))

    def _enqueue(self, event: _SessionWrite) -> None:
        if self._closed:
            raise SessionWriteError("Session writer is closed")
        if self._failure is not None:
            raise SessionWriteError("Session writer has failed") from self._failure
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull as error:
            self._failure = error
            raise SessionWriteError("Session write queue is full") from error

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if event is _STOP:
                    return
                if self._failure is None:
                    await self._persist(event)
            except Exception as error:
                self._failure = error
            finally:
                self._queue.task_done()

    async def _persist(self, event: _SessionWrite) -> None:
        async with DbSession() as db:
            session_id = await db.scalar(
                select(CallSession.id).where(CallSession.id == self.session_id).with_for_update()
            )
            if session_id is None:
                raise SessionWriteError("Session no longer exists")
            if isinstance(event, _MessageWrite):
                if self._next_sequence is None:
                    latest = await db.scalar(
                        select(func.coalesce(func.max(CallMessage.sequence), 0)).where(
                            CallMessage.session_id == self.session_id
                        )
                    )
                    self._next_sequence = int(latest or 0) + 1
                db.add(
                    CallMessage(
                        session_id=self.session_id,
                        sequence=self._next_sequence,
                        role=event.role,
                        content=event.content,
                    )
                )
            await db.commit()
            if isinstance(event, _MessageWrite):
                self._next_sequence += 1

    async def flush(self) -> None:
        if not self._closed:
            self._closed = True
            await self._queue.put(_STOP)
        await self._task
        if self._failure is not None:
            raise SessionWriteError("Session persistence failed") from self._failure

    async def drain(self) -> None:
        """Wait for queued writes to be persisted without closing the writer."""
        await self._queue.join()
        if self._failure is not None:
            raise SessionWriteError("Session persistence failed") from self._failure



async def set_session_status(
    session_id: uuid.UUID,
    status: SessionStatus,
    *,
    claim: CallClaim | None = None,
) -> bool:
    if status not in _TRANSITIONS:
        raise ValueError("Unsupported session status")
    async with DbSession() as db:
        await _acquire_coordination_lock(db)
        agent_id = await db.scalar(select(CallSession.agent_id).where(CallSession.id == session_id))
        if agent_id is None:
            return False
        agent = await db.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
        if agent is None:
            return False
        call = await db.scalar(select(CallSession).where(CallSession.id == session_id).with_for_update())
        if call is None:
            return False

        if call.call_type in {"test", "public"}:
            if claim is None:
                if call.status == "created" and status == "expired" and call.worker_id is None:
                    pass
                else:
                    raise SessionUnavailableError("Claim required for claimed voice session status change")
            elif call.claim_generation != claim.generation:
                raise SessionUnavailableError("Claim generation mismatch")
            elif claim.worker_id is not None and call.worker_id is not None and call.worker_id != claim.worker_id:
                raise SessionUnavailableError("Worker owner mismatch")

        if call.status == status:
            return True
        if status not in _TRANSITIONS.get(call.status, ()):
            raise SessionUnavailableError(f"Cannot change session status from {call.status} to {status}")

        db_now = await db.scalar(text("SELECT clock_timestamp()"))
        if status == "active":
            if call.call_type == "public" and agent.status != "published":
                raise SessionUnavailableError("Public calls require a published agent")
            if call.call_type in {"test", "chat_test"} and agent.status == "suspended":
                raise SessionUnavailableError("Suspended agents cannot start calls")
            if call.status == "created":
                if call.worker_id is not None and call.lease_expires_at is not None:
                    if call.lease_expires_at <= db_now:
                        raise SessionUnavailableError("Setup lease has expired")
                elif call.signaling_expires_at is not None and call.signaling_expires_at <= db_now:
                    raise SessionUnavailableError("Call signaling token has expired")

        call.status = status
        if status == "active":
            call.started_at = call.started_at or db_now
        if status in _TERMINAL_STATUSES:
            call.ended_at = call.ended_at or db_now
        call.updated_at = db_now
        await db.commit()
        return True


async def load_signaling_context(session_id: uuid.UUID) -> SignalingContext | None:
    async with DbSession() as db:
        row = await db.execute(
            select(CallSession, Agent.status, func.clock_timestamp())
            .join(Agent, CallSession.agent_id == Agent.id)
            .where(CallSession.id == session_id)
        )
        result = row.one_or_none()
        if result is None:
            return None
        call, agent_status, db_now = result
        if call.status not in _ACTIVE_STATUSES:
            return None
        if call.status == "created":
            if call.worker_id is not None and call.lease_expires_at is not None:
                expired = call.lease_expires_at <= db_now
            elif call.signaling_expires_at is not None:
                expired = call.signaling_expires_at <= db_now
            else:
                expired = False
        elif call.status == "active":
            if call.lease_expires_at is not None and call.lease_expires_at <= db_now:
                expired = True
            else:
                expired = False
        else:
            expired = False
        if expired:
            return None
        return {
            "snapshot": call.snapshot,
            "agent_status": agent_status,
            "session_status": call.status,
            "call_type": call.call_type,
            "worker_id": call.worker_id,
            "claim_generation": call.claim_generation,
            "lease_expires_at": call.lease_expires_at,
        }


async def load_session_snapshot(session_id: uuid.UUID) -> FrozenCallSnapshot | None:
    context = await load_signaling_context(session_id)
    return None if context is None else context["snapshot"]


async def renew_voice_claim(
    claim: CallClaim,
    coordination: Coordination,
    pc_id: str | None,
) -> LeaseObservation:
    async with DbSession() as db:
        await _acquire_coordination_lock(db)
        # 1. Re-read durable row and fence AFTER acquiring advisory lock and BEFORE Redis renewal
        db_now = await db.scalar(text("SELECT clock_timestamp()"))
        call = await db.scalar(
            select(CallSession).where(CallSession.id == claim.session_id).with_for_update()
        )
        if (
            call is None
            or call.claim_generation != claim.generation
            or call.worker_id != claim.worker_id
            or call.status not in ("created", "active")
            or call.lease_expires_at is None
            or call.lease_expires_at <= db_now
        ):
            await db.commit()
            raise RuntimeError("Durable lease renewal failed: session mismatch, non-active status, or expired deadline")

        # 2. Renew in Redis only after durable fence verifies claim and unexpired deadline
        t_before_renew = time.monotonic()
        obs = await coordination.renew(claim, pc_id)
        t_after_renew = time.monotonic()

        # 3. Compute remaining conservatively subtracting elapsed renewal wait time
        db_now2 = await db.scalar(text("SELECT clock_timestamp()"))
        elapsed = t_after_renew - t_before_renew
        rem = max(0.0, obs.remaining_seconds - elapsed)
        if rem <= 0.0:
            await db.commit()
            raise RuntimeError("Durable lease renewal failed: renewal duration exceeded remaining budget")

        db_lease_deadline = db_now2 + timedelta(seconds=rem)
        call.lease_expires_at = db_lease_deadline
        call.updated_at = db_now2
        await db.commit()
        conservative_mono = min(obs.monotonic_expires_at, t_after_renew + rem)
        return LeaseObservation(claim=obs.claim, monotonic_expires_at=conservative_mono)


async def reconcile_voice_sessions(
    coordination: Coordination,
    *,
    batch_size: int = 100,
    is_leader: Callable[[], bool] | None = None,
) -> int:
    reaped = 0
    after_id: uuid.UUID | None = None
    while True:
        if is_leader is not None and not is_leader():
            logger.info("Leadership lost during voice session reconciliation; stopping scan")
            break

        async with DbSession() as db:
            db_now = await db.scalar(text("SELECT clock_timestamp()"))
            query = (
                select(CallSession.id)
                .where(
                    CallSession.call_type.in_(["test", "public"]),
                    or_(
                        CallSession.status.in_(["created", "active"]),
                        and_(
                            CallSession.status.in_(["completed", "failed", "cancelled", "expired"]),
                            CallSession.updated_at >= db_now - timedelta(seconds=300),
                        ),
                    ),
                )
            )
            if after_id is not None:
                query = query.where(CallSession.id > after_id)
            query = query.order_by(CallSession.id).limit(batch_size)
            batch = list(await db.scalars(query))

        if not batch:
            break

        for session_id in batch:
            if is_leader is not None and not is_leader():
                logger.info("Leadership lost during voice session reconciliation batch; stopping scan")
                return reaped

            after_id = session_id
            claim_to_release: CallClaim | None = None
            async with DbSession() as db:
                await _acquire_coordination_lock(db)
                db_now = await db.scalar(text("SELECT clock_timestamp()"))
                call = await db.scalar(
                    select(CallSession).where(CallSession.id == session_id).with_for_update()
                )
                if call is None:
                    continue

                if call.status in ("completed", "failed", "cancelled", "expired"):
                    # Reconcile orphan Redis lease left behind by crash/failure between DB commit and Redis release
                    try:
                        obs = await coordination.inspect(call.id)
                    except Exception:
                        continue
                    if obs is not None and obs.claim.generation == call.claim_generation:
                        claim_to_release = obs.claim
                else:
                    should_reap = False
                    terminal_status: SessionStatus = "failed"

                    if call.status == "created" and call.worker_id is None:
                        # Unowned created grant: expires only at its original signaling deadline
                        if call.signaling_expires_at is not None and call.signaling_expires_at <= db_now:
                            should_reap = True
                            terminal_status = "expired"
                    else:
                        # Owned setup ('created' with worker_id) or active call
                        # Requires expired durable deadline AND no matching live Redis lease
                        if call.lease_expires_at is not None and call.lease_expires_at <= db_now:
                            try:
                                obs = await coordination.inspect(call.id)
                            except Exception:
                                # Defer on Redis error, do not infer absence
                                continue

                            if obs is None:
                                should_reap = True
                            elif obs.claim.generation != call.claim_generation:
                                should_reap = True

                    if should_reap:
                        if is_leader is not None and not is_leader():
                            logger.info("Leadership lost during session reap under lock; stopping write")
                            return reaped
                        obs_gen = call.claim_generation or "legacy"
                        claim_to_release = CallClaim(
                            session_id=call.id,
                            agent_id=call.agent_id,
                            call_type=call.call_type,
                            generation=obs_gen,
                            worker_id=call.worker_id,
                            phase="active" if call.status == "active" else "starting",
                        )
                        call.status = terminal_status
                        call.ended_at = call.ended_at or db_now
                        call.updated_at = db_now
                        await db.commit()
                        reaped += 1

            if claim_to_release is not None:
                try:
                    await coordination.release(claim_to_release, None)
                except Exception as exc:
                    logger.debug("Reaper release error for session %s: %s", claim_to_release.session_id, exc)

        if len(batch) < batch_size:
            break

    return reaped
