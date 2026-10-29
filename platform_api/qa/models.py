"""PostgreSQL records for bounded, snapshot-pinned Flow QA jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


class FlowQAJob(Base):
    """Owner-scoped job with its immutable graph snapshot and bounded artifacts."""

    __tablename__ = "platform_flow_qa_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("platform_agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB(), nullable=False)
    draft_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flow_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    utterance_model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    judge_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scenario_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rubric_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="flow-qa-rubric/1"
    )
    models_json: Mapped[dict[str, str | None]] = mapped_column(
        postgresql.JSONB(), nullable=False
    )
    scenarios_json: Mapped[list[dict[str, Any]]] = mapped_column(
        postgresql.JSONB(), nullable=False, default=list
    )
    results_json: Mapped[list[dict[str, Any]]] = mapped_column(
        postgresql.JSONB(), nullable=False, default=list
    )
    suggestions_json: Mapped[list[dict[str, Any]]] = mapped_column(
        postgresql.JSONB(), nullable=False, default=list
    )
    artifacts_json: Mapped[list[dict[str, Any]]] = mapped_column(
        postgresql.JSONB(), nullable=False, default=list
    )
    coverage_json: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(), nullable=False, default=dict
    )
    usage_json: Mapped[dict[str, int | None] | None] = mapped_column(
        postgresql.JSONB(), nullable=True
    )
    next_scenario_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED')",
            name="ck_flow_qa_job_status",
        ),
        CheckConstraint(
            "snapshot_kind IN ('draft', 'published')", name="ck_flow_qa_snapshot_kind"
        ),
        CheckConstraint("length(snapshot_hash) = 64", name="ck_flow_qa_snapshot_hash"),
        CheckConstraint("length(flow_hash) = 64", name="ck_flow_qa_flow_hash"),
        CheckConstraint("scenario_count BETWEEN 1 AND 64", name="ck_flow_qa_scenario_count"),
        CheckConstraint("max_turns BETWEEN 1 AND 50", name="ck_flow_qa_max_turns"),
        CheckConstraint("max_tokens BETWEEN 64 AND 16384", name="ck_flow_qa_max_tokens"),
        CheckConstraint(
            "max_runtime_seconds BETWEEN 1 AND 300", name="ck_flow_qa_runtime_seconds"
        ),
        CheckConstraint("next_scenario_index >= 0", name="ck_flow_qa_next_scenario_index"),
        CheckConstraint(
            "length(utterance_model_id) BETWEEN 1 AND 128",
            name="ck_flow_qa_utterance_model_id",
        ),
        CheckConstraint(
            "length(runtime_model_id) BETWEEN 1 AND 128",
            name="ck_flow_qa_runtime_model_id",
        ),
        Index("ix_flow_qa_job_queue", "status", "created_at"),
        Index("ix_flow_qa_job_owner_created", "owner_user_id", "created_at"),
        Index("ix_flow_qa_job_agent_created", "agent_id", "created_at"),
    )

