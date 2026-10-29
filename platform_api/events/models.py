"""PostgreSQL projections for durable session history.

These tables are history only.  They are deliberately separate from wallet
tables: the event consumer has no financial authority and must never be able
to change a balance, reservation or ledger entry.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProcessedEvent(Base):
    """One accepted event identity, with the fingerprint that was accepted."""

    __tablename__ = "processed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_processed_events_session_sequence",
        ),
        CheckConstraint("sequence > 0", name="ck_processed_events_sequence_positive"),
        CheckConstraint(
            "schema_version > 0", name="ck_processed_events_schema_version_positive"
        ),
        Index("ix_processed_events_session_sequence", "session_id", "sequence"),
        Index("ix_processed_events_session_event_type", "session_id", "event_type"),
    )


class TranscriptSegment(Base):
    """Finalized transcript text, ordered by the session-wide sequence."""

    __tablename__ = "transcript_segments"

    event_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    speaker: Mapped[str] = mapped_column(String(16), nullable=False)
    final_text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="utterance")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id", "sequence", name="uq_transcript_segments_session_sequence"
        ),
        CheckConstraint(
            "speaker IN ('user', 'assistant', 'system')",
            name="ck_transcript_segments_speaker",
        ),
        CheckConstraint(
            "kind IN ('utterance', 'welcome', 'correction')",
            name="ck_transcript_segments_kind",
        ),
        CheckConstraint(
            "start_ms >= 0 AND end_ms >= start_ms",
            name="ck_transcript_segments_span",
        ),
        Index(
            "ix_transcript_segments_session_sequence",
            "session_id",
            "sequence",
        ),
    )


class ProviderUsage(Base):
    """Provider usage is evidence, never a retail charge."""

    __tablename__ = "provider_usage"

    event_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_provider_usage_quantity_nonnegative"),
        Index("ix_provider_usage_session", "session_id"),
    )


class TranscriptState(Base):
    """Per-session transcript revision and processing/gap state."""

    __tablename__ = "transcript_state"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    processing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    last_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_transcript_state_revision_nonnegative"),
        CheckConstraint(
            "last_sequence >= 0", name="ck_transcript_state_last_sequence_nonnegative"
        ),
    )


class TranscriptGap(Base):
    """An explicit gap marker.  It never carries fabricated transcript text."""

    __tablename__ = "transcript_gaps"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False
    )
    after_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    before_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "after_sequence",
            "before_sequence",
            "reason",
            name="uq_transcript_gaps_range",
        ),
        CheckConstraint(
            "after_sequence >= 0 AND before_sequence > after_sequence",
            name="ck_transcript_gaps_order",
        ),
        Index("ix_transcript_gaps_session", "session_id"),
    )


class EventQuarantine(Base):
    """Durable record of an invalid or conflicting event body.

    The original body is retained byte-for-byte so an operator can replay or
    repair it.  It is not a wallet record and has no financial effect.
    """

    __tablename__ = "event_quarantine"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "fingerprint",
            "reason",
            name="uq_event_quarantine_identity",
        ),
        Index("ix_event_quarantine_session", "session_id"),
        CheckConstraint(
            "sequence IS NULL OR sequence > 0",
            name="ck_event_quarantine_sequence_positive",
        ),
    )


__all__ = [
    "ProcessedEvent",
    "TranscriptSegment",
    "ProviderUsage",
    "TranscriptState",
    "TranscriptGap",
    "EventQuarantine",
]
