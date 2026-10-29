"""Voice AI session database model.

Follows master spec III.3:
- UUID identifiers
- UTC timestamptz instants
- Explicit constraints (CheckConstraint, UniqueConstraint, ForeignKey)
- Restricted deletes to prevent cascading financial or audit loss
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    JSON,
    String,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


class Session(Base):
    """Voice AI conversation/call session."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    participant_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    tariff_snapshot: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="voice",
    )
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
    )
    version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
    )
    runner_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )
    connect_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    hard_end_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    failure_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "version >= 1",
            name="ck_sessions_version_positive",
        ),
        CheckConstraint(
            "kind IN ('voice', 'chat', 'voice_test')",
            name="ck_sessions_kind",
        ),
    )
