import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import pytest
from aiortc import RTCPeerConnection, RTCSessionDescription
from httpx import AsyncClient

from app.db import Base, engine
from app.redis import open_redis_resources


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_two_process_signaling_relay():
    # 1. Setup isolated Redis namespace and database
    test_redis_url = os.environ.get("TEST_REDIS_URL", os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/0"))
    namespace = f"voice-ai-rpc-test:{uuid.uuid4().hex}"

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    port_a = get_free_port()
    port_b = get_free_port()

    test_db_url = os.environ.get("TEST_DATABASE_URL", os.environ.get("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test"))

    base_env = os.environ.copy()
    # Sanitize provider keys so no real external calls are ever made
    for k in ["OPENAI_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "ELEVENLABS_API_KEY", "AZURE_SPEECH_API_KEY"]:
        base_env[k] = "test-mock-key"
    base_env["VERIFIED_OPENAI_MODELS"] = "gpt-4o-mini"
    base_env["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    env_a = base_env.copy()
    env_a["REDIS_NAMESPACE"] = namespace
    env_a["REDIS_URL"] = test_redis_url
    env_a["DATABASE_URL"] = test_db_url

    env_b = base_env.copy()
    env_b["REDIS_NAMESPACE"] = namespace
    env_b["REDIS_URL"] = test_redis_url
    env_b["DATABASE_URL"] = test_db_url

    log_a = tempfile.NamedTemporaryFile("w+", delete=False)
    log_b = tempfile.NamedTemporaryFile("w+", delete=False)

    # Start Worker A and Worker B with test_worker entrypoint
    proc_a = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.test_worker:app", "--host", "127.0.0.1", "--port", str(port_a)],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        env=env_a,
        stdout=log_a,
        stderr=log_a,
    )
    proc_b = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.test_worker:app", "--host", "127.0.0.1", "--port", str(port_b)],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        env=env_b,
        stdout=log_b,
        stderr=log_b,
    )

    url_a = f"http://127.0.0.1:{port_a}"
    url_b = f"http://127.0.0.1:{port_b}"

    client_a = AsyncClient(base_url=url_a, timeout=10.0)
    client_b = AsyncClient(base_url=url_b, timeout=10.0)
    client_pc = None

    try:
        # Wait for workers to be healthy
        ready_a = False
        ready_b = False
        for _ in range(50):
            try:
                res_a = await client_a.get("/signaling/ice-servers")
                if res_a.status_code in (200, 401):
                    ready_a = True
                res_b = await client_b.get("/signaling/ice-servers")
                if res_b.status_code in (200, 401):
                    ready_b = True
                if ready_a and ready_b:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)

        if not (ready_a and ready_b):
            log_a.seek(0)
            log_b.seek(0)
            err_a = log_a.read()
            err_b = log_b.read()
            assert ready_a, f"Worker A failed to become ready: {err_a}"
            assert ready_b, f"Worker B failed to become ready: {err_b}"

        # Register owner and create published agent on Worker A
        email = f"rpc-{uuid.uuid4().hex[:8]}@example.com"
        reg = await client_a.post("/auth/register", json={"email": email, "password": "correct horse battery staple"})
        assert reg.status_code == 201
        login = await client_a.post("/auth/login", json={"email": email, "password": "correct horse battery staple"})
        assert login.status_code == 200
        origin = os.environ.get("APP_ORIGIN", "http://localhost:3000")
        headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}

        agent_slug = f"agent-rpc-{uuid.uuid4().hex[:8]}"
        create_agent = await client_a.post(
            "/agents",
            headers=headers,
            json={
                "name": "Signaling Relay Agent",
                "slug": agent_slug,
                "instructions": "Be helpful.",
                "languages": ["en"],
                "default_language": "en",
                "stt_provider": "deepgram",
                "stt_model": "nova-3",
                "llm_provider": "openai",
                "llm_model": "gpt-4o-mini",
                "tts_provider": "cartesia",
                "tts_model": "sonic-3.6",
                "voice_id": "cartesia-en",
            },
        )
        assert create_agent.status_code == 201
        agent_id = create_agent.json()["id"]

        # Publish agent
        pub = await client_a.post(f"/agents/{agent_id}/publish", headers=headers)
        assert pub.status_code == 200

        # Create public call on Worker A
        call_res = await client_a.post(f"/public/agents/{agent_slug}/calls", json={})
        assert call_res.status_code == 201
        call_data = call_res.json()
        signaling_token = call_data["signaling_token"]

        # Generate real WebRTC offer using aiortc
        client_pc = RTCPeerConnection()
        client_pc.addTransceiver("audio", direction="sendrecv")
        offer = await client_pc.createOffer()
        await client_pc.setLocalDescription(offer)

        # 2. Initial offer to Worker A
        offer_res = await client_a.post(
            "/signaling/offer",
            json={
                "sdp": client_pc.localDescription.sdp,
                "type": "offer",
                "signaling_token": signaling_token,
            },
        )
        assert offer_res.status_code == 200
        answer_data = offer_res.json()
        assert answer_data["type"] == "answer"
        pc_id = answer_data["pc_id"]
        renegotiation_token = answer_data["renegotiation_token"]
        assert pc_id
        assert renegotiation_token

        # Set remote description so aiortc client is stable
        await client_pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer_data["sdp"], type=answer_data["type"])
        )

        # 3. Cross-worker request: Send candidates to Worker B!
        candidate_res = await client_b.patch(
            "/signaling/candidates",
            json={
                "pc_id": pc_id,
                "renegotiation_token": renegotiation_token,
                "candidates": [
                    {
                        "candidate": "candidate:1 1 UDP 2130706431 192.0.2.1 50000 typ host",
                        "sdp_mid": "0",
                        "sdp_mline_index": 0,
                    }
                ],
            },
        )
        assert candidate_res.status_code == 200
        assert candidate_res.json() == {"accepted": True}

        # 4. Cross-worker request with invalid token to Worker B -> 401
        invalid_res = await client_b.patch(
            "/signaling/candidates",
            json={
                "pc_id": pc_id,
                "renegotiation_token": "wrong-token-value",
                "candidates": [],
            },
        )
        assert invalid_res.status_code == 401

        # 5. Cross-worker renegotiation offer to Worker B!
        renego_offer = await client_pc.createOffer()
        await client_pc.setLocalDescription(renego_offer)

        renego_res = await client_b.post(
            "/signaling/offer",
            json={
                "sdp": client_pc.localDescription.sdp,
                "type": "offer",
                "pc_id": pc_id,
                "renegotiation_token": renegotiation_token,
            },
        )
        assert renego_res.status_code == 200
        assert renego_res.json()["type"] == "answer"
        assert renego_res.json()["renegotiation_token"] == renegotiation_token

    finally:
        if client_pc:
            await client_pc.close()
        await client_a.aclose()
        await client_b.aclose()

        proc_a.terminate()
        proc_b.terminate()
        try:
            proc_a.wait(timeout=5)
        except Exception:
            proc_a.kill()
        try:
            proc_b.wait(timeout=5)
        except Exception:
            proc_b.kill()

        try:
            log_a.close()
            log_b.close()
            os.unlink(log_a.name)
            os.unlink(log_b.name)
        except Exception:
            pass

        # Cleanup isolated Redis keys
        cleanup_resources = await open_redis_resources(test_redis_url, namespace)
        try:
            async for key in cleanup_resources.commands.scan_iter(match=f"{namespace}:*"):
                await cleanup_resources.commands.unlink(key)
        finally:
            await cleanup_resources.close()

