"""HTTP API, synchronous meter, and security barrier tests against fresh PostgreSQL.

Follows accounting-contract.md and verification-plan.md Phase 5:
- POST /internal/sessions/{id}/budget/checkpoint and /close fail closed (503) without Task 6 transport_auth.
- Forged runner ID is rejected (403) and cannot mutate money or hold reservations.
- GET /api/v1/wallet and /api/v1/wallet/entries (owner-only).
- POST /api/v1/admin/users/{id}/wallet-adjustments (admin step-up, CSRF, Idempotency-Key).
- Admin adjustment + audit + response commit are atomic: replay does not create duplicate audit logs.
- Simultaneous same-key and conflicting-key calls are serialized safely under lock.
- Out-of-order and decreasing usage reports return 409.
- Meter acknowledgment requires durable database commit.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import uuid

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from platform_api.app import create_app
from platform_api.billing import routes as billing_routes
from platform_api.auth.errors import ForbiddenError
from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import create_authenticated_session
from platform_api.billing.models import (
    BillingOperation,
    CallBudget,
    MeterCheckpoint,
    WalletAccount,
    WalletEntry,
)
from platform_api.billing.service import (
    ConflictError,
    IdempotencyConflictError,
    InvalidTimeReportError,
    RuntimePrincipal,
    checkpoint_and_reserve,
    close_budget,
    expire_stopped_budget,
    credit_wallet,
)
from platform_api.config import PlatformSettings
from platform_api.models.session import Session
from platform_api.models.auth import AuditLog, AuthMfa, User
from tests.support.billing import (
    assert_wallet_invariants,
    create_funded_wallet,
    create_test_session,
    create_test_user,
    statement_selects_entity,
)
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_meter_ack_requires_durable_database_commit(fresh_pg: FreshPostgres):
    """Successful checkpoint acknowledgment is backed by a committed MeterCheckpoint row."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")
        session.tariff_snapshot = {
            "tariff_version": "retail-2026-10",
            "rate_minor_per_minute": 200,
        }
        await db.commit()

        permit = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="chk-ack-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=5000,
            billing_slice_ms=5000,
            permit_ttl_ms=5000,
        )
        assert permit.authorized_through_ms == 5000

    # In a new session, verify the meter checkpoint exists in PostgreSQL
    async with fresh_pg.new_session() as db:
        stmt = select(MeterCheckpoint).where(
            MeterCheckpoint.session_id == session.id,
            MeterCheckpoint.operation_id == "chk-ack-1",
        )
        chk = (await db.execute(stmt)).scalar_one_or_none()
        assert chk is not None
        assert chk.cumulative_proven_ms == 0
        assert chk.runner_id == "runner-1"

    async with fresh_pg.new_session() as db:
        stored_session = (
            await db.execute(select(Session).where(Session.id == session.id))
        ).scalar_one()
        stored_session.tariff_snapshot = {
            "tariff_version": "retail-2026-11",
            "rate_minor_per_minute": 250,
        }
        await db.commit()

    async with fresh_pg.new_session() as db:
        chk = (
            await db.execute(
                select(MeterCheckpoint).where(
                    MeterCheckpoint.session_id == session.id,
                    MeterCheckpoint.operation_id == "chk-ack-1",
                )
            )
        ).scalar_one()
        assert chk.tariff_snapshot == {
            "tariff_version": "retail-2026-10",
            "rate_minor_per_minute": 200,
        }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_out_of_order_meter_update_obeys_contract(fresh_pg: FreshPostgres):
    """Decreasing or unauthorized billable time is rejected with 409 without mutating state."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # Initial grant 5,000ms
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="chk-init",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=5000,
            billing_slice_ms=5000,
            permit_ttl_ms=5000,
        )

        # 1. Billable time exceeds authorization (6,000ms > 5,000ms authorized) -> 409
        with pytest.raises(InvalidTimeReportError):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="chk-unauth",
                expected_budget_version=2,
                cumulative_billable_ms=6000,
                requested_future_ms=1000,
                billing_slice_ms=5000,
                permit_ttl_ms=5000,
            )

        # Checkpoint at 3,000ms
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="chk-3k",
            expected_budget_version=2,
            cumulative_billable_ms=3000,
            requested_future_ms=2000,
            billing_slice_ms=5000,
            permit_ttl_ms=5000,
        )

        # 2. Decreasing billable time (2,000ms < 3,000ms proven) -> 409
        with pytest.raises(InvalidTimeReportError):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="chk-decrease",
                expected_budget_version=3,
                cumulative_billable_ms=2000,
                requested_future_ms=1000,
                billing_slice_ms=5000,
                permit_ttl_ms=5000,
            )

        await assert_wallet_invariants(db, user_id=user.id)



@pytest.mark.postgres
@pytest.mark.parametrize("operation", ["checkpoint", "close"])
@pytest.mark.asyncio
async def test_locked_session_refresh_rejects_runner_reassignment_before_financial_changes(
    fresh_pg: FreshPostgres,
    operation: str,
):
    """A runner reassigned during a request cannot use stale ORM state to change money."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(
            db,
            owner_user_id=user.id,
            runner_id="runner-before",
        )

    initial_session_read = asyncio.Event()
    continue_request = asyncio.Event()
    pause_once = True

    class PausingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal pause_once
            result = await super().execute(statement, *args, **kwargs)
            if pause_once and statement_selects_entity(
                statement, Session, for_update=False
            ):
                pause_once = False
                initial_session_read.set()
                await continue_request.wait()
            return result

    session_factory = async_sessionmaker(
        bind=fresh_pg.engine,
        class_=PausingSession,
        expire_on_commit=False,
    )

    async def invoke():
        async with session_factory() as db:
            if operation == "checkpoint":
                return await checkpoint_and_reserve(
                    db,
                    runtime_principal=RuntimePrincipal(runner_id="runner-before"),
                    session_id=session.id,
                    operation_id="stale-session-checkpoint",
                    expected_budget_version=1,
                    cumulative_billable_ms=0,
                    requested_future_ms=1000,
                    billing_slice_ms=1000,
                    permit_ttl_ms=5000,
                )
            return await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-before"),
                session_id=session.id,
                operation_id="stale-session-close",
                expected_budget_version=1,
                cumulative_billable_ms=0,
            )

    request = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(initial_session_read.wait(), timeout=5)
        async with fresh_pg.new_session() as updater:
            await updater.execute(
                update(Session)
                .where(Session.id == session.id)
                .values(runner_id="runner-after", version=Session.version + 1)
            )
            await updater.commit()
    finally:
        continue_request.set()

    with pytest.raises(ForbiddenError):
        await request

    async with fresh_pg.new_session() as db:
        wallet = (
            await db.execute(
                select(WalletAccount).where(WalletAccount.user_id == user.id)
            )
        ).scalar_one()
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session.id)
            )
        ).scalar_one()
        assert wallet.posted_minor == 100
        assert wallet.reserved_minor == 0
        assert budget.version == 1
        assert budget.status == "OPEN"
        stored_session = (
            await db.execute(select(Session).where(Session.id == session.id))
        ).scalar_one()
        assert stored_session.runner_id == "runner-after"


