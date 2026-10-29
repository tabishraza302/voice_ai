"""Durable PostgreSQL queue records for draft-only agent generation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base

MAX_GENERATION_ATTEMPTS = 2


class GenerationJob(Base):
    """Owner-scoped generation request and its validated, non-published result."""

    __tablename__ = "platform_generation_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    generation_model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    language: Mapped[str] = mapped_column(String(2), nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    proposal_json: Mapped[dict[str, Any] | None] = mapped_column(
        postgresql.JSONB(), nullable=True
    )
    unknown_business_facts_json: Mapped[list[str]] = mapped_column(
        postgresql.JSONB(), nullable=False, default=list
    )
    validation_warnings_json: Mapped[list[dict[str, Any]]] = mapped_column(
        postgresql.JSONB(), nullable=False, default=list
    )
    usage_json: Mapped[dict[str, int | None] | None] = mapped_column(
        postgresql.JSONB(), nullable=True
    )
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
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'CANCELLING', 'COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED')",
            name="ck_generation_job_status",
        ),
        CheckConstraint("length(trim(requirement)) > 0", name="ck_generation_job_requirement"),
        CheckConstraint("length(requirement) <= 8192", name="ck_generation_job_requirement_length"),
        CheckConstraint("language IN ('en', 'hi')", name="ck_generation_job_language"),
        CheckConstraint("token_budget BETWEEN 64 AND 4096", name="ck_generation_job_token_budget"),
        CheckConstraint(
            f"attempt_count BETWEEN 0 AND {MAX_GENERATION_ATTEMPTS}",
            name="ck_generation_job_attempt_count",
        ),
        CheckConstraint("length(generation_model_id) BETWEEN 1 AND 128", name="ck_generation_job_model_id"),
        Index("ix_generation_job_queue", "status", "created_at"),
        Index("ix_generation_job_owner_created", "owner_user_id", "created_at"),
    )
