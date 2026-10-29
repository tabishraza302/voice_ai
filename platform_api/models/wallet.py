"""Core prepaid wallet account model for Task 0.

Follows Task 0 brief and accounting-contract.md:
- Declares WalletAccount needed for zero-credit account initialization.
- Full accounting/metering models (WalletEntry, CallBudget, BillingOperation,
  MeterCheckpoint) belong to Task 5 and will be introduced with their session FKs.
- Currency is INR; amounts are integer paise (BigInteger, no Python floats).
- Invariant: posted_minor >= reserved_minor >= 0.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


class WalletAccount(Base):
    """User prepaid balance ledger account."""

    __tablename__ = "wallet_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    posted_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
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
            "posted_minor >= reserved_minor AND reserved_minor >= 0",
            name="ck_wallet_balance_invariants",
        ),
        CheckConstraint(
            "currency = 'INR'",
            name="ck_wallet_currency_inr",
        ),
    )
