import asyncio
import json
import os
import time
import uuid
from unittest.mock import AsyncMock

import pytest
import redis
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.db import Base, engine
from app.main import app
from app.provider_catalog import TTSVoiceRead, _IN_MEMORY_CACHE
from app.redis import open_redis_resources, RedisResources


@pytest.fixture
async def client(redis_resources):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app.state.redis = redis_resources
    app.state.redis_namespace = redis_resources.namespace
    async with app.router.lifespan_context(app):
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


@pytest.mark.anyio
async def test_provider_catalog_concurrent_requests_share_single_fetch(client, monkeypatch):
    calls = 0
    event = asyncio.Event()

    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        nonlocal calls
        calls += 1
        await event.wait()
        return [TTSVoiceRead(id="concurrent-voice", name="Concurrent", languages=["en"], available=True)]

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", mock_cartesia)

    await login_as_owner(client)

    async def make_req():
        return await client.get("/provider-catalog")

    tasks = [asyncio.create_task(make_req()) for _ in range(20)]
    await asyncio.sleep(0.1)
    event.set()

    responses = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in responses)
    assert calls == 1
    for r in responses:
        cartesia = next(p for p in r.json()["tts_providers"] if p["provider"] == "cartesia")
        assert cartesia["available"] is True
        assert cartesia["models"][0]["voices"][0]["id"] == "concurrent-voice"


@pytest.mark.anyio
async def test_provider_catalog_empty_voice_list_is_valid_cache_hit(client, monkeypatch):
    calls = 0

    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        nonlocal calls
        calls += 1
        return []

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
    assert calls == 1


@pytest.mark.anyio
async def test_redis_resources_lifecycle_and_pool_bounds():
    url = "redis://127.0.0.1:6380/0"
    namespace = "test-pools"
    resources = await open_redis_resources(url, namespace)
    assert resources.namespace == "test-pools"
    assert resources.key("test") == "test-pools:test"
    assert resources.commands.connection_pool.max_connections == 32
    assert resources.control.connection_pool.max_connections == 8
    assert resources.blocking.connection_pool.max_connections == 32
    await resources.close()


@pytest.mark.anyio
async def test_provider_catalog_fallback_on_redis_outage(client, monkeypatch):
    async def mock_failing_get(key):
        raise ConnectionError("Simulated Redis outage")

    app_redis = getattr(app.state, "redis", None)
    if app_redis is not None:
        monkeypatch.setattr(app_redis.commands, "get", mock_failing_get)

    calls = 0

    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        nonlocal calls
        calls += 1
        return [TTSVoiceRead(id="outage-voice", name="Outage Voice", languages=["en"], available=True)]

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", mock_cartesia)

    await login_as_owner(client)

    response = await client.get("/provider-catalog")
    assert response.status_code == 200
    cartesia = next(p for p in response.json()["tts_providers"] if p["provider"] == "cartesia")
    assert cartesia["available"] is True
    assert cartesia["models"][0]["voices"][0]["id"] == "outage-voice"
    assert calls == 1


