import os
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock

from app.db import Base, engine
from app.main import app


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
    return response.json()["csrf_token"]


def mutation_headers(token: str) -> dict[str, str]:
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": token}


@pytest.mark.anyio
async def test_enhance_prompt_requires_auth_and_csrf(client):
    unauth = await client.post("/agents/enhance-prompt", json={"prompt": "Dental receptionist"})
    assert unauth.status_code == 401

    token = await login(client, f"{uuid.uuid4()}@example.com")
    no_csrf = await client.post("/agents/enhance-prompt", json={"prompt": "Dental receptionist"})
    assert no_csrf.status_code == 403


@pytest.mark.anyio
async def test_enhance_prompt_rejects_empty_prompt(client):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    response = await client.post(
        "/agents/enhance-prompt",
        json={"prompt": "   "},
        headers=mutation_headers(token),
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_enhance_prompt_success(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")

    async def mock_completion(*args, **kwargs):
        class MockChoice:
            class MockMessage:
                content = (
                    "### Role & Persona\nDental clinic receptionist.\n\n"
                    "### Primary Objective\nBook dental appointments.\n\n"
                    "### Information to Collect\nName, phone, date.\n\n"
                    "### Tone & Style Guidelines\nPolite and warm.\n\n"
                    "### Edge Cases & Fallbacks\nEmergencies refer to 911."
                )
            message = MockMessage()
        class MockResponse:
            choices = [MockChoice()]
        return MockResponse()

    monkeypatch.setattr("openai.resources.chat.completions.AsyncCompletions.create", AsyncMock(side_effect=mock_completion))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = await client.post(
        "/agents/enhance-prompt",
        json={"prompt": "Dental clinic booking receptionist"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert "enhanced_prompt" in body
    assert "Role & Persona" in body["enhanced_prompt"]


@pytest.mark.anyio
async def test_generate_stream_requires_auth_and_csrf(client):
    unauth = await client.post("/agents/generate-stream", json={"prompt": "Dental receptionist"})
    assert unauth.status_code == 401

    token = await login(client, f"{uuid.uuid4()}@example.com")
    no_csrf = await client.post("/agents/generate-stream", json={"prompt": "Dental receptionist"})
    assert no_csrf.status_code == 403


@pytest.mark.anyio
async def test_generate_stream_creates_agent_and_steps(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")

    generated_json = (
        '{"name": "Dental Assistant", '
        '"description": "Handles patient bookings and questions", '
        '"instructions": "Be polite, collect patient name and appointment time.", '
        '"steps": ['
        '{"field_key": "patient_name", "title": "Greet & Name", "objective": "Collect name", "branches": [{"target_step_index": 1}]}, '
        '{"field_key": "appointment_date", "title": "Schedule Date", "objective": "Collect desired date", "branches": [{"target_step_index": 99}]}, '
        '{"field_key": "confirm", "title": "Confirmation", "objective": "Confirm booking", "branches": []}'
        ']}'
    )

    async def mock_stream(*args, **kwargs):
        for chunk_text in [generated_json[:40], generated_json[40:100], generated_json[100:]]:
            class MockDelta:
                content = chunk_text
            class MockStreamChoice:
                delta = MockDelta()
            class MockStreamChunk:
                choices = [MockStreamChoice()]
            yield MockStreamChunk()

    monkeypatch.setattr("openai.resources.chat.completions.AsyncCompletions.create", AsyncMock(side_effect=mock_stream))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = await client.post(
        "/agents/generate-stream",
        json={"prompt": "A dental clinic appointment booking assistant"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    content = response.text
    assert "event: milestone" in content
    assert "event: complete" in content
    assert "Dental Assistant" in content


@pytest.mark.anyio
async def test_generate_stream_drops_invalid_branches_and_passes_publish_validation(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")

    # Includes out-of-range index (99), self-target (0), and both target + outcome
    generated_json = (
        '{"name": "Booking Bot", '
        '"description": "Validates branches", '
        '"instructions": "Instructions text", '
        '"steps": ['
        '{"field_key": "step_one", "title": "First", "objective": "Obj 1", "branches": [{"target_step_index": 0, "outcome": "loop"}]}, '
        '{"field_key": "step_two", "title": "Second", "objective": "Obj 2", "branches": [{"target_step_index": 99}]}, '
        '{"field_key": "step_three", "title": "Third", "objective": "Obj 3", "branches": [{"target_step_index": 2, "outcome": "finish"}]}'
        ']}'
    )

    async def mock_stream(*args, **kwargs):
        class MockDelta:
            content = generated_json
        class MockStreamChoice:
            delta = MockDelta()
        class MockStreamChunk:
            choices = [MockStreamChoice()]
        yield MockStreamChunk()

    monkeypatch.setattr("openai.resources.chat.completions.AsyncCompletions.create", AsyncMock(side_effect=mock_stream))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = await client.post(
        "/agents/generate-stream",
        json={"prompt": "A test prompt for validating branches"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200
    import json
    agent_id = None
    for line in response.text.split("\n"):
        if line.startswith("data:") and "agent_id" in line:
            agent_id = json.loads(line[5:].strip())["agent_id"]
    assert agent_id is not None

    # Verify fields can be fetched without error
    fields_resp = await client.get(f"/agents/{agent_id}/fields", headers=mutation_headers(token))
    assert fields_resp.status_code == 200
    fields = fields_resp.json()
    assert len(fields) == 3

@pytest.mark.anyio
async def test_generate_stream_uses_custom_model(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    captured_models = []

    generated_json = '{"name": "Custom Model Bot", "description": "Desc", "instructions": "Inst", "steps": []}'

    async def mock_stream(*args, **kwargs):
        captured_models.append(kwargs.get("model"))
        class MockDelta:
            content = generated_json
        class MockStreamChoice:
            delta = MockDelta()
        class MockStreamChunk:
            choices = [MockStreamChoice()]
        yield MockStreamChunk()

    monkeypatch.setattr("openai.resources.chat.completions.AsyncCompletions.create", AsyncMock(side_effect=mock_stream))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4.1-mini,gpt-4o-mini")

    response = await client.post(
        "/agents/generate-stream",
        json={"prompt": "A test prompt specifying custom model", "model": "gpt-4.1-mini"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200
    assert captured_models == ["gpt-4.1-mini"]

    import json
    agent_id = None
    for line in response.text.split("\n"):
        if line.startswith("data:") and "agent_id" in line:
            agent_id = json.loads(line[5:].strip())["agent_id"]
    assert agent_id is not None

    agent_resp = await client.get(f"/agents/{agent_id}", headers=mutation_headers(token))
    assert agent_resp.status_code == 200
    assert agent_resp.json()["llm_model"] == "gpt-4.1-mini"


@pytest.mark.anyio
async def test_generate_stream_clears_cyclic_branches(client, monkeypatch):
    """Backward conditional routes can form cycles; the sanitizer must clear
    the offending branches even though validate_publish returns UUID-keyed
    error paths (steps.<uuid>.branches), not index-keyed ones."""
    import json

    token = await login(client, f"{uuid.uuid4()}@example.com")

    # Step 0 branches forward to 1 and 2 (valid).
    # Step 2 branches backward to 1 (creates cycle with implicit next-step edges).
    generated_json = json.dumps({
        "name": "Support Router",
        "description": "Routes callers",
        "instructions": "Route to billing or tech support.",
        "steps": [
            {"field_key": "kind", "title": "Identify Type", "objective": "Ask type", "branches": [
                {"target_step_index": 1, "conditions": [{"field_key": "kind", "operator": "eq", "operand": "billing"}]},
                {"target_step_index": 2, "conditions": [{"field_key": "kind", "operator": "eq", "operand": "tech"}]},
            ]},
            {"field_key": "billing", "title": "Handle Billing", "objective": "Handle invoice", "branches": []},
            {"field_key": "confirm", "title": "Confirm Resolution", "objective": "Confirm", "branches": [
                {"target_step_index": 1, "conditions": [{"field_key": "confirm", "operator": "eq", "operand": "no"}]},
            ]},
        ],
    })

    async def mock_stream(*args, **kwargs):
        class MockDelta:
            content = generated_json
        class MockStreamChoice:
            delta = MockDelta()
        class MockStreamChunk:
            choices = [MockStreamChoice()]
        yield MockStreamChunk()

    monkeypatch.setattr("openai.resources.chat.completions.AsyncCompletions.create", AsyncMock(side_effect=mock_stream))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = await client.post(
        "/agents/generate-stream",
        json={"prompt": "Route callers to the right support department"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200
    agent_id = None
    for line in response.text.split("\n"):
        if line.startswith("data:") and "agent_id" in line:
            agent_id = json.loads(line[5:].strip())["agent_id"]
    assert agent_id is not None

    # Fetch the generated fields
    fields_resp = await client.get(f"/agents/{agent_id}/fields", headers=mutation_headers(token))
    assert fields_resp.status_code == 200
    fields = fields_resp.json()
    assert len(fields) == 3
    assert fields[0]["field_key"] == "kind"
    assert fields[1]["field_key"] == "billing"
    assert fields[2]["field_key"] == "confirm"
