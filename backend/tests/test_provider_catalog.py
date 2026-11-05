import json
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

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


async def login_as_owner(client: AsyncClient) -> None:
    email = f"{uuid.uuid4()}@example.com"
    registered = await client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    logged_in = await client.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert logged_in.status_code == 200


@pytest.mark.anyio
async def test_provider_catalog_requires_an_authenticated_owner(client):
    response = await client.get("/provider-catalog")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_provider_catalog_exposes_only_verified_available_choices(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-never-return")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini, o4-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-secret-never-return")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv(
        "VERIFIED_TTS_VOICE_CATALOG",
        json.dumps(
            {
                "cartesia": {
                    "sonic-3.6": {"voice-en": ["en"], "voice-multi": ["en", "hi"]},
                    "unapproved-model": {"unapproved-voice": ["en"]},
                },
                "elevenlabs": {"eleven_flash_v2_5": {"voice-hi": ["hi"]}},
                "unapproved-provider": {"unapproved-model": {"private-voice": ["en"]}},
            }
        ),
    )
    await login_as_owner(client)

    response = await client.get("/provider-catalog")
    assert response.status_code == 200
    assert response.json() == {
        "openai": {
            "available": True,
            "error": None,
            "models": [
                {"id": "gpt-4o-mini", "available": True},
                {"id": "o4-mini", "available": True},
            ],
        },
        "tts_providers": [
            {
                "provider": "cartesia",
                "available": True,
                "error": None,
                "models": [
                    {
                        "id": "sonic-3.6",
                        "available": True,
                        "voices": [
                            {"id": "voice-en", "languages": ["en"], "available": True},
                            {"id": "voice-multi", "languages": ["en", "hi"], "available": True},
                        ],
                    }
                ],
            },
            {
                "provider": "elevenlabs",
                "available": False,
                "error": "ELEVENLABS_API_KEY is not configured on the server.",
                "models": [
                    {
                        "id": "eleven_flash_v2_5",
                        "available": False,
                        "voices": [{"id": "voice-hi", "languages": ["hi"], "available": False}],
                    }
                ],
            },
        ],
    }
    body = response.text
    assert "openai-secret-never-return" not in body
    assert "cartesia-secret-never-return" not in body
    assert "unapproved-provider" not in body
    assert "unapproved-model" not in body
    assert "private-voice" not in body


@pytest.mark.anyio
async def test_provider_catalog_marks_choices_unavailable_when_matching_keys_are_missing(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini")
    monkeypatch.setenv(
        "VERIFIED_TTS_VOICE_CATALOG",
        json.dumps(
            {
                "cartesia": {"sonic-3.6": {"voice-en": ["en"]}},
                "elevenlabs": {"eleven_flash_v2_5": {"voice-hi": ["hi"]}},
            }
        ),
    )
    await login_as_owner(client)

    response = await client.get("/provider-catalog")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["openai"] == {
        "available": False,
        "error": "OPENAI_API_KEY is not configured on the server.",
        "models": [{"id": "gpt-4o-mini", "available": False}],
    }
    assert all(not provider["available"] and provider["error"] for provider in catalog["tts_providers"])
    assert all(
        not model["available"] and all(not voice["available"] for voice in model["voices"])
        for provider in catalog["tts_providers"]
        for model in provider["models"]
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "catalog_value",
    ['{"cartesia":', '{"cartesia":{"sonic-3.6":{"voice-en":"en"}}}'],
)
async def test_provider_catalog_fails_closed_for_malformed_allowlists(client, monkeypatch, catalog_value):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-only")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini,,o4-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-test-only")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "elevenlabs-test-only")
    monkeypatch.setenv("VERIFIED_TTS_VOICE_CATALOG", catalog_value)
    await login_as_owner(client)

    response = await client.get("/provider-catalog")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["openai"] == {
        "available": False,
        "error": "VERIFIED_OPENAI_MODELS is invalid on the server.",
        "models": [],
    }
    assert all(
        not provider["available"]
        and provider["error"] == "The server's verified TTS voice catalog is invalid."
        and provider["models"] == []
        for provider in catalog["tts_providers"]
    )
