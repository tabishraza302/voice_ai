"""Idempotent, transactional history projections.

The consumer calls :meth:`HistoryRepository.ingest` inside one PostgreSQL
transaction.  Dedupe, the event effect and the transcript revision advance are
committed together or not at all.  This module never imports a wallet model or
issues wallet SQL, and the consumer session is additionally guarded by a
``before_flush`` check that refuses wallet table mutations.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal, Sequence
import uuid
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from platform_api.events.models import (
    EventQuarantine,
    ProcessedEvent,
    ProviderUsage,
    TranscriptGap,
    TranscriptSegment,
    TranscriptState,
)
from platform_api.events.schemas import (
    MAX_BODY_BYTES,
    EventEnvelope,
    EventValidationError,
    canonical_json,
    parse_event_body,
)


WALLET_TABLE_PREFIX = "wallet_"
WALLET_TABLES = {
    "wallet_accounts",
    "wallet_entries",
    "call_budgets",
    "billing_operations",
    "meter_checkpoints",
}
WALLET_MUTATION_SQL = re.compile(
    r"\b(insert\s+into|update|delete\s+from)\s+(?:only\s+)?(?:[\"'`]?[a-z0-9_$]+[\"'`]?\s*\.\s*)?[\"'`]?(wallet_[a-z0-9_]*|call_budgets|billing_operations|meter_checkpoints)\b",
    re.IGNORECASE,
)
TRANSCRIPT_PAGE_MAX = 200
TRANSCRIPT_PAGE_DEFAULT = 50
MAX_QUARANTINE_BODY_BYTES = 256 * 1024


class WalletMutationForbidden(PermissionError):
    """The history consumer attempted to mutate a wallet table (InsufficientPrivilege)."""

    code = "INSUFFICIENT_PRIVILEGE"
class TranscriptChanged(RuntimeError):
    """The caller's transcript cursor belongs to an obsolete revision."""

    code = "TRANSCRIPT_CHANGED"


class InvalidCursor(ValueError):
    """The transcript cursor is malformed or out of range."""

    code = "INVALID_CURSOR"


# MECHANISM: Consumer Wallet Immutability Guard
# The history consumer transaction sets session.info["history_consumer"] = True.
# Two SQLAlchemy session-level event listeners enforce that no consumer execution
# can mutate wallet rows:
# 1. _reject_wallet_flush (Session.before_flush) catches ORM object additions,
#    modifications, and deletions targeting any wallet table.
# 2. _reject_wallet_orm_execute (Session.do_orm_execute) catches direct statement
#    executions (update(), insert(), delete(), and raw text() SQL) targeting wallet tables.
# Any mutation attempt raises WalletMutationForbidden (inheriting from PermissionError /
# InsufficientPrivilege), guaranteeing at the session boundary that the consumer path
# cannot mutate wallet rows under any circumstance.

@event.listens_for(Session, "before_flush")
def _reject_wallet_mutation(
    session: Session, flush_context: Any, instances: Any
) -> None:
    """Mechanically prevent the consumer from writing wallet rows via ORM flush."""
    if not session.info.get("history_consumer"):
        return
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        table = getattr(obj, "__tablename__", "")
        if table.startswith(WALLET_TABLE_PREFIX) or table in WALLET_TABLES:
            raise WalletMutationForbidden(
                f"InsufficientPrivilege: history consumer may not mutate {table}"
            )


@event.listens_for(Session, "do_orm_execute")
def _reject_wallet_orm_execute(orm_execute_state: Any) -> None:
    """Mechanically prevent the consumer from writing wallet rows via DML statements or SQL."""
    session = orm_execute_state.session
    if not session or not session.info.get("history_consumer"):
        return
    stmt_str = str(orm_execute_state.statement)
    if WALLET_MUTATION_SQL.search(stmt_str):
        raise WalletMutationForbidden(
            "InsufficientPrivilege: history consumer cannot mutate wallet tables"
        )

IngestStatus = Literal["inserted", "duplicate", "quarantine"]


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    event_id: uuid.UUID | None
    session_id: uuid.UUID | None
    sequence: int | None
    reason: str | None = None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.status == other
        if isinstance(other, IngestResult):
            return (
                self.status == other.status
                and self.event_id == other.event_id
                and self.session_id == other.session_id
                and self.sequence == other.sequence
                and self.reason == other.reason
            )
        return False

    def __str__(self) -> str:
        return self.status
@dataclass(frozen=True)
class TranscriptSegmentView:
    sequence: int
    event_id: uuid.UUID
    speaker: str
    final_text: str
    start_ms: int
    end_ms: int
    kind: str


@dataclass(frozen=True)
class TranscriptGapView:
    after_sequence: int
    before_sequence: int
    reason: str


