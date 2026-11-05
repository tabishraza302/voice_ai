from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
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


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_signaling_token_is_call_bound_and_single_use():
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    token = signaling.issue_signaling_token(session_id, agent_id, "public")
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    expiration_task = signaling._token_tasks[token_hash]

    grant = await signaling._consume_signaling_token(token)
    await asyncio.sleep(0)

    assert (grant.session_id, grant.agent_id, grant.call_type) == (session_id, agent_id, "public")
    assert token_hash not in signaling._tokens
    assert token not in signaling._tokens
    assert expiration_task.cancelled()
    with pytest.raises(HTTPException) as error:
        await signaling._consume_signaling_token(token)
    assert error.value.status_code == 401


@pytest.mark.anyio
async def test_expired_token_fails_and_marks_its_session_expired(monkeypatch):
    session_id = uuid.uuid4()
    token = signaling.issue_signaling_token(session_id, uuid.uuid4(), "test")
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    grant = signaling._tokens[token_hash]
    signaling._tokens[token_hash] = replace(grant, expires_at=time.monotonic() - 1)
    calls: list[tuple[uuid.UUID, str]] = []

    async def set_status(expired_session_id: uuid.UUID, status: str) -> bool:
        calls.append((expired_session_id, status))
        return True

    monkeypatch.setattr(sessions, "set_session_status", set_status)

    with pytest.raises(HTTPException) as error:
        await signaling._consume_signaling_token(token)

    assert error.value.status_code == 401
    assert calls == [(session_id, "expired")]
    await asyncio.sleep(0)


def test_rate_limiter_enforces_ip_window_and_expires_old_entries(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(signaling.time, "monotonic", lambda: clock[0])
    limiter = signaling._RateLimiter()

    for _ in range(signaling._IP_REQUESTS_PER_WINDOW):
        limiter.check_ip("198.51.100.5")
    with pytest.raises(HTTPException) as error:
        limiter.check_ip("198.51.100.5")
    assert error.value.status_code == 429

    clock[0] += signaling._RATE_WINDOW_SECONDS + 1
    limiter.check_ip("198.51.100.5")


def test_rate_limiter_tracks_agent_and_ip_limits_separately(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(signaling.time, "monotonic", lambda: clock[0])
    limiter = signaling._RateLimiter()
    agent_id = uuid.uuid4()

    for _ in range(signaling._AGENT_REQUESTS_PER_WINDOW):
        limiter.check_agent(agent_id)
    with pytest.raises(HTTPException) as error:
        limiter.check_agent(agent_id)
    assert error.value.status_code == 429
    limiter.check_ip("198.51.100.6")


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
async def test_call_reservations_enforce_per_agent_and_overall_concurrency_limits():
    first_session, second_session, third_session, fourth_session = (uuid.uuid4() for _ in range(4))
    first_agent, second_agent, third_agent = (uuid.uuid4() for _ in range(3))
    try:
        await signaling._reserve_call(first_session, first_agent, per_agent_limit=1, overall_limit=2)
        with pytest.raises(HTTPException) as per_agent_error:
            await signaling._reserve_call(second_session, first_agent, per_agent_limit=1, overall_limit=2)
        assert per_agent_error.value.status_code == 429

        await signaling._reserve_call(third_session, second_agent, per_agent_limit=1, overall_limit=2)
        with pytest.raises(HTTPException) as overall_error:
            await signaling._reserve_call(fourth_session, third_agent, per_agent_limit=1, overall_limit=2)
        assert overall_error.value.status_code == 429
    finally:
        async with signaling._registry_lock:
            for session_id in (first_session, second_session, third_session, fourth_session):
                signaling._reservations.pop(session_id, None)
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


def test_offer_and_candidate_schemas_enforce_size_caps():
    with pytest.raises(ValidationError):
        signaling._OfferBody.model_validate(
            {
                "sdp": "x" * (signaling._MAX_SDP_CHARS + 1),
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
                "candidates": [candidate] * (signaling._MAX_CANDIDATES + 1),
            }
        )


@pytest.mark.anyio
async def test_ice_endpoint_returns_short_lived_turn_rest_credentials(monkeypatch):
    secret = "turn-rest-secret-value-with-at-least-32-bytes"
    monkeypatch.setattr(signaling, "_rate_limiter", signaling._RateLimiter())
    monkeypatch.setenv("SMALLWEBRTC_STUN_URLS", "stun:stun.example.test:3478")
    monkeypatch.setenv(
        "SMALLWEBRTC_TURN_URLS",
        "turn:turn.example.test:3478?transport=udp,turns:turn.example.test:5349?transport=tcp",
    )
    monkeypatch.setenv("SMALLWEBRTC_TURN_SHARED_SECRET", secret)
    monkeypatch.setenv("SMALLWEBRTC_TURN_CREDENTIAL_TTL_SECONDS", "300")
    monkeypatch.delenv("SMALLWEBRTC_ICE_SERVERS", raising=False)
    request = SimpleNamespace(client=SimpleNamespace(host="198.51.100.30"))
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
