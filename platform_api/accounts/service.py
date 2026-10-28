"""Account initialization and lifecycle service.

Follows master contract and auth-contract.md:
- Produces async initialize_user_account(db, *, user_id: UUID, now: datetime) -> None.
- Wallet creation is idempotent by user ID and stays in the caller's transaction.
- Starts with zero credit (0 INR paise); no paid credit without explicit policy.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.models.wallet import WalletAccount


async def initialize_user_account(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    now: datetime,
) -> None:
    """Initialize account-owned wallet record in the caller's transaction.

    Idempotent by user_id. Does not commit.
    """
    stmt = select(WalletAccount).where(WalletAccount.user_id == user_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing is not None:
        return

    wallet = WalletAccount(
        user_id=user_id,
        currency="INR",
        posted_minor=0,
        reserved_minor=0,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(wallet)
    await db.flush()
