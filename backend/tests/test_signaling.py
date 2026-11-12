from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app import sessions, signaling
from app.coordination import CallClaim, Coordination


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_signaling_token_is_call_bound_and_single_use(redis_resources):
    coord = Coordination(redis_resources, "worker-test")
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    obs = await coord.reserve_created(session_id, agent_id, "public", per_agent_limit=10, overall_limit=10)
    token = await coord.issue_token(obs.claim, max_remaining_seconds=120)

    grant = await coord.consume_token(token)
    assert (grant.session_id, grant.agent_id, grant.call_type) == (session_id, agent_id, "public")

    with pytest.raises(HTTPException) as error:
        await coord.consume_token(token)
    assert error.value.status_code == 401


@pytest.mark.anyio
async def test_expired_token_fails_and_marks_its_session_expired(redis_resources):
    coord = Coordination(redis_resources, "worker-test")
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    obs = await coord.reserve_created(session_id, agent_id, "test", per_agent_limit=10, overall_limit=10)
    token = await coord.issue_token(obs.claim, max_remaining_seconds=1)

    # Let token expire
    await asyncio.sleep(1.1)

    with pytest.raises(HTTPException) as error:
        await coord.consume_token(token)
    assert error.value.status_code == 401


@pytest.mark.anyio
async def test_rate_limiter_enforces_ip_window_and_expires_old_entries(redis_resources):
    coord = Coordination(redis_resources, "worker-test")
    ip = f"198.51.100.{secrets.randbelow(200) + 1}"

    for _ in range(60):
        await coord.check_ip(ip)
    with pytest.raises(HTTPException) as error:
        await coord.check_ip(ip)
    assert error.value.status_code == 429
    assert "Retry-After" in error.value.headers


@pytest.mark.anyio
async def test_rate_limiter_tracks_agent_and_ip_limits_separately(redis_resources):
    coord = Coordination(redis_resources, "worker-test")
    agent_id = uuid.uuid4()
    ip = f"198.51.100.{secrets.randbelow(200) + 1}"

    for _ in range(120):
        await coord.check_agent(agent_id)
    with pytest.raises(HTTPException) as error:
        await coord.check_agent(agent_id)
    assert error.value.status_code == 429
    # Other IP is not affected
    await coord.check_ip(ip)


def test_offer_schema_requires_matching_single_use_or_live_connection_credentials():
    initial = signaling._OfferBody.model_validate(
        {"sdp": "offer-sdp", "type": "offer", "signaling_token": "single-use"}
    )
    renegotiation = signaling._OfferBody.model_validate(
        {
            "sdp": "offer-sdp",
            "type": "offer",
            "pc_id": "peer-connection",
            "renegotiation_token": "live-connection",
        }
    )

    assert initial.signaling_token == "single-use"
    assert renegotiation.pc_id == "peer-connection"
    with pytest.raises(ValidationError):
        signaling._OfferBody.model_validate({"sdp": "offer-sdp", "type": "offer"})
    with pytest.raises(ValidationError):
        signaling._OfferBody.model_validate(
            {
                "sdp": "offer-sdp",
                "type": "offer",
                "signaling_token": "initial-token",
                "pc_id": "peer-connection",
                "renegotiation_token": "live-token",
            }
        )
    with pytest.raises(ValidationError):
        signaling._OfferBody.model_validate(
            {"sdp": "offer-sdp", "type": "offer", "signaling_token": "initial-token", "restart_pc": True}
        )


@pytest.mark.anyio
async def test_call_reservations_enforce_per_agent_and_overall_concurrency_limits(redis_resources):
    coord = Coordination(redis_resources, "worker-test")
    first_session, second_session, third_session, fourth_session = (uuid.uuid4() for _ in range(4))
    first_agent, second_agent, third_agent = (uuid.uuid4() for _ in range(3))

    await coord.reserve_created(first_session, first_agent, "test", per_agent_limit=1, overall_limit=2)
    with pytest.raises(HTTPException) as per_agent_error:
        await coord.reserve_created(second_session, first_agent, "test", per_agent_limit=1, overall_limit=2)
    assert per_agent_error.value.status_code == 429

    await coord.reserve_created(third_session, second_agent, "test", per_agent_limit=1, overall_limit=2)
    with pytest.raises(HTTPException) as overall_error:
        await coord.reserve_created(fourth_session, third_agent, "test", per_agent_limit=1, overall_limit=2)
    assert overall_error.value.status_code == 429
