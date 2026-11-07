import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.admin import get_frozen_settings
from app.agents import Agent
from app.db import Base, Session, engine
from app.main import app
from app.models import User
from app.sessions import CallSession


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


async def promote_to_admin(email: str) -> None:
    async with Session() as db:
        user = await db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = "admin"
        await db.commit()


def mutation_headers(token: str) -> dict[str, str]:
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": token}


async def admin_login(client: AsyncClient) -> tuple[str, str]:
    email = f"admin-{uuid.uuid4()}@example.com"
    token = await login(client, email)
    await promote_to_admin(email)
    return email, token


@pytest.mark.anyio
async def test_admin_routes_require_admin_and_csrf(client):
    assert (await client.get("/admin/global-prompt")).status_code == 401
    assert (
        await client.put("/admin/global-prompt", json={"prompt_text": "A safe voice assistant."})
    ).status_code == 401

    email = f"user-{uuid.uuid4()}@example.com"
    token = await login(client, email)
    assert (await client.get("/admin/users")).status_code == 403
    valid_policy = {
        "max_call_duration_seconds": 900,
        "max_concurrent_calls_per_agent": 3,
        "max_concurrent_calls_overall": 25,
    }
    assert (await client.put("/admin/policy", json=valid_policy)).status_code == 403

    await promote_to_admin(email)
    assert (await client.get("/admin/global-prompt")).status_code == 200
    assert (await client.put("/admin/policy", json=valid_policy)).status_code == 403
    assert (
        await client.put(
            "/admin/policy",
            json={"max_call_duration_seconds": 900, "max_concurrent_calls_per_agent": 3,
                  "max_concurrent_calls_overall": 25},
            headers=mutation_headers(token),
        )
    ).status_code == 200


@pytest.mark.anyio
async def test_prompt_versions_policy_validation_and_frozen_snapshot(client):
    _, token = await admin_login(client)
    headers = mutation_headers(token)

    initial = await client.get("/admin/global-prompt")
    assert initial.status_code == 200
    assert initial.json()["version"] == 1
    async with Session() as db:
        frozen_before_edit = await get_frozen_settings(db)
    json.dumps(frozen_before_edit)
    assert frozen_before_edit == {
        "global_prompt": {"text": initial.json()["prompt_text"], "version": 1},
        "policy": {
            "max_call_duration_seconds": 900,
            "max_concurrent_calls_per_agent": 3,
            "max_concurrent_calls_overall": 25,
        },
    }

    updated = await client.put(
        "/admin/global-prompt",
        json={"prompt_text": "Follow the approved call policy. Keep private details private."},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    repeated = await client.put(
        "/admin/global-prompt",
        json={"prompt_text": "Follow the approved call policy. Keep private details private."},
        headers=headers,
    )
    assert repeated.json()["version"] == 2
    assert (await client.put("/admin/global-prompt", json={"prompt_text": "   "}, headers=headers)).status_code == 422

    async with Session() as db:
        frozen_after_edit = await get_frozen_settings(db)
        assert frozen_after_edit["global_prompt"]["version"] == 2
    assert frozen_before_edit["global_prompt"]["version"] == 1
    assert frozen_after_edit["global_prompt"] == {"text": updated.json()["prompt_text"], "version": 2}

    defaults = await client.get("/admin/policy")
    assert defaults.status_code == 200
    assert defaults.json() == frozen_after_edit["policy"]
    policy = {
        "max_call_duration_seconds": 600,
        "max_concurrent_calls_per_agent": 2,
        "max_concurrent_calls_overall": 8,
    }
    changed_policy = await client.put("/admin/policy", json=policy, headers=headers)
    assert changed_policy.status_code == 200
    assert changed_policy.json() == policy
    assert (await client.get("/admin/policy")).json() == policy

    async with Session() as db:
        frozen_after_policy = await get_frozen_settings(db)
    assert frozen_after_policy["global_prompt"]["version"] == 2
    assert frozen_after_policy["policy"] == policy

    too_long = {**policy, "max_call_duration_seconds": 3601}
    assert (await client.put("/admin/policy", json=too_long, headers=headers)).status_code == 422
    inconsistent = {**policy, "max_concurrent_calls_per_agent": 9}
    assert (await client.put("/admin/policy", json=inconsistent, headers=headers)).status_code == 422
    assert (await client.get("/admin/policy")).json() == policy


@pytest.mark.anyio
async def test_admin_directories_expose_metadata_counts_and_suspend_with_csrf(client):
    admin_email, admin_token = await admin_login(client)
    owner_email = f"owner-{uuid.uuid4()}@example.com"
    owner_token = await login(client, owner_email)
    created = await client.post(
        "/agents",
        json={"name": "Front desk", "slug": f"front-desk-{uuid.uuid4().hex}", "instructions": "PRIVATE AGENT PROMPT"},
        headers=mutation_headers(owner_token),
    )
    assert created.status_code == 201
    agent_id = uuid.UUID(created.json()["id"])
    async with Session() as db:
        agent = await db.get(Agent, agent_id)
        assert agent is not None
        await db.commit()
        expiry = datetime.now(timezone.utc) + timedelta(minutes=2)
        db.add_all(
            [
                CallSession(
                    owner_id=agent.owner_id,
                    agent_id=agent_id,
                    call_type="test",
                    status="active",
                    snapshot={"transcript": "PRIVATE SESSION DATA"},
                    signaling_expires_at=expiry,
                ),
                CallSession(
                    owner_id=agent.owner_id,
                    agent_id=agent_id,
                    call_type="public",
                    status="completed",
                    snapshot={"prompt_text": "PRIVATE SESSION DATA"},
                    signaling_expires_at=expiry,
                ),
            ]
        )
        await db.commit()
    admin_token = await login_existing(client, admin_email)

    users_response = await client.get("/admin/users")
    agents_response = await client.get("/admin/agents")
    assert users_response.status_code == agents_response.status_code == 200
    owner = next(item for item in users_response.json() if item["email"] == owner_email)
    agent = next(item for item in agents_response.json() if item["id"] == str(agent_id))
    assert owner["agent_count"] == 1
    assert owner["session_count"] == 2
    assert owner["active_session_count"] == 1
    assert agent["owner_id"] == owner["id"]
    assert agent["owner_email"] == owner_email
    assert agent["step_count"] == 0
    assert agent["session_count"] == 2
    assert agent["active_session_count"] == 1
    assert admin_email in {item["email"] for item in users_response.json()}

    directory_json = users_response.text + agents_response.text
    for private_value in (
        "password_hash",
        "PRIVATE AGENT PROMPT",
        "PRIVATE SESSION DATA",
        "prompt_text",
        "answers",
        "transcript",
    ):
        assert private_value not in directory_json

    assert (await client.post(f"/admin/agents/{agent_id}/suspend")).status_code == 403
    async with Session() as db:
        stored_agent = await db.get(Agent, agent_id)
        assert stored_agent is not None and stored_agent.status == "draft"

    suspended = await client.post(
        f"/admin/agents/{agent_id}/suspend",
        headers=mutation_headers(admin_token),
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"
    assert set(suspended.json()) == {"id", "status", "updated_at"}
    await login_existing(client, owner_email)
    assert (await client.get(f"/agents/{agent_id}")).json()["status"] == "suspended"

    admin_token = await login_existing(client, admin_email)

    assert (
        await client.post(
            f"/admin/agents/{uuid.uuid4()}/suspend",
            headers=mutation_headers(admin_token),
        )
    ).status_code == 404