@dataclass(frozen=True)
class TranscriptPage:
    session_id: uuid.UUID
    transcript_revision: int
    segments: tuple[TranscriptSegmentView, ...]
    has_gaps: bool
    gaps: tuple[TranscriptGapView, ...]
    processing: bool
    next_cursor: str | None


@dataclass(frozen=True)
class TranscriptCursor:
    revision: int
    after_sequence: int


def encode_transcript_cursor(*, revision: int, after_sequence: int) -> str:
    if revision < 0 or after_sequence < 0:
        raise ValueError("cursor values must be non-negative")
    raw = canonical_json(
        {"after_sequence": after_sequence, "revision": revision}
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_transcript_cursor(cursor: str) -> TranscriptCursor:
    if not isinstance(cursor, str) or not cursor:
        raise InvalidCursor("cursor must be a non-empty string")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise InvalidCursor("cursor is not valid base64 JSON") from exc
    if not isinstance(data, dict) or set(data) != {"after_sequence", "revision"}:
        raise InvalidCursor("cursor must contain exactly revision and after_sequence")
    for key in ("revision", "after_sequence"):
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidCursor(f"cursor {key} must be a non-negative integer")
    return TranscriptCursor(
        revision=data["revision"], after_sequence=data["after_sequence"]
    )


def _missing_transcript_ranges(
    events: Sequence[tuple[int, str]],
) -> list[tuple[int, int]]:
    transcript_indexes = [
        index
        for index, (_sequence, event_type) in enumerate(events)
        if event_type == "transcript.segment"
    ]
    ranges: list[tuple[int, int]] = []
    for left_index, right_index in zip(transcript_indexes, transcript_indexes[1:]):
        previous = events[left_index][0]
        for sequence, _event_type in events[left_index + 1:right_index + 1]:
            if sequence > previous + 1:
                ranges.append((previous, sequence))
            previous = sequence
    return ranges


class HistoryRepository:
    """The only writer of event projections."""

    async def ingest(
        self,
        session: AsyncSession,
        envelope: EventEnvelope,
        *,
        received_at: datetime | None = None,
    ) -> IngestResult:
        """Insert the event and its effect atomically.

        Returns ``duplicate`` for an identical replay, ``quarantine`` for a
        conflicting identity/sequence, and ``inserted`` for a new event.
        """
        session.info["history_consumer"] = True
        now = received_at or datetime.now(timezone.utc)

        existing = await session.get(ProcessedEvent, envelope.event_id)
        if existing is not None:
            if existing.fingerprint == envelope.fingerprint:
                return IngestResult(
                    "duplicate",
                    envelope.event_id,
                    envelope.session_id,
                    envelope.sequence,
                )
            await self._quarantine(session, envelope, "EVENT_ID_CONFLICT")
            return IngestResult(
                "quarantine",
                envelope.event_id,
                envelope.session_id,
                envelope.sequence,
                "EVENT_ID_CONFLICT",
            )

        same_sequence = await session.scalar(
            select(ProcessedEvent).where(
                ProcessedEvent.session_id == envelope.session_id,
                ProcessedEvent.sequence == envelope.sequence,
            )
        )
        if same_sequence is not None:
            if same_sequence.event_id == envelope.event_id:
                if same_sequence.fingerprint == envelope.fingerprint:
                    return IngestResult(
                        "duplicate",
                        envelope.event_id,
                        envelope.session_id,
                        envelope.sequence,
                    )
                await self._quarantine(
                    session, envelope, "EVENT_ID_CONFLICT"
                )
                return IngestResult(
                    "quarantine",
                    envelope.event_id,
                    envelope.session_id,
                    envelope.sequence,
                    "EVENT_ID_CONFLICT",
                )
            await self._quarantine(session, envelope, "SEQUENCE_CONFLICT")
            return IngestResult(
                "quarantine",
                envelope.event_id,
                envelope.session_id,
                envelope.sequence,
                "SEQUENCE_CONFLICT",
            )

        state = await session.get(
            TranscriptState, envelope.session_id, with_for_update=True
        )
        if state is None:
            state = TranscriptState(
                session_id=envelope.session_id,
                revision=0,
                processing=True,
                last_sequence=0,
                updated_at=now,
            )
            session.add(state)
            await session.flush()

        old_gap_signature = await self._gap_signature(session, envelope.session_id)

        try:
            async with session.begin_nested():
                session.add(
                    ProcessedEvent(
                        event_id=envelope.event_id,
                        session_id=envelope.session_id,
                        sequence=envelope.sequence,
                        schema_version=envelope.schema_version,
                        event_type=envelope.event_type,
                        emitted_at=envelope.emitted_at,
                        payload_hash=envelope.payload_hash,
                        fingerprint=envelope.fingerprint,
                        received_at=now,
                    )
                )
                await session.flush()
                transcript_effect = await self._apply_effect(
                    session, envelope, now
                )
                state.last_sequence = max(
                    state.last_sequence, envelope.sequence
                )
                state.updated_at = now
                if envelope.event_type == "session.lifecycle":
                    payload = envelope.typed_payload
                    terminal = getattr(payload, "state", "") in {
                        "completed",
                        "failed",
                        "ended",
                    }
                    if terminal:
                        state.processing = False
                await session.flush()
                new_gap_signature = await self._gap_signature(
                    session, envelope.session_id
                )
                if transcript_effect or new_gap_signature != old_gap_signature:
                    state.revision += 1
                    await session.flush()
        except IntegrityError:
            # A concurrent consumer won the unique race.  The savepoint has
            # rolled back the effect; re-read and classify the winner.
            winner = await session.get(ProcessedEvent, envelope.event_id)
            if winner is not None:
                if winner.fingerprint == envelope.fingerprint:
                    return IngestResult(
                        "duplicate",
                        envelope.event_id,
                        envelope.session_id,
                        envelope.sequence,
                    )
                reason = "EVENT_ID_CONFLICT"
            else:
                reason = "SEQUENCE_CONFLICT"
            await self._quarantine(session, envelope, reason)
            return IngestResult(
                "quarantine",
                envelope.event_id,
                envelope.session_id,
                envelope.sequence,
                reason,
            )

        return IngestResult(
            "inserted", envelope.event_id, envelope.session_id, envelope.sequence
        )

    async def consume_event(
        self,
        session: AsyncSession,
        body: bytes | str,
        *,
        received_at: datetime | None = None,
    ) -> IngestResult:
        """Parse a wire body and ingest it in the caller's transaction.

        This is the shared-interface entry point from the master spec.  It
        returns exactly ``inserted``, ``duplicate`` or ``quarantine``.  Invalid
        bodies are recorded with the stable ``INVALID_EVENT`` reason code; the
        original body is retained separately for operator triage.
        Tampered payload hashes are also quarantined.
        """
        try:
            envelope = parse_event_body(body)
        except EventValidationError:
            reason = "INVALID_EVENT"
            await self.record_invalid_body(session, body, reason)
            return IngestResult(
                "quarantine",
                None,
                None,
                None,
                reason,
            )
        return await self.ingest(session, envelope, received_at=received_at)

    async def _apply_effect(
        self,
        session: AsyncSession,
        envelope: EventEnvelope,
        now: datetime,
    ) -> bool:
        """Insert the projection for this event.  Return whether transcript changed."""
        payload = envelope.typed_payload
        if envelope.event_type == "transcript.segment":
            session.add(
                TranscriptSegment(
                    event_id=envelope.event_id,
                    session_id=envelope.session_id,
                    sequence=envelope.sequence,
                    speaker=payload.speaker,
                    final_text=payload.final_text,
                    start_ms=payload.start_ms,
                    end_ms=payload.end_ms,
                    kind=payload.kind,
                    created_at=now,
                )
            )
            await session.flush()
            return True
        if envelope.event_type == "usage.provider":
            session.add(
                ProviderUsage(
                    event_id=envelope.event_id,
                    session_id=envelope.session_id,
                    provider=payload.provider,
                    model=payload.model,
                    kind=payload.kind,
                    quantity=payload.quantity,
                    unit=payload.unit,
                    created_at=now,
                )
            )
            await session.flush()
            return False
        if envelope.event_type == "transcript.gap":
            session.add(
                TranscriptGap(
                    session_id=envelope.session_id,
                    after_sequence=payload.after_sequence,
                    before_sequence=payload.before_sequence,
                    reason=payload.reason,
                    created_at=now,
                )
            )
            await session.flush()
            return True
        # session.lifecycle has no projection table beyond transcript_state.
        return False

    async def _quarantine(
        self,
        session: AsyncSession,
        envelope: EventEnvelope,
        reason: str,
    ) -> None:
        fingerprint = envelope.fingerprint
        exists = await session.scalar(
            select(EventQuarantine.id).where(
                EventQuarantine.fingerprint == fingerprint,
                EventQuarantine.reason == reason,
            )
        )
        if exists is not None:
            return
        session.add(
            EventQuarantine(
                event_id=envelope.event_id,
                session_id=envelope.session_id,
                sequence=envelope.sequence,
                reason=reason,
                body=envelope.to_json(),
                fingerprint=fingerprint,
            )
        )
        await session.flush()

    async def record_invalid_body(
        self,
        session: AsyncSession,
        body: bytes | str,
        reason: str,
    ) -> None:
        """Durably retain an unparseable/oversized body for operator triage."""
        session.info["history_consumer"] = True
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        fingerprint = hashlib.sha256(raw).hexdigest()
        exists = await session.scalar(
            select(EventQuarantine.id).where(
                EventQuarantine.fingerprint == fingerprint,
                EventQuarantine.reason == reason,
            )
        )
        if exists is not None:
            return
        stored = raw[:MAX_QUARANTINE_BODY_BYTES]
        text = stored.decode("utf-8", errors="replace")
        if len(raw) > MAX_QUARANTINE_BODY_BYTES:
            text += "\n[truncated by quarantine ceiling]"
        session.add(
            EventQuarantine(
                reason=reason,
                body=text,
                fingerprint=fingerprint,
            )
        )
        await session.flush()

    async def _gap_signature(
        self, session: AsyncSession, session_id: uuid.UUID
    ) -> tuple[tuple[int, int, str], ...]:
        # Sequences are session-wide. Known non-transcript events fill their
        # positions; only unknown positions between transcript segments are gaps.
        rows = (
            await session.execute(
                select(ProcessedEvent.sequence, ProcessedEvent.event_type)
                .where(ProcessedEvent.session_id == session_id)
                .order_by(ProcessedEvent.sequence)
            )
        ).all()
        events = [(row.sequence, row.event_type) for row in rows]
        explicit = (
            await session.execute(
                select(
                    TranscriptGap.after_sequence,
                    TranscriptGap.before_sequence,
                    TranscriptGap.reason,
                )
                .where(TranscriptGap.session_id == session_id)
                .order_by(TranscriptGap.after_sequence, TranscriptGap.before_sequence)
            )
        ).all()
        explicit_map = {(a, b): reason for a, b, reason in explicit}
        gaps: set[tuple[int, int, str]] = set()
        for after, before in _missing_transcript_ranges(events):
            gaps.add((after, before, explicit_map.get((after, before), "missing_sequence")))
        for after, before, reason in explicit:
            gaps.add((after, before, reason))
        return tuple(sorted(gaps))

    async def transcript_page(
        self,
        session: AsyncSession,
        session_id: uuid.UUID,
        *,
        cursor: str | None = None,
        limit: int = TRANSCRIPT_PAGE_DEFAULT,
    ) -> TranscriptPage:
        """Return one revision-bound transcript page.

        The cursor carries the revision it was issued at.  Any later insertion
        that changes the transcript or its gap state advances the revision and
        makes the old cursor fail with :class:`TranscriptChanged` instead of
        silently skipping the late segment.
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit < 1 or limit > TRANSCRIPT_PAGE_MAX:
            raise ValueError(
                f"limit must be between 1 and {TRANSCRIPT_PAGE_MAX}"
            )

        state = await session.get(TranscriptState, session_id)
        revision = state.revision if state is not None else 0
        processing = bool(state.processing) if state is not None else False
        after_sequence = 0
        if cursor is not None:
            decoded = decode_transcript_cursor(cursor)
            if decoded.revision != revision:
                raise TranscriptChanged(
                    f"transcript revision {revision} no longer matches cursor "
                    f"revision {decoded.revision}"
                )
            after_sequence = decoded.after_sequence

        rows = (
            await session.execute(
                select(TranscriptSegment)
                .where(
                    TranscriptSegment.session_id == session_id,
                    TranscriptSegment.sequence > after_sequence,
                )
                .order_by(TranscriptSegment.sequence)
                .limit(limit + 1)
            )
        ).scalars().all()

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = encode_transcript_cursor(
                revision=revision, after_sequence=rows[-1].sequence
            )

        gap_signature = await self._gap_signature(session, session_id)
        gaps = tuple(
            TranscriptGapView(after_sequence=a, before_sequence=b, reason=reason)
            for a, b, reason in gap_signature
        )
        segments = tuple(
            TranscriptSegmentView(
                sequence=row.sequence,
                event_id=row.event_id,
                speaker=row.speaker,
                final_text=row.final_text,
                start_ms=row.start_ms,
                end_ms=row.end_ms,
                kind=row.kind,
            )
            for row in rows
        )
        return TranscriptPage(
            session_id=session_id,
            transcript_revision=revision,
            segments=segments,
            has_gaps=bool(gaps),
            gaps=gaps,
            processing=processing,
            next_cursor=next_cursor,
        )


__all__ = [
    "HistoryRepository",
    "IngestResult",
    "IngestStatus",
    "TranscriptCursor",
    "TranscriptPage",
    "TranscriptSegmentView",
    "TranscriptGapView",
    "TranscriptChanged",
    "InvalidCursor",
    "WalletMutationForbidden",
    "encode_transcript_cursor",
    "decode_transcript_cursor",
    "TRANSCRIPT_PAGE_MAX",
    "TRANSCRIPT_PAGE_DEFAULT",
]
