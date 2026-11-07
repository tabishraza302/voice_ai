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
from app.fields import AgentField, AgentOutcome
from app.main import app
from app.models import User
from app.sessions import _frozen_call_snapshot


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


async def registered_client(client, email=None):
    email = email or f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    assert (await client.post("/auth/register", json={"email": email, "password": password})).status_code == 201
    login = await client.post("/auth/login", json={"email": email, "password": password})
    return login.json()["csrf_token"]


async def create_agent(email):
    async with Session() as db:
        user = await db.scalar(select(User).where(User.email == email))
        agent = Agent(owner_id=user.id, slug=f"agent-{uuid.uuid4().hex}", name="Support")
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


def mutation_headers(csrf_token):
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token}


def field_payload(field_key, **overrides):
    payload = {
        "field_key": field_key,
        "description": f"Collect {field_key}",
        "guidance": f"Ask for {field_key}.",
        "options": [],
        "required": False,
        "required_when": [],
    }
    payload.update(overrides)
    return payload


def condition(field_key, operator="eq", operand=None):
    return {"field_key": field_key, "operator": operator, "operand": operand}


def outcome_payload(label, **overrides):
    payload = {
        "label": label,
        "conditions": [],
        "required_fields": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.anyio
async def test_field_crud_roundtrip(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)

    created = await client.post(
        f"/agents/{agent_id}/fields",
        headers=headers,
        json=field_payload(
            "intent",
            description=" Caller intent ",
            guidance="Ask why they called.",
            options=["order", "cancel"],
            required=True,
            required_when=[condition("intent", "eq", "order")],
        ),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["agent_id"] == str(agent_id)
    assert body["field_key"] == "intent"
    assert body["description"] == "Caller intent"
    assert body["options"] == ["order", "cancel"]
    assert body["required"] is True
    assert body["required_when"] == [condition("intent", "eq", "order")]
    assert body["sort_order"] == 0

    listed = await client.get(f"/agents/{agent_id}/fields")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]

    patched = await client.patch(
        f"/agents/{agent_id}/fields/{body['id']}",
        headers=headers,
        json={"description": "Updated intent", "required": False},
    )
    assert patched.status_code == 200
    assert patched.json()["description"] == "Updated intent"
    assert patched.json()["required"] is False
    assert patched.json()["field_key"] == "intent"

    deleted = await client.delete(f"/agents/{agent_id}/fields/{body['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/agents/{agent_id}/fields")).json() == []


@pytest.mark.anyio
async def test_field_create_assigns_next_sort_order_and_reorders(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)

    ids = []
    for key in ("first", "second", "third"):
        response = await client.post(f"/agents/{agent_id}/fields", headers=headers, json=field_payload(key))
        assert response.status_code == 201
        ids.append(response.json()["id"])

    listed = (await client.get(f"/agents/{agent_id}/fields")).json()
    assert [item["sort_order"] for item in listed] == [0, 1, 2]

    reordered = await client.put(
        f"/agents/{agent_id}/fields/order",
        headers=headers,
        json={"field_ids": [ids[2], ids[0], ids[1]]},
    )
    assert reordered.status_code == 200
    assert [item["id"] for item in reordered.json()] == [ids[2], ids[0], ids[1]]
    assert [item["sort_order"] for item in reordered.json()] == [0, 1, 2]

    invalid = await client.put(
        f"/agents/{agent_id}/fields/order",
        headers=headers,
        json={"field_ids": [ids[2], ids[2], ids[1]]},
    )
    assert invalid.status_code == 422


@pytest.mark.anyio
async def test_duplicate_field_key_returns_conflict(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)

    first = await client.post(f"/agents/{agent_id}/fields", headers=headers, json=field_payload("intent"))
    duplicate = await client.post(f"/agents/{agent_id}/fields", headers=headers, json=field_payload("intent"))

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert len((await client.get(f"/agents/{agent_id}/fields")).json()) == 1


@pytest.mark.anyio
async def test_field_validation_rejects_blank_key_unknown_operator_and_extra_fields(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)

    blank = await client.post(f"/agents/{agent_id}/fields", headers=headers, json=field_payload("   "))
    bad_operator = await client.post(
        f"/agents/{agent_id}/fields",
        headers=headers,
        json=field_payload("intent", required_when=[condition("intent", "bogus", "order")]),
    )
    extra = await client.post(
        f"/agents/{agent_id}/fields",
        headers=headers,
        json={**field_payload("intent"), "unknown": True},
    )

    assert blank.status_code == 422
    assert bad_operator.status_code == 422
    assert extra.status_code == 422


