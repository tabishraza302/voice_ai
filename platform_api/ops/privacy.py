"""Audited user export and transcript-retention operations.

Financial and audit rows are intentionally immutable. Retention replaces transcript
content in-place so session, sequence, cursor, and billing evidence remain intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.audit import record_audit
from platform_api.auth.errors import NotFoundError, ValidationError
from platform_api.billing.models import (
    BillingOperation,
    CallBudget,
    MeterCheckpoint,
    WalletEntry,
)
from platform_api.events.models import (
    EventQuarantine,
    ProcessedEvent,
    ProviderUsage,
    TranscriptGap,
    TranscriptSegment,
    TranscriptState,
)
from platform_api.generation.models import GenerationJob
from platform_api.models.auth import AuthMail, AuthMfa, AuthSession, AuthToken, AuditLog, User
from platform_api.models.session import Session
from platform_api.models.wallet import WalletAccount
from platform_api.agents.models import Agent, AgentDraft, AgentVersion, IdempotencyRecord
from platform_api.public.models import PublicDeployment
from platform_api.sessions.models import (
    RuntimeRegistration,
    SessionJoinJti,
    SessionParticipant,
    SessionPeer,
    SessionSlot,
)
from platform_api.testing.models import ChatMessage, ChatTestSession

REDACTED = "[REDACTED]"
MAX_RETENTION_BATCH = 1000
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_EXPORT_RECORDS = 10_000
MIN_AUDIT_REASON_LENGTH = 10
MAX_AUDIT_REASON_LENGTH = 500
_SECRET_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "credential",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
    "private_key",
    "ciphertext",
    "digest",
    "key_fingerprint",
    "otp",
)
_PRIVATE_CONTENT_FIELDS = {
    "content",
    "body",
    "final_text",
    "transcript",
    "trace_json",
    "response_json",
    "response_headers",
}
_TEXT_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)"
        r"\s*([:=])\s*([^\s,;]+)"
    ),
)


@dataclass(frozen=True)
class ExportSummary:
    user_id: uuid.UUID
    output_path: Path
    record_counts: dict[str, int]
    size_bytes: int
    include_transcripts: bool


def _database_endpoint(url: Any) -> tuple[str, int, str]:
    return (url.host or "<unix-socket>").lower(), url.port or 5432, url.database or ""


def validate_retention_database_url(database_url: str, retention_url: str) -> str:
    """Require a separate PostgreSQL role on the exact shared DB endpoint."""
    if not retention_url or not retention_url.strip():
        raise ValueError("PLATFORM_RETENTION_DATABASE_URL is required for retention operations")
    try:
        platform = make_url(database_url)
        maintenance = make_url(retention_url.strip())
    except Exception as exc:
        raise ValueError("Invalid PostgreSQL retention connection settings") from exc

    for label, url in (("DATABASE_URL", platform), ("PLATFORM_RETENTION_DATABASE_URL", maintenance)):
        if url.get_backend_name() != "postgresql" or url.get_driver_name() != "asyncpg":
            raise ValueError(f"{label} must use PostgreSQL with asyncpg")
        if not url.username or not url.database:
            raise ValueError(f"{label} must name a role and database")
        if any(
            key.lower()
            in {"db", "database", "dbname", "host", "hostaddr", "port", "user", "username"}
            for key in url.query
        ):
            raise ValueError(f"{label} must specify its endpoint outside connection query parameters")
    if platform.username == maintenance.username:
        raise ValueError("Retention operations require a distinct maintenance database role")
    if _database_endpoint(platform) != _database_endpoint(maintenance):
        raise ValueError("The retention role must target the DATABASE_URL PostgreSQL database")
    return retention_url.strip()


def sanitize_operator_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise ValidationError("An operator reason is required")
    clean = reason.strip()
    if not MIN_AUDIT_REASON_LENGTH <= len(clean) <= MAX_AUDIT_REASON_LENGTH:
        raise ValidationError("Operator reason must be 10 to 500 characters")
    return _redact_text(clean)


def _redact_text(value: str) -> str:
    result = value
    for pattern in _TEXT_SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)\\bBearer"):
            result = pattern.sub("Bearer [REDACTED]", result)
        else:
            result = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", result)
    return result


def _safe_value(value: Any, *, key: str = "", include_transcripts: bool = False) -> Any:
    normalized_key = key.lower()
    if any(part in normalized_key for part in _SECRET_KEY_PARTS):
        return REDACTED
    if normalized_key in _PRIVATE_CONTENT_FIELDS and not include_transcripts:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(child_key): _safe_value(
                child_value,
                key=str(child_key),
                include_transcripts=include_transcripts,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_value(item, include_transcripts=include_transcripts)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return REDACTED
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return str(value)
    return value


def _model_data(record: Any, *, include_transcripts: bool = False) -> dict[str, Any]:
    return {
        column.name: _safe_value(
            getattr(record, column.name),
            key=column.name,
            include_transcripts=include_transcripts,
        )
        for column in record.__table__.columns
    }


class _RecordBudget:
    def __init__(self, max_records: int):
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise ValidationError("Export record limit must be positive")
        self.remaining = max_records

    def consume(self, count: int) -> None:
        if count > self.remaining:
            raise ValidationError("Export exceeds its configured record limit")
        self.remaining -= count


async def _records(
    db: AsyncSession,
    model: type,
    *criteria: Any,
    record_budget: _RecordBudget | None = None,
) -> list[Any]:
    statement = select(model)
    if criteria:
        statement = statement.where(*criteria)
    statement = statement.order_by(*model.__table__.primary_key.columns)
    if record_budget is not None:
        statement = statement.limit(record_budget.remaining + 1)
    records = list((await db.execute(statement)).scalars().all())
    if record_budget is not None:
        record_budget.consume(len(records))
    return records


class _BoundedTextWriter:
    def __init__(self, stream: Any, max_bytes: int):
        self.stream = stream
        self.max_bytes = max_bytes
        self.size_bytes = 0

    def write(self, value: str) -> int:
        encoded = value.encode("utf-8")
        if self.size_bytes + len(encoded) > self.max_bytes:
            raise ValidationError("Export exceeds its configured size limit")
        self.stream.write(encoded)
        self.size_bytes += len(encoded)
        return len(value)


def _write_private_json(path: Path, payload: dict[str, Any], *, max_bytes: int) -> int:
    """Create a bounded mode-0600 JSON export without overwriting an existing path."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValidationError("Export size limit must be positive")
    output = Path(path)
    if not output.is_absolute():
        raise ValidationError("Export path must be absolute")
    if not output.parent.is_dir():
        raise ValidationError("Export directory must already exist")
    if output.exists():
        raise FileExistsError("Export output already exists")

    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(temporary_name)
    linked = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            writer = _BoundedTextWriter(stream, max_bytes)
            json.dump(payload, writer, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            writer.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
        linked = True
        temporary.unlink()
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return writer.size_bytes
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if linked:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
        raise


async def export_user_data(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    output_path: Path,
    reason: str,
    include_transcripts: bool = False,
    max_bytes: int = MAX_EXPORT_BYTES,
    max_records: int = MAX_EXPORT_RECORDS,
    now: datetime | None = None,
) -> ExportSummary:
    """Write a bounded, redacted user export and append its audit record."""
    safe_reason = sanitize_operator_reason(reason)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValidationError("Export size limit must be positive")
    budget = _RecordBudget(max_records)
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    budget.consume(1)

    async def select_records(model: type, *criteria: Any) -> list[Any]:
        return await _records(db, model, *criteria, record_budget=budget)

    agents = await select_records(Agent, Agent.owner_user_id == user_id)
    agent_ids = [record.id for record in agents]
    sessions = await select_records(Session, Session.owner_user_id == user_id)
    session_ids = [record.id for record in sessions]
    tests = await select_records(ChatTestSession, ChatTestSession.owner_user_id == user_id)
    test_ids = [record.id for record in tests]
    auth_tokens = await select_records(AuthToken, AuthToken.user_id == user_id)
    auth_token_ids = [record.id for record in auth_tokens]
    records_by_name: dict[str, list[Any]] = {
        "auth_sessions": await select_records(AuthSession, AuthSession.user_id == user_id),
        "auth_tokens": auth_tokens,
        "auth_mail": (
            await select_records(AuthMail, AuthMail.token_id.in_(auth_token_ids))
            if auth_token_ids
            else []
        ),
        "auth_mfa": await select_records(AuthMfa, AuthMfa.user_id == user_id),
        "idempotency_records": await select_records(
            IdempotencyRecord, IdempotencyRecord.user_id == user_id
        ),
        "agents": agents,
        "public_deployments": await select_records(
            PublicDeployment, PublicDeployment.owner_user_id == user_id
        ),
        "agent_drafts": await select_records(AgentDraft, AgentDraft.agent_id.in_(agent_ids)) if agent_ids else [],
        "agent_versions": await select_records(AgentVersion, AgentVersion.agent_id.in_(agent_ids)) if agent_ids else [],
        "generation_jobs": await select_records(GenerationJob, GenerationJob.owner_user_id == user_id),
        "chat_test_sessions": tests,
        "chat_test_messages": await select_records(ChatMessage, ChatMessage.test_id.in_(test_ids)) if test_ids else [],
        "wallet_accounts": await select_records(WalletAccount, WalletAccount.user_id == user_id),
        "wallet_entries": await select_records(WalletEntry, WalletEntry.user_id == user_id),
        "sessions": sessions,
        "session_slots": await select_records(SessionSlot, SessionSlot.session_id.in_(session_ids)) if session_ids else [],
        "runner_registrations": await select_records(RuntimeRegistration, RuntimeRegistration.session_id.in_(session_ids)) if session_ids else [],
        "session_participants": await select_records(SessionParticipant, SessionParticipant.session_id.in_(session_ids)) if session_ids else [],
        "session_join_jtis": await select_records(SessionJoinJti, SessionJoinJti.session_id.in_(session_ids)) if session_ids else [],
        "session_peers": await select_records(SessionPeer, SessionPeer.session_id.in_(session_ids)) if session_ids else [],
        "call_budgets": await select_records(CallBudget, CallBudget.session_id.in_(session_ids)) if session_ids else [],
        "billing_operations": await select_records(BillingOperation, BillingOperation.session_id.in_(session_ids)) if session_ids else [],
        "meter_checkpoints": await select_records(MeterCheckpoint, MeterCheckpoint.session_id.in_(session_ids)) if session_ids else [],
        "processed_events": await select_records(ProcessedEvent, ProcessedEvent.session_id.in_(session_ids)) if session_ids else [],
        "transcript_segments": await select_records(TranscriptSegment, TranscriptSegment.session_id.in_(session_ids)) if session_ids else [],
        "transcript_gaps": await select_records(TranscriptGap, TranscriptGap.session_id.in_(session_ids)) if session_ids else [],
        "transcript_state": await select_records(TranscriptState, TranscriptState.session_id.in_(session_ids)) if session_ids else [],
        "provider_usage": await select_records(ProviderUsage, ProviderUsage.session_id.in_(session_ids)) if session_ids else [],
        "event_quarantine": await select_records(EventQuarantine, EventQuarantine.session_id.in_(session_ids)) if session_ids else [],
    }
    related_ids = {user_id}
    for records in records_by_name.values():
        for record in records:
            related_ids.update(
                getattr(record, column.name)
                for column in record.__table__.primary_key.columns
            )
    audit_criteria = [
        AuditLog.actor_id == user_id,
        AuditLog.target_id.in_(related_ids),
    ]
    if session_ids:
        audit_criteria.append(AuditLog.session_id.in_(session_ids))
    audits = await select_records(AuditLog, or_(*audit_criteria))

    payload: dict[str, Any] = {
        "format": "voice-ai-user-export-v1",
        "exported_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "include_transcripts": include_transcripts,
        "user": _model_data(user),
        "records": {
            name: [_model_data(record, include_transcripts=include_transcripts) for record in records]
            for name, records in records_by_name.items()
        },
        "audit": [
            {
                "id": _safe_value(item.id),
                "action": _safe_value(item.action),
                "target_type": _safe_value(item.target_type),
                "result": _safe_value(item.result),
                "created_at": _safe_value(item.created_at),
            }
            for item in audits
        ],
    }
    counts = {name: len(records) for name, records in records_by_name.items()}
    counts["audit"] = len(audits)
    output = Path(output_path)
    size_bytes = _write_private_json(output, payload, max_bytes=max_bytes)
    try:
        await record_audit(
            db,
            actor_kind="operator",
            action="ops.user.exported",
            target_type="user",
            target_id=user_id,
            reason=safe_reason,
            result="success",
            change_summary={
                "record_counts": counts,
                "include_transcripts": include_transcripts,
                "size_bytes": size_bytes,
            },
            created_at=now or datetime.now(timezone.utc),
        )
    except Exception:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        raise
    return ExportSummary(
        user_id=user_id,
        output_path=output,
        record_counts=counts,
        size_bytes=size_bytes,
        include_transcripts=include_transcripts,
    )


async def _select_redaction_batch(
    db: AsyncSession,
    *,
    model: type,
    id_column: Any,
    content_column: Any,
    date_column: Any,
    before: datetime | None,
    batch_size: int,
    criteria: tuple[Any, ...] = (),
) -> list[tuple[Any, Any]]:
    conditions = [content_column != REDACTED, *criteria]
    if before is not None:
        conditions.append(date_column < before)
    session_column = getattr(model, "session_id", None)
    columns = (id_column, session_column) if session_column is not None else (id_column,)
    statement = (
        select(*columns)
        .where(*conditions)
        .order_by(date_column, id_column)
        .limit(batch_size)
    )
    rows = (await db.execute(statement)).all()
    return [(row[0], row[1] if session_column is not None else None) for row in rows]


async def _redact_batches(
    db: AsyncSession,
    *,
    session_ids: Any | None,
    chat_test_ids: Any | None,
    before: datetime | None,
    batch_size: int,
    now: datetime,
) -> dict[str, int]:
    segment_rows = await _select_redaction_batch(
        db,
        model=TranscriptSegment,
        id_column=TranscriptSegment.event_id,
        content_column=TranscriptSegment.final_text,
        date_column=TranscriptSegment.created_at,
        before=before,
        batch_size=batch_size,
        criteria=(TranscriptSegment.session_id.in_(session_ids),) if session_ids is not None else (),
    )
    chat_rows = await _select_redaction_batch(
        db,
        model=ChatMessage,
        id_column=ChatMessage.id,
        content_column=ChatMessage.content,
        date_column=ChatMessage.created_at,
        before=before,
        batch_size=batch_size,
        criteria=(ChatMessage.test_id.in_(chat_test_ids),) if chat_test_ids is not None else (),
    )
    quarantine_rows = await _select_redaction_batch(
        db,
        model=EventQuarantine,
        id_column=EventQuarantine.id,
        content_column=EventQuarantine.body,
        date_column=EventQuarantine.received_at,
        before=before,
        batch_size=batch_size,
        criteria=(EventQuarantine.session_id.in_(session_ids),) if session_ids is not None else (),
    )

    changed_session_ids = {session_id for _, session_id in segment_rows if session_id is not None}
    if changed_session_ids:
        await db.execute(
            update(TranscriptState)
            .where(TranscriptState.session_id.in_(changed_session_ids))
            .values(revision=TranscriptState.revision + 1, updated_at=now)
        )
    if segment_rows:
        await db.execute(
            update(TranscriptSegment)
            .where(TranscriptSegment.event_id.in_([row_id for row_id, _ in segment_rows]))
            .values(final_text=REDACTED)
        )
    if chat_rows:
        await db.execute(
            update(ChatMessage)
            .where(ChatMessage.id.in_([row_id for row_id, _ in chat_rows]))
            .values(content=REDACTED)
        )
    if quarantine_rows:
        await db.execute(
            update(EventQuarantine)
            .where(EventQuarantine.id.in_([row_id for row_id, _ in quarantine_rows]))
            .values(body=REDACTED)
        )
    return {
        "transcript_segments": len(segment_rows),
        "chat_test_messages": len(chat_rows),
        "event_quarantine_bodies": len(quarantine_rows),
    }


async def _audit_content_operation(
    db: AsyncSession,
    *,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None,
    reason: str,
    counts: dict[str, int],
    cutoff: datetime | None,
    now: datetime,
) -> None:
    await record_audit(
        db,
        actor_kind="operator",
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=sanitize_operator_reason(reason),
        result="success",
        change_summary={
            "redacted_counts": counts,
            "cutoff": cutoff.isoformat() if cutoff else None,
        },
        created_at=now,
    )


async def redact_user_transcripts(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    reason: str,
    batch_size: int = MAX_RETENTION_BATCH,
    now: datetime | None = None,
) -> dict[str, int]:
    """Redact one bounded batch of a user's transcript and chat-test content."""
    if not 1 <= batch_size <= MAX_RETENTION_BATCH:
        raise ValidationError(f"Batch size must be between 1 and {MAX_RETENTION_BATCH}")
    if await db.scalar(select(User.id).where(User.id == user_id)) is None:
        raise NotFoundError("User not found")
    current_time = now or datetime.now(timezone.utc)
    session_ids = select(Session.id).where(Session.owner_user_id == user_id)
    test_ids = select(ChatTestSession.id).where(ChatTestSession.owner_user_id == user_id)
    counts = await _redact_batches(
        db,
        session_ids=session_ids,
        chat_test_ids=test_ids,
        before=None,
        batch_size=batch_size,
        now=current_time,
    )
    await _audit_content_operation(
        db,
        action="ops.user.transcripts_redacted",
        target_type="user",
        target_id=user_id,
        reason=reason,
        counts=counts,
        cutoff=None,
        now=current_time,
    )
    return counts


async def apply_retention(
    db: AsyncSession,
    *,
    before: datetime,
    reason: str,
    batch_size: int = MAX_RETENTION_BATCH,
    now: datetime | None = None,
) -> dict[str, int]:
    """Redact one bounded, cutoff-scoped batch while preserving financial and audit evidence."""
    if before.tzinfo is None or before.utcoffset() is None:
        raise ValidationError("Retention cutoff must include a timezone")
    if not 1 <= batch_size <= MAX_RETENTION_BATCH:
        raise ValidationError(f"Batch size must be between 1 and {MAX_RETENTION_BATCH}")
    current_time = now or datetime.now(timezone.utc)
    counts = await _redact_batches(
        db,
        session_ids=None,
        chat_test_ids=None,
        before=before.astimezone(timezone.utc),
        batch_size=batch_size,
        now=current_time,
    )
    await _audit_content_operation(
        db,
        action="ops.retention.applied",
        target_type="retention_policy",
        target_id=None,
        reason=reason,
        counts=counts,
        cutoff=before.astimezone(timezone.utc),
        now=current_time,
    )
    return counts