@pytest.mark.postgres
@pytest.mark.parametrize("operation", ["checkpoint", "close"])
@pytest.mark.asyncio
async def test_meter_replay_revalidates_runner_after_initial_session_read(
    fresh_pg: FreshPostgres,
    operation: str,
):
    """A runner reassigned during an idempotent retry cannot receive stored output."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(
            db,
            owner_user_id=user.id,
            runner_id="runner-before",
        )
        if operation == "checkpoint":
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-before"),
                session_id=session.id,
                operation_id="replay-runner-checkpoint",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=1000,
                billing_slice_ms=1000,
                permit_ttl_ms=5000,
            )
        else:
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-before"),
                session_id=session.id,
                operation_id="replay-runner-close",
                expected_budget_version=1,
                cumulative_billable_ms=0,
            )

    async with fresh_pg.new_session() as db:
        wallet = (
            await db.execute(
                select(WalletAccount).where(WalletAccount.user_id == user.id)
            )
        ).scalar_one()
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session.id)
            )
        ).scalar_one()
        wallet_before = (
            wallet.posted_minor,
            wallet.reserved_minor,
            wallet.version,
        )
        budget_before = (
            budget.cumulative_proven_ms,
            budget.cumulative_authorized_ms,
            budget.cumulative_charged_minor,
            budget.reserved_minor,
            budget.version,
            budget.status,
        )

    initial_session_read = asyncio.Event()
    continue_request = asyncio.Event()
    pause_once = True

    class PausingReplaySession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal pause_once
            result = await super().execute(statement, *args, **kwargs)
            if pause_once and statement_selects_entity(
                statement, Session, for_update=False
            ):
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
            if operation == "checkpoint":
                return await checkpoint_and_reserve(
                    db,
                    runtime_principal=RuntimePrincipal(runner_id="runner-before"),
                    session_id=session.id,
                    operation_id="replay-runner-checkpoint",
                    expected_budget_version=1,
                    cumulative_billable_ms=0,
                    requested_future_ms=1000,
                    billing_slice_ms=1000,
                    permit_ttl_ms=5000,
                )
            return await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-before"),
                session_id=session.id,
                operation_id="replay-runner-close",
                expected_budget_version=1,
                cumulative_billable_ms=0,
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
                .values(runner_id="runner-after", version=Session.version + 1)
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

    with pytest.raises(ForbiddenError):
        await replay_task

    async with fresh_pg.new_session() as db:
        wallet = (
            await db.execute(
                select(WalletAccount).where(WalletAccount.user_id == user.id)
            )
        ).scalar_one()
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session.id)
            )
        ).scalar_one()
        stored_session = (
            await db.execute(select(Session).where(Session.id == session.id))
        ).scalar_one()
        assert (
            wallet.posted_minor,
            wallet.reserved_minor,
            wallet.version,
        ) == wallet_before
        assert (
            budget.cumulative_proven_ms,
            budget.cumulative_authorized_ms,
            budget.cumulative_charged_minor,
            budget.reserved_minor,
            budget.version,
            budget.status,
        ) == budget_before
        assert stored_session.runner_id == "runner-after"

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expiry_replay_revalidates_session_budget_owner(fresh_pg: FreshPostgres):
    """A recovery replay cannot return a stored budget after session ownership changes."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        original_owner = await create_test_user(db)
        replacement_owner = await create_test_user(db)
        await create_funded_wallet(db, user_id=original_owner.id, credited_minor=100)
        await create_funded_wallet(db, user_id=replacement_owner.id, credited_minor=0)
        session, _ = await create_test_session(
            db,
            owner_user_id=original_owner.id,
            runner_id="runner-expiry-owner",
            created_at=t0,
        )
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-expiry-owner"),
            session_id=session.id,
            operation_id="expiry-owner-checkpoint",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=1000,
            billing_slice_ms=1000,
            permit_ttl_ms=1000,
            now=t0,
        )
        await expire_stopped_budget(
            db,
            session_id=session.id,
            operation_id="expiry-owner-replay",
            now=t0 + timedelta(seconds=1),
        )

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

    async with fresh_pg.new_session() as db:
        with pytest.raises(ConflictError) as exc_info:
            await expire_stopped_budget(
                db,
                session_id=session.id,
                operation_id="expiry-owner-replay",
                now=t0 + timedelta(seconds=2),
            )
        assert exc_info.value.status_code == 409

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
        assert stored_budget.status == "CLOSED"

