import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.agents import Agent
from app.coordination import CallClaim
from app.db import Base, Session, engine
from app.main import app
from app.models import User
from app.sessions import (
    CallMessage,
    CallSession,
    SessionUnavailableError,
    SessionWriter,
    _open_call_count,
    load_session_snapshot,
    load_signaling_context,
    reconcile_voice_sessions,
    renew_voice_claim,
    set_session_status,
)
from app.voice import CallDefinition
from voice_pipeline_harness import verified_environment


async def activate_call(session_id: uuid.UUID, signaling_token: str) -> CallClaim:
    coordination = getattr(app.state, "coordination", None)
    claim = await coordination.consume_token(signaling_token)
    obs = await coordination.begin_setup(claim)
    async with Session() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(42000001)"))
        await db.execute(
            update(CallSession)
            .where(CallSession.id == session_id)
            .values(
                worker_id=coordination.worker_id,
                claim_generation=obs.claim.generation,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=45),
            )
        )
        await db.commit()
    await set_session_status(session_id, "active", claim=obs.claim)
    active_obs = await coordination.activate(obs.claim, f"pc-{session_id.hex[:8]}")
    return active_obs.claim


@pytest.fixture
async def client(redis_resources):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    app.state.redis = redis_resources
    app.state.redis_namespace = redis_resources.namespace
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000") as test_client:
            yield test_client


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def login(client: AsyncClient, email: str) -> str:
    password = "correct horse battery staple"
    await client.post("/auth/register", json={"email": email, "password": password})
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def mutation_headers(token: str) -> dict[str, str]:
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": token}


async def create_agent(client: AsyncClient, token: str, slug: str) -> dict:
    response = await client.post(
        "/agents",
        headers=mutation_headers(token),
        json={
            "name": "Reception",
            "slug": slug,
            "instructions": "Help the caller.",
            "languages": ["en"],
            "default_language": "en",
            "stt_provider": "deepgram",
            "stt_model": "nova-3",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "tts_provider": "cartesia",
            "tts_model": "sonic-3.6",
            "voice_id": "voice-test",
        },
    )
    assert response.status_code == 201
    return response.json()


async def publish_agent(agent_id: str) -> None:
    async with Session() as db:
        agent = await db.get(Agent, uuid.UUID(agent_id))
        assert agent is not None
        agent.status = "published"
        await db.commit()


