"""Session admission records attached to the canonical platform session."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


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
    """Owner or anonymous guest identity bound to one exact origin."""

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
    auth_session_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("auth_sessions.id", ondelete="RESTRICT"),
        nullable=True,
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
        CheckConstraint(
            "kind IN ('owner', 'guest')",
            name="ck_session_participants_kind",
        ),
        CheckConstraint(
            "(kind = 'owner' AND auth_session_id IS NOT NULL) OR "
            "(kind = 'guest' AND auth_session_id IS NULL)",
            name="ck_session_participants_auth_binding",
        ),
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
    auth_session_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("auth_sessions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    participant_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="owner",
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
        CheckConstraint(
            "participant_kind IN ('owner', 'guest')",
            name="ck_session_join_jtis_participant_kind",
        ),
        CheckConstraint(
            "(participant_kind = 'owner' AND auth_session_id IS NOT NULL) OR "
            "(participant_kind = 'guest' AND auth_session_id IS NULL)",
            name="ck_session_join_jtis_auth_binding",
        ),
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


class FlowRuntimeState(Base):
    """Latest durable reducer snapshot for one canonical platform session."""

    __tablename__ = "flow_runtime_states"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    flow_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[dict] = mapped_column(postgresql.JSONB, nullable=False)
    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "length(flow_hash) = 64",
            name="ck_flow_runtime_states_flow_hash",
        ),
        CheckConstraint(
            "event_count >= 0",
            name="ck_flow_runtime_states_event_count",
        ),
    )


class FlowRuntimeEvent(Base):
    """Append-only event and resulting reduction committed as one sequence."""

    __tablename__ = "flow_runtime_events"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reduction_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    event_json: Mapped[dict] = mapped_column(postgresql.JSONB, nullable=False)
    reduction_json: Mapped[dict] = mapped_column(postgresql.JSONB, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "event_id",
            name="uq_flow_runtime_events_session_event",
        ),
        CheckConstraint(
            "sequence >= 1",
            name="ck_flow_runtime_events_sequence",
        ),
        CheckConstraint(
            "length(event_id) BETWEEN 1 AND 256",
            name="ck_flow_runtime_events_event_id",
        ),
        CheckConstraint(
            "length(event_digest) = 64 AND length(reduction_digest) = 64",
            name="ck_flow_runtime_events_digests",
        ),
    )


class CommittedPlaybackReceipt(Base):
    """Verifier-issued sample evidence committed with its reducer event."""

    __tablename__ = "committed_playback_receipts"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
    )
    evidence_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    transport_epoch: Mapped[str] = mapped_column(String(256), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(256), nullable=False)
    generation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    manifest_id: Mapped[str] = mapped_column(String(256), nullable=False)
    fragment_id: Mapped[str] = mapped_column(String(256), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    peer_id: Mapped[str] = mapped_column(String(256), nullable=False)
    verifier_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    sample_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sample_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    frame_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    render_sample_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "event_id"],
            ["flow_runtime_events.session_id", "flow_runtime_events.event_id"],
            ondelete="RESTRICT",
            name="fk_committed_playback_receipts_event",
        ),
        UniqueConstraint(
            "session_id",
            "event_id",
            name="uq_committed_playback_receipts_event",
        ),
        Index(
            "ix_committed_playback_receipts_epoch_samples",
            "session_id",
            "transport_epoch",
            "sample_start",
            "sample_end",
        ),
        CheckConstraint(
            "length(evidence_id) BETWEEN 1 AND 128",
            name="ck_committed_playback_receipts_evidence_id",
        ),
        CheckConstraint(
            "sample_start >= 0 AND sample_end > sample_start",
            name="ck_committed_playback_receipts_sample_range",
        ),
        CheckConstraint(
            "sample_rate BETWEEN 8000 AND 192000",
            name="ck_committed_playback_receipts_sample_rate",
        ),
        CheckConstraint(
            "frame_start >= 0 AND frame_end > frame_start",
            name="ck_committed_playback_receipts_frame_range",
        ),
        CheckConstraint(
            "render_sample_rate BETWEEN 8000 AND 192000",
            name="ck_committed_playback_receipts_render_rate",
        ),
        CheckConstraint(
            "length(content_hash) = 64 AND length(capability_report_hash) = 64 "
            "AND length(receipt_digest) = 64",
            name="ck_committed_playback_receipts_digests",
        ),
        CheckConstraint(
            "length(transport_epoch) BETWEEN 1 AND 256 "
            "AND length(turn_id) BETWEEN 1 AND 256 "
            "AND length(generation_id) BETWEEN 1 AND 256 "
            "AND length(manifest_id) BETWEEN 1 AND 256 "
            "AND length(fragment_id) BETWEEN 1 AND 256 "
            "AND length(peer_id) BETWEEN 1 AND 256 "
            "AND length(verifier_identity) BETWEEN 1 AND 256",
            name="ck_committed_playback_receipts_binding_text",
        ),
    )


class RuntimeStopRecordRow(Base):
    """Immutable idempotent runner stop proof for one session version."""

    __tablename__ = "runtime_stop_records"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    session_version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attestation_json: Mapped[dict] = mapped_column(postgresql.JSONB, nullable=False)
    expected_budget_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cumulative_billable_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    record_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "session_version >= 1",
            name="ck_runtime_stop_records_session_version",
        ),
        CheckConstraint(
            "expected_budget_version >= 1",
            name="ck_runtime_stop_records_budget_version",
        ),
        CheckConstraint(
            "cumulative_billable_ms >= 0",
            name="ck_runtime_stop_records_billable_ms",
        ),
        CheckConstraint(
            "length(record_digest) = 64",
            name="ck_runtime_stop_records_digest",
        ),
    )


__all__ = [
    "CommittedPlaybackReceipt",
    "FlowRuntimeEvent",
    "FlowRuntimeState",
    "RunnerCapacity",
    "RuntimeRegistration",
    "RuntimeStopRecordRow",
    "SessionJoinJti",
    "SessionParticipant",
    "SessionPeer",
    "SessionSlot",
]
