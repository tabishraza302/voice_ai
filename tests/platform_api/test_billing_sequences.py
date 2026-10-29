"""Property-based randomized sequence tests verifying continuous ledger invariants.

Follows accounting-contract.md and verification-plan.md Phase 5:
- Seeded pseudo-random financial sequences with fixed seed.
- Multi-session, multi-owner operations.
- Continuous verification of all 6 wallet/budget/ledger invariants after every single commit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random
import uuid

import pytest

from platform_api.billing.service import (
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
async def test_seeded_financial_sequences_preserve_committed_invariants(fresh_pg: FreshPostgres):
    """Execute a recorded randomized sequence of financial operations across two owners and multiple sessions.

    Asserts all ledger and reservation invariants after each and every transition.
    """
    await fresh_pg.initialize()
    rng = random.Random(133742)
    t = datetime(2026, 10, 28, 10, 0, 0, tzinfo=timezone.utc)

    # Setup Owner A and Owner B
    async with fresh_pg.new_session() as db:
        user_a = await create_test_user(db, email="owner_a_seq@example.com")
        user_b = await create_test_user(db, email="owner_b_seq@example.com")
        await create_funded_wallet(db, user_id=user_a.id, credited_minor=1000)
        await create_funded_wallet(db, user_id=user_b.id, credited_minor=1000)

        # Owner A has 2 sessions, Owner B has 1 session
        s_a1, _ = await create_test_session(db, owner_user_id=user_a.id, runner_id="r-a1", pinned_rate=200)
        s_a2, _ = await create_test_session(db, owner_user_id=user_a.id, runner_id="r-a2", pinned_rate=100)
        s_b1, _ = await create_test_session(db, owner_user_id=user_b.id, runner_id="r-b1", pinned_rate=300)

    sessions_meta = {
        s_a1.id: {"user_id": user_a.id, "runner_id": "r-a1", "proven_ms": 0, "auth_ms": 0, "ver": 1, "closed": False, "expires_at": t},
        s_a2.id: {"user_id": user_a.id, "runner_id": "r-a2", "proven_ms": 0, "auth_ms": 0, "ver": 1, "closed": False, "expires_at": t},
        s_b1.id: {"user_id": user_b.id, "runner_id": "r-b1", "proven_ms": 0, "auth_ms": 0, "ver": 1, "closed": False, "expires_at": t},
    }
    op_counter = 0

    # Execute 30 transitions
    for step in range(30):
        t += timedelta(seconds=1)
        op_counter += 1
        action_type = rng.choice(["credit", "checkpoint", "close"])

        async with fresh_pg.new_session() as db:
            if action_type == "credit":
                target_user = rng.choice([user_a, user_b])
                amt = rng.randint(50, 300)
                await credit_wallet(
                    db,
                    actor="admin:seq",
                    user_id=target_user.id,
                    amount_minor=amt,
                    reason=f"Seeded topup at step {step}",
                    operation_id=f"seq-credit-{op_counter}",
                    now=t,
                )

            elif action_type == "checkpoint":
                active_sids = [sid for sid, m in sessions_meta.items() if not m["closed"]]
                if not active_sids:
                    continue
                sid = rng.choice(active_sids)
                meta = sessions_meta[sid]

                # Progress proven time up to authorized time
                max_prove = meta["auth_ms"]
                new_proven = rng.randint(meta["proven_ms"], max_prove) if max_prove > meta["proven_ms"] else meta["proven_ms"]
                future_req = rng.randint(1000, 5000)

                permit = await checkpoint_and_reserve(
                    db,
                    runtime_principal=RuntimePrincipal(runner_id=meta["runner_id"]),
                    session_id=sid,
                    operation_id=f"seq-chk-{op_counter}",
                    expected_budget_version=meta["ver"],
                    cumulative_billable_ms=new_proven,
                    requested_future_ms=future_req,
                    billing_slice_ms=5000,
                    permit_ttl_ms=5000,
                    now=t,
                )
                meta["proven_ms"] = permit.proven_ms
                meta["auth_ms"] = permit.authorized_through_ms
                meta["ver"] = permit.budget_version
                meta["expires_at"] = permit.expires_at
            elif action_type == "close":
                active_sids = [sid for sid, m in sessions_meta.items() if not m["closed"]]
                if not active_sids:
                    continue
                sid = rng.choice(active_sids)
                meta = sessions_meta[sid]
                max_prove = meta["auth_ms"]
                new_proven = rng.randint(meta["proven_ms"], max_prove) if max_prove > meta["proven_ms"] else meta["proven_ms"]
                # Advance clock past permit expiry to exercise expired-permission closure
                t = max(t, meta["expires_at"] + timedelta(seconds=1))
                close_view = await close_budget(
                    db,
                    runtime_principal=RuntimePrincipal(runner_id=meta["runner_id"]),
                    session_id=sid,
                    operation_id=f"seq-close-{op_counter}",
                    expected_budget_version=meta["ver"],
                    cumulative_billable_ms=new_proven,
                    stop_evidence={"session_id": sid, "runner_id": meta["runner_id"], "reason": "seq_close"},
                    now=t,
                )
                meta["proven_ms"] = close_view.proven_ms
                meta["auth_ms"] = close_view.authorized_through_ms
                meta["ver"] = close_view.budget_version
                meta["closed"] = True

            # Invariant check after EVERY single transition
            await assert_wallet_invariants(db, user_id=user_a.id)
            await assert_wallet_invariants(db, user_id=user_b.id)
