"""Idempotency and lost-response replay tests against fresh PostgreSQL.

Follows accounting-contract.md and verification-plan.md Phase 5:
- Duplicate requests return identical recorded results.
- Duplicate operations never create duplicate holds or ledger charges.
- Same operation key with conflicting payload is rejected with 409 without mutations.
- Replaying after simulated lost response returns the original outcome safely.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from platform_api.billing.models import (
    BillingOperation,
    CallBudget,
    WalletAccount,
    WalletEntry,
)
from platform_api.billing.service import (
    IdempotencyConflictError,
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
async def test_duplicate_grant_returns_one_operation_and_one_hold(fresh_pg: FreshPostgres):
    """Calling checkpoint twice with identical operation_id returns identical permit with no double hold."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # First grant
        permit1 = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="grant-op-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
        )

        # Duplicate grant (retry)
        permit2 = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="grant-op-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
        )

        assert permit1 == permit2
        assert permit1.reserved_minor == 34
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_duplicate_settlement_posts_once(fresh_pg: FreshPostgres):
    """Calling close twice with identical operation_id posts single charge and returns identical view."""
    await fresh_pg.initialize()
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # Initial grant with permit expiring at t0 + 5s
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="grant-op-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )

        # First close after permit expiry at t0 + 6s
        view1 = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="close-op-1",
            expected_budget_version=2,
            cumulative_billable_ms=10000,
            stop_evidence={"session_id": str(session.id), "runner_id": "runner-1", "reason": "normal_stop"},
            now=t0 + timedelta(seconds=6),
        )

        # Duplicate close retry
        view2 = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="close-op-1",
            expected_budget_version=2,
            cumulative_billable_ms=10000,
            stop_evidence={"session_id": str(session.id), "runner_id": "runner-1", "reason": "normal_stop"},
            now=t0 + timedelta(seconds=6),
        )
        assert view1 == view2
        assert view1.charged_minor == 34
        assert view1.reserved_minor == 0
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_duplicate_credit_posts_once(fresh_pg: FreshPostgres):
    """Calling credit_wallet twice with same operation_id returns identical WalletView without double credit."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=50)

        # First topup
        w1 = await credit_wallet(
            db,
            actor="admin:1",
            user_id=user.id,
            amount_minor=100,
            reason="Topup operation",
            operation_id="topup-key-1",
        )
        assert w1.posted_minor == 150

        # Duplicate topup retry
        w2 = await credit_wallet(
            db,
            actor="admin:1",
            user_id=user.id,
            amount_minor=100,
            reason="Topup operation",
            operation_id="topup-key-1",
        )
        assert w1 == w2
        assert w2.posted_minor == 150

        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_key_different_payload_rejected(fresh_pg: FreshPostgres):
    """Replaying same operation_id with conflicting payload returns 409 without mutations."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # First grant for 10,000ms
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="test-op-conflict",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
        )

        # Same operation_id, different requested_future_ms -> conflict!
        with pytest.raises(IdempotencyConflictError):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="test-op-conflict",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=20000,
                billing_slice_ms=20000,
                permit_ttl_ms=5000,
            )

        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lost_response_after_commit_retries_original_result(fresh_pg: FreshPostgres):
    """Simulated lost response after DB commit is replayed cleanly by the client."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # First commit succeeds
        permit = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="lost-response-op",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=5000,
            billing_slice_ms=5000,
            permit_ttl_ms=5000,
        )

    # Client believes connection dropped, retries in fresh DB session
    async with fresh_pg.new_session() as db:
        retry_permit = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="lost-response-op",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=5000,
            billing_slice_ms=5000,
            permit_ttl_ms=5000,
        )

        assert retry_permit == permit
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_simultaneous_same_key_grants_return_identical_permit(fresh_pg: FreshPostgres):
    """Simultaneous requests with identical operation_id return exact same permit without duplicate holds."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

    barrier = asyncio.Barrier(2)

    async def do_checkpoint():
        async with fresh_pg.new_session() as db:
            await barrier.wait()
            return await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="simul-chk-same",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=10000,
                billing_slice_ms=10000,
                permit_ttl_ms=5000,
            )

    res1, res2 = await asyncio.gather(do_checkpoint(), do_checkpoint())
    assert res1 == res2
    assert res1.reserved_minor == 34

    async with fresh_pg.new_session() as db:
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_simultaneous_conflicting_key_grants_rejected(fresh_pg: FreshPostgres):
    """Simultaneous requests with same operation_id but conflicting payloads: one succeeds, one raises 409."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

    barrier = asyncio.Barrier(2)

    async def do_checkpoint(future_ms: int):
        async with fresh_pg.new_session() as db:
            await barrier.wait()
            try:
                return await checkpoint_and_reserve(
                    db,
                    runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                    session_id=session.id,
                    operation_id="simul-chk-conflict",
                    expected_budget_version=1,
                    cumulative_billable_ms=0,
                    requested_future_ms=future_ms,
                    billing_slice_ms=10000,
                    permit_ttl_ms=5000,
                )
            except IdempotencyConflictError as e:
                return e

    r1, r2 = await asyncio.gather(do_checkpoint(5000), do_checkpoint(10000))
    results = [r1, r2]

    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, IdempotencyConflictError)]

    assert len(successes) == 1
    assert len(conflicts) == 1

    async with fresh_pg.new_session() as db:
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_operation_id_across_sessions_does_not_collide(fresh_pg: FreshPostgres):
    """Per-session operation ids may repeat across sessions without colliding ledger keys."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        session_a, _ = await create_test_session(
            db, owner_user_id=user.id, runner_id="runner-a", created_at=t0
        )
        session_b, _ = await create_test_session(
            db, owner_user_id=user.id, runner_id="runner-b", created_at=t0
        )

        # Two runners commonly number their first operations the same way.
        for session, runner in ((session_a, "runner-a"), (session_b, "runner-b")):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id=runner),
                session_id=session.id,
                operation_id="grant-1",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=10000,
                billing_slice_ms=10000,
                permit_ttl_ms=5000,
                now=t0,
            )
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id=runner),
                session_id=session.id,
                operation_id="usage-1",
                expected_budget_version=2,
                cumulative_billable_ms=3000,
                requested_future_ms=7000,
                billing_slice_ms=10000,
                permit_ttl_ms=5000,
                now=t0 + timedelta(seconds=1),
            )

        for session, runner in ((session_a, "runner-a"), (session_b, "runner-b")):
            view = await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id=runner),
                session_id=session.id,
                operation_id="close-1",
                expected_budget_version=3,
                cumulative_billable_ms=10000,
                stop_evidence={"reason": "normal_stop"},
                now=t0 + timedelta(seconds=7),
            )
            assert view.charged_minor == 34
            assert view.reserved_minor == 0

        charges = list(
            (
                await db.execute(
                    select(WalletEntry).where(
                        WalletEntry.user_id == user.id,
                        WalletEntry.kind == "charge",
                    )
                )
            ).scalars().all()
        )
        # One usage charge and one final charge per session.
        assert len(charges) == 4
        assert len({entry.operation_id for entry in charges}) == 4

        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_max_length_operation_id_accepted_and_ledger_key_bounded(fresh_pg: FreshPostgres):
    """A 128-character operation id is accepted, and its session-scoped ledger key fits."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    grant_op = "g" * 128
    usage_op = "u" * 128
    close_op = "c" * 128

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(
            db, owner_user_id=user.id, runner_id="runner-max-op", created_at=t0
        )

        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-max-op"),
            session_id=session.id,
            operation_id=grant_op,
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        permit = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-max-op"),
            session_id=session.id,
            operation_id=usage_op,
            expected_budget_version=2,
            cumulative_billable_ms=3000,
            requested_future_ms=7000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0 + timedelta(seconds=1),
        )
        assert permit.proven_ms == 3000

        view = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-max-op"),
            session_id=session.id,
            operation_id=close_op,
            expected_budget_version=3,
            cumulative_billable_ms=3000,
            now=t0 + timedelta(seconds=7),
        )
        assert view.charged_minor == 10

        charges = list(
            (
                await db.execute(
                    select(WalletEntry).where(
                        WalletEntry.session_id == session.id,
                        WalletEntry.kind == "charge",
                    )
                )
            ).scalars().all()
        )
        assert len(charges) == 1
        assert charges[0].operation_id == f"{session.id}:{usage_op}:charge"
        assert len(charges[0].operation_id) == len(str(session.id)) + 1 + 128 + 7
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conflicting_close_replay_rejected_without_mutation(fresh_pg: FreshPostgres):
    """A different close payload under the same operation key is rejected and changes nothing."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        session, _ = await create_test_session(
            db, owner_user_id=user.id, runner_id="runner-close-conflict", created_at=t0
        )

        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-close-conflict"),
            session_id=session.id,
            operation_id="grant-close-conflict",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )

        original = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-close-conflict"),
            session_id=session.id,
            operation_id="close-conflict",
            expected_budget_version=2,
            cumulative_billable_ms=10000,
            stop_evidence={"reason": "normal_stop"},
            now=t0 + timedelta(seconds=6),
        )
        assert original.charged_minor == 34

    # Different cumulative time under the same key is a conflict.
    async with fresh_pg.new_session() as db:
        with pytest.raises(IdempotencyConflictError):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-close-conflict"),
                session_id=session.id,
                operation_id="close-conflict",
                expected_budget_version=2,
                cumulative_billable_ms=9000,
                stop_evidence={"reason": "normal_stop"},
                now=t0 + timedelta(seconds=6),
            )

    # Different stop evidence under the same key is also a conflict.
    async with fresh_pg.new_session() as db:
        with pytest.raises(IdempotencyConflictError):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-close-conflict"),
                session_id=session.id,
                operation_id="close-conflict",
                expected_budget_version=2,
                cumulative_billable_ms=10000,
                stop_evidence={"reason": "different_stop"},
                now=t0 + timedelta(seconds=6),
            )

    async with fresh_pg.new_session() as db:
        charges = list(
            (
                await db.execute(
                    select(WalletEntry).where(
                        WalletEntry.session_id == session.id,
                        WalletEntry.kind == "charge",
                    )
                )
            ).scalars().all()
        )
        operations = list(
            (
                await db.execute(
                    select(BillingOperation).where(
                        BillingOperation.session_id == session.id,
                        BillingOperation.operation_id == "close-conflict",
                    )
                )
            ).scalars().all()
        )
        wallet = (
            await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))
        ).scalar_one()
        budget = (
            await db.execute(select(CallBudget).where(CallBudget.session_id == session.id))
        ).scalar_one()
        assert len(charges) == 1
        assert len(operations) == 1
        assert wallet.posted_minor == 166
        assert wallet.reserved_minor == 0
        assert budget.status == "CLOSED"
        assert budget.cumulative_proven_ms == 10000
        assert budget.cumulative_charged_minor == 34
        await assert_wallet_invariants(db, user_id=user.id)
