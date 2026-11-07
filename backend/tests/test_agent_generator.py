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
