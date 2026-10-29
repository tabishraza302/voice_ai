"""Shared test helpers and verification fixtures for billing and ledger tests.

Implements the contract specified in verification-plan.md Phase 5 and accounting-contract.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.billing.models import CallBudget, WalletAccount, WalletEntry
from platform_api.billing.pricing import charge_minor
from platform_api.models.auth import User
from platform_api.models.session import Session


def reference_cost(rate: int, cumulative_ms: int) -> int:
    """Independent specification reference oracle using Decimal ROUND_CEILING."""
    if rate < 0 or cumulative_ms < 0:
        raise ValueError("negative rate/time")
    if rate == 0 or cumulative_ms == 0:
        return 0
    return int(
        (Decimal(rate) * Decimal(cumulative_ms) / Decimal(60000)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


async def create_test_user(
    db: AsyncSession,
    *,
    email: str | None = None,
    role: str = "user",
    display_name: str = "Test User",
) -> User:
    """Create and persist an active verified test user."""
    now = datetime.now(timezone.utc)
    uid = uuid.uuid4()
    actual_email = email or f"user-{uid.hex[:8]}@example.com"
    user = User(
        id=uid,
        email=actual_email,
        email_key=actual_email.lower(),
        display_name=display_name,
        password_hash="scrypt$test",
        role=role,
        status="active",
        email_verified_at=now,
        preferred_language="en",
        timezone="Asia/Kolkata",
        low_balance_notifications=True,
        auth_version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    await db.commit()
    return user


async def create_funded_wallet(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    credited_minor: int,
) -> WalletAccount:
    """Create wallet with an initial positive balance and corresponding ledger entry."""
    now = datetime.now(timezone.utc)
    wallet = WalletAccount(
        user_id=user_id,
        currency="INR",
        posted_minor=credited_minor,
        reserved_minor=0,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(wallet)

    if credited_minor > 0:
        entry = WalletEntry(
            id=uuid.uuid4(),
            user_id=user_id,
            kind="credit",
            amount_minor=credited_minor,
            session_id=None,
            operation_id=f"init-credit-{uuid.uuid4().hex[:12]}",
            actor="test:setup",
            reason="Initial test funding",
            created_at=now,
        )
        db.add(entry)

    await db.commit()
    return wallet


async def create_test_session(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    runner_id: str = "runner-1",
    pinned_rate: int = 200,
    state: str = "ACTIVE",
    hard_end_deadline: datetime | None = None,
    created_at: datetime | None = None,
) -> tuple[Session, CallBudget]:
    """Create a test session and its associated open call budget."""
    now = created_at or datetime.now(timezone.utc)
    sid = uuid.uuid4()
    session = Session(
        id=sid,
        owner_user_id=owner_user_id,
        runner_id=runner_id,
        kind="voice",
        state=state,
        version=1,
        hard_end_deadline=hard_end_deadline,
        created_at=now,
    )
    budget = CallBudget(
        session_id=sid,
        owner_user_id=owner_user_id,
        pinned_rate=pinned_rate,
        cumulative_proven_ms=0,
        cumulative_authorized_ms=0,
        cumulative_charged_minor=0,
        reserved_minor=0,
        permit_expiry=None,
        version=1,
        status="OPEN",
        waived_unknown_usage=False,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    await db.flush()
    db.add(budget)
    await db.commit()
    return session, budget


async def assert_wallet_invariants(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> None:
    """Verify all ledger, wallet, and budget invariants for a given user.

    Invariants:
    1. wallet.posted_minor >= wallet.reserved_minor >= 0
    2. wallet.posted_minor == sum(wallet_entries.amount_minor)
    3. wallet.reserved_minor == sum(open call_budgets.reserved_minor)
    4. For every budget: cumulative_charged_minor == charge_minor(rate, proven_ms)
    5. For every budget: 0 <= proven_ms <= authorized_ms
    6. For closed budgets: reserved_minor == 0
    """
    stmt_wallet = select(WalletAccount).where(WalletAccount.user_id == user_id)
    wallet = (await db.execute(stmt_wallet)).scalar_one_or_none()
    assert wallet is not None, f"Wallet for {user_id} must exist"

    # Invariant 1
    assert wallet.posted_minor >= wallet.reserved_minor >= 0, (
        f"Wallet balance invariant violated: posted={wallet.posted_minor}, "
        f"reserved={wallet.reserved_minor}"
    )

    # Invariant 2: posted == sum(entries)
    stmt_entries = select(WalletEntry).where(WalletEntry.user_id == user_id)
    entries = list((await db.execute(stmt_entries)).scalars().all())
    expected_posted = sum(e.amount_minor for e in entries)
    assert wallet.posted_minor == expected_posted, (
        f"Ledger mismatch: wallet posted={wallet.posted_minor}, "
        f"sum of entries={expected_posted}"
    )

    # Invariant 3: reserved == sum(open call_budgets.reserved)
    stmt_budgets = select(CallBudget).where(CallBudget.owner_user_id == user_id)
    budgets = list((await db.execute(stmt_budgets)).scalars().all())
    open_reserved = sum(b.reserved_minor for b in budgets if b.status == "OPEN")
    assert wallet.reserved_minor == open_reserved, (
        f"Reservation mismatch: wallet reserved={wallet.reserved_minor}, "
        f"sum of open budgets={open_reserved}"
    )

    # Invariants 4, 5, 6 for budgets
    for b in budgets:
        expected_charged = charge_minor(b.pinned_rate, b.cumulative_proven_ms)
        assert b.cumulative_charged_minor == expected_charged, (
            f"Budget {b.session_id} charged mismatch: charged={b.cumulative_charged_minor}, "
            f"expected={expected_charged}"
        )
        assert 0 <= b.cumulative_proven_ms <= b.cumulative_authorized_ms, (
            f"Budget {b.session_id} time mismatch: proven={b.cumulative_proven_ms}, "
            f"authorized={b.cumulative_authorized_ms}"
        )
        if b.status == "CLOSED":
            assert b.reserved_minor == 0, (
                f"Closed budget {b.session_id} has non-zero reservation: {b.reserved_minor}"
            )