@pytest.mark.anyio
async def test_live_renegotiation_credential_is_bound_and_reauthorized(monkeypatch):
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    pc_id = "peer-connection"
    token = "per-connection-secret"
    runtime = SimpleNamespace(
        session_id=session_id,
        agent_id=agent_id,
        definition=SimpleNamespace(call_type="test"),
        closed=False,
        closing=False,
    )
    live = signaling._LiveCall(
        runtime=runtime,
        pc_id=pc_id,
        renegotiation_token_hash=hashlib.sha256(token.encode("utf-8")).digest(),
        handler=None,
    )
    signaling._live_calls_by_pc[pc_id] = live
    context = {
        "session_status": "active",
        "call_type": "test",
        "snapshot": {"agent": {"id": str(agent_id)}},
    }

    async def load_context(requested_session_id: uuid.UUID):
        assert requested_session_id == session_id
        return context

    monkeypatch.setattr(signaling, "_load_context", load_context)
    try:
        assert await signaling._authorize_live_call(pc_id, token) is live
        with pytest.raises(HTTPException) as invalid_credential:
            await signaling._authorize_live_call(pc_id, "wrong-secret")
        assert invalid_credential.value.status_code == 401

        context["session_status"] = "cancelled"
        with pytest.raises(HTTPException) as closed_session:
            await signaling._authorize_live_call(pc_id, token)
        assert closed_session.value.status_code == 404
    finally:
        signaling._live_calls_by_pc.pop(pc_id, None)


@pytest.mark.anyio
async def test_followup_rejects_if_lease_expired_during_quota_checks(monkeypatch):
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    pc_id = "pc-followup-expire-test"
    token = "token-test"

    runtime = object.__new__(signaling.CallRuntime)
    runtime.definition = SimpleNamespace(session_id=session_id, agent_id=agent_id, call_type="test")
    runtime.claim = CallClaim(
        session_id=session_id,
        agent_id=agent_id,
        call_type="test",
        generation="gen-1",
        worker_id="worker-local",
        phase="active",
    )
    runtime.coordination = None
    runtime.lease_lock = asyncio.Lock()
    runtime.lease_deadline = time.monotonic() - 1.0
    runtime.closed = False
    runtime.closing = False

    live = signaling._LiveCall(
        runtime=runtime,
        pc_id=pc_id,
        renegotiation_token_hash=hashlib.sha256(token.encode("utf-8")).digest(),
        handler=None,
    )
    signaling._live_calls_by_pc[pc_id] = live

    context = {
        "session_status": "active",
        "call_type": "test",
        "worker_id": "worker-local",
        "claim_generation": "gen-1",
        "snapshot": {"agent": {"id": str(agent_id)}},
    }

    async def mock_load_context(sid):
        return context

    monkeypatch.setattr(signaling, "_load_context", mock_load_context)

    payload = {"pc_id": pc_id, "renegotiation_token": token}
    try:
        reply = await signaling._dispatch_local_signal("renegotiate", payload, runtime.claim, 10.0)
        assert reply.status_code == 410
        assert "lease" in reply.body.get("detail", "").lower()
    finally:
        signaling._live_calls_by_pc.pop(pc_id, None)



def test_offer_and_candidate_schemas_enforce_size_caps():
    with pytest.raises(ValidationError):
        signaling._OfferBody.model_validate(
            {
                "sdp": "x" * (signaling.MAX_SDP_CHARS + 1),
                "type": "offer",
                "signaling_token": "token",
            }
        )

    candidate = {"candidate": "", "sdp_mid": "audio", "sdp_mline_index": 0}
    with pytest.raises(ValidationError):
        signaling._CandidateBody.model_validate(
            {
                "pc_id": "peer",
                "renegotiation_token": "credential",
                "candidates": [candidate] * (signaling.MAX_CANDIDATES + 1),
            }
        )