@pytest.mark.anyio
async def test_owner_test_call_persists_a_frozen_snapshot_and_hides_it_from_api(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"test-{uuid.uuid4().hex[:12]}")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:3000"
    ) as admin_client:
        admin_email = f"{uuid.uuid4()}@example.com"
        admin_token = await login(admin_client, admin_email)
        async with Session() as db:
            admin_user = await db.scalar(select(User).where(User.email == admin_email))
            assert admin_user is not None
            admin_user.role = "admin"
            await db.commit()
        original_prompt = await admin_client.get("/admin/global-prompt")
        assert original_prompt.status_code == 200

        response = await client.post(
            f"/agents/{agent['id']}/test-calls",
            headers=mutation_headers(token),
            json={"language": "en"},
        )
        assert response.status_code == 201
        result = response.json()
        assert result["session_id"]
        assert result["signaling_token"]
        assert "snapshot" not in result
        snapshot = await load_session_snapshot(uuid.UUID(result["session_id"]))
        assert snapshot is not None
        assert snapshot["language"] == "en"
        assert snapshot["agent"]["instructions"] == "Help the caller."
        assert snapshot["agent"]["llm_model"] == "gpt-4o-mini"
        assert snapshot["global_prompt"] == {
            "text": original_prompt.json()["prompt_text"],
            "version": original_prompt.json()["version"],
        }
        assert "policy" in snapshot

        changed_prompt = await admin_client.put(
            "/admin/global-prompt",
            headers=mutation_headers(admin_token),
            json={"prompt_text": "Updated global policy."},
        )
        assert changed_prompt.status_code == 200
        assert changed_prompt.json()["version"] > original_prompt.json()["version"]
        async with Session() as db:
            stored = await db.get(CallSession, uuid.UUID(result["session_id"]))
            assert stored is not None
            stored_agent = await db.get(Agent, uuid.UUID(agent["id"]))
            assert stored_agent is not None
            stored_agent.instructions = "Changed after the call started."
            await db.commit()

        snapshot = await load_session_snapshot(uuid.UUID(result["session_id"]))
        assert snapshot is not None
        assert snapshot["agent"]["instructions"] == "Help the caller."
        assert snapshot["global_prompt"] == {
            "text": original_prompt.json()["prompt_text"],
            "version": original_prompt.json()["version"],
        }
        detail = await client.get(f"/sessions/{result['session_id']}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "created"
        assert "snapshot" not in detail.json()


@pytest.mark.anyio
async def test_public_metadata_and_call_response_exclude_internal_agent_settings(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    slug = f"public-{uuid.uuid4().hex[:12]}"
    agent = await create_agent(client, token, slug)
    await publish_agent(agent["id"])

    metadata = await client.get(f"/public/agents/{slug}")
    assert metadata.status_code == 200
    assert metadata.json() == {
        "name": "Reception",
        "slug": slug,
        "description": None,
        "languages": ["en"],
        "default_language": "en",
    }

    call = await client.post(f"/public/agents/{slug}/calls")
    assert call.status_code == 201
    assert set(call.json()) == {"session_id", "signaling_token"}
    assert "instructions" not in call.json()
    assert "provider" not in call.json()


@pytest.mark.anyio
async def test_call_creation_enforces_per_agent_and_global_limits(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent_a = await create_agent(client, token, f"cap-a-{uuid.uuid4().hex[:8]}")
    agent_b = await create_agent(client, token, f"cap-b-{uuid.uuid4().hex[:8]}")
    agent_c = await create_agent(client, token, f"cap-c-{uuid.uuid4().hex[:8]}")
    policy = {
        "max_call_duration_seconds": 900,
        "max_concurrent_calls_per_agent": 1,
        "max_concurrent_calls_overall": 2,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:3000"
    ) as admin_client:
        admin_email = f"{uuid.uuid4()}@example.com"
        admin_token = await login(admin_client, admin_email)
        async with Session() as db:
            admin_user = await db.scalar(select(User).where(User.email == admin_email))
            assert admin_user is not None
            admin_user.role = "admin"
            await db.commit()
        configured = await admin_client.put(
            "/admin/policy",
            headers=mutation_headers(admin_token),
            json=policy,
        )
        assert configured.status_code == 200
        assert configured.json() == policy

        first = await client.post(
            f"/agents/{agent_a['id']}/test-calls", headers=mutation_headers(token)
        )
        assert first.status_code == 201
        first_snapshot = await load_session_snapshot(uuid.UUID(first.json()["session_id"]))
        assert first_snapshot is not None
        assert first_snapshot["policy"] == policy

        same_agent = await client.post(
            f"/agents/{agent_a['id']}/test-calls", headers=mutation_headers(token)
        )
        other_agent = await client.post(
            f"/agents/{agent_b['id']}/test-calls", headers=mutation_headers(token)
        )
        global_limit = await client.post(
            f"/agents/{agent_c['id']}/test-calls", headers=mutation_headers(token)
        )
        assert same_agent.status_code == 429
        assert other_agent.status_code == 201
        assert global_limit.status_code == 429
        assert same_agent.json()["detail"] == "Concurrent call limit reached"
        assert global_limit.json()["detail"] == "Concurrent call limit reached"


@pytest.mark.anyio
async def test_session_history_is_owner_scoped_for_detail_messages_and_answers(client):
    owner_email = f"{uuid.uuid4()}@example.com"
    owner_token = await login(client, owner_email)
    agent = await create_agent(client, owner_token, f"owned-{uuid.uuid4().hex[:12]}")
    call = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(owner_token))
    session_id = uuid.UUID(call.json()["session_id"])

    writer = SessionWriter(session_id)
    writer.enqueue_message("user", "I need help.")
    writer.enqueue_message("assistant", "How can I help?")
    await writer.flush()

    history = await client.get(f"/agents/{agent['id']}/sessions")
    assert history.status_code == 200
    assert [item["id"] for item in history.json()] == [str(session_id)]
    messages = await client.get(f"/sessions/{session_id}/messages")
    assert messages.status_code == 200
    assert [message["content"] for message in messages.json()] == ["I need help.", "How can I help?"]
    assert [message["sequence"] for message in messages.json()] == [1, 2]
    await login(client, f"{uuid.uuid4()}@example.com")
    assert (await client.get(f"/sessions/{session_id}")).status_code == 404
    assert (await client.get(f"/sessions/{session_id}/messages")).status_code == 404
    assert (await client.get(f"/agents/{agent['id']}/sessions")).status_code == 404

    async with Session() as db:
        stored_messages = list(
            await db.scalars(select(CallMessage).where(CallMessage.session_id == session_id).order_by(CallMessage.sequence))
        )
    assert len(stored_messages) == 2





@pytest.mark.anyio
async def test_expired_signaling_cannot_activate_a_call(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"expired-{uuid.uuid4().hex[:12]}")
    call = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    session_id = uuid.UUID(call.json()["session_id"])

    async with Session() as db:
        stored = await db.get(CallSession, session_id)
        assert stored is not None
        stored.signaling_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

    claim = CallClaim(
        session_id=session_id,
        agent_id=stored.agent_id,
        call_type="test",
        generation=stored.claim_generation or "",
        worker_id=None,
        phase="created",
    )
    with pytest.raises(SessionUnavailableError, match="expired"):
        await set_session_status(session_id, "active", claim=claim)
    assert await load_signaling_context(session_id) is None
    coordination = app.state.coordination
    await reconcile_voice_sessions(coordination)
    async with Session() as db:
        stored = await db.get(CallSession, session_id)
        assert stored is not None
        assert stored.status == "expired"
    with pytest.raises(SessionUnavailableError, match="expired to active"):
        await set_session_status(session_id, "active", claim=claim)


@pytest.mark.anyio
async def test_non_destructive_recovery_preserves_valid_and_reaps_expired(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"restart-{uuid.uuid4().hex[:12]}")

    # 1. Valid unexpired created session
    r1 = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    unexpired_created_id = uuid.UUID(r1.json()["session_id"])

    # 2. Expired created session
    r2 = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    expired_created_id = uuid.UUID(r2.json()["session_id"])
    async with Session() as db:
        c = await db.get(CallSession, expired_created_id)
        assert c is not None
        c.signaling_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await db.commit()

    # 3. Active session with expired lease and no Redis lease
    r3 = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    expired_active_id = uuid.UUID(r3.json()["session_id"])
    active_claim = await activate_call(expired_active_id, r3.json()["signaling_token"])
    # Expire Redis lease and DB lease
    coordination = app.state.coordination
    await coordination.release(active_claim, f"pc-{expired_active_id.hex[:8]}")
    async with Session() as db:
        a = await db.get(CallSession, expired_active_id)
        assert a is not None
        a.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await db.commit()

    # 4. Completed session
    r4 = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    completed_id = uuid.UUID(r4.json()["session_id"])
    completed_claim = await activate_call(completed_id, r4.json()["signaling_token"])
    await set_session_status(completed_id, "completed", claim=completed_claim)
    async with Session() as db:
        comp = await db.get(CallSession, completed_id)
        assert comp is not None
        completed_ended_at = comp.ended_at
        assert completed_ended_at is not None

    # 5. Chat session (call_type='chat_test', signaling_expires_at=None)
    chat_r = await client.post(f"/agents/{agent['id']}/chat-sessions", headers=mutation_headers(token))
    chat_session_id = uuid.UUID(chat_r.json()["session_id"])

    # Run reconciliation (which also runs on startup)
    reaped = await reconcile_voice_sessions(coordination)
    assert reaped >= 2

    async with Session() as db:
        unexp = await db.get(CallSession, unexpired_created_id)
        exp_cr = await db.get(CallSession, expired_created_id)
        exp_act = await db.get(CallSession, expired_active_id)
        comp = await db.get(CallSession, completed_id)
        chat_sess = await db.get(CallSession, chat_session_id)

        assert unexp is not None
        assert unexp.status == "created"  # Preserved!
        assert exp_cr is not None
        assert exp_cr.status == "expired"  # Reaped!
        assert exp_act is not None
        assert exp_act.status == "failed"  # Reaped!
        assert comp is not None
        assert comp.status == "completed"  # Preserved!
        assert comp.ended_at == completed_ended_at
        assert chat_sess is not None
        assert chat_sess.status == "created"  # Preserved chat row!


@pytest.mark.anyio
async def test_app_startup_fails_without_session_schema(client):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)

    try:
        with pytest.raises(RuntimeError, match="Run 'alembic upgrade head'"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)


@pytest.mark.anyio
async def test_agent_delete_rejects_an_active_call_while_agent_is_locked(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"active-{uuid.uuid4().hex[:12]}")
    call = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    session_id = uuid.UUID(call.json()["session_id"])
    signaling_token = call.json()["signaling_token"]
    writer = SessionWriter(session_id)
    writer.enqueue_message("user", "Please call me back.")
    await writer.flush()
    active_claim = await activate_call(session_id, signaling_token)

    deletion = await client.delete(f"/agents/{agent['id']}", headers=mutation_headers(token))
    assert deletion.status_code == 409

    await set_session_status(session_id, "completed", claim=active_claim)
    deletion = await client.delete(f"/agents/{agent['id']}", headers=mutation_headers(token))
    assert deletion.status_code == 204
    async with Session() as db:
        assert await db.get(CallSession, session_id) is None
        assert await db.scalar(select(CallMessage.id).where(CallMessage.session_id == session_id)) is None


@pytest.mark.anyio
async def test_suspended_agents_cannot_start_owner_or_public_calls(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"suspended-{uuid.uuid4().hex[:12]}")
    await publish_agent(agent["id"])
    async with Session() as db:
        stored = await db.get(Agent, uuid.UUID(agent["id"]))
        assert stored is not None
        stored.status = "suspended"
        await db.commit()

    owner_call = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    public_call = await client.post(f"/public/agents/{agent['slug']}/calls")
    metadata = await client.get(f"/public/agents/{agent['slug']}")
    assert owner_call.status_code == 409
    assert public_call.status_code == metadata.status_code == 404


@pytest.mark.anyio
async def test_azure_stt_locale_freeze_and_isolation(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    slug = f"azure-freeze-{uuid.uuid4().hex[:12]}"
    create_resp = await client.post(
        "/agents",
        headers=mutation_headers(token),
        json={
            "name": "Azure Reception",
            "slug": slug,
            "instructions": "Help the caller in British English.",
            "languages": ["en"],
            "default_language": "en",
            "stt_provider": "azure",
            "stt_model": "azure-speech",
            "azure_stt_english_locale": "en-GB",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "tts_provider": "cartesia",
            "tts_model": "sonic-3.6",
            "voice_id": "cartesia-en",
        },
    )
    assert create_resp.status_code == 201
    agent = create_resp.json()

    # First call en-GB
    first_call_resp = await client.post(
        f"/agents/{agent['id']}/test-calls",
        headers=mutation_headers(token),
        json={"language": "en"},
    )
    assert first_call_resp.status_code == 201
    first_session_id = uuid.UUID(first_call_resp.json()["session_id"])

    first_snapshot = await load_session_snapshot(first_session_id)
    assert first_snapshot is not None
    assert first_snapshot["agent"]["azure_stt_english_locale"] == "en-GB"

    # Verify no server credentials/region/endpoint in snapshot
    snap_str = str(first_snapshot).lower()
    assert "api_key" not in snap_str
    assert "stt_region" not in snap_str
    assert "stt_endpoint" not in snap_str
    assert "azure_speech" not in snap_str

    env = verified_environment()
    env["AZURE_SPEECH_API_KEY"] = "azure-secret"
    env["AZURE_SPEECH_REGION"] = "eastus"

    def1 = CallDefinition.from_snapshot(first_session_id, "test", first_snapshot, env)
    assert def1.providers.stt_provider == "azure"
    assert def1.providers.stt_language == "en-GB"

    # Edit agent to en-US
    patch_resp = await client.patch(
        f"/agents/{agent['id']}",
        headers=mutation_headers(token),
        json={"azure_stt_english_locale": "en-US"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["azure_stt_english_locale"] == "en-US"

    # Second call en-US
    second_call_resp = await client.post(
        f"/agents/{agent['id']}/test-calls",
        headers=mutation_headers(token),
        json={"language": "en"},
    )
    assert second_call_resp.status_code == 201
    second_session_id = uuid.UUID(second_call_resp.json()["session_id"])

    # First call remains en-GB
    first_snapshot = await load_session_snapshot(first_session_id)
    assert first_snapshot is not None
    assert first_snapshot["agent"]["azure_stt_english_locale"] == "en-GB"
    def1_again = CallDefinition.from_snapshot(first_session_id, "test", first_snapshot, env)
    assert def1_again.providers.stt_language == "en-GB"

    second_snapshot = await load_session_snapshot(second_session_id)
    assert second_snapshot is not None
    assert second_snapshot["agent"]["azure_stt_english_locale"] == "en-US"

    def2 = CallDefinition.from_snapshot(second_session_id, "test", second_snapshot, env)
    assert def2.providers.stt_language == "en-US"

    # Verify public metadata has no keys/region/endpoint
    await publish_agent(agent["id"])
    metadata = await client.get(f"/public/agents/{slug}")
    assert metadata.status_code == 200
    meta_str = str(metadata.json()).lower()
    assert "key" not in meta_str
    assert "region" not in meta_str
    assert "endpoint" not in meta_str


@pytest.mark.anyio
async def test_set_session_status_rejects_stale_claim_even_if_status_matches(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"test-{uuid.uuid4().hex[:12]}")

    call_resp = await client.post(
        f"/agents/{agent['id']}/test-calls",
        headers=mutation_headers(token),
        json={"language": "en"},
    )
    assert call_resp.status_code == 201
    session_id = uuid.UUID(call_resp.json()["session_id"])

    # Simulate activated session under worker-B and generation gen-2
    async with Session() as db:
        await db.execute(
            update(CallSession)
            .where(CallSession.id == session_id)
            .values(
                status="active",
                worker_id="worker-B",
                claim_generation="gen-2",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            )
        )
        await db.commit()

    stale_claim = CallClaim(
        session_id=session_id,
        agent_id=uuid.UUID(agent["id"]),
        call_type="test",
        generation="gen-1",
        worker_id="worker-A",
        phase="active",
    )

    # Calling with status="active" must NOT bypass claim check
    with pytest.raises(SessionUnavailableError, match="Claim generation mismatch"):
        await set_session_status(session_id, "active", claim=stale_claim)

    # Wrong worker with matching generation
    wrong_worker_claim = CallClaim(
        session_id=session_id,
        agent_id=uuid.UUID(agent["id"]),
        call_type="test",
        generation="gen-2",
        worker_id="worker-A",
        phase="active",
    )
    with pytest.raises(SessionUnavailableError, match="Worker owner mismatch"):
        await set_session_status(session_id, "active", claim=wrong_worker_claim)

    # Missing claim on claimed session
    with pytest.raises(SessionUnavailableError, match="Claim required"):
        await set_session_status(session_id, "active", claim=None)

    # Valid claim succeeds
    valid_claim = CallClaim(
        session_id=session_id,
        agent_id=uuid.UUID(agent["id"]),
        call_type="test",
        generation="gen-2",
        worker_id="worker-B",
        phase="active",
    )
    assert await set_session_status(session_id, "active", claim=valid_claim) is True


@pytest.mark.anyio
async def test_load_signaling_context_expires_active_session_when_lease_past_db_now(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"test-{uuid.uuid4().hex[:12]}")

    call_resp = await client.post(
        f"/agents/{agent['id']}/test-calls",
        headers=mutation_headers(token),
        json={"language": "en"},
    )
    assert call_resp.status_code == 201
    session_id = uuid.UUID(call_resp.json()["session_id"])

    # Active session with expired lease_expires_at
    past_time = datetime.now(timezone.utc) - timedelta(seconds=10)
    async with Session() as db:
        await db.execute(
            update(CallSession)
            .where(CallSession.id == session_id)
            .values(
                status="active",
                worker_id="worker-B",
                claim_generation="gen-2",
                lease_expires_at=past_time,
            )
        )
        await db.commit()

    context = await load_signaling_context(session_id)
    assert context is None

    # Future lease_expires_at returns durable context
    future_time = datetime.now(timezone.utc) + timedelta(seconds=60)
    async with Session() as db:
        await db.execute(
            update(CallSession)
            .where(CallSession.id == session_id)
            .values(lease_expires_at=future_time)
        )
        await db.commit()

    context = await load_signaling_context(session_id)
    assert context is not None
    assert context["worker_id"] == "worker-B"
    assert context["claim_generation"] == "gen-2"
    assert context["session_status"] == "active"


@pytest.mark.anyio
async def test_reconcile_voice_sessions_stops_write_when_leader_lost_under_lock(client, monkeypatch):
    coordination = getattr(app.state, "coordination", None)
    if coordination is None:
        pytest.skip("Coordination required")

    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"test-{uuid.uuid4().hex[:12]}")

    call_resp = await client.post(
        f"/agents/{agent['id']}/test-calls",
        headers=mutation_headers(token),
        json={"language": "en"},
    )
    assert call_resp.status_code == 201
    session_id = uuid.UUID(call_resp.json()["session_id"])

    # Make session expired active with no matching live Redis lease (otherwise reapable)
    async with Session() as db:
        await db.execute(
            update(CallSession)
            .where(CallSession.id == session_id)
            .values(
                status="active",
                worker_id="worker-dead",
                claim_generation="gen-dead",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=60),
            )
        )
        await db.commit()

    is_leader_active = True
    inspect_called = False
    orig_inspect = coordination.inspect

    async def inspect_and_lose_leadership(sid: uuid.UUID):
        nonlocal is_leader_active, inspect_called
        result = await orig_inspect(sid)
        if sid == session_id:
            inspect_called = True
            # Leadership lost deterministically after coordination.inspect under lock
            is_leader_active = False
        return result

    monkeypatch.setattr(coordination, "inspect", inspect_and_lose_leadership)

    # Leadership is initially True, drops to False under lock after inspect
    reaped = await reconcile_voice_sessions(coordination, is_leader=lambda: is_leader_active)
    assert inspect_called is True
    assert reaped == 0

    # Ensure without the in-transaction guard the row would have terminalized to "failed",
    # but with the guard it remains untouched in "active" status.
    async with Session() as db:
        row = await db.scalar(select(CallSession).where(CallSession.id == session_id))
        assert row is not None
        assert row.status == "active"


@pytest.mark.anyio
async def test_renew_voice_claim_fails_before_redis_if_durable_lease_expired_under_lock(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"renew-fence-{uuid.uuid4().hex[:12]}")
    call_resp = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    session_id = uuid.UUID(call_resp.json()["session_id"])

    coordination = app.state.coordination
    assert coordination is not None

    claim = CallClaim(
        session_id=session_id,
        agent_id=uuid.UUID(agent["id"]),
        call_type="test",
        generation="gen-test-renew",
        worker_id=coordination.worker_id,
        phase="active",
    )

    # Set up durable row with expired lease deadline
    async with Session() as db:
        await db.execute(
            update(CallSession)
            .where(CallSession.id == session_id)
            .values(
                status="active",
                worker_id=coordination.worker_id,
                claim_generation=claim.generation,
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
        )
        await db.commit()

    redis_renew_called = False
    orig_renew = coordination.renew

    async def tracking_renew(*args, **kwargs):
        nonlocal redis_renew_called
        redis_renew_called = True
        return await orig_renew(*args, **kwargs)

    coordination.renew = tracking_renew
    try:
        with pytest.raises(RuntimeError, match="expired deadline"):
            await renew_voice_claim(claim, coordination, "pc-test")
        # Proves durable row was checked and fenced BEFORE any cross-store mutation
        assert redis_renew_called is False
    finally:
        coordination.renew = orig_renew


@pytest.mark.anyio
async def test_reconcile_voice_sessions_cleans_up_orphaned_terminal_redis_lease(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"orphan-reconcile-{uuid.uuid4().hex[:12]}")
    call_resp = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    session_id = uuid.UUID(call_resp.json()["session_id"])

    coordination = app.state.coordination
    assert coordination is not None

    # The call creation already reserved a created lease in Redis
    obs_before = await coordination.inspect(session_id)
    assert obs_before is not None

    # Simulate crash/failure: DB committed as "completed" (terminal),
    # but Redis still has an active setup lease with matching generation
    async with Session() as db:
        await db.execute(
            update(CallSession)
            .where(CallSession.id == session_id)
            .values(
                status="completed",
                claim_generation=obs_before.claim.generation,
                ended_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    # Run reaper
    await reconcile_voice_sessions(coordination)

    # Verify reaper cleaned up the orphaned Redis lease
    obs_after = await coordination.inspect(session_id)
    assert obs_after is None