@pytest.mark.anyio
async def test_app_startup_prefers_redis_url_over_test_redis_url(monkeypatch):
    """Real reachable Redis URL vs deliberately unreachable test URL proves app startup selection."""
    from app.provider_catalog import _IN_MEMORY_CACHE
    _IN_MEMORY_CACHE.clear()

    test_namespace = f"voice-ai-pref-{uuid.uuid4().hex[:8]}"
    real_redis_url = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6380/0")
    unreachable_test_url = "redis://127.0.0.1:63999/0"

    monkeypatch.setenv("REDIS_URL", real_redis_url)
    monkeypatch.setenv("TEST_REDIS_URL", unreachable_test_url)
    monkeypatch.setenv("REDIS_NAMESPACE", test_namespace)

    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        return [TTSVoiceRead(id="pref-voice", name="Pref Voice", languages=["en"], available=True)]

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("VERIFIED_OPENAI_MODELS", "gpt-4o-mini")
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", mock_cartesia)
    monkeypatch.setattr("app.sessions.recover_interrupted_sessions", AsyncMock())

    from app.main import app
    from app.provider_catalog import _build_provider_catalog

    # Reset any existing app.state.redis and redis_namespace so lifespan opens from environment
    app.state.redis = None
    if hasattr(app.state, "redis_namespace"):
        delattr(app.state, "redis_namespace")

    async with app.router.lifespan_context(app):
        assert app.state.redis is not None
        # Consumer behavior: build catalog using app's open Redis resources
        catalog = await _build_provider_catalog(os.environ, resources=app.state.redis)
        cartesia = next(p for p in catalog.tts_providers if p.provider == "cartesia")
        assert cartesia.available is True
        assert cartesia.models[0].voices[0].id == "pref-voice"

        # Observe actual cached result on the reachable runtime Redis under the configured namespace
        raw_cached = await app.state.redis.commands.get(app.state.redis.key("tts:voices:cartesia"))
        assert raw_cached is not None
        assert "pref-voice" in raw_cached

        # Cleanup test namespace key
        await app.state.redis.commands.delete(app.state.redis.key("tts:voices:cartesia"))


@pytest.mark.anyio
async def test_provider_catalog_redis_outage_immediate_fallback(monkeypatch):
    """Known Redis outage must bypass waiter polling completely and fallback to provider."""
    from app.provider_catalog import _tts_provider_catalog, _IN_MEMORY_CACHE

    _IN_MEMORY_CACHE.clear()

    # Fail immediately if waiter polling sleep is entered
    async def fail_if_slept(duration):
        pytest.fail(f"Outage path must immediately bypass waiter poll, but asyncio.sleep({duration}) was called")

    monkeypatch.setattr("asyncio.sleep", fail_if_slept)

    mock_resources = AsyncMock()
    mock_resources.key = lambda s: f"voice-ai:{s}"
    mock_resources.commands.get = AsyncMock(side_effect=ConnectionError("Redis down"))
    mock_resources.commands.set = AsyncMock(side_effect=ConnectionError("Redis down"))

    calls = 0

    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        nonlocal calls
        calls += 1
        return [TTSVoiceRead(id="fast-voice", name="Fast Voice", languages=["en"], available=True)]

    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", mock_cartesia)

    result = await _tts_provider_catalog(
        env={"CARTESIA_API_KEY": "test-key"},
        provider="cartesia",
        model_id="sonic-english",
        resources=mock_resources,
    )

    assert result.available is True
    assert result.models[0].voices[0].id == "fast-voice"
    assert calls == 1


@pytest.mark.anyio
async def test_provider_catalog_redis_outage_returns_unexpired_memory_cache(monkeypatch):
    """Known Redis outage must return unexpired memory cache without polling or calling provider."""
    from app.provider_catalog import _tts_provider_catalog, _IN_MEMORY_CACHE

    _IN_MEMORY_CACHE["cartesia"] = (
        time.monotonic() + 300,
        [{"id": "mem-voice", "name": "Memory Voice", "languages": ["en"], "available": True}],
    )

    async def fail_if_slept(duration):
        pytest.fail(f"Memory cache fallback must not sleep, but asyncio.sleep({duration}) was called")

    monkeypatch.setattr("asyncio.sleep", fail_if_slept)

    mock_resources = AsyncMock()
    mock_resources.key = lambda s: f"voice-ai:{s}"
    mock_resources.commands.get = AsyncMock(side_effect=ConnectionError("Redis down"))
    mock_resources.commands.set = AsyncMock(side_effect=ConnectionError("Redis down"))

    async def fail_if_fetched(key: str) -> list[TTSVoiceRead]:
        pytest.fail("Provider fetch should not be called when unexpired memory cache exists")

    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", fail_if_fetched)

    result = await _tts_provider_catalog(
        env={"CARTESIA_API_KEY": "test-key"},
        provider="cartesia",
        model_id="sonic-english",
        resources=mock_resources,
    )

    assert result.available is True
    assert result.models[0].voices[0].id == "mem-voice"


