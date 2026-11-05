import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.agents import Agent
from app.db import Base, Session, engine
from app.main import app
from app.steps import Step


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
    registered = await client.post("/auth/register", json={"email": email, "password": password})
    assert registered.status_code == 201
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return response.json()["csrf_token"]


async def login_existing(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def mutation_headers(token: str) -> dict[str, str]:
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": token}


async def create_agent(client: AsyncClient, token: str, slug: str) -> dict:
    response = await client.post(
        "/agents",
        json={"name": "Reception", "slug": slug},
        headers=mutation_headers(token),
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.anyio
async def test_agent_routes_hide_other_owners_agents(client):
    owner_id = str(uuid.uuid4())
    owner_token = await login(client, f"{owner_id}@example.com")
    agent = await create_agent(client, owner_token, f"agent-{owner_id}")
    assert [item["id"] for item in (await client.get("/agents")).json()] == [agent["id"]]

    other_id = str(uuid.uuid4())
    other_token = await login(client, f"{other_id}@example.com")
    assert (await client.get("/agents")).json() == []
    assert (await client.get(f"/agents/{agent['id']}")).status_code == 404
    headers = mutation_headers(other_token)
    assert (await client.patch(f"/agents/{agent['id']}", json={"name": "Changed"}, headers=headers)).status_code == 404
    assert (await client.post(f"/agents/{agent['id']}/publish", headers=headers)).status_code == 404
    assert (await client.post(f"/agents/{agent['id']}/unpublish", headers=headers)).status_code == 404
    assert (await client.delete(f"/agents/{agent['id']}", headers=headers)).status_code == 404

    owner_token = await login_existing(client, f"{owner_id}@example.com")
    detail = await client.get(f"/agents/{agent['id']}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Reception"
    assert detail.json()["status"] == "draft"


@pytest.mark.anyio
async def test_slug_is_globally_unique_across_owners(client):
    first_id = str(uuid.uuid4())
    first_token = await login(client, f"{first_id}@example.com")
    await create_agent(client, first_token, "shared-agent")

    second_id = str(uuid.uuid4())
    second_token = await login(client, f"{second_id}@example.com")
    response = await client.post(
        "/agents",
        json={"name": "Another agent", "slug": "shared-agent"},
        headers=mutation_headers(second_token),
    )
    assert response.status_code == 409
    assert (await client.get("/agents")).json() == []


@pytest.mark.anyio
async def test_owner_can_edit_a_draft_without_changing_its_state(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, "draft-agent")
    assert agent["status"] == "draft"

    response = await client.patch(
        f"/agents/{agent['id']}",
        json={
            "name": "Updated reception",
            "instructions": "Help callers find the right team.",
            "languages": ["en", "hi"],
            "default_language": "en",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        },
        headers=mutation_headers(token),
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Updated reception"
    assert response.json()["instructions"] == "Help callers find the right team."
    assert response.json()["languages"] == ["en", "hi"]
    assert response.json()["llm_provider"] == "openai"
    assert response.json()["status"] == "draft"

    invalid = await client.patch(
        f"/agents/{agent['id']}",
        json={"status": "published"},
        headers=mutation_headers(token),
    )
    assert invalid.status_code == 422
    assert (await client.get(f"/agents/{agent['id']}")).json()["status"] == "draft"


@pytest.mark.anyio
async def test_every_agent_mutation_requires_csrf(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    created = await client.post("/agents", json={"name": "Reception", "slug": "csrf-agent"})
    assert created.status_code == 403

    agent = await create_agent(client, token, "csrf-agent")
    agent_id = agent["id"]
    assert (await client.patch(f"/agents/{agent_id}", json={"name": "No token"})).status_code == 403
    assert (await client.post(f"/agents/{agent_id}/publish")).status_code == 403
    assert (await client.post(f"/agents/{agent_id}/unpublish")).status_code == 403
    assert (await client.delete(f"/agents/{agent_id}")).status_code == 403
    assert (await client.get(f"/agents/{agent_id}")).json()["name"] == "Reception"


@pytest.mark.anyio
async def test_publish_fails_closed_without_server_provider_configuration(client, monkeypatch):
    for name in (
        "DEEPGRAM_API_KEY",
        "OPENAI_API_KEY",
        "CARTESIA_API_KEY",
        "ELEVENLABS_API_KEY",
        "VERIFIED_OPENAI_MODELS",
        "VERIFIED_TTS_VOICE_CATALOG",
    ):
        monkeypatch.delenv(name, raising=False)

    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, "incomplete-agent")
    updated = await client.patch(
        f"/agents/{agent['id']}",
        headers=mutation_headers(token),
        json={
            "instructions": "Help callers find the right team.",
            "languages": ["en"],
            "default_language": "en",
            "stt_provider": "deepgram",
            "stt_model": "nova-3",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "tts_provider": "cartesia",
            "tts_model": "sonic-3.6",
            "voice_id": "test-voice",
        },
    )
    assert updated.status_code == 200
    step = await client.post(
        f"/agents/{agent['id']}/steps",
        headers=mutation_headers(token),
        json={
            "field_key": "welcome",
            "title": "Welcome",
            "objective": "Greet the caller",
            "answer_type": "message",
            "wording_mode": "flexible",
            "sort_order": 0,
        },
    )
    assert step.status_code == 201

    response = await client.post(
        f"/agents/{agent['id']}/publish",
        headers=mutation_headers(token),
    )
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert "agent.stt_provider" in errors
    assert "agent.llm_model" in errors
    assert "agent.voice_id" in errors
    assert (await client.get(f"/agents/{agent['id']}")).json()["status"] == "draft"


@pytest.mark.anyio
async def test_suspension_cannot_be_cleared_by_owner_edit_or_publish(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, "suspended-agent")
    async with Session() as db:
        stored = await db.scalar(select(Agent).where(Agent.id == uuid.UUID(agent["id"])))
        stored.status = "suspended"
        await db.commit()

    edited = await client.patch(
        f"/agents/{agent['id']}",
        json={"name": "Edited while suspended"},
        headers=mutation_headers(token),
    )
    assert edited.status_code == 200
    assert edited.json()["name"] == "Edited while suspended"
    assert edited.json()["status"] == "suspended"

    published = await client.post(
        f"/agents/{agent['id']}/publish",
        headers=mutation_headers(token),
    )
    assert published.status_code == 409
    unpublished = await client.post(
        f"/agents/{agent['id']}/unpublish",
        headers=mutation_headers(token),
    )
    assert unpublished.status_code == 409
    assert (await client.get(f"/agents/{agent['id']}")).json()["status"] == "suspended"

@pytest.mark.anyio
async def test_deleting_agent_cascades_to_its_question_steps(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, "agent-with-step")
    agent_id = uuid.UUID(agent["id"])

    async with Session() as db:
        db.add(Step(agent_id=agent_id, sort_order=0))
        await db.commit()

    response = await client.delete(f"/agents/{agent['id']}", headers=mutation_headers(token))
    assert response.status_code == 204
    async with Session() as db:
        remaining_steps = list(await db.scalars(select(Step.id).where(Step.agent_id == agent_id)))
    assert remaining_steps == []

@pytest.mark.anyio
async def test_publish_and_unpublish_persist_agent_state(client, monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "test-only")
    monkeypatch.setenv(
        "VERIFIED_TTS_VOICE_CATALOG",
        '{"cartesia":{"sonic-3.6":{"test-voice":["en"]}}}',
    )
    token = await login(client, f"{uuid.uuid4()}@example.com")
    agent = await create_agent(client, token, "publishable-agent")
    headers = mutation_headers(token)

    updated = await client.patch(
        f"/agents/{agent['id']}",
        json={
            "instructions": "Help callers find the right team.",
            "languages": ["en"],
            "default_language": "en",
            "stt_provider": "deepgram",
            "stt_model": "nova-3",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "tts_provider": "cartesia",
            "tts_model": "sonic-3.6",
            "voice_id": "test-voice",
        },
        headers=headers,
    )
    assert updated.status_code == 200

    step = await client.post(
        f"/agents/{agent['id']}/steps",
        json={
            "field_key": "welcome",
            "title": "Welcome",
            "objective": "Greet the caller",
            "answer_type": "message",
            "wording_mode": "flexible",
            "sort_order": 0,
        },
        headers=headers,
    )
    assert step.status_code == 201

    published = await client.post(f"/agents/{agent['id']}/publish", headers=headers)
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    unpublished = await client.post(f"/agents/{agent['id']}/unpublish", headers=headers)
    assert unpublished.status_code == 200
    assert unpublished.json()["status"] == "draft"