@pytest.mark.anyio
async def test_ice_endpoint_returns_short_lived_turn_rest_credentials(monkeypatch):
    secret = "turn-rest-secret-value-with-at-least-32-bytes"
    monkeypatch.setenv("SMALLWEBRTC_STUN_URLS", "stun:stun.example.test:3478")
    monkeypatch.setenv(
        "SMALLWEBRTC_TURN_URLS",
        "turn:turn.example.test:3478?transport=udp,turns:turn.example.test:5349?transport=tcp",
    )
    monkeypatch.setenv("SMALLWEBRTC_TURN_SHARED_SECRET", secret)
    monkeypatch.setenv("SMALLWEBRTC_TURN_CREDENTIAL_TTL_SECONDS", "300")
    monkeypatch.delenv("SMALLWEBRTC_ICE_SERVERS", raising=False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="198.51.100.30"),
        app=SimpleNamespace(state=SimpleNamespace(coordination=None)),
    )
    headers = Response()
    response = await signaling.ice_servers(request, headers)
    assert headers.headers["cache-control"] == "no-store"

    assert response["iceServers"][0] == {"urls": ["stun:stun.example.test:3478"]}
    turn = response["iceServers"][1]
    assert turn["urls"] == [
        "turn:turn.example.test:3478?transport=udp",
        "turns:turn.example.test:5349?transport=tcp",
    ]
    expiry = int(turn["username"].split(":", 1)[0])
    assert int(time.time()) + 299 <= expiry <= int(time.time()) + 300
    expected = base64.b64encode(
        hmac.new(secret.encode(), turn["username"].encode(), hashlib.sha1).digest()
    ).decode("ascii")
    assert turn["credential"] == expected
    assert secret not in repr(response)


def test_legacy_ice_servers_accept_a_stun_url(monkeypatch):
    monkeypatch.setenv(
        "SMALLWEBRTC_ICE_SERVERS",
        json.dumps([{"urls": "stun:stun.example.test:3478"}]),
    )
    monkeypatch.delenv("SMALLWEBRTC_STUN_URLS", raising=False)
    monkeypatch.delenv("SMALLWEBRTC_TURN_URLS", raising=False)
    monkeypatch.delenv("SMALLWEBRTC_TURN_SHARED_SECRET", raising=False)

    assert signaling._ice_server_configs() == [
        {"urls": ["stun:stun.l.google.com:19302", "stun:stun.example.test:3478"]}
    ]


def test_ice_configuration_rejects_static_turn_credentials_and_bad_urls(monkeypatch):
    monkeypatch.setenv(
        "SMALLWEBRTC_ICE_SERVERS",
        json.dumps(
            [
                {
                    "urls": "turn:turn.example.test:3478",
                    "username": "static-user",
                    "credential": "static-secret",
                }
            ]
        ),
    )
    with pytest.raises(HTTPException) as static_turn:
        signaling._ice_server_configs()
    assert static_turn.value.status_code == 503
    assert "static-secret" not in str(static_turn.value.detail)

    monkeypatch.delenv("SMALLWEBRTC_ICE_SERVERS", raising=False)
    monkeypatch.setenv("SMALLWEBRTC_TURN_URLS", "turn:user:pass@turn.example.test")
    monkeypatch.setenv("SMALLWEBRTC_TURN_SHARED_SECRET", "turn-rest-secret-value-with-at-least-32-bytes")
    with pytest.raises(HTTPException) as invalid_url:
        signaling._ice_server_configs()
    assert invalid_url.value.status_code == 503
    assert "user:pass" not in str(invalid_url.value.detail)


def test_ice_credential_lifetime_is_bounded(monkeypatch):
    monkeypatch.setenv("SMALLWEBRTC_TURN_URLS", "turn:turn.example.test:3478")
    monkeypatch.setenv("SMALLWEBRTC_TURN_SHARED_SECRET", "turn-rest-secret-value-with-at-least-32-bytes")
    monkeypatch.setenv("SMALLWEBRTC_TURN_CREDENTIAL_TTL_SECONDS", "45")

    with pytest.raises(HTTPException) as invalid_ttl:
        signaling._ice_server_configs()

    assert invalid_ttl.value.status_code == 503


