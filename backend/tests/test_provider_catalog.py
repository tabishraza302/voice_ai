import json
import os
import uuid
import pytest
import redis
from httpx import ASGITransport, AsyncClient
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.db import Base, engine
from app.main import app
from app.provider_catalog import TTSVoiceRead, _IN_MEMORY_CACHE


@pytest.fixture
async def client():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clear_catalog_cache():
    _IN_MEMORY_CACHE.clear()
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            r = redis.from_url(redis_url, socket_timeout=1.0)
            r.delete("tts:voices:cartesia", "tts:voices:elevenlabs")
            r.close()
        except Exception:
            pass
    yield
    _IN_MEMORY_CACHE.clear()
    if redis_url:
        try:
            r = redis.from_url(redis_url, socket_timeout=1.0)
            r.delete("tts:voices:cartesia", "tts:voices:elevenlabs")
            r.close()
        except Exception:
            pass

async def login_as_owner(client: AsyncClient) -> None:
    email = f"{uuid.uuid4()}@example.com"
    registered = await client.post(
        "/auth/register",
        json={"email": email, "password": "correct-horse-battery-staple"},
    )
    assert registered.status_code == 201
    logged_in = await client.post(
        "/auth/login",
        json={"email": email, "password": "correct-horse-battery-staple"},
    )
    assert logged_in.status_code == 200


@pytest.mark.anyio
async def test_provider_catalog_requires_an_authenticated_owner(client):
    response = await client.get("/provider-catalog")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_provider_catalog_exposes_available_choices(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-never-return")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini, o4-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-secret-never-return")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        return [
            TTSVoiceRead(id="voice-en", name="English Voice", languages=["en"], available=True),
            TTSVoiceRead(id="voice-multi", name="Multi Voice", languages=["en", "hi"], available=True),
        ]

    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", mock_cartesia)
    await login_as_owner(client)

    response = await client.get("/provider-catalog")
    assert response.status_code == 200
    data = response.json()
    assert data["openai"] == {
        "available": True,
        "error": None,
        "models": [
            {"id": "gpt-4o-mini", "available": True},
            {"id": "o4-mini", "available": True},
        ],
    }
    assert len(data["tts_providers"]) == 2
    cartesia = next(p for p in data["tts_providers"] if p["provider"] == "cartesia")
    assert cartesia["available"] is True
    assert cartesia["error"] is None
    assert cartesia["models"][0]["id"] == "sonic-3.6"
    assert cartesia["models"][0]["voices"] == [
        {"id": "voice-en", "name": "English Voice", "languages": ["en"], "available": True, "sample_url": None},
        {"id": "voice-multi", "name": "Multi Voice", "languages": ["en", "hi"], "available": True, "sample_url": None},
    ]

    elevenlabs = next(p for p in data["tts_providers"] if p["provider"] == "elevenlabs")
    assert elevenlabs["available"] is False
    assert elevenlabs["error"] == "ELEVENLABS_API_KEY is not configured on the server."

    body = response.text
    assert "openai-secret-never-return" not in body
    assert "cartesia-secret-never-return" not in body


@pytest.mark.anyio
async def test_provider_catalog_marks_choices_unavailable_when_matching_keys_are_missing(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini")
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
    assert all(not provider["models"] for provider in catalog["tts_providers"])


@pytest.mark.anyio
async def test_provider_catalog_caches_fetched_voices(client, monkeypatch):
    calls = 0

    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        nonlocal calls
        calls += 1
        return [TTSVoiceRead(id="cached-voice", name="Cached", languages=["en"], available=True)]

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", mock_cartesia)

    await login_as_owner(client)

    r1 = await client.get("/provider-catalog")
    assert r1.status_code == 200
    assert calls == 1

    r2 = await client.get("/provider-catalog")
    assert r2.status_code == 200
    assert calls == 1  # Returned from cache, didn't call API again


@pytest.mark.anyio
async def test_provider_catalog_handles_fetch_error_gracefully(client, monkeypatch):
    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        raise RuntimeError("Network timeout connecting to Cartesia")

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", mock_cartesia)

    await login_as_owner(client)

    response = await client.get("/provider-catalog")
    assert response.status_code == 200
    data = response.json()
    cartesia = next(p for p in data["tts_providers"] if p["provider"] == "cartesia")
    assert cartesia["available"] is False
    assert "Network timeout" in cartesia["error"]