@pytest.mark.anyio
async def test_field_routes_require_csrf_and_hide_another_owners_fields(client):
    owner_email = f"{uuid.uuid4()}@example.com"
    owner_csrf = await registered_client(client, owner_email)
    agent_id = await create_agent(owner_email)
    created = await client.post(
        f"/agents/{agent_id}/fields",
        headers=mutation_headers(owner_csrf),
        json=field_payload("intent"),
    )
    field_id = created.json()["id"]

    assert (await client.post(f"/agents/{agent_id}/fields", json=field_payload("other"))).status_code == 403

    other = AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000")
    try:
        other_email = f"{uuid.uuid4()}@example.com"
        other_csrf = await registered_client(other, other_email)
        assert (await other.get(f"/agents/{agent_id}/fields")).status_code == 404
        assert (await other.patch(
            f"/agents/{agent_id}/fields/{field_id}",
            headers=mutation_headers(other_csrf),
            json={"description": "Changed"},
        )).status_code == 404
        assert (await other.delete(
            f"/agents/{agent_id}/fields/{field_id}",
            headers=mutation_headers(other_csrf),
        )).status_code == 404
        assert (await other.put(
            f"/agents/{agent_id}/fields/order",
            headers=mutation_headers(other_csrf),
            json={"field_ids": [field_id]},
        )).status_code == 404
    finally:
        await other.aclose()


@pytest.mark.anyio
async def test_outcome_crud_roundtrip(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)

    created = await client.post(
        f"/agents/{agent_id}/outcomes",
        headers=headers,
        json=outcome_payload(
            "order_placed",
            conditions=[condition("intent", "eq", "order")],
            required_fields=["items", "address"],
        ),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["agent_id"] == str(agent_id)
    assert body["label"] == "order_placed"
    assert body["conditions"] == [condition("intent", "eq", "order")]
    assert body["required_fields"] == ["items", "address"]

    listed = await client.get(f"/agents/{agent_id}/outcomes")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]

    patched = await client.patch(
        f"/agents/{agent_id}/outcomes/{body['id']}",
        headers=headers,
        json={"required_fields": ["items"]},
    )
    assert patched.status_code == 200
    assert patched.json()["required_fields"] == ["items"]
    assert patched.json()["label"] == "order_placed"

    deleted = await client.delete(f"/agents/{agent_id}/outcomes/{body['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/agents/{agent_id}/outcomes")).json() == []


@pytest.mark.anyio
async def test_duplicate_outcome_label_returns_conflict(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)

    first = await client.post(f"/agents/{agent_id}/outcomes", headers=headers, json=outcome_payload("done"))
    duplicate = await client.post(f"/agents/{agent_id}/outcomes", headers=headers, json=outcome_payload("done"))

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert len((await client.get(f"/agents/{agent_id}/outcomes")).json()) == 1


@pytest.mark.anyio
async def test_outcome_validation_rejects_blank_label_and_unknown_operator(client):
    email = f"{uuid.uuid4()}@example.com"
    csrf_token = await registered_client(client, email)
    agent_id = await create_agent(email)
    headers = mutation_headers(csrf_token)

    blank = await client.post(f"/agents/{agent_id}/outcomes", headers=headers, json=outcome_payload("   "))
    bad_operator = await client.post(
        f"/agents/{agent_id}/outcomes",
        headers=headers,
        json=outcome_payload("done", conditions=[condition("intent", "bogus", "order")]),
    )

    assert blank.status_code == 422
    assert bad_operator.status_code == 422


@pytest.mark.anyio
async def test_frozen_snapshot_reads_fields_and_outcomes(client):
    email = f"{uuid.uuid4()}@example.com"
    await registered_client(client, email)
    agent_id = await create_agent(email)

    async with Session() as db:
        agent = await db.get(Agent, agent_id)
        assert agent is not None
        second = AgentField(
            agent_id=agent.id,
            field_key="items",
            description="Order items",
            guidance="Ask what they want.",
            options=["pizza", "salad"],
            required=False,
            required_when=[condition("intent", "eq", "order")],
            sort_order=1,
        )
        first = AgentField(
            agent_id=agent.id,
            field_key="intent",
            description="Caller intent",
            guidance="Ask why they called.",
            options=["order", "cancel"],
            required=True,
            required_when=[],
            sort_order=0,
        )
        outcome = AgentOutcome(
            agent_id=agent.id,
            label="order_placed",
            conditions=[condition("intent", "eq", "order")],
            required_fields=["items"],
        )
        db.add_all([second, first, outcome])
        await db.commit()

        snapshot = await _frozen_call_snapshot(db, agent, "en")

    assert [item["field_key"] for item in snapshot["fields"]] == ["intent", "items"]
    assert snapshot["fields"][0] == {
        "id": str(first.id),
        "field_key": "intent",
        "description": "Caller intent",
        "guidance": "Ask why they called.",
        "options": ["order", "cancel"],
        "required": True,
        "required_when": [],
        "sort_order": 0,
    }
    assert snapshot["fields"][1]["required_when"] == [condition("intent", "eq", "order")]
    assert snapshot["outcomes"] == [
        {
            "id": str(outcome.id),
            "label": "order_placed",
            "conditions": [condition("intent", "eq", "order")],
            "required_fields": ["items"],
        }
    ]