@pytest.mark.anyio
async def test_dispatch_local_signal_rejects_durable_generation_or_worker_mismatch(monkeypatch):
    import time
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    pc_id = "peer-connection-dispatch"
    token = "secret-token-123"
    claim = CallClaim(
        session_id=session_id,
        agent_id=agent_id,
        call_type="test",
        generation="gen-expected",
        worker_id="worker-A",
        phase="active",
    )
    runtime = SimpleNamespace(
        session_id=session_id,
        agent_id=agent_id,
        definition=SimpleNamespace(call_type="test"),
        closed=False,
        closing=False,
        claim=claim,
        lease_deadline=time.monotonic() + 60.0,
        coordination=None,
    )
    live = signaling._LiveCall(
        runtime=runtime,
        pc_id=pc_id,
        renegotiation_token_hash=hashlib.sha256(token.encode("utf-8")).digest(),
        handler=None,
    )
    signaling._live_calls_by_pc[pc_id] = live

    context = {
        "session_status": "active",
        "call_type": "test",
        "worker_id": "worker-A",
        "claim_generation": "gen-expected",
        "snapshot": {"agent": {"id": str(agent_id)}},
    }

    async def mock_load_context(sid):
        return context

    monkeypatch.setattr(signaling, "_load_context", mock_load_context)

    payload = {"pc_id": pc_id, "renegotiation_token": token}

    try:
        # Generation mismatch in durable DB context -> 409
        context["claim_generation"] = "gen-different"
        reply = await signaling._dispatch_local_signal("renegotiate", payload, claim, 10.0)
        assert reply.status_code == 409
        assert "generation mismatch" in reply.body.get("detail", "").lower()

        # Worker mismatch in durable DB context -> 409
        context["claim_generation"] = "gen-expected"
        context["worker_id"] = "worker-other"
        reply = await signaling._dispatch_local_signal("renegotiate", payload, claim, 10.0)
        assert reply.status_code == 409
        assert "worker mismatch" in reply.body.get("detail", "").lower()

        # Expired/missing context in durable DB -> 410
        async def mock_none_context(sid):
            return None

        monkeypatch.setattr(signaling, "_load_context", mock_none_context)
        reply = await signaling._dispatch_local_signal("renegotiate", payload, claim, 10.0)
        assert reply.status_code == 410
    finally:
        signaling._live_calls_by_pc.pop(pc_id, None)


@pytest.mark.anyio
async def test_followup_rejects_if_lease_expired_during_quota_checks(monkeypatch):
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    pc_id = "pc-followup-expire-test"
    token = "token-test"

    runtime = object.__new__(signaling.CallRuntime)
    runtime.definition = SimpleNamespace(session_id=session_id, agent_id=agent_id, call_type="test")
    runtime.claim = CallClaim(
        session_id=session_id,
        agent_id=agent_id,
        call_type="test",
        generation="gen-1",
        worker_id="worker-local",
        phase="active",
    )
    runtime.coordination = None
    runtime.lease_lock = asyncio.Lock()
    runtime.lease_deadline = time.monotonic() - 1.0
    runtime.closed = False
    runtime.closing = False

    live = signaling._LiveCall(
        runtime=runtime,
        pc_id=pc_id,
        renegotiation_token_hash=hashlib.sha256(token.encode("utf-8")).digest(),
        handler=None,
    )
    signaling._live_calls_by_pc[pc_id] = live

    context = {
        "session_status": "active",
        "call_type": "test",
        "worker_id": "worker-local",
        "claim_generation": "gen-1",
        "snapshot": {"agent": {"id": str(agent_id)}},
    }

    async def mock_load_context(sid):
        return context

    monkeypatch.setattr(signaling, "_load_context", mock_load_context)

    payload = {"pc_id": pc_id, "renegotiation_token": token}
    try:
        reply = await signaling._dispatch_local_signal("renegotiate", payload, runtime.claim, 10.0)
        assert reply.status_code == 410
        assert "lease" in reply.body.get("detail", "").lower()
    finally:
        signaling._live_calls_by_pc.pop(pc_id, None)


