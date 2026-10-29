"""Recovery, crash semantics, and runner loss write-off tests against fresh PostgreSQL.

Follows accounting-contract.md and verification-plan.md Phase 5:
- Crash/rollback before commit leaves no partial hold.
- Crash after settlement does not double charge.
- Unknown usage is not guessed or automatically charged.
- Lost runner budget expiry waits for permit expiration, waives unproven time, and releases holds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
import pytest
from sqlalchemy import select

from platform_api.billing.models import CallBudget, WalletAccount, WalletEntry
from platform_api.models.session import Session
from platform_api.billing.service import (
    ConflictError,
    RuntimePrincipal,
    checkpoint_and_reserve,
    close_budget,
    expire_stopped_budget,
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
async def test_crash_before_commit_leaves_no_partial_hold(fresh_pg: FreshPostgres):
    """An uncommitted exception/rollback leaves the wallet and call budget completely unchanged."""
    await fresh_pg.initialize()

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

    # Attempt operation that crashes/aborts inside a transaction
    try:
        async with fresh_pg.new_session() as db:
            # Manually simulate uncommitted hold followed by an exception
            wallet = (
                await db.execute(
                    select(WalletAccount).where(WalletAccount.user_id == user.id).with_for_update()
                )
            ).scalar_one()
            wallet.reserved_minor = 50
            raise RuntimeError("Simulated crash before commit")
    except RuntimeError:
        pass

    # Verify state rolled back cleanly
    async with fresh_pg.new_session() as db:
        wallet = (
            await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))
        ).scalar_one()
        assert wallet.posted_minor == 100
        assert wallet.reserved_minor == 0
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_crash_after_settlement_commit_does_not_double_charge(fresh_pg: FreshPostgres):
    """If client drops after settlement commit, retrying does not create a double debit."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # Initial grant 10,000ms expiring at t0 + 5s
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="grant-1",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )

        # Settle & close after permit expiry at t0 + 6s
        v1 = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="close-1",
            expected_budget_version=2,
            cumulative_billable_ms=10000,
            stop_evidence={"session_id": str(session.id), "runner_id": "runner-1", "reason": "normal_exit"},
            now=t0 + timedelta(seconds=6),
        )
        assert v1.charged_minor == 34

    # Reconnect in a fresh DB session and replay close
    async with fresh_pg.new_session() as db:
        v2 = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="close-1",
            expected_budget_version=2,
            cumulative_billable_ms=10000,
            stop_evidence={"session_id": str(session.id), "runner_id": "runner-1", "reason": "normal_exit"},
            now=t0 + timedelta(seconds=6),
        )
        assert v1 == v2
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unknown_usage_is_not_guessed_or_automatically_refunded(fresh_pg: FreshPostgres):
    """Lost runner budget write-off waits for permit expiry, waives unproven time, and releases holds."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="lost-runner")

        # Initial grant reserving 10,000ms (34 paise)
        permit = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="lost-runner"),
            session_id=session.id,
            operation_id="init-lost-grant",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        assert permit.reserved_minor == 34
        expiry = permit.expires_at

        # 1. Attempting to write off BEFORE permit expiry is rejected with 409
        t_before = expiry - timedelta(seconds=1)
        with pytest.raises(ConflictError, match="Permit has not expired yet"):
            await expire_stopped_budget(
                db,
                session_id=session.id,
                operation_id="expire-early",
                now=t_before,
            )

        # 2. After permit expiry, expire_stopped_budget succeeds cleanly
        t_after = expiry + timedelta(seconds=1)
        expired_view = await expire_stopped_budget(
            db,
            session_id=session.id,
            operation_id="expire-success",
            now=t_after,
        )
        assert expired_view.status == "CLOSED"
        assert expired_view.waived_unknown_usage is True
        assert expired_view.reserved_minor == 0
        assert expired_view.proven_ms == 0
        assert expired_view.charged_minor == 0

        # Unproven time was waived; balance returns to full 100 paise
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_close_budget_empty_and_forged_stop_with_active_permit_rejected(fresh_pg: FreshPostgres):
    """When permit is still live, empty or forged stop evidence is rejected and no reservation is released."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # Grant 10,000ms with permit expiry t0 + 5000ms (held = 34 paise)
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="chk-live-permit",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )

    # At t0 + 1s (permit still active until t0 + 5s)
    t_live = t0 + timedelta(seconds=1)

    # 1. Empty stop evidence (None) must be rejected
    async with fresh_pg.new_session() as db:
        with pytest.raises(ConflictError, match="Cannot close active budget and release reservations while permit is still live"):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="close-empty-stop",
                expected_budget_version=2,
                cumulative_billable_ms=1000,
                stop_evidence=None,
                now=t_live,
            )

    # 2. Forged runner in stop evidence must be rejected
    async with fresh_pg.new_session() as db:
        with pytest.raises(ConflictError, match="Cannot close active budget and release reservations while permit is still live"):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="close-forged-runner",
                expected_budget_version=2,
                cumulative_billable_ms=1000,
                stop_evidence={"session_id": session.id, "runner_id": "forged-runner", "reason": "user_hangup"},
                now=t_live,
            )

    # 3. Forged session_id in stop evidence must be rejected
    async with fresh_pg.new_session() as db:
        with pytest.raises(ConflictError, match="Cannot close active budget and release reservations while permit is still live"):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="close-forged-session",
                expected_budget_version=2,
                cumulative_billable_ms=1000,
                stop_evidence={"session_id": uuid.uuid4(), "runner_id": "runner-1", "reason": "user_hangup"},
                now=t_live,
            )

    # 4. Unbound stop evidence (missing session_id/runner_id) must be rejected
    async with fresh_pg.new_session() as db:
        with pytest.raises(ConflictError, match="Cannot close active budget and release reservations while permit is still live"):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="close-unbound-stop",
                expected_budget_version=2,
                cumulative_billable_ms=1000,
                stop_evidence={"reason": "user_hangup"},
                now=t_live,
            )

    # Verify 0 funds were released; reservation remains fully held
    async with fresh_pg.new_session() as db:
        wallet = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))).scalar_one()
        assert wallet.posted_minor == 100
        assert wallet.reserved_minor == 34
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_matching_ids_in_stop_evidence_cannot_release_active_permit(fresh_pg: FreshPostgres):
    """Even matching session/runner IDs in client JSON cannot release an active permit before Task 6."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="chk-genuine-stop",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )

    # At t0 + 1s (permit still live until t0 + 5s)
    t_live = t0 + timedelta(seconds=1)
    async with fresh_pg.new_session() as db:
        # Even with matching session_id, runner_id, and valid reason, fails closed until expiry
        with pytest.raises(ConflictError, match="Cannot close active budget and release reservations while permit is still live"):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="close-genuine-stop",
                expected_budget_version=2,
                cumulative_billable_ms=3000,
                stop_evidence={"session_id": str(session.id), "runner_id": "runner-1", "reason": "user_hangup"},
                now=t_live,
            )

        # Balance and reservation remain fully held
        wallet = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))).scalar_one()
        assert wallet.posted_minor == 100
        assert wallet.reserved_minor == 34
        await assert_wallet_invariants(db, user_id=user.id)

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_close_budget_expired_permit_succeeds_without_stop_evidence(fresh_pg: FreshPostgres):
    """After permit expiry, close_budget releases remaining reservation even without stop evidence."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="chk-expiry-close",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )

    # After permit expiry (t0 + 6s > t0 + 5s)
    t_expired = t0 + timedelta(seconds=6)
    async with fresh_pg.new_session() as db:
        view = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="close-after-expiry",
            expected_budget_version=2,
            cumulative_billable_ms=3000,
            stop_evidence=None,
            now=t_expired,
        )
        assert view.status == "CLOSED"
        assert view.charged_minor == 10
        assert view.reserved_minor == 0
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_checkpoint_replay_allowed_on_terminal_session_without_new_authority(fresh_pg: FreshPostgres):
    """Replay of committed checkpoint returns recorded permit even on terminal session, but new authority is rejected."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-1")

        # Checkpoint commits successfully
        permit1 = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="chk-before-term",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )

        # Session closes and becomes terminal after permit expiry
        await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="close-term-op",
            expected_budget_version=2,
            cumulative_billable_ms=10000,
            stop_evidence={"session_id": str(session.id), "runner_id": "runner-1", "reason": "normal_exit"},
            now=t0 + timedelta(seconds=6),
        )

    # Replaying the exact same checkpoint on the now-terminal session returns original recorded result
    async with fresh_pg.new_session() as db:
        permit_replay = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-1"),
            session_id=session.id,
            operation_id="chk-before-term",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0 + timedelta(seconds=7),
        )
        # Attempting a NEW checkpoint on the terminal session is strictly rejected
        with pytest.raises(ConflictError, match="Call budget is closed|Session is terminal"):
            await checkpoint_and_reserve(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-1"),
                session_id=session.id,
                operation_id="new-chk-on-terminal",
                expected_budget_version=3,
                cumulative_billable_ms=10000,
                requested_future_ms=5000,
                billing_slice_ms=10000,
                permit_ttl_ms=5000,
                now=t0 + timedelta(seconds=8),
            )

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reconnect_gap_is_uncharged(fresh_pg: FreshPostgres):
    """Disconnected reconnect gap is uncharged; only proven active audio milliseconds are billed."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=100)
        session, _ = await create_test_session(db, owner_user_id=user.id, runner_id="runner-reconnect", pinned_rate=200)

        # 1. Initial grant for 5,000ms
        p1 = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-reconnect"),
            session_id=session.id,
            operation_id="chk-before-reconnect",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=5000,
            billing_slice_ms=5000,
            permit_ttl_ms=5000,
            now=t0,
        )
        assert p1.authorized_through_ms == 5000

        # 2. Checkpoint at 3,000ms active time (t0 + 3s)
        t_active1 = t0 + timedelta(seconds=3)
        p2 = await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-reconnect"),
            session_id=session.id,
            operation_id="chk-reconnect-active1",
            expected_budget_version=2,
            cumulative_billable_ms=3000,
            requested_future_ms=5000,
            billing_slice_ms=5000,
            permit_ttl_ms=5000,
            now=t_active1,
        )
        # 3,000ms cost @ 200/min = (200 * 3000 + 59999) // 60000 = 10 paise
        await assert_wallet_invariants(db, user_id=user.id)

    # 3. Disconnect happens. Wall-clock advances by 15 seconds!
    # Reconnect occurs, runner resumes media and proves active audio up to 4,500ms total (only +1500ms active).
    # Close after permit expiry (t_active1 + 5s = expiry, so t_active1 + 15s is well past permit expiry).
    t_reconnected_close = t_active1 + timedelta(seconds=15)
    async with fresh_pg.new_session() as db:
        close_view = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-reconnect"),
            session_id=session.id,
            operation_id="close-reconnected",
            expected_budget_version=3,
            cumulative_billable_ms=4500,
            stop_evidence={"reason": "normal_stop_after_reconnect"},
            now=t_reconnected_close,
        )
        # Total charged for 4,500ms @ 200 paise/min:
        # (200 * 4500 + 59999) // 60000 = 15 paise
        assert close_view.charged_minor == 15
        assert close_view.proven_ms == 4500
        assert close_view.reserved_minor == 0
        assert close_view.status == "CLOSED"

        # Disconnected 15 seconds of wall clock is 100% uncharged:
        # User balance is exactly 100 - 15 = 85 paise.
        wallet = (await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))).scalar_one()
        assert wallet.posted_minor == 85
        assert wallet.reserved_minor == 0
        await assert_wallet_invariants(db, user_id=user.id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_crash_after_usage_persist_recovers_one_settlement(fresh_pg: FreshPostgres):
    """After a runner crash, recovery settles committed usage once and never guesses unproven time."""
    await fresh_pg.initialize()
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with fresh_pg.new_session() as db:
        user = await create_test_user(db)
        await create_funded_wallet(db, user_id=user.id, credited_minor=200)
        session, _ = await create_test_session(
            db, owner_user_id=user.id, runner_id="runner-recover", created_at=t0
        )

        # A checkpoint commits 3,000ms of proven usage, then the runner dies.
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-recover"),
            session_id=session.id,
            operation_id="grant-recover",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0,
        )
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-recover"),
            session_id=session.id,
            operation_id="usage-recover",
            expected_budget_version=2,
            cumulative_billable_ms=3000,
            requested_future_ms=7000,
            billing_slice_ms=10000,
            permit_ttl_ms=5000,
            now=t0 + timedelta(seconds=1),
        )

    t_recover = t0 + timedelta(seconds=7)
    async with fresh_pg.new_session() as db:
        view = await expire_stopped_budget(
            db,
            session_id=session.id,
            operation_id="expire-recover-1",
            now=t_recover,
        )
        assert view.status == "CLOSED"
        assert view.proven_ms == 3000
        assert view.charged_minor == 10
        assert view.reserved_minor == 0
        assert view.waived_unknown_usage is True
        assert view.stop_evidence == {"reason": "runner_lost_permit_expired"}

        replay = await expire_stopped_budget(
            db,
            session_id=session.id,
            operation_id="expire-recover-1",
            now=t_recover,
        )
        assert replay == view

        # A new recovery key on an already-closed budget is a no-op, not a second write-off.
        second = await expire_stopped_budget(
            db,
            session_id=session.id,
            operation_id="expire-recover-2",
            now=t_recover + timedelta(seconds=1),
        )
        assert second == view

        # A late close may report only the already-committed usage; a larger
        # report cannot create a surprise debit after closure.
        late = await close_budget(
            db,
            runtime_principal=RuntimePrincipal(runner_id="runner-recover"),
            session_id=session.id,
            operation_id="late-close",
            expected_budget_version=view.budget_version,
            cumulative_billable_ms=3000,
            now=t_recover + timedelta(seconds=2),
        )
        assert late == view

        with pytest.raises(ConflictError, match="Late charge after budget closure"):
            await close_budget(
                db,
                runtime_principal=RuntimePrincipal(runner_id="runner-recover"),
                session_id=session.id,
                operation_id="late-close-larger",
                expected_budget_version=view.budget_version,
                cumulative_billable_ms=4000,
                now=t_recover + timedelta(seconds=3),
            )

    async with fresh_pg.new_session() as db:
        wallet = (
            await db.execute(select(WalletAccount).where(WalletAccount.user_id == user.id))
        ).scalar_one()
        budget = (
            await db.execute(select(CallBudget).where(CallBudget.session_id == session.id))
        ).scalar_one()
        session_row = (
            await db.execute(select(Session).where(Session.id == session.id))
        ).scalar_one()
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
        assert wallet.posted_minor == 190
        assert wallet.reserved_minor == 0
        assert budget.status == "CLOSED"
        assert budget.cumulative_proven_ms == 3000
        assert budget.cumulative_charged_minor == 10
        assert budget.reserved_minor == 0
        assert session_row.state == "EXPIRED"
        assert session_row.failure_code == "RUNNER_LOST"
        assert len(charges) == 1
        assert charges[0].amount_minor == -10
        await assert_wallet_invariants(db, user_id=user.id)
