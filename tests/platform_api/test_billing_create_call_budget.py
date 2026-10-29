"""Idempotency, owner-derivation and concurrency tests for create_call_budget.

Follows accounting-contract.md:
- Owner identity is derived from the stored session, never from caller-supplied input.
- Bounded validation of inputs (operation_id, pinned_rate).
- Identical operation replay returns recorded BudgetView response, reconstructed from BillingOperation.response_body.
- Mutating/advancing the budget after creation does not alter the recorded replay outcome.
- Conflicting request payload under the same operation key is rejected with IdempotencyConflictError (409).
- A second different operation attempting to create a budget on the same session is rejected with ConflictError (409).
- Concurrent different operations racing on the same session result in exactly one winner and one rejected with ConflictError (409).
- Nonexistent session raises NotFoundError (404).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from platform_api.auth.errors import NotFoundError
from platform_api.billing.models import BillingOperation, CallBudget
from platform_api.billing.schemas import BudgetView
from platform_api.billing.service import (
    BillingValidationError,
    ConflictError,
    IdempotencyConflictError,
    RuntimePrincipal,
    checkpoint_and_reserve,
    close_budget,
    create_call_budget,
)
from platform_api.models.session import Session
from tests.support.billing import create_funded_wallet, create_test_session, create_test_user
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_derives_owner_from_stored_session(fresh_pg: FreshPostgres):
    """Owner identity is derived strictly from stored session and operation is durably recorded."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=0)
        session = Session(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            kind="voice",
            state="ACTIVE",
            version=1,
            runner_id="runner-1",
        )
        db.add(session)
        await db.commit()

        # Create call budget
        view = await create_call_budget(
            db,
            session_id=session.id,
            operation_id="init-budget-1",
            pinned_rate_minor_per_minute=200,
        )

        assert isinstance(view, BudgetView)
        assert view.session_id == session.id
        assert view.owner_user_id == user.id
        assert view.pinned_rate == 200
        assert view.budget_version == 1
        assert view.status == "OPEN"
        assert view.proven_ms == 0
        assert view.authorized_through_ms == 0
        assert view.charged_minor == 0
        assert view.reserved_minor == 0

    # Verify committed state in fresh transaction
    async with fresh_pg.new_session() as db:
        stored_budget = (
            await db.execute(select(CallBudget).where(CallBudget.session_id == session.id))
        ).scalar_one()
        assert stored_budget.owner_user_id == user.id
        assert stored_budget.pinned_rate == 200

        op = (
            await db.execute(
                select(BillingOperation).where(
                    BillingOperation.session_id == session.id,
                    BillingOperation.operation_id == "init-budget-1",
                )
            )
        ).scalar_one()
        assert op.status_code == 200
        assert op.response_body["pinned_rate"] == 200
        assert op.response_body["owner_user_id"] == str(user.id)
        assert op.response_body["budget_version"] == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_replay_after_budget_advancement_returns_original_response(fresh_pg: FreshPostgres):
    """Replaying create_call_budget after budget mutated/advanced returns the exact original recorded BudgetView."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=0)
        session = Session(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            kind="voice",
            state="ACTIVE",
            version=1,
            runner_id="runner-1",
        )
        db.add(session)
        await db.commit()

        # 1. Create budget initially
        view_original = await create_call_budget(
            db,
            session_id=session.id,
            operation_id="init-budget-advance",
            pinned_rate_minor_per_minute=200,
        )
        assert view_original.budget_version == 1
        assert view_original.proven_ms == 0

    # 2. Mutate/advance the budget row directly (simulating progress/checkpoints)
    async with fresh_pg.new_session() as db:
        budget = (
            await db.execute(select(CallBudget).where(CallBudget.session_id == session.id))
        ).scalar_one()
        budget.version = 5
        budget.cumulative_proven_ms = 15000
        budget.cumulative_authorized_ms = 20000
        budget.cumulative_charged_minor = 50
        budget.reserved_minor = 17
        await db.commit()

    # 3. Replay create_call_budget with identical operation_id
    async with fresh_pg.new_session() as db:
        view_replayed = await create_call_budget(
            db,
            session_id=session.id,
            operation_id="init-budget-advance",
            pinned_rate_minor_per_minute=200,
        )

        # The replayed response MUST match the original recorded outcome, NOT current mutable row state
        assert isinstance(view_replayed, BudgetView)
        assert view_replayed.budget_version == 1
        assert view_replayed.proven_ms == 0
        assert view_replayed.authorized_through_ms == 0
        assert view_replayed.charged_minor == 0
        assert view_replayed.reserved_minor == 0
        assert view_replayed == view_original


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_conflicting_replay_raises_error(fresh_pg: FreshPostgres):
    """Same operation key with conflicting payload raises IdempotencyConflictError."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=0)
        session = Session(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            kind="voice",
            state="ACTIVE",
            version=1,
            runner_id="runner-1",
        )
        db.add(session)
        await db.commit()

        await create_call_budget(
            db,
            session_id=session.id,
            operation_id="init-budget-conflict",
            pinned_rate_minor_per_minute=200,
        )

        # Conflicting pinned_rate under same operation_id
        with pytest.raises(IdempotencyConflictError):
            await create_call_budget(
                db,
                session_id=session.id,
                operation_id="init-budget-conflict",
                pinned_rate_minor_per_minute=300,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_second_operation_on_same_session_rejected(fresh_pg: FreshPostgres):
    """A second different operation trying to create a budget on same session is rejected."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=0)
        session = Session(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            kind="voice",
            state="ACTIVE",
            version=1,
            runner_id="runner-1",
        )
        db.add(session)
        await db.commit()

        # First operation creates budget
        await create_call_budget(
            db,
            session_id=session.id,
            operation_id="init-budget-op-1",
            pinned_rate_minor_per_minute=200,
        )

        # Second different operation on same session (even with same pinned_rate)
        with pytest.raises(ConflictError) as exc_info:
            await create_call_budget(
                db,
                session_id=session.id,
                operation_id="init-budget-op-2",
                pinned_rate_minor_per_minute=200,
            )

        assert "already exists" in str(exc_info.value).lower()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_concurrent_different_operations_race(fresh_pg: FreshPostgres):
    """Two concurrent distinct operations trying to create a budget on the same session race: one succeeds, one gets ConflictError."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session = Session(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            kind="voice",
            state="ACTIVE",
            version=1,
            runner_id="runner-1",
        )
        db.add(session)
        await db.commit()

    barrier = asyncio.Barrier(2)

    async def _worker(op_id: str):
        async with fresh_pg.new_session() as db:
            await barrier.wait()
            return await create_call_budget(
                db,
                session_id=session.id,
                operation_id=op_id,
                pinned_rate_minor_per_minute=200,
            )

    results = await asyncio.gather(
        _worker("race-op-1"),
        _worker("race-op-2"),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, BudgetView)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert "already exists" in str(conflicts[0]).lower()

    # Verify exactly 1 budget exists in database
    async with fresh_pg.new_session() as db:
        budgets = list(
            (
                await db.execute(select(CallBudget).where(CallBudget.session_id == session.id))
            ).scalars().all()
        )
        assert len(budgets) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_session_not_found(fresh_pg: FreshPostgres):
    """Nonexistent session raises NotFoundError."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        with pytest.raises(NotFoundError):
            await create_call_budget(
                db,
                session_id=uuid.uuid4(),
                operation_id="init-budget-missing",
                pinned_rate_minor_per_minute=200,
            )


@pytest.mark.asyncio
async def test_create_call_budget_input_validation():
    """Negative, boolean, empty or out-of-bound inputs raise BillingValidationError."""
    from unittest.mock import AsyncMock
    db = AsyncMock()

    with pytest.raises(BillingValidationError):
        await create_call_budget(
            db,
            session_id=uuid.uuid4(),
            operation_id="",
            pinned_rate_minor_per_minute=200,
        )

    with pytest.raises(BillingValidationError):
        await create_call_budget(
            db,
            session_id=uuid.uuid4(),
            operation_id="op-1",
            pinned_rate_minor_per_minute=-1,
        )

    with pytest.raises(BillingValidationError):
        await create_call_budget(
            db,
            session_id=uuid.uuid4(),
            operation_id="op-1",
            pinned_rate_minor_per_minute=True,  # type: ignore[arg-type]
        )


@pytest.mark.postgres
@pytest.mark.parametrize("meter_operation", ["checkpoint", "close"])
@pytest.mark.asyncio
async def test_create_and_meter_operations_use_consistent_row_lock_order(
    fresh_pg: FreshPostgres,
    meter_operation: str,
):
    """Creation and checkpoint/close serialize on rows in the same order."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(
            db,
            owner_user_id=user.id,
            runner_id="runner-lock-order",
            pinned_rate=200,
        )

    meter_budget_locked = asyncio.Event()
    release_meter_operation = asyncio.Event()
    create_first_lock_started = asyncio.Event()
    create_first_lock_table: str | None = None

    class PausingMeterSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            query = str(statement).lower()
            if "for update" in query and "from call_budgets" in query:
                meter_budget_locked.set()
                await release_meter_operation.wait()
            return result

    class ObservedCreateSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal create_first_lock_table
            query = str(statement).lower()
            if "for update" in query and create_first_lock_table is None:
                for table in ("wallet_accounts", "call_budgets", "sessions"):
                    if f"from {table}" in query:
                        create_first_lock_table = table
                        create_first_lock_started.set()
                        break
            return await super().execute(statement, *args, **kwargs)

    meter_sessions = async_sessionmaker(
        bind=fresh_pg.engine,
        class_=PausingMeterSession,
        expire_on_commit=False,
    )
    create_sessions = async_sessionmaker(
        bind=fresh_pg.engine,
        class_=ObservedCreateSession,
        expire_on_commit=False,
    )

    async def meter_worker():
        async with meter_sessions() as db:
            if meter_operation == "checkpoint":
                return await checkpoint_and_reserve(
                    db,
                    runtime_principal=RuntimePrincipal(runner_id="runner-lock-order"),
                    session_id=session.id,
                    operation_id="lock-order-checkpoint",
                    expected_budget_version=1,
                    cumulative_billable_ms=0,
                    requested_future_ms=1000,
                    billing_slice_ms=1000,
                    permit_ttl_ms=5000,
                )
            return await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-lock-order"),
                session_id=session.id,
                operation_id="lock-order-close",
                expected_budget_version=1,
                cumulative_billable_ms=0,
            )

    async def create_worker():
        async with create_sessions() as db:
            return await create_call_budget(
                db,
                session_id=session.id,
                operation_id="lock-order-duplicate",
                pinned_rate_minor_per_minute=200,
            )

    meter_task = asyncio.create_task(meter_worker())
    meter_lock_wait_task = asyncio.create_task(meter_budget_locked.wait())
    create_task = None
    outcomes = None
    try:
        done, _ = await asyncio.wait(
            (meter_task, meter_lock_wait_task),
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if meter_task in done:
            await meter_task
        assert meter_lock_wait_task in done, (
            "meter operation did not reach the locked budget row"
        )
        create_task = asyncio.create_task(create_worker())
        await asyncio.wait_for(create_first_lock_started.wait(), timeout=5)
        assert create_first_lock_table == "wallet_accounts"
        release_meter_operation.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                meter_task,
                create_task,
                return_exceptions=True,
            ),
            timeout=10,
        )
    finally:
        release_meter_operation.set()
        tasks = [meter_task, meter_lock_wait_task]
        if create_task is not None:
            tasks.append(create_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert outcomes is not None
    if meter_operation == "checkpoint":
        assert outcomes[0].authorized_through_ms == 1000
    else:
        assert outcomes[0].status == "CLOSED"
    assert isinstance(outcomes[1], ConflictError)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_replay_revalidates_session_owner(
    fresh_pg: FreshPostgres,
):
    """A replay cannot return the old owner's stored budget after reassignment."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        original_owner = await create_test_user(db)
        replacement_owner = await create_test_user(db)
        await create_funded_wallet(db, user_id=original_owner.id, credited_minor=0)
        await create_funded_wallet(db, user_id=replacement_owner.id, credited_minor=0)
        session = Session(
            id=uuid.uuid4(),
            owner_user_id=original_owner.id,
            runner_id="runner-create-replay",
            kind="voice",
            state="ACTIVE",
            version=1,
        )
        db.add(session)
        await db.commit()
        original_view = await create_call_budget(
            db,
            session_id=session.id,
            operation_id="create-owner-replay",
            pinned_rate_minor_per_minute=200,
        )
        assert original_view.owner_user_id == original_owner.id

    initial_session_read = asyncio.Event()
    continue_request = asyncio.Event()
    pause_once = True

    class PausingReplaySession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal pause_once
            result = await super().execute(statement, *args, **kwargs)
            query = str(statement).lower()
            if pause_once and "from sessions" in query and "for update" not in query:
                pause_once = False
                initial_session_read.set()
                await continue_request.wait()
            return result

    session_factory = async_sessionmaker(
        bind=fresh_pg.engine,
        class_=PausingReplaySession,
        expire_on_commit=False,
    )

    async def replay():
        async with session_factory() as db:
            return await create_call_budget(
                db,
                session_id=session.id,
                operation_id="create-owner-replay",
                pinned_rate_minor_per_minute=200,
            )

    replay_task = asyncio.create_task(replay())
    read_wait_task = asyncio.create_task(initial_session_read.wait())
    try:
        done, _ = await asyncio.wait(
            (replay_task, read_wait_task),
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if replay_task in done:
            await replay_task
        assert read_wait_task in done, "replay did not reach the initial session read"
        async with fresh_pg.new_session() as updater:
            await updater.execute(
                update(Session)
                .where(Session.id == session.id)
                .values(
                    owner_user_id=replacement_owner.id,
                    version=Session.version + 1,
                )
            )
            await updater.commit()
    finally:
        continue_request.set()
        if not replay_task.done():
            await asyncio.wait((replay_task,), timeout=5)
        for task in (replay_task, read_wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(replay_task, read_wait_task, return_exceptions=True)

    with pytest.raises(ConflictError):
        await replay_task

    # A replay that starts after reassignment is rejected by the locked budget-owner check.
    with pytest.raises(ConflictError):
        async with fresh_pg.new_session() as db:
            await create_call_budget(
                db,
                session_id=session.id,
                operation_id="create-owner-replay",
                pinned_rate_minor_per_minute=200,
            )

    async with fresh_pg.new_session() as db:
        stored_session = (
            await db.execute(select(Session).where(Session.id == session.id))
        ).scalar_one()
        stored_budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session.id)
            )
        ).scalar_one()
        assert stored_session.owner_user_id == replacement_owner.id
        assert stored_budget.owner_user_id == original_owner.id
