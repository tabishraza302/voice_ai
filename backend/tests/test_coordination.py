import asyncio
import os
import secrets
import time
import uuid
import pytest
from httpx import ASGITransport, AsyncClient

from app.redis import open_redis_resources, RedisResources
from app.coordination import Coordination, CallClaim, LeaseObservation, LiveRoute


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_transferable_grant_survives_issuer_death_and_starts_on_worker_b(redis_resources: RedisResources):
    # Worker A creates grant and immediately shuts down
    coord_a = Coordination(redis_resources, "worker-a")
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    obs_created = await coord_a.reserve_created(session_id, agent_id, "public", 2, 5)
    token = await coord_a.issue_token(obs_created.claim, max_remaining_seconds=120.0)

    # Worker A dies (simulate by never using coord_a again)
    del coord_a

    # Worker B consumes the transferable token and starts setup
    coord_b = Coordination(redis_resources, "worker-b")
    consumed_claim = await coord_b.consume_token(token)
    assert consumed_claim.session_id == session_id
    assert consumed_claim.generation == obs_created.claim.generation
    assert consumed_claim.worker_id is None

    # Worker B claims setup (CAS to starting under fresh generation v2 and worker-b)
    obs_setup = await coord_b.begin_setup(consumed_claim)
    assert obs_setup.claim.phase == "starting"
    assert obs_setup.claim.worker_id == "worker-b"
    assert obs_setup.claim.generation != obs_created.claim.generation

    # Worker B activates with pc_id
    pc_id = f"pc-{uuid.uuid4().hex[:8]}"
    obs_active = await coord_b.activate(obs_setup.claim, pc_id)
    assert obs_active.claim.phase == "active"

    # Route resolves to Worker B
    route = await coord_b.route(pc_id)
    assert route is not None
    assert route.claim.worker_id == "worker-b"
    assert route.claim.generation == obs_setup.claim.generation
    assert route.claim.call_type == "public"


@pytest.mark.anyio
async def test_worker_owner_and_phase_fencing(redis_resources: RedisResources):
    coord_a = Coordination(redis_resources, "worker-a")
    coord_b = Coordination(redis_resources, "worker-b")
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    obs_created = await coord_a.reserve_created(session_id, agent_id, "test", 2, 5)
    obs_setup = await coord_a.begin_setup(obs_created.claim)
    pc_id = f"pc-{uuid.uuid4().hex[:8]}"
    obs_active = await coord_a.activate(obs_setup.claim, pc_id)

    # Worker B attempting to renew Worker A's active lease must fail
    fake_b_claim = CallClaim(
        session_id=session_id,
        agent_id=agent_id,
        call_type="test",
        generation=obs_active.claim.generation,
        worker_id="worker-b",
        phase="active",
    )
    with pytest.raises(Exception):
        await coord_b.renew(fake_b_claim, pc_id)

    # Worker B attempting to release Worker A's active lease must return False and preserve lease
    released = await coord_b.release(fake_b_claim, pc_id)
    assert released is False

    # Lease and route remain valid for Worker A
    route = await coord_a.route(pc_id)
    assert route is not None
    assert route.claim.worker_id == "worker-a"


@pytest.mark.anyio
async def test_shared_rate_limiting_across_coordinators(redis_resources: RedisResources):
    coord_a = Coordination(redis_resources, "worker-a")
    coord_b = Coordination(redis_resources, "worker-b")

    client_ip = f"192.0.2.{secrets.randbelow(250) + 1}"

    # Allowance is 60 requests per 60 seconds
    for _ in range(30):
        await coord_a.check_ip(client_ip)
    for _ in range(30):
        await coord_b.check_ip(client_ip)

    # 61st request must raise 429
    with pytest.raises(Exception) as exc_info:
        await coord_a.check_ip(client_ip)
    assert getattr(exc_info.value, "status_code", None) == 429


@pytest.mark.anyio
async def test_single_use_token_atomic_consume_and_replay_rejection(redis_resources: RedisResources):
    coord_a = Coordination(redis_resources, "worker-a")
    coord_b = Coordination(redis_resources, "worker-b")

    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    # Reserve created lease
    observation = await coord_a.reserve_created(
        session_id=session_id,
        agent_id=agent_id,
        call_type="public",
        per_agent_limit=2,
        overall_limit=5,
    )
    assert observation.claim.phase == "created"
    assert observation.claim.worker_id is None

    token = await coord_a.issue_token(observation.claim, max_remaining_seconds=120.0)
    assert isinstance(token, str) and len(token) > 20

    # Worker B consumes token
    consumed_claim = await coord_b.consume_token(token)
    assert consumed_claim.session_id == session_id
    assert consumed_claim.agent_id == agent_id
    assert consumed_claim.generation == observation.claim.generation

    # Replay must fail with 401
    with pytest.raises(Exception) as exc_info:
        await coord_a.consume_token(token)
    assert getattr(exc_info.value, "status_code", None) == 401


@pytest.mark.anyio
async def test_setup_lease_headroom_and_activation(redis_resources: RedisResources):
    coord_b = Coordination(redis_resources, "worker-b")
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    obs1 = await coord_b.reserve_created(
        session_id=session_id,
        agent_id=agent_id,
        call_type="test",
        per_agent_limit=2,
        overall_limit=5,
    )

    # Begin setup: promotes to starting, assigns worker-b, sets setup lease 45s
    obs2 = await coord_b.begin_setup(obs1.claim)
    assert obs2.claim.phase == "starting"
    assert obs2.claim.worker_id == "worker-b"
    assert obs2.claim.generation != obs1.claim.generation  # Generation advanced
    assert obs2.remaining_seconds > 40.0

    # Activate with valid pc_id
    pc_id = "test-pc-12345"
    obs3 = await coord_b.activate(obs2.claim, pc_id=pc_id)
    assert obs3.claim.phase == "active"
    assert obs3.remaining_seconds <= 15.0

    # Check routing published
    route = await coord_b.route(pc_id)
    assert route is not None
    assert route.pc_id == pc_id
    assert route.claim.worker_id == "worker-b"
    assert route.claim.generation == obs3.claim.generation


@pytest.mark.anyio
async def test_stale_generation_release_and_renewal_rejection(redis_resources: RedisResources):
    coord = Coordination(redis_resources, "worker-a")
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    obs1 = await coord.reserve_created(session_id, agent_id, "public", 2, 5)
    obs2 = await coord.begin_setup(obs1.claim)

    # Attempting to renew or release with old generation v1 must fail
    old_claim = obs1.claim
    with pytest.raises(Exception):
        await coord.renew(old_claim, pc_id=None)

    released = await coord.release(old_claim, pc_id=None)
    assert released is False

    # Current lease remains alive under v2
    inspected = await coord.inspect(session_id)
    assert inspected is not None
    assert inspected.claim.generation == obs2.claim.generation