@pytest.mark.anyio
async def test_provider_catalog_waiter_bounds_budget_and_discards_late_get(monkeypatch):
    """Waiter cancels a blocked GET exceeding deadline; direct provider fallback is used and late GET is discarded."""
    from app.provider_catalog import _tts_provider_catalog, _IN_MEMORY_CACHE

    _IN_MEMORY_CACHE.clear()

    # Initial check before lock attempt returns None (cache miss).
    # In the waiter loop, the GET blocks for 5.0s, which is well beyond the 3.0s total waiter budget.
    get_count = 0

    async def blocking_late_get(key):
        nonlocal get_count
        get_count += 1
        if get_count == 1:
            return None
        await asyncio.sleep(5.0)
        return json.dumps([{"id": "late-stale-voice", "name": "Late Stale", "languages": ["en"], "available": True}])

    mock_resources = AsyncMock()
    mock_resources.key = lambda s: f"voice-ai:{s}"
    # Lock held by another worker
    mock_resources.commands.set = AsyncMock(return_value=None)
    mock_resources.commands.get = blocking_late_get

    calls = 0

    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        nonlocal calls
        calls += 1
        return [TTSVoiceRead(id="fallback-voice", name="Fallback Voice", languages=["en"], available=True)]

    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", mock_cartesia)

    t0 = time.monotonic()
    result = await _tts_provider_catalog(
        env={"CARTESIA_API_KEY": "test-key"},
        provider="cartesia",
        model_id="sonic-english",
        resources=mock_resources,
    )
    elapsed = time.monotonic() - t0

    # Must return fallback voice from provider, NEVER late-stale-voice from cancelled GET
    assert result.available is True
    assert result.models[0].voices[0].id == "fallback-voice"
    assert calls == 1
    # Bounded by 3.0s budget; must not have waited 5.0s for the late GET
    assert 3.0 <= elapsed < 3.5


@pytest.mark.anyio
async def test_provider_catalog_waiter_aborts_immediately_on_redis_error(monkeypatch):
    """Redis error during waiter polling aborts waiter immediately on first failure without repeated polling."""
    from app.provider_catalog import _tts_provider_catalog, _IN_MEMORY_CACHE

    _IN_MEMORY_CACHE.clear()

    sleep_count = 0
    orig_sleep = asyncio.sleep

    async def counting_sleep(duration):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count > 1:
            pytest.fail(f"Waiter must abort immediately on Redis error, but polled again (sleep #{sleep_count})")
        await orig_sleep(duration)

    monkeypatch.setattr("asyncio.sleep", counting_sleep)

    get_call_count = 0

    async def mock_get(key):
        nonlocal get_call_count
        get_call_count += 1
        if get_call_count == 1:
            # Initial cache check before lock attempt: miss
            return None
        # During waiter loop: Redis crashes
        raise ConnectionError("Redis crashed during wait")

    mock_resources = AsyncMock()
    mock_resources.key = lambda s: f"voice-ai:{s}"
    mock_resources.commands.get = mock_get
    mock_resources.commands.set = AsyncMock(return_value=None)  # lock held

    calls = 0

    async def mock_cartesia(key: str) -> list[TTSVoiceRead]:
        nonlocal calls
        calls += 1
        return [TTSVoiceRead(id="abort-voice", name="Abort Voice", languages=["en"], available=True)]

    monkeypatch.setattr("app.provider_catalog._fetch_cartesia_voices", mock_cartesia)

    result = await _tts_provider_catalog(
        env={"CARTESIA_API_KEY": "test-key"},
        provider="cartesia",
        model_id="sonic-english",
        resources=mock_resources,
    )

    assert result.available is True
    assert result.models[0].voices[0].id == "abort-voice"
    assert calls == 1
    # Exactly one sleep occurred before the error broke the loop
    assert sleep_count == 1
