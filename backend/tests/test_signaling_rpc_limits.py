from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from app.coordination import CallClaim, LiveRoute
from app.redis import RedisResources
from app.signaling_rpc import (
    MAX_CANDIDATE_BYTES,
    MAX_OFFER_BYTES,
    MAX_REPLY_BYTES,
    MAX_SDP_CHARS,
    RpcReply,
    SignalingRpc,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def active_route(worker_id: str) -> LiveRoute:
    claim = CallClaim(
        session_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_type="public",
        generation="generation",
        worker_id=worker_id,
        phase="active",
    )
    return LiveRoute(pc_id="pc-id", claim=claim)


@pytest.mark.anyio
async def test_valid_maximum_offer_reaches_the_owner(redis_resources: RedisResources):
    received: list[dict[str, object]] = []

    async def dispatch(action, payload, claim, remaining):
        received.append({"action": action, "payload": payload, "remaining": remaining})
        return RpcReply(200, {"accepted": True}, {})

    rpc = SignalingRpc(redis_resources, "worker-a", dispatch)
    heartbeat = redis_resources.key("worker:worker-a:heartbeat")
    await redis_resources.control.set(heartbeat, "1", ex=15)
    runner = asyncio.create_task(rpc.run())
    try:
        deadline = await rpc.new_deadline()
        reply = await rpc.request(
            active_route("worker-a"),
            "renegotiate",
            {
                "pc_id": "pc-id",
                "renegotiation_token": "token",
                "sdp": "a" * 131_072,
                "type": "offer",
            },
            deadline=deadline,
        )
        assert reply == RpcReply(200, {"accepted": True}, {})
        assert received and received[0]["payload"]["sdp"] == "a" * 131_072
        assert received[0]["remaining"] <= 40
    finally:
        await rpc.close()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.anyio
async def test_expired_queued_rpc_never_dispatches(redis_resources: RedisResources):
    dispatched = False

    async def dispatch(action, payload, claim, remaining):
        nonlocal dispatched
        dispatched = True
        return RpcReply(200, {}, {})

    rpc = SignalingRpc(redis_resources, "worker-a", dispatch)
    route = active_route("worker-a")
    raw_envelope = json.dumps(
        {
            "rpc_id": "a" * 32,
            "action": "candidates",
            "pc_id": route.pc_id,
            "claim": {
                "session_id": str(route.claim.session_id),
                "agent_id": str(route.claim.agent_id),
                "call_type": route.claim.call_type,
                "generation": route.claim.generation,
                "worker_id": route.claim.worker_id,
                "phase": route.claim.phase,
            },
            "payload": {"pc_id": route.pc_id, "renegotiation_token": "token", "candidates": []},
            "expires_at_ms": 0,
        }
    )

    await rpc._process_one(raw_envelope)

    assert dispatched is False


@pytest.mark.anyio
async def test_relay_rejects_waiters_after_its_bounded_reply_capacity(
    redis_resources: RedisResources, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(action, payload, claim, remaining):
        return RpcReply(200, {}, {})

    async def wait_in_enqueue(*args, **kwargs):
        entered.set()
        await release.wait()
        return [1, 0]

    rpc = SignalingRpc(redis_resources, "worker-a", dispatch)
    monkeypatch.setattr(redis_resources.commands, "eval", wait_in_enqueue)
    deadline = await rpc.new_deadline()
    route = active_route("worker-b")
    valid = {"pc_id": "pc-id", "renegotiation_token": "token", "candidates": []}
    waiting = [
        asyncio.create_task(rpc.request(route, "candidates", valid, deadline=deadline))
        for _ in range(16)
    ]
    await entered.wait()
    rejected = await rpc.request(route, "candidates", {"pc_id": "pc-id", "renegotiation_token": "token", "candidates": []}, deadline=deadline)
    assert rejected.status_code == 503
    release.set()
    for task in waiting:
        task.cancel()
    await asyncio.gather(*waiting, return_exceptions=True)


@pytest.mark.anyio
async def test_sender_rejects_candidates_above_the_candidate_envelope_limit(
    redis_resources: RedisResources,
):
    async def dispatch(action, payload, claim, remaining):
        return RpcReply(200, {}, {})

    rpc = SignalingRpc(redis_resources, "worker-sender", dispatch)
    deadline = await rpc.new_deadline()
    payload = {
        "pc_id": "pc-id",
        "renegotiation_token": "token",
        "candidates": [
            {"candidate": "a" * 4_096, "sdp_mid": "0", "sdp_mline_index": 0}
            for _ in range(17)
        ],
    }
    # The payload is well under MAX_OFFER_BYTES but over MAX_CANDIDATE_BYTES + overhead.
    assert len(json.dumps(payload).encode("utf-8")) > MAX_CANDIDATE_BYTES + 2_048

    reply = await rpc.request(active_route("worker-b"), "candidates", payload, deadline=deadline)

    assert reply.status_code == 413
    assert reply.body == {"detail": "Signaling request is too large."}


@pytest.mark.anyio
async def test_receiver_drops_candidates_envelope_above_the_candidate_limit(
    redis_resources: RedisResources,
):
    dispatched = False

    async def dispatch(action, payload, claim, remaining):
        nonlocal dispatched
        dispatched = True
        return RpcReply(200, {}, {})

    rpc = SignalingRpc(redis_resources, "worker-a", dispatch)
    deadline = await rpc.new_deadline()
    route = active_route("worker-a")
    raw_envelope = json.dumps(
        {
            "rpc_id": "a" * 32,
            "action": "candidates",
            "pc_id": route.pc_id,
            "claim": {
                "session_id": str(route.claim.session_id),
                "agent_id": str(route.claim.agent_id),
                "call_type": route.claim.call_type,
                "generation": route.claim.generation,
                "worker_id": route.claim.worker_id,
                "phase": route.claim.phase,
            },
            "payload": {
                "pc_id": route.pc_id,
                "renegotiation_token": "token",
                "candidates": [
                    {"candidate": "a" * 4_096, "sdp_mid": "0", "sdp_mline_index": 0}
                    for _ in range(140)
                ],
            },
            "expires_at_ms": deadline.expires_at_ms,
        }
    )
    # Under the generic offer envelope ceiling, over the candidate action limit.
    assert len(raw_envelope.encode("utf-8")) < MAX_OFFER_BYTES + 2_048
    assert len(raw_envelope.encode("utf-8")) > MAX_CANDIDATE_BYTES + 2_048

    await rpc._process_one(raw_envelope)

    assert dispatched is False


@pytest.mark.anyio
async def test_receiver_drops_offer_payload_above_the_sdp_limit(
    redis_resources: RedisResources,
):
    dispatched = False

    async def dispatch(action, payload, claim, remaining):
        nonlocal dispatched
        dispatched = True
        return RpcReply(200, {}, {})

    rpc = SignalingRpc(redis_resources, "worker-a", dispatch)
    deadline = await rpc.new_deadline()
    route = active_route("worker-a")
    raw_envelope = json.dumps(
        {
            "rpc_id": "b" * 32,
            "action": "renegotiate",
            "pc_id": route.pc_id,
            "claim": {
                "session_id": str(route.claim.session_id),
                "agent_id": str(route.claim.agent_id),
                "call_type": route.claim.call_type,
                "generation": route.claim.generation,
                "worker_id": route.claim.worker_id,
                "phase": route.claim.phase,
            },
            "payload": {
                "pc_id": route.pc_id,
                "renegotiation_token": "token",
                "sdp": "a" * (MAX_SDP_CHARS + 1),
                "type": "offer",
                "signaling_token": None,
                "restart_pc": False,
            },
            "expires_at_ms": deadline.expires_at_ms,
        }
    )

    await rpc._process_one(raw_envelope)

    assert dispatched is False


def test_reply_encoding_keeps_only_retry_after_and_enforces_one_mebibyte():
    encoded = RpcReply(429, {"detail": "slow down"}, {"Retry-After": "5", "X-Internal": "no"}).to_bytes()
    assert json.loads(encoded) == {
        "status_code": 429,
        "body": {"detail": "slow down"},
        "headers": {"Retry-After": "5"},
    }
    with pytest.raises(ValueError, match="maximum size"):
        RpcReply(200, {"answer": "x" * MAX_REPLY_BYTES}, {}).to_bytes()


def test_reply_header_types_are_validated():
    with pytest.raises(ValueError, match="headers"):
        RpcReply(200, {}, None).to_bytes()  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="headers"):
        RpcReply(200, {}, ["Retry-After", "5"]).to_bytes()  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_forwarded_reply_keeps_only_valid_retry_after_header(
    redis_resources: RedisResources,
):
    async def dispatch(action, payload, claim, remaining):
        return RpcReply(200, {}, {})

    rpc = SignalingRpc(redis_resources, "worker-a", dispatch)
    target_worker = "worker-b"
    await redis_resources.control.set(
        redis_resources.key(f"worker:{target_worker}:heartbeat"), "1", ex=15
    )
    target_queue = redis_resources.key(f"queue:worker:{target_worker}:signals")

    async def hostile_peer() -> None:
        item = await redis_resources.blocking.brpop(target_queue, timeout=5)
        assert item is not None
        _, raw_envelope = item
        envelope = json.loads(raw_envelope)
        raw_reply = json.dumps(
            {
                "status_code": 200,
                "body": {"accepted": True},
                "headers": {
                    "Retry-After": "5",
                    "Set-Cookie": "session=attacker",
                    "X-Internal": "leak",
                },
            }
        )
        await redis_resources.commands.rpush(
            redis_resources.key(f"queue:rpc:reply:{envelope['rpc_id']}"),
            raw_reply,
        )

    peer = asyncio.create_task(hostile_peer())
    try:
        deadline = await rpc.new_deadline()
        reply = await rpc.request(
            active_route(target_worker),
            "candidates",
            {"pc_id": "pc-id", "renegotiation_token": "token", "candidates": []},
            deadline=deadline,
        )
    finally:
        peer.cancel()
        await asyncio.gather(peer, return_exceptions=True)

    assert reply.status_code == 200
    assert reply.body == {"accepted": True}
    assert reply.headers == {"Retry-After": "5"}


@pytest.mark.anyio
async def test_missing_target_heartbeat_returns_410(redis_resources: RedisResources):
    async def dispatch(action, payload, claim, remaining):
        return RpcReply(200, {}, {})

    rpc = SignalingRpc(redis_resources, "worker-sender", dispatch)
    deadline = await rpc.new_deadline()
    route = active_route("worker-dead")
    # No heartbeat set for worker-dead
    reply = await rpc.request(route, "candidates", {"pc_id": "pc-id", "renegotiation_token": "token", "candidates": []}, deadline=deadline)
    assert reply.status_code == 410
    assert reply.body.get("detail") == "Call session is no longer active."


@pytest.mark.anyio
async def test_overloaded_target_queue_returns_503(redis_resources: RedisResources):
    async def dispatch(action, payload, claim, remaining):
        return RpcReply(200, {}, {})

    rpc = SignalingRpc(redis_resources, "worker-sender", dispatch)
    target_worker = "worker-busy"
    hb_key = redis_resources.key(f"worker:{target_worker}:heartbeat")
    await redis_resources.control.set(hb_key, "1", ex=15)
    queue_key = redis_resources.key(f"queue:worker:{target_worker}:signals")
    # Pre-populate queue with 128 dummy envelopes
    dummy = ["x"] * 128
    await redis_resources.control.rpush(queue_key, *dummy)

    deadline = await rpc.new_deadline()
    route = active_route(target_worker)
    reply = await rpc.request(route, "candidates", {"pc_id": "pc-id", "renegotiation_token": "token", "candidates": []}, deadline=deadline)
    assert reply.status_code == 503
    assert reply.body.get("detail") == "Signaling target worker is overloaded."


@pytest.mark.anyio
async def test_local_dispatch_rejects_immediately_when_at_capacity(
    redis_resources: RedisResources,
):
    entered_count = 0
    all_entered = asyncio.Event()
    release_event = asyncio.Event()

    async def blocking_dispatch(action, payload, claim, remaining):
        nonlocal entered_count
        entered_count += 1
        if entered_count == 16:
            all_entered.set()
        await release_event.wait()
        return RpcReply(200, {"ok": True}, {})

    rpc = SignalingRpc(redis_resources, "worker-a", blocking_dispatch)
    deadline = await rpc.new_deadline()
    route = active_route("worker-a")
    payload = {"pc_id": "pc-id", "renegotiation_token": "token", "candidates": []}

    # Start 16 concurrent local dispatches to saturate capacity
    tasks = [
        asyncio.create_task(
            rpc.dispatch_local_with_deadline("candidates", payload, route.claim, deadline=deadline)
        )
        for _ in range(16)
    ]

    # Wait for all 16 to be actively running in blocking_dispatch
    await asyncio.wait_for(all_entered.wait(), timeout=2.0)
    assert entered_count == 16

    # 17th request must be rejected immediately with HTTP 503 without waiting
    start = time.monotonic()
    overload_reply = await rpc.dispatch_local_with_deadline(
        "candidates", payload, route.claim, deadline=deadline
    )
    elapsed = time.monotonic() - start

    assert overload_reply.status_code == 503
    assert overload_reply.body == {"detail": "Signaling dispatcher is overloaded."}
    assert elapsed < 0.1, "Overload rejection should be immediate (non-waiting)"

    # Release the 16 running tasks
    release_event.set()
    replies = await asyncio.gather(*tasks)
    for reply in replies:
        assert reply.status_code == 200

    # Once capacity is released, a new local dispatch must succeed
    post_release_reply = await rpc.dispatch_local_with_deadline(
        "candidates", payload, route.claim, deadline=deadline
    )
    assert post_release_reply.status_code == 200


@pytest.mark.anyio
async def test_remote_dispatch_at_max_concurrency_no_deadlock(
    redis_resources: RedisResources,
):
    entered_count = 0
    all_entered = asyncio.Event()
    release_event = asyncio.Event()

    async def remote_dispatch(action, payload, claim, remaining):
        nonlocal entered_count
        entered_count += 1
        if entered_count == 16:
            all_entered.set()
        await release_event.wait()
        return RpcReply(200, {"success": True}, {})

    target_worker = "worker-target"
    rpc = SignalingRpc(redis_resources, target_worker, remote_dispatch)
    heartbeat_key = redis_resources.key(f"worker:{target_worker}:heartbeat")
    await redis_resources.control.set(heartbeat_key, "1", ex=15)

    runner = asyncio.create_task(rpc.run())
    try:
        sender_rpc = SignalingRpc(redis_resources, "worker-sender", remote_dispatch)
        route = active_route(target_worker)
        deadline = await sender_rpc.new_deadline()
        payload = {"pc_id": "pc-id", "renegotiation_token": "token", "candidates": []}

        # Send 16 concurrent requests across workers via Redis
        send_tasks = [
            asyncio.create_task(
                sender_rpc.request(route, "candidates", payload, deadline=deadline)
            )
            for _ in range(16)
        ]

        # All 16 remote dispatches must enter dispatch without deadlocking
        await asyncio.wait_for(all_entered.wait(), timeout=5.0)
        assert entered_count == 16

        # While 16 remote dispatches are holding all slots on target_worker:
        # A local dispatch on target_worker must be rejected with 503 (shared capacity!)
        local_reply = await rpc.dispatch_local_with_deadline(
            "candidates", payload, route.claim, deadline=deadline
        )
        assert local_reply.status_code == 503
        assert local_reply.body == {"detail": "Signaling dispatcher is overloaded."}

        # Release all 16 remote dispatches and ensure all finish successfully
        release_event.set()
        replies = await asyncio.wait_for(asyncio.gather(*send_tasks), timeout=5.0)
        for r in replies:
            assert r.status_code == 200
            assert r.body == {"success": True}
    finally:
        await rpc.close()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.anyio
async def test_shutdown_drain_bounded_and_rejects_new_work(
    redis_resources: RedisResources,
):
    entered = asyncio.Event()
    hang_forever = asyncio.Event()

    async def slow_dispatch(action, payload, claim, remaining):
        entered.set()
        await hang_forever.wait()
        return RpcReply(200, {"ok": True}, {})

    rpc = SignalingRpc(redis_resources, "worker-drain", slow_dispatch)
    deadline = await rpc.new_deadline()
    route = active_route("worker-drain")
    payload = {"pc_id": "pc-id", "renegotiation_token": "token", "candidates": []}

    # Start 1 in-flight local dispatch
    task = asyncio.create_task(
        rpc.dispatch_local_with_deadline("candidates", payload, route.claim, deadline=deadline)
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    # 1. Begin shutdown: new admissions must be rejected immediately
    rpc.begin_shutdown()
    rejected = await rpc.dispatch_local_with_deadline(
        "candidates", payload, route.claim, deadline=deadline
    )
    assert rejected.status_code == 503
    assert rejected.body == {"detail": "Signaling dispatcher is overloaded."}

    # 2. Drain with a short timeout must return False boundedly when a task hangs
    start = time.monotonic()
    drained = await rpc.drain(timeout=0.2)
    elapsed = time.monotonic() - start
    assert drained is False
    assert 0.15 <= elapsed < 0.5, "drain must respect the timeout bound"

    # 3. close() cancels pending tasks and cleanly gathers them
    await rpc.close(drain_timeout=0.1)
    assert task.done()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_shutdown_drain_success_when_in_flight_finishes(
    redis_resources: RedisResources,
):
    entered = asyncio.Event()
    finish_task = asyncio.Event()

    async def in_flight_dispatch(action, payload, claim, remaining):
        entered.set()
        await finish_task.wait()
        return RpcReply(200, {"done": True}, {})

    rpc = SignalingRpc(redis_resources, "worker-drain-clean", in_flight_dispatch)
    deadline = await rpc.new_deadline()
    route = active_route("worker-drain-clean")
    payload = {"pc_id": "pc-id", "renegotiation_token": "token", "candidates": []}

    task = asyncio.create_task(
        rpc.dispatch_local_with_deadline("candidates", payload, route.claim, deadline=deadline)
    )
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    rpc.begin_shutdown()
    finish_task.set()
    drained = await rpc.drain(timeout=2.0)
    assert drained is True
    reply = await task
    assert reply.status_code == 200
    await rpc.close(drain_timeout=0.1)
