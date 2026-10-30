"""Immutable customer tariff versions and the single current-version pointer."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionTariffRecord(Base):
    """One immutable rate and billing-window policy pinned by new sessions."""

    __tablename__ = "session_tariff_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rate_minor_per_minute: Mapped[int] = mapped_column(BigInteger, nullable=False)
    billing_slice_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    permit_ttl_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("revision", name="uq_session_tariff_versions_revision"),
        CheckConstraint("revision > 0", name="ck_session_tariff_versions_revision"),
        CheckConstraint(
            "rate_minor_per_minute >= 0",
            name="ck_session_tariff_versions_rate_nonnegative",
        ),
        CheckConstraint(
            "billing_slice_ms > 0",
            name="ck_session_tariff_versions_slice_positive",
        ),
        CheckConstraint(
            "permit_ttl_ms > 0",
            name="ck_session_tariff_versions_permit_ttl_positive",
        ),
    )


class ActiveSessionTariff(Base):
    """Singleton pointer selecting the tariff used by new sessions."""

    __tablename__ = "active_session_tariff"

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_tariff_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("session_tariff_versions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_active_session_tariff_singleton"),
    )
