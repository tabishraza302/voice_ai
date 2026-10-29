"""Session admission records attached to the canonical platform session."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base
from platform_api.models.session import Session


class RunnerCapacity(Base):
    """Deployment-configured hard capacity for one local runner."""

    __tablename__ = "runner_capacities"

    runner_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    max_active_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "length(runner_id) BETWEEN 1 AND 64",
            name="ck_runner_capacities_runner_id",
        ),
        CheckConstraint(
            "max_active_sessions > 0",
            name="ck_runner_capacities_max_active_sessions",
        ),
    )


class SessionSlot(Base):
    """One reserved owner/runner execution slot for a canonical session."""

    __tablename__ = "session_slots"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    runner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint("length(runner_id) BETWEEN 1 AND 64", name="ck_session_slots_runner_id"),
        Index(
            "ix_session_slots_owner_active",
            "owner_user_id",
            postgresql_where=text("released_at IS NULL"),
        ),
        Index(
            "ix_session_slots_runner_active",
            "runner_id",
            postgresql_where=text("released_at IS NULL"),
        ),
    )


class RuntimeRegistration(Base):
    """Idempotent registration of one session on its sole assigned runner."""

    __tablename__ = "runner_registrations"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    runner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    registration_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    registered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'REGISTERED', 'RELEASE_PENDING', 'FAILED', 'RELEASED')",
            name="ck_runner_registrations_status",
        ),
        CheckConstraint("length(runner_id) BETWEEN 1 AND 64", name="ck_runner_registrations_runner_id"),
    )


class SessionParticipant(Base):
    """Owner identity and exact origin bound to a session participant."""

    __tablename__ = "session_participants"

    participant_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    auth_session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("auth_sessions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")
    origin: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "kind", name="uq_session_participants_session_kind"),
        CheckConstraint("kind = 'owner'", name="ck_session_participants_kind"),
        CheckConstraint("length(origin) BETWEEN 1 AND 512", name="ck_session_participants_origin"),
    )


class SessionJoinJti(Base):
    """Single-use join identifier; the signed bearer token is never stored."""

    __tablename__ = "session_join_jtis"

    jti: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("session_participants.participant_id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    auth_session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("auth_sessions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    runner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    config_version_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    registration_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    session_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signaling_url: Mapped[str] = mapped_column(String(512), nullable=False)
    origin: Mapped[str] = mapped_column(String(512), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_peer_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("session_id", "participant_id", "jti", name="uq_session_join_jtis_binding"),
        CheckConstraint("length(runner_id) BETWEEN 1 AND 64", name="ck_session_join_jtis_runner_id"),
        CheckConstraint("session_version >= 1", name="ck_session_join_jtis_session_version"),
        CheckConstraint("length(signaling_url) BETWEEN 1 AND 512", name="ck_session_join_jtis_signaling_url"),
        CheckConstraint("length(origin) BETWEEN 1 AND 512", name="ck_session_join_jtis_origin"),
        CheckConstraint("length(key_fingerprint) = 64", name="ck_session_join_jtis_key_fingerprint"),
        CheckConstraint("expires_at > issued_at", name="ck_session_join_jtis_expiry"),
    )


class SessionPeer(Base):
    """The sole current peer assignment for a session, replaceable after release."""

    __tablename__ = "session_peers"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    peer_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=False,
        unique=True,
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("session_participants.participant_id", ondelete="RESTRICT"),
        nullable=False,
    )
    runner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconnect_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'RELEASED', 'ENDED')",
            name="ck_session_peers_status",
        ),
        CheckConstraint("length(runner_id) BETWEEN 1 AND 64", name="ck_session_peers_runner_id"),
    )


__all__ = [
    "RunnerCapacity",
    "RuntimeRegistration",
    "Session",
    "SessionJoinJti",
    "SessionParticipant",
    "SessionPeer",
    "SessionSlot",
]
