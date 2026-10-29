"""Concurrency, reservation hold, and wallet race tests against fresh PostgreSQL.

Follows accounting-contract.md and verification-plan.md Phase 5:
- Real PostgreSQL SELECT ... FOR UPDATE locking.
- Two sessions compete for last reservation at a concurrency barrier.
- Topup races reservation without lost update.
- Renewal cannot exceed confirmed credit.
- Unused reservation release is exact.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select

from platform_api.billing.models import CallBudget, WalletAccount
from platform_api.billing.pricing import charge_minor
from platform_api.billing.service import (
    BillingError,
    InvalidTimeReportError,
    ReservationHoldError,
    RuntimePrincipal,
    checkpoint_and_reserve,
    close_budget,
    credit_wallet,
)
from tests.support.billing import (
    assert_wallet_invariants,
    create_funded_wallet,
    create_test_session,
    create_test_user,
)
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_two_sessions_compete_for_last_reservation(fresh_pg: FreshPostgres):
    """Two concurrent sessions start at a barrier and compete for the last reservation.

    Committed outcomes must never exceed available balance.
    """
    await fresh_pg.initialize()

    # User funded with 50 paise. Rate is 200 paise/min.
    # 10 seconds costs 34 paise. Two 10s reservations need 68 paise (more than 50).
    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=50)
        session_a, budget_a = await create_test_session(db, owner_user_id=user.id, runner_id="runner-a")
        session_b, budget_b = await create_test_session(db, owner_user_id=user.id, runner_id="runner-b")

    barrier = asyncio.Barrier(2)

    async def run_reservation(session_id: uuid.UUID, runner_id: str):
        # Open separate DB session / connection per coroutine
        async with fresh_pg.new_session() as db:
            await barrier.wait()
            return await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id=runner_id),
                session_id=session_id,
                operation_id=f"grant-{runner_id}",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=10000,
                billing_slice_ms=10000,
                permit_ttl_ms=5000,
            )

    results = await asyncio.gather(
        run_reservation(session_a.id, "runner-a"),
        run_reservation(session_b.id, "runner-b"),
    )

    permit_a, permit_b = results[0], results[1]

    # Total reserved across both permits must not exceed the initial 50 paise!
    total_reserved = permit_a.reserved_minor + permit_b.reserved_minor
    assert total_reserved <= 50, f"Over-reservation occurred: total_reserved={total_reserved} > 50"

    # One got full 10,000ms (34 paise); the other got the remaining 16 paise (or truncated)
    assert (permit_a.authorized_through_ms == 10000 and permit_b.authorized_through_ms < 10000) or (
        permit_b.authorized_through_ms == 10000 and permit_a.authorized_through_ms < 10000
    )

    # Invariants check
    async with fresh_pg.new_session() as db:
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_topup_races_reservation_without_lost_update(fresh_pg: FreshPostgres):
    """Credit topup races with a checkpoint reservation without lost updates."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, budget = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

    barrier = asyncio.Barrier(2)

    async def do_topup():
        async with fresh_pg.new_session() as db:
            await barrier.wait()
            return await credit_wallet(
                db,
                actor="admin:tester",
                user_id=user.id,
                amount_minor=200,
                reason="Racing topup funding",
                operation_id="racing-topup-op",
            )

    async def do_reserve():
        async with fresh_pg.new_session() as db:
            await barrier.wait()
            return await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="racing-reserve-op",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=10000,
                billing_slice_ms=10000,
                permit_ttl_ms=5000,
            )

    await asyncio.gather(do_topup(), do_reserve())

    async with fresh_pg.new_session() as db:
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_renewal_cannot_spend_unconfirmed_credit(fresh_pg: FreshPostgres):
    """Session reservation cannot exceed confirmed available balance."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        # Fund with exactly 34 paise (cost of 10s @ 200/min)
        await create_funded_wallet(db, user_id=user.id, credited_minor=34)
        session_1, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")
        session_2, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-2")

        # Session 1 reserves 10s -> consumes all 34 paise
        p1 = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session_1.id,
            operation_id="s1-res",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
        )
        assert p1.authorized_through_ms == 10000
        assert p1.reserved_minor == 34
        assert not p1.funds_exhausted

        # Session 2 requests 10s -> 0 paise available
        p2 = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-2"),
            session_id=session_2.id,
            operation_id="s2-res",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
        )
        assert p2.authorized_through_ms == 0
        assert p2.reserved_minor == 0
        assert p2.funds_exhausted

        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unused_reservation_release_is_exact(fresh_pg: FreshPostgres):
    """When a session closes early, its remaining unused reservation is released exactly."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # 1. Reserve 10,000ms (held = 34 paise)
        p = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="init-grant",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        assert p.reserved_minor == 34

        # 2. Close session after permit expiry (at t0 + 6s) with 3,000ms proven
        # Cost of 3,000ms at 200/min: (200 * 3000 + 59999) // 60000 = 10 paise.
        close_view = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="close-op",
            expected_budget_version=2,
            cumulative_billable_ms=3000,
            stop_evidence={"reason": "user_hangup"},
            now=t0 + timedelta(seconds=6),
        )
        assert close_view.charged_minor == 10
        assert close_view.reserved_minor == 0
        assert close_view.status == "CLOSED"

        # Posted balance should now be 100 - 10 = 90 paise.
        # Reserved balance should be 0. Available = 90.
        await assert_wallet_invariants(db, user_id=user.id)

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_debit_below_reservation_rejected(fresh_pg: FreshPostgres):
    """Admin debit that would reduce posted balance below outstanding holds is rejected with 409."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        # Fund with 100 paise
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # Reserve 10,000ms (held = 34 paise, available = 66 paise)
        permit = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="chk-reserve-debit-test",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
        )
        assert permit.reserved_minor == 34

    # 1. Admin attempts debit of -80 paise (posted 100 - 80 = 20 < 34 held) -> 409
    async with fresh_pg.new_session() as db:
        with pytest.raises(ReservationHoldError, match="Adjustment would reduce posted balance below outstanding reservations"):
            await credit_wallet(
                db,
                actor="admin:tester",
                user_id=user.id,
                amount_minor=-80,
                reason="Administrative chargeback exceeding available",
                operation_id="admin-excessive-debit",
                kind="adjustment",
            )

    # Invariants prove balance was not changed
    async with fresh_pg.new_session() as db:
        wallet = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))).scalar_one()
        assert wallet.posted_minor == 100
        assert wallet.reserved_minor == 34
        await assert_wallet_invariants(db, user_id=user.id)

    # 2. Admin attempts debit of -50 paise (posted 100 - 50 = 50 >= 34 held) -> succeeds!
    async with fresh_pg.new_session() as db:
        updated = await credit_wallet(
            db,
            actor="admin:tester",
            user_id=user.id,
            amount_minor=-50,
            reason="Permitted administrative adjustment within available",
            operation_id="admin-valid-debit",
            kind="adjustment",
        )
        assert updated.posted_minor == 50
        assert updated.reserved_minor == 34
        assert updated.available_minor == 16
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_hard_end_deadline_caps_permit_expiry_and_denies_authority_after_deadline(
    fresh_pg: FreshPostgres,
):
    """Permits expire at the hard deadline, and no authority is issued after it."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=500)

        capped_session, _ = await create_test_session(
            db,
            owner_user_id=user.id,
            runner_id="runner-capped",
            created_at=t0,
            hard_end_deadline=t0 + timedelta(milliseconds=3000),
        )
        expired_session, _ = await create_test_session(
            db,
            owner_user_id=user.id,
            runner_id="runner-expired",
            created_at=t0,
            hard_end_deadline=t0 - timedelta(milliseconds=1),
        )

        late_session, _ = await create_test_session(
            db,
            owner_user_id=user.id,
            runner_id="runner-late",
            created_at=t0,
            hard_end_deadline=t0 + timedelta(milliseconds=3000),
        )

        capped = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-capped"),
            session_id=capped_session.id,
            operation_id="capped-grant",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        assert capped.authorized_through_ms == 3000
        assert capped.reserved_minor == charge_minor(200, 3000) == 10
        assert capped.expires_at == t0 + timedelta(milliseconds=3000)
        assert not capped.funds_exhausted

        late_grant = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-late"),
            session_id=late_session.id,
            operation_id="late-deadline-grant",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        assert late_grant.authorized_through_ms == 3000
        assert late_grant.expires_at == t0 + timedelta(milliseconds=3000)

        late_expired = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-late"),
            session_id=late_session.id,
            operation_id="late-deadline-expired",
            expected_budget_version=2,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0 + timedelta(seconds=4),
        )
        assert late_expired.authorized_through_ms == 0
        assert late_expired.reserved_minor == 0
        assert late_expired.funds_exhausted
        assert late_expired.expires_at == t0 + timedelta(seconds=4)

        expired = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-expired"),
            session_id=expired_session.id,
            operation_id="expired-grant",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        assert expired.authorized_through_ms == 0
        assert expired.reserved_minor == 0
        assert expired.funds_exhausted
        assert expired.expires_at == t0

        capped_close = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-capped"),
            session_id=capped_session.id,
            operation_id="capped-close",
            expected_budget_version=2,
            cumulative_billable_ms=3000,
            stop_evidence={"reason": "hard_deadline_reached"},
            now=t0 + timedelta(seconds=6),
        )
        assert capped_close.charged_minor == 10
        assert capped_close.reserved_minor == 0

        expired_close = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-expired"),
            session_id=expired_session.id,
            operation_id="expired-close",
            expected_budget_version=2,
            cumulative_billable_ms=0,
            now=t0,
        )
        assert expired_close.charged_minor == 0
        assert expired_close.reserved_minor == 0
        assert expired_close.status == "CLOSED"

        late_close = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-late"),
            session_id=late_session.id,
            operation_id="late-deadline-close",
            expected_budget_version=3,
            cumulative_billable_ms=0,
            now=t0 + timedelta(seconds=6),
        )
        assert late_close.charged_minor == 0
        assert late_close.reserved_minor == 0
        assert late_close.status == "CLOSED"

        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_settlement_cannot_charge_beyond_authorized_reservation(fresh_pg: FreshPostgres):
    """Close rejects both unauthorized time and a charge larger than the budget's held reservation."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(
            db, owner_user_id=user.id, runner_id="runner-settlement"
        )

        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-settlement"),
            session_id=session.id,
            operation_id="settlement-grant",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )

    # A report beyond the authorized cumulative ceiling is a conflict, not a charge.
    async with fresh_pg.new_session() as db:
        with pytest.raises(InvalidTimeReportError):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-settlement"),
                session_id=session.id,
                operation_id="settlement-unauthorized",
                expected_budget_version=2,
                cumulative_billable_ms=11000,
                now=t0 + timedelta(seconds=6),
            )

    # Even if stored authorization/reservation evidence is inconsistent, the
    # final charge may never exceed the amount actually held for this budget.
    async with fresh_pg.new_session() as db:
        budget = (
            await db.execute(select(CallBudget).where(CallBudget.session_id == session.id))
        ).scalar_one()
        wallet = (
            await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))
        ).scalar_one()
        budget.cumulative_authorized_ms = 60000
        budget.reserved_minor = 10
        wallet.reserved_minor = 10
        await db.commit()

    async with fresh_pg.new_session() as db:
        with pytest.raises(BillingError, match="Final charge exceeds the authorized reservation"):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-settlement"),
                session_id=session.id,
                operation_id="settlement-overheld",
                expected_budget_version=2,
                cumulative_billable_ms=60000,
                now=t0 + timedelta(seconds=6),
            )

    async with fresh_pg.new_session() as db:
        budget = (
            await db.execute(select(CallBudget).where(CallBudget.session_id == session.id))
        ).scalar_one()
        wallet = (
            await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))
        ).scalar_one()
        assert budget.status == "OPEN"
        assert budget.cumulative_proven_ms == 0
        assert budget.cumulative_charged_minor == 0
        assert budget.reserved_minor == 10
        assert wallet.posted_minor == 100
        assert wallet.reserved_minor == 10
