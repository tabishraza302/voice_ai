"""Durable PostgreSQL records for snapshot-pinned text chat tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


class ChatTestSession(Base):
    """One immutable compiled snapshot with a serialized, durable turn ledger."""

    __tablename__ = "platform_chat_test_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("platform_agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(), nullable=False
    )
    flow_state_json: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(), nullable=False
    )
    trace_json: Mapped[list[dict[str, Any]]] = mapped_column(
        postgresql.JSONB(), nullable=False, default=list
    )
    turn_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_turn_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    pending_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    pending_lease_token: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    pending_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_progress"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint("length(snapshot_hash) = 64", name="ck_chat_test_snapshot_hash"),
        CheckConstraint(
            "snapshot_kind IN ('draft', 'published')",
            name="ck_chat_test_snapshot_kind",
        ),
        CheckConstraint("turn_sequence >= 0", name="ck_chat_test_turn_sequence"),
        CheckConstraint(
            "pending_turn_sequence IS NULL OR pending_turn_sequence = turn_sequence",
            name="ck_chat_test_pending_turn_sequence",
        ),
        CheckConstraint(
            "outcome IN ('in_progress', 'completed', 'partial', 'aborted', 'failed')",
            name="ck_chat_test_outcome",
        ),
        Index(
            "ix_chat_test_owner_agent_updated",
            "owner_user_id",
            "agent_id",
            "updated_at",
        ),
    )


class ChatMessage(Base):
    """A user turn or assistant reply committed before it is returned to a client."""

    __tablename__ = "platform_chat_test_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    test_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("platform_chat_test_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint(
            "test_id", "turn_sequence", "role", name="uq_chat_test_message_turn_role"
        ),
        CheckConstraint("turn_sequence >= 1", name="ck_chat_test_message_sequence"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_test_message_role"),
        CheckConstraint(
            "length(content) <= 16384", name="ck_chat_test_message_content_length"
        ),
        Index("ix_chat_test_message_test_sequence", "test_id", "turn_sequence"),
    )
