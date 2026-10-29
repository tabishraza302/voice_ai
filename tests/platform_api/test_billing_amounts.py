"""Unit tests for pricing arithmetic, rounding policy, and input validation.

Follows accounting-contract.md and verification-plan.md Phase 5:
- charge(t, r) = (r * t + 59_999) // 60_000
- Pure integer pricing; no float conversion.
- Rejection of fractional amounts, booleans, wrong currencies, and 64-bit overflow.
- Strict parity with independent Decimal ROUND_CEILING reference oracle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from platform_api.billing.models import CallBudget, WalletAccount, WalletEntry
from platform_api.billing.pricing import MAX_BIGINT, charge_minor
from platform_api.billing.schemas import (
    AdminWalletAdjustmentRequest,
    BudgetPermit,
    BudgetView,
    CheckpointRequest,
    CloseBudgetRequest,
    WalletView,
    decode_stored_response,
)
from platform_api.billing.service import (
    BillingValidationError,
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
    reference_cost,
)
from tests.support.postgres import FreshPostgres

def test_retail_rounding_matches_policy():
    """Verify standard retail price and rounding vectors from contract and oracle."""
    # Disclosed examples
    assert charge_minor(200, 10000) == 34
    assert charge_minor(200, 30000) == 100
    assert charge_minor(200, 300) == 1
    assert charge_minor(0, 999999) == 0
    assert charge_minor(200, 0) == 0

    # 3x 10-second calls need 102 paise
    three_independent_calls = 3 * charge_minor(200, 10000)
    assert three_independent_calls == 102

    # Mixed rates
    low_rate = charge_minor(100, 10000)   # (100 * 10000 + 59999) // 60000 = 17
    high_rate = charge_minor(300, 10000)  # (300 * 10000 + 59999) // 60000 = 50
    assert low_rate == 17
    assert high_rate == 50
    assert low_rate + high_rate == 67


def test_money_rejects_fractional_or_wrong_currency_input():
    """Float, boolean, and invalid types must be rejected across functions and schemas."""
    # 1. charge_minor type checking
    with pytest.raises(TypeError):
        charge_minor(200.5, 1000)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        charge_minor(200, 1000.5)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        charge_minor(True, 1000)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        charge_minor(200, False)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        charge_minor("200", 1000)  # type: ignore[arg-type]

    # 2. CheckpointRequest rejection of strings, floats, and booleans
    with pytest.raises(ValidationError):
        CheckpointRequest(
            operation_id="op-1",
            expected_budget_version="1",  # type: ignore[arg-type]
            cumulative_billable_ms=1000,
            requested_future_ms=1000,
        )

    with pytest.raises(ValidationError):
        CheckpointRequest(
            operation_id="op-1",
            expected_budget_version=1,
            cumulative_billable_ms="0",  # type: ignore[arg-type]
            requested_future_ms=1000,
        )

    with pytest.raises(ValidationError):
        CheckpointRequest(
            operation_id="op-1",
            expected_budget_version=1,
            cumulative_billable_ms="1000",  # type: ignore[arg-type]
            requested_future_ms=1000,
        )

    with pytest.raises(ValidationError):
        CheckpointRequest(
            operation_id="op-1",
            expected_budget_version=1,
            cumulative_billable_ms=1000,
            requested_future_ms="1000",  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        CheckpointRequest(
            operation_id="op-1",
            expected_budget_version=1,
            cumulative_billable_ms=1000.5,  # type: ignore[arg-type]
            requested_future_ms=1000,
        )

    with pytest.raises(ValidationError):
        CheckpointRequest(
            operation_id="op-1",
            expected_budget_version=1,
            cumulative_billable_ms=1000,
            requested_future_ms=True,  # type: ignore[arg-type]
        )

    # 3. CloseBudgetRequest rejection of strings, floats, and booleans
    with pytest.raises(ValidationError):
        CloseBudgetRequest(
            operation_id="op-1",
            expected_budget_version="1",  # type: ignore[arg-type]
            cumulative_billable_ms=0,
        )

    with pytest.raises(ValidationError):
        CloseBudgetRequest(
            operation_id="op-1",
            expected_budget_version=1,
            cumulative_billable_ms="0",  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        CloseBudgetRequest(
            operation_id="op-1",
            expected_budget_version=1,
            cumulative_billable_ms="500",  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        CloseBudgetRequest(
            operation_id="op-1",
            expected_budget_version=1,
            cumulative_billable_ms=500.25,  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        CloseBudgetRequest(
            operation_id="op-1",
            expected_budget_version=1,
            cumulative_billable_ms=False,  # type: ignore[arg-type]
        )

    # 4. AdminWalletAdjustmentRequest rejection of zero, string zero, string numbers, floats, and booleans
    with pytest.raises(ValidationError):
        AdminWalletAdjustmentRequest(
            amount_minor="0",  # type: ignore[arg-type]
            reason="Valid 10-char reason",
        )

    with pytest.raises(ValidationError):
        AdminWalletAdjustmentRequest(
            amount_minor="100",  # type: ignore[arg-type]
            reason="Valid 10-char reason",
        )

    with pytest.raises(ValidationError):
        AdminWalletAdjustmentRequest(
            amount_minor=100.5,  # type: ignore[arg-type]
            reason="Valid 10-char reason",
        )

    with pytest.raises(ValidationError):
        AdminWalletAdjustmentRequest(
            amount_minor=0,
            reason="Valid 10-char reason",
        )

    with pytest.raises(ValidationError):
        AdminWalletAdjustmentRequest(
            amount_minor=True,  # type: ignore[arg-type]
            reason="Valid 10-char reason",
        )

    # 5. WalletView rejects wrong currency
    with pytest.raises(ValidationError):
        WalletView(
            user_id="00000000-0000-0000-0000-000000000001",  # type: ignore[arg-type]
            currency="USD",  # type: ignore[arg-type]
            posted_minor=100,
            reserved_minor=0,
            available_minor=100,
            version=1,
            updated_at="2026-10-28T00:00:00Z",  # type: ignore[arg-type]
        )


def test_negative_and_overflow_inputs_rejected():
    """Negative values and inputs exceeding signed 64-bit int bounds are rejected."""
    with pytest.raises(ValueError, match="non-negative"):
        charge_minor(-1, 1000)

    with pytest.raises(ValueError, match="non-negative"):
        charge_minor(200, -50)

    with pytest.raises(ValueError, match="signed 64-bit"):
        charge_minor(MAX_BIGINT + 1, 1000)

    with pytest.raises(ValueError, match="signed 64-bit"):
        charge_minor(200, MAX_BIGINT + 1)


def test_independent_oracle_grid_equivalence():
    """Verify integer charge_minor matches Decimal ROUND_CEILING oracle across a parameter grid."""
    rates = [0, 1, 50, 100, 200, 333, 500, 1000, 5000, 10000]
    durations = [0, 1, 299, 300, 301, 1000, 9999, 10000, 10001, 30000, 59999, 60000, 60001, 120000]

    for rate in rates:
        for duration in durations:
            production_val = charge_minor(rate, duration)
            oracle_val = reference_cost(rate, duration)
            assert production_val == oracle_val, (
                f"Mismatch for rate={rate}, duration={duration}: "
                f"charge_minor={production_val}, oracle={oracle_val}"
            )


def test_decode_stored_response_json_mode():
    """Verify decode_stored_response safely parses ISO timestamps and UUID strings under strict mode."""
    import uuid
    from datetime import datetime, timezone

    uid_str = str(uuid.uuid4())
    ts_str = "2026-10-28T12:00:00Z"

    wallet_dict = {
        "user_id": uid_str,
        "currency": "INR",
        "posted_minor": 100,
        "reserved_minor": 34,
        "available_minor": 66,
        "version": 2,
        "updated_at": ts_str,
    }

    view = decode_stored_response(WalletView, wallet_dict)
    assert isinstance(view, WalletView)
    assert isinstance(view.user_id, uuid.UUID)
    assert str(view.user_id) == uid_str
    assert isinstance(view.updated_at, datetime)
    assert view.posted_minor == 100

    permit_dict = {
        "session_id": uid_str,
        "budget_version": 2,
        "proven_ms": 0,
        "authorized_through_ms": 10000,
        "reserved_minor": 34,
        "permit_id": str(uuid.uuid4()),
        "expires_at": ts_str,
        "funds_exhausted": False,
    }
    permit = decode_stored_response(BudgetPermit, permit_dict)
    assert isinstance(permit, BudgetPermit)
    assert isinstance(permit.session_id, uuid.UUID)
    assert isinstance(permit.expires_at, datetime)

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_service_boundary_three_ten_second_calls_vs_cumulative(fresh_pg: FreshPostgres):
    """Three independent 10-second calls charge 102 paise total, while one 30-second call charges 100 paise."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    # User A runs 3 separate 10-second calls
    async with fresh_pg.new_session() as db:
        user_a = await create_test_user(db, email="three_calls@example.com")
        await create_funded_wallet(db, user_id=user_a.id, credited_minor=200)

        for i in range(1, 4):
            sess, _ = await create_test_session(db, owner_user_id=user_a.id, runner_id=f"runner-3x-{i}", pinned_rate=200)
            p = await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id=f"runner-3x-{i}"),
                session_id=sess.id,
                operation_id=f"chk-3x-{i}",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=10000,
                billing_slice_ms=10000,
                permit_ttl_ms=5000,
                now=t0,
            )
            assert p.reserved_minor == 34
            v = await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id=f"runner-3x-{i}"),
                session_id=sess.id,
                operation_id=f"close-3x-{i}",
                expected_budget_version=2,
                cumulative_billable_ms=10000,
                stop_evidence={"reason": f"call_{i}_completed"},
                now=t0 + timedelta(seconds=6),
            )
            # Each 10s call costs 34 paise
            assert v.charged_minor == 34
            assert v.reserved_minor == 0

        # Total charged across 3 separate calls is 3 * 34 = 102 paise
        # Remaining balance is 200 - 102 = 98 paise
        wallet_a = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user_a.id))).scalar_one()
        assert wallet_a.posted_minor == 98
        assert wallet_a.reserved_minor == 0
        await assert_wallet_invariants(db, user_id=user_a.id)

    # User B runs one continuous 30-second call
    async with fresh_pg.new_session() as db:
        user_b = await create_test_user(db, email="one_call@example.com")
        await create_funded_wallet(db, user_id=user_b.id, credited_minor=200)

        sess_b, _ = await create_test_session(db, owner_user_id=user_b.id, runner_id="runner-30s", pinned_rate=200)
        p_b = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-30s"),
            session_id=sess_b.id,
            operation_id="chk-30s-init",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=30000,
            billing_slice_ms=30000,
            permit_ttl_ms=5000,
            now=t0,
        )
        # 30s reservation: (200 * 30000 + 59999) // 60000 = 100 paise
        assert p_b.reserved_minor == 100

        v_b = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-30s"),
            session_id=sess_b.id,
            operation_id="close-30s",
            expected_budget_version=2,
            cumulative_billable_ms=30000,
            stop_evidence={"reason": "call_30s_completed"},
            now=t0 + timedelta(seconds=6),
        )
        assert v_b.charged_minor == 100
        assert v_b.reserved_minor == 0

        # One cumulative 30s call costs 100 paise (saving 2 paise vs 3 separate calls)
        wallet_b = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user_b.id))).scalar_one()
        assert wallet_b.posted_minor == 100
        assert wallet_b.reserved_minor == 0
        await assert_wallet_invariants(db, user_id=user_b.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_service_boundary_subsecond_accumulation(fresh_pg: FreshPostgres):
    """Subsecond active time increments accumulate cumulative charge without per-chunk rounding."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db, email="subsecond@example.com")
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        sess, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-subsecond", pinned_rate=200)

        # Initial grant 5000ms
        p = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-subsecond"),
            session_id=sess.id,
            operation_id="chk-sub-0",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=5000,
            billing_slice_ms=5000,
            permit_ttl_ms=5000,
            now=t0,
        )

        # Sequence of subsecond checkpoints:
        # 250ms:  ceil(200 * 250 / 60000)  = ceil(0.833) = 1 paise
        # 500ms:  ceil(200 * 500 / 60000)  = ceil(1.666) = 2 paise
        # 750ms:  ceil(200 * 750 / 60000)  = ceil(2.500) = 3 paise
        # 1200ms: ceil(200 * 1200 / 60000) = ceil(4.000) = 4 paise
        subsecond_checkpoints = [(250, 1), (500, 2), (750, 3), (1200, 4)]
        ver = p.budget_version

        for ms, expected_cumulative_cost in subsecond_checkpoints:
            t_chk = t0 + timedelta(milliseconds=ms)
            p = await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-subsecond"),
                session_id=sess.id,
                operation_id=f"chk-sub-{ms}",
                expected_budget_version=ver,
                cumulative_billable_ms=ms,
                requested_future_ms=2000,
                billing_slice_ms=5000,
                permit_ttl_ms=5000,
                now=t_chk,
            )
            ver = p.budget_version
            assert p.proven_ms == ms

            # Check wallet posted balance reflects exact cumulative cost
            w = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))).scalar_one()
            assert w.posted_minor == 100 - expected_cumulative_cost
            await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_service_boundary_mixed_rates(fresh_pg: FreshPostgres):
    """Concurrent sessions on different pinned rates compute distinct charges against same wallet."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db, email="mixed_rates@example.com")
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)

        # Session Low @ 100 paise/min
        sess_low, _ = await create_test_session(db, owner_user_id=user.id, runner_id="r-low", pinned_rate=100)
        # Session High @ 300 paise/min
        sess_high, _ = await create_test_session(db, owner_user_id=user.id, runner_id="r-high", pinned_rate=300)

        # Checkpoint both for 10,000ms
        p_low = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="r-low"),
            session_id=sess_low.id,
            operation_id="chk-low-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        # 10s @ 100/min: (100 * 10000 + 59999) // 60000 = 17 paise
        assert p_low.reserved_minor == 17

        p_high = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="r-high"),
            session_id=sess_high.id,
            operation_id="chk-high-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        # 10s @ 300/min: (300 * 10000 + 59999) // 60000 = 50 paise
        assert p_high.reserved_minor == 50

        # Total held: 17 + 50 = 67 paise
        w = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))).scalar_one()
        assert w.reserved_minor == 67
        assert w.posted_minor == 200
        await assert_wallet_invariants(db, user_id=user.id)

        # Close both after 10s proven
        t_close = t0 + timedelta(seconds=6)
        v_low = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="r-low"),
            session_id=sess_low.id,
            operation_id="close-low",
            expected_budget_version=2,
            cumulative_billable_ms=10000,
            stop_evidence={"reason": "normal_stop"},
            now=t_close,
        )
        v_high = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="r-high"),
            session_id=sess_high.id,
            operation_id="close-high",
            expected_budget_version=2,
            cumulative_billable_ms=10000,
            stop_evidence={"reason": "normal_stop"},
            now=t_close,
        )

        assert v_low.charged_minor == 17
        assert v_high.charged_minor == 50
        assert v_low.reserved_minor == 0
        assert v_high.reserved_minor == 0

        w_final = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))).scalar_one()
        assert w_final.posted_minor == 200 - 67 == 133
        assert w_final.reserved_minor == 0
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_service_boundary_zero_rate_and_zero_duration_setup(fresh_pg: FreshPostgres):
    """Rate zero is explicitly free, and 0-duration setup calls charge zero paise."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db, email="free_tier@example.com")
        await create_funded_wallet(db, user_id=user.id, credited_minor=50)

        # 1. Pinned rate 0 (free session)
        sess_free, _ = await create_test_session(db, owner_user_id=user.id, runner_id="r-free", pinned_rate=0)
        p_free = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="r-free"),
            session_id=sess_free.id,
            operation_id="chk-free-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=60000,
            billing_slice_ms=60000,
            permit_ttl_ms=5000,
            now=t0,
        )
        assert p_free.authorized_through_ms == 60000
        assert p_free.reserved_minor == 0
        assert not p_free.funds_exhausted

        v_free = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="r-free"),
            session_id=sess_free.id,
            operation_id="close-free-1",
            expected_budget_version=2,
            cumulative_billable_ms=60000,
            stop_evidence={"reason": "free_call_end"},
            now=t0 + timedelta(seconds=6),
        )
        assert v_free.charged_minor == 0
        assert v_free.reserved_minor == 0

        # 2. Paid session cancelled at 0 ms (setup only)
        sess_setup, _ = await create_test_session(db, owner_user_id=user.id, runner_id="r-setup", pinned_rate=200)
        p_setup = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="r-setup"),
            session_id=sess_setup.id,
            operation_id="chk-setup-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        assert p_setup.reserved_minor == 34

        v_setup = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="r-setup"),
            session_id=sess_setup.id,
            operation_id="close-setup-1",
            expected_budget_version=2,
            cumulative_billable_ms=0,
            stop_evidence={"reason": "cancelled_during_setup"},
            now=t0 + timedelta(seconds=6),
        )
        assert v_setup.charged_minor == 0
        assert v_setup.reserved_minor == 0
        assert v_setup.proven_ms == 0

        # Balance unchanged from initial 50 paise
        w = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))).scalar_one()
        assert w.posted_minor == 50
        assert w.reserved_minor == 0
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_service_rejects_invalid_bounds_before_any_mutation(fresh_pg: FreshPostgres):
    """Service-boundary validation rejects zero, wrong-sign, overlong and overflowing inputs atomically."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db, email="service_bounds@example.com")
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(
            db, owner_user_id=user.id, runner_id="runner-bounds"
        )

    async def expect_validation(call) -> None:
        async with fresh_pg.new_session() as db:
            with pytest.raises(BillingValidationError):
                await call(db)

    await expect_validation(
        lambda db: credit_wallet(
            db,
            actor="admin:test",
            user_id=user.id,
            amount_minor=0,
            reason="Zero amount adjustment",
            operation_id="zero-amount",
        )
    )
    await expect_validation(
        lambda db: credit_wallet(
            db,
            actor="admin:test",
            user_id=user.id,
            amount_minor=MAX_BIGINT + 1,
            reason="Overflowing adjustment",
            operation_id="overflow-amount",
        )
    )
    await expect_validation(
        lambda db: credit_wallet(
            db,
            actor="admin:test",
            user_id=user.id,
            amount_minor=True,  # type: ignore[arg-type]
            reason="Boolean adjustment",
            operation_id="boolean-amount",
        )
    )
    await expect_validation(
        lambda db: credit_wallet(
            db,
            actor="admin:test",
            user_id=user.id,
            amount_minor=10,
            reason="Positive charge entry",
            operation_id="positive-charge",
            kind="charge",
        )
    )
    await expect_validation(
        lambda db: credit_wallet(
            db,
            actor="admin:test",
            user_id=user.id,
            amount_minor=10,
            reason="Overlong operation id",
            operation_id="x" * 129,
        )
    )
    await expect_validation(
        lambda db: checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-bounds"),
            session_id=session.id,
            operation_id="bad-version",
            expected_budget_version=0,
            cumulative_billable_ms=0,
            requested_future_ms=1000,
            billing_slice_ms=1000,
            permit_ttl_ms=5000,
        )
    )
    await expect_validation(
        lambda db: checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-bounds"),
            session_id=session.id,
            operation_id="negative-time",
            expected_budget_version=1,
            cumulative_billable_ms=-1,
            requested_future_ms=1000,
            billing_slice_ms=1000,
            permit_ttl_ms=5000,
        )
    )
    await expect_validation(
        lambda db: checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-bounds"),
            session_id=session.id,
            operation_id="overflow-time",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=MAX_BIGINT + 1,
            billing_slice_ms=1000,
            permit_ttl_ms=5000,
        )
    )
    await expect_validation(
        lambda db: checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-bounds"),
            session_id=session.id,
            operation_id="",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=1000,
            billing_slice_ms=1000,
            permit_ttl_ms=5000,
        )
    )

    async with fresh_pg.new_session() as db:
        wallet = (
            await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))
        ).scalar_one()
        budget = (
            await db.execute(select(CallBudget).where(CallBudget.session_id == session.id))
        ).scalar_one()
        entries = list(
            (
                await db.execute(
                    select(WalletEntry).where(WalletEntry.user_id == user.id)
                )
            ).scalars().all()
        )
        assert wallet.posted_minor == 100
        assert wallet.reserved_minor == 0
        assert len(entries) == 1  # only the initial funding entry
        assert budget.version == 1
        assert budget.status == "OPEN"
        assert budget.reserved_minor == 0
        assert budget.cumulative_proven_ms == 0
        await assert_wallet_invariants(db, user_id=user.id)
