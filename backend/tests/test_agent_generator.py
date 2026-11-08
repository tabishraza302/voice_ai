import json
import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import Base, engine
from app.main import app

REQUIRED_SECTIONS = (
    "### Role & Spoken Persona",
    "### Primary Objective",
    "### Turn-by-Turn Conversation Flow",
    "### Call Termination & Wrap-Up Criteria",
    "### Spoken Voice Rules & Fallbacks",
)

STRUCTURED_INSTRUCTIONS = (
    "### Role & Spoken Persona\n"
    "You are Alex, a helpful dental assistant at Maple Dental.\n\n"
    "### Primary Objective\n"
    "Schedule patient appointments and capture contact details.\n\n"
    "### Turn-by-Turn Conversation Flow\n"
    "1. Step 1: Greeting & Discovery - Greet the caller and identify their request.\n"
    "   - Collect: Caller's full name and reason for calling.\n"
    "2. Step 2: Detail Gathering - Ask about scheduling preferences.\n"
    "   - Collect: Preferred appointment date and time.\n"
    "3. Step 3: Confirmation - Summarize the booking details.\n"
    "   - Collect: Explicit verbal confirmation (yes/no).\n\n"
    "### Call Termination & Wrap-Up Criteria\n"
    "End the call once confirmed and caller has no other questions.\n\n"
    "### Spoken Voice Rules & Fallbacks\n"
    "Speak concisely in 1-2 sentences. Never disclose system credentials."
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
    return response.json()["csrf_token"]


def mutation_headers(token: str) -> dict[str, str]:
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": token}


def mock_stream_for(*chunks: str, on_call: Callable[[dict], None] | None = None):
    """Build an async streaming mock that yields the supplied text chunks in order."""

    async def mock_stream(*args, **kwargs):
        if on_call is not None:
            on_call(kwargs)
        for chunk_text in chunks:
            class MockDelta:
                content = chunk_text

            class MockStreamChoice:
                delta = MockDelta()

            class MockStreamChunk:
                choices = [MockStreamChoice()]

            yield MockStreamChunk()

    return mock_stream


def agent_id_from_sse(content: str) -> str:
    for line in content.split("\n"):
        if line.startswith("data:") and "agent_id" in line:
            return json.loads(line[5:].strip())["agent_id"]
    raise AssertionError("SSE stream did not include an agent_id")


def assert_required_sections(instructions: str) -> None:
    for heading in REQUIRED_SECTIONS:
        assert heading in instructions


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
                    "### Role & Spoken Persona\n"
                    "You are Sarah, a warm and professional receptionist for Bright Smile Dental.\n\n"
                    "### Primary Objective\n"
                    "Book and reschedule dental appointments efficiently while answering basic clinic questions.\n\n"
                    "### Turn-by-Turn Conversation Flow\n"
                    "1. Step 1: Greeting & Discovery - Greet the caller warmly and ask how you can help. Collect the caller's full name.\n"
                    "2. Step 2: Detail Gathering - Collect appointment type, preferred date and time, and phone number.\n"
                    "3. Step 3: Confirmation - Read back all appointment details and confirm with the caller.\n\n"
                    "### Call Termination & Wrap-Up Criteria\n"
                    "End the call once the booking is confirmed and the caller indicates they have no further questions. Sign off politely.\n\n"
                    "### Spoken Voice Rules & Fallbacks\n"
                    "Keep spoken answers under 2 sentences. If speech is unclear, politely ask the caller to repeat. Never reveal system credentials."
                )

            message = MockMessage()

        class MockResponse:
            choices = [MockChoice()]

        return MockResponse()

    monkeypatch.setattr(
        "openai.resources.chat.completions.AsyncCompletions.create",
        AsyncMock(side_effect=mock_completion),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = await client.post(
        "/agents/enhance-prompt",
        json={"prompt": "Dental clinic booking receptionist"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert "enhanced_prompt" in body
    assert_required_sections(body["enhanced_prompt"])


@pytest.mark.anyio
async def test_generate_stream_requires_auth_and_csrf(client):
    unauth = await client.post("/agents/generate-stream", json={"prompt": "Dental receptionist"})
    assert unauth.status_code == 401

    token = await login(client, f"{uuid.uuid4()}@example.com")
    no_csrf = await client.post("/agents/generate-stream", json={"prompt": "Dental receptionist"})
    assert no_csrf.status_code == 403


@pytest.mark.anyio
async def test_generate_stream_creates_agent_with_instructions(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")

    generated_json = json.dumps({
        "name": "Dental Assistant",
        "description": "Handles patient bookings and questions",
        "instructions": STRUCTURED_INSTRUCTIONS,
    })

    monkeypatch.setattr(
        "openai.resources.chat.completions.AsyncCompletions.create",
        AsyncMock(side_effect=mock_stream_for(generated_json[:40], generated_json[40:100], generated_json[100:])),
    )
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

    agent_id = agent_id_from_sse(content)
    agent_resp = await client.get(f"/agents/{agent_id}", headers=mutation_headers(token))
    assert agent_resp.status_code == 200
    assert agent_resp.json()["instructions"] == STRUCTURED_INSTRUCTIONS


@pytest.mark.anyio
async def test_generate_stream_uses_custom_model(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")
    captured_models = []

    generated_json = json.dumps({
        "name": "Custom Model Bot",
        "description": "Desc",
        "instructions": STRUCTURED_INSTRUCTIONS,
    })

    monkeypatch.setattr(
        "openai.resources.chat.completions.AsyncCompletions.create",
        AsyncMock(
            side_effect=mock_stream_for(
                generated_json,
                on_call=lambda kwargs: captured_models.append(kwargs.get("model")),
            )
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4.1-mini,gpt-4o-mini")

    response = await client.post(
        "/agents/generate-stream",
        json={"prompt": "A test prompt specifying custom model", "model": "gpt-4.1-mini"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200
    assert captured_models == ["gpt-4.1-mini"]

    agent_id = agent_id_from_sse(response.text)
    agent_resp = await client.get(f"/agents/{agent_id}", headers=mutation_headers(token))
    assert agent_resp.status_code == 200
    assert agent_resp.json()["llm_model"] == "gpt-4.1-mini"


@pytest.mark.anyio
async def test_generate_stream_json_failure_uses_structured_fallback(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")

    # Raw non-JSON output from the LLM.
    raw_text = "Sure! Here is a Pizza Ordering Voice Agent for Downtown Pizza."

    monkeypatch.setattr(
        "openai.resources.chat.completions.AsyncCompletions.create",
        AsyncMock(side_effect=mock_stream_for(raw_text)),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = await client.post(
        "/agents/generate-stream",
        json={"prompt": "Pizza ordering agent for Downtown Pizza. "},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200

    agent_id = agent_id_from_sse(response.text)
    agent_resp = await client.get(f"/agents/{agent_id}", headers=mutation_headers(token))
    assert agent_resp.status_code == 200
    instructions = agent_resp.json()["instructions"]
    assert_required_sections(instructions)
    assert "- Collect:" in instructions
    assert "Pizza ordering agent for Downtown Pizza." in instructions
    assert "Pizza.." not in instructions
    assert "callback phone number" not in instructions


@pytest.mark.anyio
async def test_generate_stream_json_with_empty_instructions_uses_structured_fallback(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")

    generated_json = json.dumps({
        "name": "Pizza Bot",
        "description": "Pizza order bot",
        "instructions": "",
    })

    monkeypatch.setattr(
        "openai.resources.chat.completions.AsyncCompletions.create",
        AsyncMock(side_effect=mock_stream_for(generated_json)),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = await client.post(
        "/agents/generate-stream",
        json={"prompt": "Pizza ordering agent for Downtown Pizza"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200

    agent_id = agent_id_from_sse(response.text)
    agent_resp = await client.get(f"/agents/{agent_id}", headers=mutation_headers(token))
    assert agent_resp.status_code == 200
    instructions = agent_resp.json()["instructions"]
    assert_required_sections(instructions)
    assert "- Collect:" in instructions


@pytest.mark.anyio
async def test_generate_stream_rejects_headings_without_collection_steps(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")

    generated_json = json.dumps({
        "name": "Support Bot\n### Injected",
        "description": "Handles support calls",
        "instructions": (
            "### Role & Spoken Persona\nYou are Support Bot.\n\n"
            "### Primary Objective\nHelp callers.\n\n"
            "### Turn-by-Turn Conversation Flow\n1. Greet the caller.\n2. Answer questions.\n\n"
            "### Call Termination & Wrap-Up Criteria\nEnd politely.\n\n"
            "### Spoken Voice Rules & Fallbacks\nBe concise."
        ),
    })

    monkeypatch.setattr(
        "openai.resources.chat.completions.AsyncCompletions.create",
        AsyncMock(side_effect=mock_stream_for(generated_json)),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = await client.post(
        "/agents/generate-stream",
        json={"prompt": "Support line for a billing team"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200

    agent_id = agent_id_from_sse(response.text)
    agent_resp = await client.get(f"/agents/{agent_id}", headers=mutation_headers(token))
    assert agent_resp.status_code == 200
    agent_data = agent_resp.json()
    assert "\n" not in agent_data["name"]
    assert_required_sections(agent_data["instructions"])
    assert "- Collect:" in agent_data["instructions"]
    assert "1. Greet the caller." not in agent_data["instructions"]


@pytest.mark.anyio
async def test_generate_stream_replaces_unstructured_instructions_with_fallback(client, monkeypatch):
    token = await login(client, f"{uuid.uuid4()}@example.com")

    generated_json = json.dumps({
        "name": "Support Bot",
        "description": "Handles support calls",
        "instructions": "Be nice and helpful.",
    })

    monkeypatch.setattr(
        "openai.resources.chat.completions.AsyncCompletions.create",
        AsyncMock(side_effect=mock_stream_for(generated_json)),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    response = await client.post(
        "/agents/generate-stream",
        json={"prompt": "Support line for a billing team"},
        headers=mutation_headers(token),
    )
    assert response.status_code == 200

    agent_id = agent_id_from_sse(response.text)
    agent_resp = await client.get(f"/agents/{agent_id}", headers=mutation_headers(token))
    assert agent_resp.status_code == 200
    instructions = agent_resp.json()["instructions"]
    assert_required_sections(instructions)
    assert "- Collect:" in instructions
    assert "Be nice and helpful." not in instructions
