"""Prepaid billing, reservations, ledger, and checkpoint database models.

Follows accounting-contract.md:
- Currency is INR; amounts are integer paise (BigInteger, no Python floats).
- Invariant: posted_minor >= reserved_minor >= 0 on wallet_accounts.
- Append-only ledger: wallet_entries records credits, charges, refunds, adjustments.
- Call budgets: per-session budget holding reservations, proven time, cumulative charges.
- Billing operations: idempotent request/response persistence for lost-response replay.
- Meter checkpoints: synchronous durable proof of billable audio time.
"""

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
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base
from platform_api.models.wallet import WalletAccount


class WalletEntry(Base):
    """Immutable ledger entry tracking balance adjustments and session charges."""

    __tablename__ = "wallet_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    operation_id: Mapped[str] = mapped_column(
        # Globally unique ledger key. Charge rows embed the session id because
        # billing operation ids are only unique per session.
        String(256),
        nullable=False,
        unique=True,
        index=True,
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('credit', 'charge', 'refund', 'adjustment')",
            name="ck_wallet_entries_kind",
        ),
        CheckConstraint(
            "amount_minor != 0",
            name="ck_wallet_entries_non_zero",
        ),
        CheckConstraint(
            "(kind = 'charge' AND amount_minor < 0) OR "
            "(kind IN ('credit', 'refund') AND amount_minor > 0) OR "
            "(kind = 'adjustment' AND amount_minor <> 0)",
            name="ck_wallet_entries_sign",
        ),
    )


class CallBudget(Base):
    """Per-session call budget tracking retail reservations, proven time, and status."""

    __tablename__ = "call_budgets"

    session_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    pinned_rate: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cumulative_proven_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    cumulative_authorized_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    cumulative_charged_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    reserved_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    permit_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="OPEN"
    )
    waived_unknown_usage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    stop_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "pinned_rate >= 0",
            name="ck_call_budgets_rate_non_negative",
        ),
        CheckConstraint(
            "cumulative_proven_ms >= 0",
            name="ck_call_budgets_proven_non_negative",
        ),
        CheckConstraint(
            "cumulative_authorized_ms >= cumulative_proven_ms",
            name="ck_call_budgets_auth_ge_proven",
        ),
        CheckConstraint(
            "cumulative_charged_minor >= 0",
            name="ck_call_budgets_charged_non_negative",
        ),
        CheckConstraint(
            "reserved_minor >= 0",
            name="ck_call_budgets_reserved_non_negative",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'CLOSED')",
            name="ck_call_budgets_status",
        ),
    )


class BillingOperation(Base):
    """Idempotent record of billing requests, hashes, and responses."""

    __tablename__ = "billing_operations"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    operation_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_body: Mapped[dict] = mapped_column(JSON, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "operation_id",
            name="uq_billing_operations_session_op",
        ),
        Index(
            "uq_billing_operations_null_session_op",
            "operation_id",
            unique=True,
            postgresql_where=text("session_id IS NULL"),
        ),
    )


class MeterCheckpoint(Base):
    """Synchronous durable checkpoint evidence for billable active time."""

    __tablename__ = "meter_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(
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
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cumulative_proven_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Immutable evidence snapshot: a checkpoint must remain interpretable even
    # if the session/config/tariff records change later.
    pinned_rate: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tariff_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    session_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    config_version_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "operation_id",
            name="uq_meter_checkpoints_session_op",
        ),
        CheckConstraint(
            "cumulative_proven_ms >= 0",
            name="ck_meter_checkpoints_proven_non_negative",
        ),
        CheckConstraint(
            "pinned_rate >= 0",
            name="ck_meter_checkpoints_rate_non_negative",
        ),
        CheckConstraint(
            "session_version >= 1",
            name="ck_meter_checkpoints_session_version_positive",
        ),
    )


__all__ = [
    "WalletAccount",
    "WalletEntry",
    "CallBudget",
    "BillingOperation",
    "MeterCheckpoint",
]
