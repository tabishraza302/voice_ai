"""Public deployment links and hashed, session-scoped guest capabilities."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


class PublicDeployment(Base):
    """Opaque public link tied to exactly one enabled agent and its owner."""

    __tablename__ = "public_deployments"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("platform_agents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    origin: Mapped[str] = mapped_column(String(512), nullable=False)
    disclosure_version: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_public_deployments_owner", "owner_user_id"),
        Index("ix_public_deployments_agent", "agent_id"),
        CheckConstraint(
            "length(public_id) BETWEEN 32 AND 64",
            name="ck_public_deployments_public_id_length",
        ),
        CheckConstraint(
            "length(origin) BETWEEN 1 AND 512",
            name="ck_public_deployments_origin_length",
        ),
        CheckConstraint(
            "length(disclosure_version) BETWEEN 1 AND 64",
            name="ck_public_deployments_disclosure_version_length",
        ),
        CheckConstraint(
            "(enabled AND disabled_at IS NULL) OR (NOT enabled AND disabled_at IS NOT NULL)",
            name="ck_public_deployments_disabled_timestamp",
        ),
    )


class PublicGuestCapability(Base):
    """Hashed bearer capability limited to one guest participant and session."""

    __tablename__ = "public_guest_capabilities"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("public_deployments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("session_participants.participant_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    capability_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_public_guest_capabilities_deployment", "deployment_id"),
        CheckConstraint(
            "length(capability_digest) = 64",
            name="ck_public_guest_capabilities_digest_length",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_public_guest_capabilities_expiry",
        ),
    )


__all__ = ["PublicDeployment", "PublicGuestCapability"]
