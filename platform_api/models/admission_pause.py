"""Persisted global, user, and agent admission-pause policies."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdmissionPause(Base):
    """Current persisted pause state for one bounded admission scope."""

    __tablename__ = "admission_pauses"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope: Mapped[str] = mapped_column(String(8), nullable=False)
    # For `user`, owner_user_id is the target user. For `agent`, it is the
    # agent's owner, keeping the owner-scoped relation explicit.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("platform_agents.id", ondelete="RESTRICT"),
        nullable=True,
    )
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    updated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "(scope = 'global' AND owner_user_id IS NULL AND agent_id IS NULL) OR "
            "(scope = 'user' AND owner_user_id IS NOT NULL AND agent_id IS NULL) OR "
            "(scope = 'agent' AND owner_user_id IS NOT NULL AND agent_id IS NOT NULL)",
            name="ck_admission_pauses_scope_target",
        ),
        CheckConstraint("revision > 0", name="ck_admission_pauses_revision_positive"),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 10 AND 500",
            name="ck_admission_pauses_reason_length",
        ),
        Index(
            "uq_admission_pauses_global",
            "scope",
            unique=True,
            postgresql_where=text("scope = 'global'"),
        ),
        Index(
            "uq_admission_pauses_user",
            "owner_user_id",
            unique=True,
            postgresql_where=text("scope = 'user'"),
        ),
        Index(
            "uq_admission_pauses_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("scope = 'agent'"),
        ),
        Index("ix_admission_pauses_owner", "owner_user_id"),
    )
