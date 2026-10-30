"""Audited owner-visibility rules and append-only transcript corrections."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TranscriptPolicy(Base):
    """Current owner-visible flag and revision for one session transcript."""

    __tablename__ = "transcript_policies"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    visible_to_owner: Mapped[bool] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_transcript_policies_revision_positive"),
    )


class TranscriptCorrection(Base):
    """Append-only replacement text linked to one consumer-owned transcript event."""

    __tablename__ = "transcript_corrections"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("transcript_segments.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    replacement_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "policy_revision > 0",
            name="ck_transcript_corrections_revision_positive",
        ),
        CheckConstraint(
            "length(trim(replacement_text)) BETWEEN 1 AND 16384",
            name="ck_transcript_corrections_text_length",
        ),
        UniqueConstraint(
            "session_id", "policy_revision", name="uq_transcript_corrections_revision"
        ),
        Index("ix_transcript_corrections_session_event", "session_id", "event_id"),
    )
