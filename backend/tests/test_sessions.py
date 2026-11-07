import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.agents import Agent
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
    set_session_status,
)


@pytest.fixture
async def client():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
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

    with pytest.raises(SessionUnavailableError, match="expired"):
        await set_session_status(session_id, "active")
    assert await load_signaling_context(session_id) is None
    async with Session() as db:
        stored = await db.get(CallSession, session_id)
        assert stored is not None
        assert stored.status == "expired"
    with pytest.raises(SessionUnavailableError, match="expired to active"):
        await set_session_status(session_id, "active")


@pytest.mark.anyio
async def test_app_startup_expires_unusable_created_sessions_and_fails_active_sessions(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"restart-{uuid.uuid4().hex[:12]}")
    session_ids = []
    for _ in range(3):
        response = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
        assert response.status_code == 201
        session_ids.append(uuid.UUID(response.json()["session_id"]))
    created_session_id, active_session_id, completed_session_id = session_ids
    await set_session_status(active_session_id, "active")
    await set_session_status(completed_session_id, "active")
    await set_session_status(completed_session_id, "completed")

    async with Session() as db:
        assert await _open_call_count(db, datetime.now(timezone.utc)) == 2
        created = await db.get(CallSession, created_session_id)
        completed = await db.get(CallSession, completed_session_id)
        assert created is not None
        assert created.signaling_expires_at > datetime.now(timezone.utc)
        assert completed is not None
        completed_ended_at = completed.ended_at
        assert completed_ended_at is not None

    async with app.router.lifespan_context(app):
        pass

    async with Session() as db:
        created = await db.get(CallSession, created_session_id)
        active = await db.get(CallSession, active_session_id)
        completed = await db.get(CallSession, completed_session_id)
        assert created is not None
        assert created.status == "expired"
        assert created.ended_at is not None
        assert active is not None
        assert active.status == "failed"
        assert active.ended_at is not None
        assert completed is not None
        assert completed.status == "completed"
        assert completed.ended_at == completed_ended_at
        assert await _open_call_count(db, datetime.now(timezone.utc)) == 0


@pytest.mark.anyio
async def test_app_startup_does_not_require_session_schema(client):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)

    async with app.router.lifespan_context(app):
        pass






@pytest.mark.anyio
async def test_agent_delete_rejects_an_active_call_while_agent_is_locked(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, f"active-{uuid.uuid4().hex[:12]}")
    call = await client.post(f"/agents/{agent['id']}/test-calls", headers=mutation_headers(token))
    session_id = uuid.UUID(call.json()["session_id"])
    writer = SessionWriter(session_id)
    writer.enqueue_message("user", "Please call me back.")
    await writer.flush()
    await set_session_status(session_id, "active")

    deletion = await client.delete(f"/agents/{agent['id']}", headers=mutation_headers(token))
    assert deletion.status_code == 409

    await set_session_status(session_id, "completed")
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
