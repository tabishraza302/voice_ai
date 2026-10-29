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
from sqlalchemy import select

from platform_api.auth.errors import NotFoundError
from platform_api.billing.models import BillingOperation, CallBudget
from platform_api.billing.schemas import BudgetView
from platform_api.billing.service import (
    BillingValidationError,
    ConflictError,
    IdempotencyConflictError,
    create_call_budget,
)
from platform_api.models.session import Session
from tests.support.billing import create_test_user
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_derives_owner_from_stored_session(fresh_pg: FreshPostgres):
    """Owner identity is derived strictly from stored session and operation is durably recorded."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
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
            pinned_rate=200,
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
            pinned_rate=200,
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
            pinned_rate=200,
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
            pinned_rate=200,
        )

        # Conflicting pinned_rate under same operation_id
        with pytest.raises(IdempotencyConflictError):
            await create_call_budget(
                db,
                session_id=session.id,
                operation_id="init-budget-conflict",
                pinned_rate=300,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_second_operation_on_same_session_rejected(fresh_pg: FreshPostgres):
    """A second different operation trying to create a budget on same session is rejected."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
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
            pinned_rate=200,
        )

        # Second different operation on same session (even with same pinned_rate)
        with pytest.raises(ConflictError) as exc_info:
            await create_call_budget(
                db,
                session_id=session.id,
                operation_id="init-budget-op-2",
                pinned_rate=200,
            )

        assert "already exists" in str(exc_info.value).lower()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_create_call_budget_concurrent_different_operations_race(fresh_pg: FreshPostgres):
    """Two concurrent distinct operations trying to create a budget on the same session race: one succeeds, one gets ConflictError."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
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
                pinned_rate=200,
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
                pinned_rate=200,
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
            pinned_rate=200,
        )

    with pytest.raises(BillingValidationError):
        await create_call_budget(
            db,
            session_id=uuid.uuid4(),
            operation_id="op-1",
            pinned_rate=-1,
        )

    with pytest.raises(BillingValidationError):
        await create_call_budget(
            db,
            session_id=uuid.uuid4(),
            operation_id="op-1",
            pinned_rate=True,  # type: ignore[arg-type]
        )