@pytest.mark.postgres
@pytest.mark.parametrize("operation", ["checkpoint", "close", "expire"])
@pytest.mark.asyncio
async def test_owner_reassigned_after_wallet_lock_cannot_mutate_previous_wallet(
    fresh_pg: FreshPostgres,
    operation: str,
):
    """A matching session/budget owner change cannot redirect old wallet locks."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    operation_now = t0 + timedelta(seconds=2)

    async with fresh_pg.new_session() as db:
        original_owner = await create_test_user(db)
        replacement_owner = await create_test_user(db)
        await create_funded_wallet(db, user_id=original_owner.id, credited_minor=100)
        await create_funded_wallet(db, user_id=replacement_owner.id, credited_minor=100)
        session, _ = await create_test_session(
            db,
            owner_user_id=original_owner.id,
            runner_id="runner-wallet-owner",
            created_at=t0,
        )
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-wallet-owner"),
            session_id=session.id,
            operation_id="wallet-owner-prime",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=5000,
            billing_slice_ms=5000,
            permit_ttl_ms=1000,
            now=t0,
        )

    async with fresh_pg.new_session() as db:
        accounts = (
            await db.execute(
                select(WalletAccount).where(
                    WalletAccount.user_id.in_(
                        (original_owner.id, replacement_owner.id)
                    )
                )
            )
        ).scalars().all()
        wallet_before = {
            account.user_id: (
                account.posted_minor,
                account.reserved_minor,
                account.version,
            )
            for account in accounts
        }
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session.id)
            )
        ).scalar_one()
        budget_before = (
            budget.cumulative_proven_ms,
            budget.cumulative_authorized_ms,
            budget.cumulative_charged_minor,
            budget.reserved_minor,
            budget.version,
            budget.status,
            budget.permit_expiry,
        )

    wallet_row_locked = asyncio.Event()
    continue_request = asyncio.Event()
    pause_once = True

    class PausingWalletSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal pause_once
            result = await super().execute(statement, *args, **kwargs)
            if pause_once and statement_selects_entity(
                statement, WalletAccount, for_update=True
            ):
                pause_once = False
                wallet_row_locked.set()
                await continue_request.wait()
            return result

    session_factory = async_sessionmaker(
        bind=fresh_pg.engine,
        class_=PausingWalletSession,
        expire_on_commit=False,
    )
    operation_id = f"owner-wallet-race-{operation}"

    async def invoke():
        async with session_factory() as db:
            if operation == "checkpoint":
                return await checkpoint_and_reserve(
                    db,
                    runtime_principal=RuntimePrincipal(runner_id="runner-wallet-owner"),
                    session_id=session.id,
                    operation_id=operation_id,
                    expected_budget_version=2,
                    cumulative_billable_ms=1000,
                    requested_future_ms=1000,
                    billing_slice_ms=1000,
                    permit_ttl_ms=1000,
                    now=operation_now,
                )
            if operation == "close":
                return await close_budget(
                    db,
                    runtime_principal=RuntimePrincipal(runner_id="runner-wallet-owner"),
                    session_id=session.id,
                    operation_id=operation_id,
                    expected_budget_version=2,
                    cumulative_billable_ms=1000,
                    now=operation_now,
                )
            return await expire_stopped_budget(
                db,
                session_id=session.id,
                operation_id=operation_id,
                now=operation_now,
            )

    request_task = asyncio.create_task(invoke())
    wallet_wait_task = asyncio.create_task(wallet_row_locked.wait())
    try:
        done, _ = await asyncio.wait(
            (request_task, wallet_wait_task),
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if request_task in done:
            await request_task
        assert wallet_wait_task in done, "billing operation did not lock its wallet first"
        async with fresh_pg.new_session() as updater:
            await updater.execute(
                update(CallBudget)
                .where(CallBudget.session_id == session.id)
                .values(owner_user_id=replacement_owner.id)
            )
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
        if not request_task.done():
            await asyncio.wait((request_task,), timeout=5)
        for task in (request_task, wallet_wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(request_task, wallet_wait_task, return_exceptions=True)

    with pytest.raises(ConflictError) as exc_info:
        await request_task
    assert exc_info.value.status_code == 409

    async with fresh_pg.new_session() as db:
        accounts = (
            await db.execute(
                select(WalletAccount).where(
                    WalletAccount.user_id.in_(
                        (original_owner.id, replacement_owner.id)
                    )
                )
            )
        ).scalars().all()
        wallet_after = {
            account.user_id: (
                account.posted_minor,
                account.reserved_minor,
                account.version,
            )
            for account in accounts
        }
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session.id)
            )
        ).scalar_one()
        stored_session = (
            await db.execute(select(Session).where(Session.id == session.id))
        ).scalar_one()
        attempted_operation = (
            await db.execute(
                select(BillingOperation).where(
                    BillingOperation.session_id == session.id,
                    BillingOperation.operation_id == operation_id,
                )
            )
        ).scalar_one_or_none()
        assert wallet_after == wallet_before
        assert (
            budget.cumulative_proven_ms,
            budget.cumulative_authorized_ms,
            budget.cumulative_charged_minor,
            budget.reserved_minor,
            budget.version,
            budget.status,
            budget.permit_expiry,
        ) == budget_before
        assert budget.owner_user_id == replacement_owner.id
        assert stored_session.owner_user_id == replacement_owner.id
        assert stored_session.state == "ACTIVE"
        assert attempted_operation is None




@pytest.mark.postgres
@pytest.mark.asyncio
async def test_existing_session_without_budget_returns_conflict(fresh_pg: FreshPostgres):
    """Checkpoint and close on a stored session without its budget return 409 conflicts."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session = Session(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            runner_id="runner-no-budget",
            kind="voice",
            state="ACTIVE",
            version=1,
        )
        db.add(session)
        await db.commit()

    async with fresh_pg.new_session() as db:
        with pytest.raises(ConflictError) as exc_info:
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-no-budget"),
                session_id=session.id,
                operation_id="missing-budget-checkpoint",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=1000,
                billing_slice_ms=1000,
                permit_ttl_ms=5000,
            )
        assert exc_info.value.status_code == 409

    async with fresh_pg.new_session() as db:
        with pytest.raises(ConflictError) as exc_info:
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-no-budget"),
                session_id=session.id,
                operation_id="missing-budget-close",
                expected_budget_version=1,
                cumulative_billable_ms=0,
            )
        assert exc_info.value.status_code == 409

    async with fresh_pg.new_session() as db:
        with pytest.raises(ConflictError) as exc_info:
            await expire_stopped_budget(
                db,
                session_id=session.id,
                operation_id="missing-budget-expiry",
                now=datetime.now(timezone.utc),
            )
        assert exc_info.value.status_code == 409

    async with fresh_pg.new_session() as db:
        wallet = (
            await db.execute(
                select(WalletAccount).where(WalletAccount.user_id == user.id)
            )
        ).scalar_one()
        stored_session = (
            await db.execute(select(Session).where(Session.id == session.id))
        ).scalar_one()
        assert wallet.posted_minor == 100
        assert wallet.reserved_minor == 0
        assert stored_session.state == "ACTIVE"

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_forged_runner_cannot_mutate_money(fresh_pg: FreshPostgres):
    """An unauthenticated or forged runner ID cannot mutate money, add holds, or close budgets."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="legitimate-runner")

    # Forged runner attempts checkpoint
    async with fresh_pg.new_session() as db:
        with pytest.raises(ForbiddenError, match="Runner ID does not match"):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="forged-attacker"),
                session_id=session.id,
                operation_id="forged-chk",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=5000,
                billing_slice_ms=5000,
                permit_ttl_ms=5000,
            )

    # Forged runner attempts close
    async with fresh_pg.new_session() as db:
        with pytest.raises(ForbiddenError, match="Runner ID does not match"):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="forged-attacker"),
                session_id=session.id,
                operation_id="forged-close",
                expected_budget_version=1,
                cumulative_billable_ms=0,
            )

    # Invariants prove 0 paise were spent or held by attacker
    async with fresh_pg.new_session() as db:
        wallet = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))).scalar_one()
        assert wallet.posted_minor == 200
        assert wallet.reserved_minor == 0
        await assert_wallet_invariants(db, user_id=user.id)



@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unassigned_runner_session_fails_closed(fresh_pg: FreshPostgres):
    """Session without an assigned runner fails closed and never auto-binds on checkpoint or close."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        # Session created with empty/unassigned runner
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="")

    # Checkpoint on unassigned runner session must fail closed
    async with fresh_pg.new_session() as db:
        with pytest.raises(ForbiddenError, match="Session has no assigned runner"):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-attempting-auto-bind"),
                session_id=session.id,
                operation_id="auto-bind-chk",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=5000,
                billing_slice_ms=5000,
                permit_ttl_ms=5000,
            )

    # Close on unassigned runner session must fail closed
    async with fresh_pg.new_session() as db:
        with pytest.raises(ForbiddenError, match="Session has no assigned runner"):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-attempting-auto-bind"),
                session_id=session.id,
                operation_id="auto-bind-close",
                expected_budget_version=1,
                cumulative_billable_ms=0,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cross_runner_replay_disclosure_prevented(fresh_pg: FreshPostgres):
    """Attacker knowing another runner's operation key cannot retrieve cached permit or view."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="authorized-runner")

        # Legitimate runner executes checkpoint
        permit = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="authorized-runner"),
            session_id=session.id,
            operation_id="secret-op-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=5000,
            billing_slice_ms=5000,
            permit_ttl_ms=5000,
        )
        assert permit.authorized_through_ms == 5000

    # Attacker tries to replay secret-op-1 using their own runner ID
    async with fresh_pg.new_session() as db:
        with pytest.raises(ForbiddenError, match="Runner ID does not match"):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="attacker-runner"),
                session_id=session.id,
                operation_id="secret-op-1",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=5000,
                billing_slice_ms=5000,
                permit_ttl_ms=5000,
            )
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_deployment_configured_slice_and_ttl_enforced(fresh_pg: FreshPostgres):
    """Explicit deployment-configured billing_slice_ms and permit_ttl_ms are strictly enforced."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 14, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=500)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-timing")

        # Custom deployment slice = 2500ms, TTL = 12000ms
        permit = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-timing"),
            session_id=session.id,
            operation_id="chk-custom-timing",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=2500,
            permit_ttl_ms=12000,
            now=t0,
        )
        # Capped to 2500ms exactly
        assert permit.authorized_through_ms == 2500
        expected_expiry = t0 + timedelta(milliseconds=12000)
        assert permit.expires_at == expected_expiry

        # Invalid non-positive configurations must be rejected
        with pytest.raises(ValueError, match="billing_slice_ms must be positive"):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-timing"),
                session_id=session.id,
                operation_id="chk-bad-slice",
                expected_budget_version=2,
                cumulative_billable_ms=0,
                requested_future_ms=1000,
                billing_slice_ms=0,
                permit_ttl_ms=5000,
            )

        with pytest.raises(ValueError, match="permit_ttl_ms must be positive"):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-timing"),
                session_id=session.id,
                operation_id="chk-bad-ttl",
                expected_budget_version=2,
                cumulative_billable_ms=0,
                requested_future_ms=1000,
                billing_slice_ms=1000,
                permit_ttl_ms=-50,
            )

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_http_internal_routes_fail_closed_without_task6_transport_auth(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Internal HTTP routes fail-closed (503) without accepting untrusted caller-supplied headers."""
    async with initialized_pg.sessions() as session:
        user = await create_test_user(session)
        await create_funded_wallet(session, user_id=user.id, credited_minor=200)
        s, _ = await create_test_session(session, owner_user_id=user.id, runner_id="runner-alpha")

    # 1. Checkpoint route fails closed
    chk_res = await app_client.post(
        f"/internal/sessions/{s.id}/budget/checkpoint",
        headers={"X-Runner-ID": "runner-alpha", "Content-Type": "application/json"},
        json={
            "operation_id": "http-chk-1",
            "expected_budget_version": 1,
            "cumulative_billable_ms": 0,
            "requested_future_ms": 5000,
        },
    )
    assert chk_res.status_code == 503
    assert chk_res.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

    # 2. Close route fails closed
    close_res = await app_client.post(
        f"/internal/sessions/{s.id}/budget/close",
        headers={"X-Runner-ID": "runner-alpha", "Content-Type": "application/json"},
        json={
            "operation_id": "http-close-1",
            "expected_budget_version": 1,
            "cumulative_billable_ms": 0,
            "stop_evidence": {"reason": "caller_ended"},
        },
    )
    assert close_res.status_code == 503
    assert close_res.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_http_owner_wallet_endpoints(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Owner endpoints GET /api/v1/wallet and /api/v1/wallet/entries return own balance only."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("pw-secure-12345")

    async with initialized_pg.sessions() as session:
        user_a = User(
            id=uuid.uuid4(),
            email="owner_a@example.com",
            email_key="owner_a@example.com",
            display_name="Owner A",
            password_hash=pw_hash,
            role="user",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(user_a)
        await session.flush()
        await create_funded_wallet(session, user_id=user_a.id, credited_minor=500)

        sess_a, secret_a = await create_authenticated_session(
            session, test_platform_settings, user=user_a, client_ip="127.0.0.1", user_agent="A", now=now
        )
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    headers_a = {"Cookie": f"{cookie_name}={secret_a}"}

    # GET /api/v1/wallet
    w_res = await app_client.get("/api/v1/wallet", headers=headers_a)
    assert w_res.status_code == 200
    w_data = w_res.json()
    assert w_data["user_id"] == str(user_a.id)
    assert w_data["posted_minor"] == 500
    assert w_data["reserved_minor"] == 0
    assert w_data["available_minor"] == 500

    # GET /api/v1/wallet/entries
    entries_res = await app_client.get("/api/v1/wallet/entries", headers=headers_a)
    assert entries_res.status_code == 200
    entries_data = entries_res.json()
    assert len(entries_data["items"]) == 1
    assert entries_data["items"][0]["amount_minor"] == 500



@pytest.mark.postgres
@pytest.mark.asyncio
async def test_billing_database_connection_failure_returns_retryable_503(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    monkeypatch: pytest.MonkeyPatch,
):
    now = datetime.now(timezone.utc)
    async with initialized_pg.sessions() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=0)
        _, secret = await create_authenticated_session(
            db,
            test_platform_settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="WalletOutage",
            now=now,
        )
        await db.commit()

    async def unavailable_wallet(*args, **kwargs):
        raise OperationalError(
            "SELECT wallet_accounts",
            {},
            ConnectionError("connection refused"),
        )

    monkeypatch.setattr(billing_routes, "get_wallet_view", unavailable_wallet)
    response = await app_client.get(
        "/api/v1/wallet",
        headers={
            "Cookie": (
                f"{test_platform_settings.session_cookie_name}={secret}"
            )
        },
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is True

    async def unavailable_auth(*args, **kwargs):
        raise OperationalError(
            "SELECT auth_sessions",
            {},
            ConnectionError("connection refused"),
        )

    with monkeypatch.context() as patch:
        patch.setattr(billing_routes, "require_user", unavailable_auth)
        auth_response = await app_client.get("/api/v1/wallet")
    assert auth_response.status_code == 503
    assert auth_response.json()["error"]["retryable"] is True

    async def invalid_wallet_query(*args, **kwargs):
        raise ProgrammingError("SELECT wallet_accounts", {}, Exception("bad SQL"))

    monkeypatch.setattr(billing_routes, "get_wallet_view", invalid_wallet_query)
    with pytest.raises(ProgrammingError, match="bad SQL"):
        await app_client.get(
            "/api/v1/wallet",
            headers={
                "Cookie": (
                    f"{test_platform_settings.session_cookie_name}={secret}"
                )
            },
        )

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_wallet_adjustment_atomic_and_no_duplicate_audit_on_replay(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Admin wallet adjustment, audit log, and idempotency commit atomically; replay avoids duplicate audit."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-secure-pw-12345")

    async with initialized_pg.sessions() as session:
        target_user = await create_test_user(session, email="customer@example.com")
        await create_funded_wallet(session, user_id=target_user.id, credited_minor=100)

        admin = User(
            id=uuid.uuid4(),
            email="admin@example.com",
            email_key="admin@example.com",
            display_name="Admin",
            password_hash=pw_hash,
            role="admin",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(admin)
        await session.flush()

        mfa = AuthMfa(
            user_id=admin.id,
            secret_ciphertext="encrypted-secret",
            enabled_at=now,
            last_accepted_step=1,
        )
        session.add(mfa)
        await session.flush()

        sess, secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=admin,
            client_ip="127.0.0.1",
            user_agent="AdminDevice",
            now=now,
            mfa_verified=True,
        )
        sess.step_up_at = now
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    boot_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin, "Cookie": cookie_header},
    )
    csrf = boot_res.json()["csrf_token"]

    headers = {
        "Origin": test_platform_settings.web_origin,
        "X-CSRF-Token": csrf,
        "Cookie": cookie_header,
        "Idempotency-Key": "admin-adj-key-atomic",
        "Content-Type": "application/json",
    }
    payload = {"amount_minor": 150, "reason": "Administrative goodwill credit for outage"}

    # 1. First adjustment call
    res1 = await app_client.post(
        f"/api/v1/admin/users/{target_user.id}/wallet-adjustments",
        headers=headers,
        json=payload,
    )
    assert res1.status_code == 200
    assert res1.json()["posted_minor"] == 250

    # Verify exactly 1 audit log and 1 wallet entry in database
    async with initialized_pg.sessions() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.target_id == target_user.id,
                    AuditLog.action == "wallet.adjustment",
                )
            )
        ).scalars().all()
        assert len(audits) == 1

        entries = (
            await session.execute(
                select(WalletEntry).where(WalletEntry.user_id == target_user.id)
            )
        ).scalars().all()
        # initial funding entry + adjustment entry = 2
        assert len(entries) == 2

    # 2. Replay adjustment with exact same Idempotency-Key
    res2 = await app_client.post(
        f"/api/v1/admin/users/{target_user.id}/wallet-adjustments",
        headers=headers,
        json=payload,
    )
    assert res2.status_code == 200
    assert res2.json() == res1.json()

    # Verify STILL exactly 1 audit log (no duplicate audit on replay)
    async with initialized_pg.sessions() as session:
        audits_after = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.target_id == target_user.id,
                    AuditLog.action == "wallet.adjustment",
                )
            )
        ).scalars().all()
        assert len(audits_after) == 1
        await assert_wallet_invariants(session, user_id=target_user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_simultaneous_same_key_adjustments_replayed_without_duplicate_audit(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Simultaneous concurrent adjustments with same Idempotency-Key return exact replay and single audit."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-secure-pw-12345")

    async with initialized_pg.sessions() as session:
        target_user = await create_test_user(session, email="customer2@example.com")
        await create_funded_wallet(session, user_id=target_user.id, credited_minor=100)

        admin = User(
            id=uuid.uuid4(),
            email="admin2@example.com",
            email_key="admin2@example.com",
            display_name="Admin 2",
            password_hash=pw_hash,
            role="admin",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(admin)
        await session.flush()

        mfa = AuthMfa(
            user_id=admin.id,
            secret_ciphertext="encrypted-secret",
            enabled_at=now,
            last_accepted_step=1,
        )
        session.add(mfa)
        await session.flush()

        sess, secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=admin,
            client_ip="127.0.0.1",
            user_agent="AdminDevice",
            now=now,
            mfa_verified=True,
        )
        sess.step_up_at = now
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    boot_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin, "Cookie": cookie_header},
    )
    csrf = boot_res.json()["csrf_token"]

    headers = {
        "Origin": test_platform_settings.web_origin,
        "X-CSRF-Token": csrf,
        "Cookie": cookie_header,
        "Idempotency-Key": "simultaneous-adj-key",
        "Content-Type": "application/json",
    }
    payload = {"amount_minor": 100, "reason": "Simultaneous adjustment test"}

    # Concurrent gather of both calls
    results = await asyncio.gather(
        app_client.post(f"/api/v1/admin/users/{target_user.id}/wallet-adjustments", headers=headers, json=payload),
        app_client.post(f"/api/v1/admin/users/{target_user.id}/wallet-adjustments", headers=headers, json=payload),
    )

    assert results[0].status_code == 200
    assert results[1].status_code == 200
    assert results[0].json() == results[1].json()
    assert results[0].json()["posted_minor"] == 200

    # Only one audit log and one wallet entry
    async with initialized_pg.sessions() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.target_id == target_user.id,
                    AuditLog.action == "wallet.adjustment",
                )
            )
        ).scalars().all()
        assert len(audits) == 1
        await assert_wallet_invariants(session, user_id=target_user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_simultaneous_conflicting_key_adjustments_rejected(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Simultaneous concurrent adjustments with same Idempotency-Key but different payloads: one 200, one 409."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-secure-pw-12345")

    async with initialized_pg.sessions() as session:
        target_user = await create_test_user(session, email="customer3@example.com")
        await create_funded_wallet(session, user_id=target_user.id, credited_minor=100)

        admin = User(
            id=uuid.uuid4(),
            email="admin3@example.com",
            email_key="admin3@example.com",
            display_name="Admin 3",
            password_hash=pw_hash,
            role="admin",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(admin)
        await session.flush()

        mfa = AuthMfa(
            user_id=admin.id,
            secret_ciphertext="encrypted-secret",
            enabled_at=now,
            last_accepted_step=1,
        )
        session.add(mfa)
        await session.flush()

        sess, secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=admin,
            client_ip="127.0.0.1",
            user_agent="AdminDevice",
            now=now,
            mfa_verified=True,
        )
        sess.step_up_at = now
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    boot_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin, "Cookie": cookie_header},
    )
    csrf = boot_res.json()["csrf_token"]

    headers = {
        "Origin": test_platform_settings.web_origin,
        "X-CSRF-Token": csrf,
        "Cookie": cookie_header,
        "Idempotency-Key": "simultaneous-adj-conflict",
        "Content-Type": "application/json",
    }
    payload_a = {"amount_minor": 100, "reason": "First payload"}
    payload_b = {"amount_minor": 200, "reason": "Conflicting payload"}

    results = await asyncio.gather(
        app_client.post(f"/api/v1/admin/users/{target_user.id}/wallet-adjustments", headers=headers, json=payload_a),
        app_client.post(f"/api/v1/admin/users/{target_user.id}/wallet-adjustments", headers=headers, json=payload_b),
    )

    statuses = {results[0].status_code, results[1].status_code}
    assert statuses == {200, 409}

    conflict_res = results[0] if results[0].status_code == 409 else results[1]
    assert conflict_res.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    # Only one adjustment applied in database
    async with initialized_pg.sessions() as session:
        await assert_wallet_invariants(session, user_id=target_user.id)

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_http_admin_debit_below_reservation_rejected(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Admin HTTP adjustment endpoint rejects debits that violate outstanding reservation holds with 409."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("admin-secure-pw-12345")

    async with initialized_pg.sessions() as session:
        target_user = await create_test_user(session, email="debit_cust@example.com")
        await create_funded_wallet(session, user_id=target_user.id, credited_minor=100)
        s, _ = await create_test_session(session, owner_user_id=target_user.id, runner_id="runner-debit-test")

        # Reserve 10,000ms (34 paise hold, 66 paise available)
        await checkpoint_and_reserve(
            session,
            runtime_principal=RuntimePrincipal(runner_id="runner-debit-test"),
            session_id=s.id,
            operation_id="chk-debit-hold",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
        )

        admin = User(
            id=uuid.uuid4(),
            email="admin_debit@example.com",
            email_key="admin_debit@example.com",
            display_name="Admin Debit",
            password_hash=pw_hash,
            role="admin",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(admin)
        await session.flush()

        mfa = AuthMfa(
            user_id=admin.id,
            secret_ciphertext="encrypted-secret",
            enabled_at=now,
            last_accepted_step=1,
        )
        session.add(mfa)
        await session.flush()

        sess, secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=admin,
            client_ip="127.0.0.1",
            user_agent="AdminDevice",
            now=now,
            mfa_verified=True,
        )
        sess.step_up_at = now
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    boot_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin, "Cookie": cookie_header},
    )
    csrf = boot_res.json()["csrf_token"]

    headers = {
        "Origin": test_platform_settings.web_origin,
        "X-CSRF-Token": csrf,
        "Cookie": cookie_header,
        "Idempotency-Key": "admin-debit-below-res-key",
        "Content-Type": "application/json",
    }

    # Attempt debit of -80 paise (posted 100 - 80 = 20 < 34 held) -> 409 RESERVATION_HOLD
    res = await app_client.post(
        f"/api/v1/admin/users/{target_user.id}/wallet-adjustments",
        headers=headers,
        json={"amount_minor": -80, "reason": "Administrative debit exceeding available balance"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "RESERVATION_HOLD"

    # Attempt debit of -50 paise (posted 100 - 50 = 50 >= 34 held) -> 200 OK
    headers["Idempotency-Key"] = "admin-debit-valid-key"
    res_valid = await app_client.post(
        f"/api/v1/admin/users/{target_user.id}/wallet-adjustments",
        headers=headers,
        json={"amount_minor": -50, "reason": "Administrative debit within available balance"},
    )
    assert res_valid.status_code == 200
    assert res_valid.json()["posted_minor"] == 50
    assert res_valid.json()["reserved_minor"] == 34
    assert res_valid.json()["available_minor"] == 16

    async with initialized_pg.sessions() as session:
        await assert_wallet_invariants(session, user_id=target_user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_duplicate_meter_update_has_one_usage_effect(fresh_pg: FreshPostgres):
    """Replaying one checkpoint records one operation, one checkpoint row and one charge."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        session, _ = await create_test_session(
            db, owner_user_id=user.id, runner_id="runner-dup-meter", created_at=t0
        )

        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-dup-meter"),
            session_id=session.id,
            operation_id="grant-dup-meter",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )

        first = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-dup-meter"),
            session_id=session.id,
            operation_id="usage-dup-meter",
            expected_budget_version=2,
            cumulative_billable_ms=3000,
            requested_future_ms=7000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0 + timedelta(seconds=1),
        )
        second = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-dup-meter"),
            session_id=session.id,
            operation_id="usage-dup-meter",
            expected_budget_version=2,
            cumulative_billable_ms=3000,
            requested_future_ms=7000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0 + timedelta(seconds=1),
        )
        assert first == second
        assert first.proven_ms == 3000
        assert first.reserved_minor == 24

        checkpoints = list(
            (
                await db.execute(
                    select(MeterCheckpoint).where(
                        MeterCheckpoint.session_id == session.id,
                        MeterCheckpoint.operation_id == "usage-dup-meter",
                    )
                )
            ).scalars().all()
        )
        operations = list(
            (
                await db.execute(
                    select(BillingOperation).where(
                        BillingOperation.session_id == session.id,
                        BillingOperation.operation_id == "usage-dup-meter",
                    )
                )
            ).scalars().all()
        )
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
        wallet = (
            await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))
        ).scalar_one()

        assert len(checkpoints) == 1
        assert len(operations) == 1
        assert len(charges) == 1
        assert charges[0].amount_minor == -10
        assert wallet.posted_minor == 190
        assert wallet.reserved_minor == 24
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_meter_outage_blocks_unconfirmed_chargeable_work(
    fresh_pg: FreshPostgres, monkeypatch: pytest.MonkeyPatch
):
    """A failed durability commit yields no permit and leaves no usage or hold behind."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        session, _ = await create_test_session(
            db, owner_user_id=user.id, runner_id="runner-outage"
        )

    async with fresh_pg.new_session() as db:
        async def failing_commit() -> None:
            raise RuntimeError("simulated database outage at commit")

        monkeypatch.setattr(db, "commit", failing_commit)
        with pytest.raises(RuntimeError, match="simulated database outage at commit"):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-outage"),
                session_id=session.id,
                operation_id="outage-checkpoint",
                expected_budget_version=1,
                cumulative_billable_ms=0,
                requested_future_ms=5000,
                billing_slice_ms=5000,
                permit_ttl_ms=5000,
            )

    async with fresh_pg.new_session() as db:
        checkpoints = list(
            (
                await db.execute(
                    select(MeterCheckpoint).where(
                        MeterCheckpoint.session_id == session.id
                    )
                )
            ).scalars().all()
        )
        operations = list(
            (
                await db.execute(
                    select(BillingOperation).where(
                        BillingOperation.session_id == session.id
                    )
                )
            ).scalars().all()
        )
        wallet = (
            await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))
        ).scalar_one()
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session.id)
            )
        ).scalar_one()

        assert checkpoints == []
        assert operations == []
        assert wallet.posted_minor == 200
        assert wallet.reserved_minor == 0
        assert budget.version == 1
        assert budget.status == "OPEN"
        assert budget.reserved_minor == 0
        assert budget.cumulative_proven_ms == 0
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.asyncio
async def test_billing_route_surface_matches_contract(
    test_platform_settings: PlatformSettings,
):
    """The platform app mounts exactly the five contract billing routes and methods."""
    app = create_app(test_platform_settings)
    paths = app.openapi()["paths"]

    billing_paths = {}
    for path, operations in paths.items():
        if (
            path in {
                "/api/v1/wallet",
                "/api/v1/wallet/entries",
            }
            or path.endswith("/wallet-adjustments")
            or path.startswith("/internal/sessions/")
        ):
            billing_paths[path] = frozenset(
                method
                for method in operations
                if method in {"get", "post", "patch", "put", "delete"}
            )

    assert billing_paths == {
        "/api/v1/wallet": frozenset({"get"}),
        "/api/v1/wallet/entries": frozenset({"get"}),
        "/api/v1/admin/users/{user_id}/wallet-adjustments": frozenset({"post"}),
        "/internal/sessions/{session_id}/budget/checkpoint": frozenset({"post"}),
        "/internal/sessions/{session_id}/budget/close": frozenset({"post"}),
    }


@pytest.mark.asyncio
async def test_wallet_routes_require_authentication(app_client: httpx.AsyncClient):
    """Owner wallet reads and admin adjustments reject unauthenticated callers."""
    wallet_res = await app_client.get("/api/v1/wallet")
    assert wallet_res.status_code == 401
    assert wallet_res.json()["error"]["code"] == "UNAUTHENTICATED"

    entries_res = await app_client.get("/api/v1/wallet/entries")
    assert entries_res.status_code == 401
    assert entries_res.json()["error"]["code"] == "UNAUTHENTICATED"

    admin_res = await app_client.post(
        f"/api/v1/admin/users/{uuid.uuid4()}/wallet-adjustments",
        json={"amount_minor": 100, "reason": "Unauthenticated adjustment"},
    )
    assert admin_res.status_code == 401
    assert admin_res.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_normal_user_cannot_call_admin_wallet_adjustment(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """A normal user receives 403 and cannot change another user's wallet."""
    now = datetime.now(timezone.utc)

    async with initialized_pg.sessions() as session:
        normal_user = await create_test_user(session, email="normal_user@example.com")
        target_user = await create_test_user(session, email="normal_target@example.com")
        await create_funded_wallet(session, user_id=normal_user.id, credited_minor=100)
        await create_funded_wallet(session, user_id=target_user.id, credited_minor=100)

        _, secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=normal_user,
            client_ip="127.0.0.1",
            user_agent="NormalUser",
            now=now,
        )
        await session.commit()

    cookie = f"{test_platform_settings.session_cookie_name}={secret}"
    boot = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": test_platform_settings.web_origin, "Cookie": cookie},
    )
    csrf = boot.json()["csrf_token"]

    res = await app_client.post(
        f"/api/v1/admin/users/{target_user.id}/wallet-adjustments",
        headers={
            "Origin": test_platform_settings.web_origin,
            "X-CSRF-Token": csrf,
            "Cookie": cookie,
            "Idempotency-Key": "normal-user-admin-attempt",
            "Content-Type": "application/json",
        },
        json={"amount_minor": 50, "reason": "Normal user attempts admin credit"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"

    async with initialized_pg.sessions() as session:
        wallet = (
            await session.execute(
                select(WalletAccount).where(WalletAccount.user_id == target_user.id)
            )
        ).scalar_one()
        entries = list(
            (
                await session.execute(
                    select(WalletEntry).where(WalletEntry.user_id == target_user.id)
                )
            ).scalars().all()
        )
        assert wallet.posted_minor == 100
        assert wallet.reserved_minor == 0
        assert len(entries) == 1
        await assert_wallet_invariants(session, user_id=target_user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_wallet_entries_owner_isolation_and_stable_pagination(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Each owner sees only their own ledger, with stable cursor pagination."""
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(minutes=1)

    async with initialized_pg.sessions() as session:
        user_a = await create_test_user(session, email="ledger_a@example.com")
        user_b = await create_test_user(session, email="ledger_b@example.com")
        await create_funded_wallet(session, user_id=user_a.id, credited_minor=0)
        await create_funded_wallet(session, user_id=user_b.id, credited_minor=100)

        for index, amount in enumerate((100, 50, 25), start=1):
            await credit_wallet(
                session,
                actor="admin:test",
                user_id=user_a.id,
                amount_minor=amount,
                reason=f"Pagination credit number {index}",
                operation_id=f"page-a-{index}",
                now=t0 + timedelta(seconds=index),
            )

        _, secret_a = await create_authenticated_session(
            session,
            test_platform_settings,
            user=user_a,
            client_ip="127.0.0.1",
            user_agent="LedgerA",
            now=now,
        )
        _, secret_b = await create_authenticated_session(
            session,
            test_platform_settings,
            user=user_b,
            client_ip="127.0.0.1",
            user_agent="LedgerB",
            now=now,
        )
        await session.commit()

    cookie_name = test_platform_settings.session_cookie_name
    headers_a = {"Cookie": f"{cookie_name}={secret_a}"}
    headers_b = {"Cookie": f"{cookie_name}={secret_b}"}

    page_one = await app_client.get(
        "/api/v1/wallet/entries", params={"limit": 2}, headers=headers_a
    )
    assert page_one.status_code == 200
    body_one = page_one.json()
    assert len(body_one["items"]) == 2
    assert body_one["next_cursor"]

    page_two = await app_client.get(
        "/api/v1/wallet/entries",
        params={"limit": 2, "cursor": body_one["next_cursor"]},
        headers=headers_a,
    )
    assert page_two.status_code == 200
    body_two = page_two.json()
    assert len(body_two["items"]) == 1
    assert body_two["next_cursor"] is None

    ids_a = {item["id"] for item in body_one["items"] + body_two["items"]}
    assert len(ids_a) == 3
    assert all(
        item["user_id"] == str(user_a.id)
        for item in body_one["items"] + body_two["items"]
    )

    page_b = await app_client.get(
        "/api/v1/wallet/entries", params={"limit": 50}, headers=headers_b
    )
    assert page_b.status_code == 200
    items_b = page_b.json()["items"]
    assert len(items_b) == 1
    assert items_b[0]["user_id"] == str(user_b.id)
    assert items_b[0]["id"] not in ids_a


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_wallet_entries_invalid_cursor_and_limit_rejected(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Malformed, timezone-naive and invalid-id cursors plus out-of-range limits return 422."""
    now = datetime.now(timezone.utc)

    async with initialized_pg.sessions() as session:
        user = await create_test_user(session, email="cursor_user@example.com")
        await create_funded_wallet(session, user_id=user.id, credited_minor=100)
        _, secret = await create_authenticated_session(
            session,
            test_platform_settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="CursorUser",
            now=now,
        )
        await session.commit()

    headers = {"Cookie": f"{test_platform_settings.session_cookie_name}={secret}"}

    def encode_cursor(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    invalid_cursors = [
        "not-a-cursor",
        encode_cursor(
            {"t": "2026-01-01T00:00:00", "id": str(uuid.uuid4())}
        ),
        encode_cursor(
            {"t": "2026-01-01T00:00:00+00:00", "id": "not-a-uuid"}
        ),
        encode_cursor({"t": "not-a-time", "id": str(uuid.uuid4())}),
    ]
    for cursor in invalid_cursors:
        res = await app_client.get(
            "/api/v1/wallet/entries",
            params={"cursor": cursor},
            headers=headers,
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION"

    for limit in (0, 101):
        res = await app_client.get(
            "/api/v1/wallet/entries",
            params={"limit": limit},
            headers=headers,
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION"
