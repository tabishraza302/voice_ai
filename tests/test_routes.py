import uuid
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool
from bot.api.router import create_api_router
from bot.config import Settings
from bot.db.base import Base
from bot.db.session import get_db_session
import bot.db.models  # noqa: F401


@pytest_asyncio.fixture
async def api_client():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db_session():
        async with session_factory() as session:
            yield session

    app = FastAPI()
    router = create_api_router(Settings())
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db_session] = override_get_db_session

    with TestClient(app) as client:
        yield client

    await engine.dispose()

def test_list_agents(api_client):
    res = api_client.get("/api/agents")
    assert res.status_code == 200
    agents = res.json()
    assert isinstance(agents, list)
    assert len(agents) >= 1
    # Check default agent
    active = [a for a in agents if a["is_active"]]
    assert len(active) == 1


def test_create_and_get_agent(api_client):
    uid = uuid.uuid4().hex[:6]
    payload = {
        "name": f"Custom Coder {uid}",
        "slug": f"custom-coder-{uid}",
        "description": "Writes code and tests",
        "persona_prompt": "You are an expert Python engineer.",
        "llm_model": "qwen3.5:4b",
        "llm_temperature": 0.3,
        "llm_reasoning_effort": "none",
        "stt_provider": "mlx-audio-realtime",
        "tts_voice_en": "am_adam",
        "tts_voice_hi": "hm_omega",
        "tts_speed": 1.1,
    }
    create_res = api_client.post("/api/agents", json=payload)
    assert create_res.status_code == 201
    created = create_res.json()
    assert created["slug"] == f"custom-coder-{uid}"
    assert created["name"] == f"Custom Coder {uid}"
    assert created["llm_temperature"] == 0.3

    agent_id = created["id"]
    get_res = api_client.get(f"/api/agents/{agent_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == agent_id


def test_update_agent(api_client):
    uid = uuid.uuid4().hex[:6]
    create_res = api_client.post(
        "/api/agents",
        json={
            "name": f"To Update {uid}",
            "slug": f"to-update-{uid}",
            "persona_prompt": "Original prompt",
        },
    )
    agent_id = create_res.json()["id"]

    update_res = api_client.put(
        f"/api/agents/{agent_id}",
        json={"name": "Updated Name", "llm_temperature": 0.95},
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["name"] == "Updated Name"
    assert updated["llm_temperature"] == 0.95


def test_activate_agent(api_client):
    uid = uuid.uuid4().hex[:6]
    create_res = api_client.post(
        "/api/agents",
        json={"name": f"New Active Agent {uid}", "slug": f"new-active-{uid}"},
    )
    agent_id = create_res.json()["id"]

    act_res = api_client.post(f"/api/agents/{agent_id}/activate")
    assert act_res.status_code == 200
    assert act_res.json()["is_active"] is True

    # Check that it's now the active agent
    list_res = api_client.get("/api/agents")
    active = [a for a in list_res.json() if a["is_active"]]
    assert len(active) == 1
    assert active[0]["id"] == agent_id


def test_get_and_update_base_prompt(api_client):
    get_res = api_client.get("/api/prompts/base")
    assert get_res.status_code == 200
    data = get_res.json()
    assert "prompt_text" in data
    assert len(data["prompt_text"]) > 0

    put_res = api_client.put(
        "/api/prompts/base",
        json={
            "prompt_text": "Updated shared base prompt rules.",
            "change_reason": "Testing update",
        },
    )
    assert put_res.status_code == 200
    assert put_res.json()["prompt_text"] == "Updated shared base prompt rules."


def test_preview_prompt(api_client):
    preview_res = api_client.post(
        "/api/prompts/preview",
        json={
            "base_prompt": "Base: Hello {name}.",
            "persona_prompt": "Persona: Help user.",
            "variables": {"name": "Alice"},
        },
    )
    assert preview_res.status_code == 200
    res_json = preview_res.json()
    compiled = res_json["compiled_prompt"]
    assert "Base: Hello Alice." in compiled
    assert "Persona: Help user." in compiled
    assert "token_estimate" in res_json
    assert res_json["token_estimate"] > 0

def test_models_and_voices_catalog(api_client):
    models_res = api_client.get("/api/models")
    assert models_res.status_code == 200
    models_data = models_res.json()
    assert "llm_models" in models_data
    assert "stt_providers" in models_data

    voices_res = api_client.get("/api/voices")
    assert voices_res.status_code == 200
    voices_data = voices_res.json()
    assert "voices" in voices_data
    assert len(voices_data["voices"]) >= 8

    catalog_res = api_client.get("/api/catalog")
    assert catalog_res.status_code == 200
    catalog = catalog_res.json()
    assert "llm_models" in catalog
    assert "stt_providers" in catalog
    assert "tts_voices" in catalog
    assert len(catalog["tts_voices"]) == 8


def test_agent_not_found_statuses(api_client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert api_client.put(f"/api/agents/{missing}", json={"name": "Changed"}).status_code == 404
    assert api_client.post(f"/api/agents/{missing}/activate").status_code == 404
    assert api_client.delete(f"/api/agents/{missing}").status_code == 404
    assert api_client.post("/api/prompts/preview", json={"agent_id": missing}).status_code == 404


def test_duplicate_and_preset_delete_statuses(api_client):
    payload = {"name": "Duplicate", "slug": "duplicate-slug"}
    assert api_client.post("/api/agents", json=payload).status_code == 201
    assert api_client.post("/api/agents", json=payload).status_code == 409
    presets = api_client.get("/api/agents").json()
    preset = next(agent for agent in presets if agent["is_preset"])
    assert api_client.delete(f"/api/agents/{preset['id']}").status_code == 409


@pytest.mark.parametrize("payload", [
    {"name": "   ", "slug": "valid-slug"},
    {"name": "Valid", "slug": "  "},
    {"name": "Valid", "slug": "valid-slug", "llm_model": None},
    {"name": "Valid", "slug": "valid-slug", "llm_model": "  "},
    {"name": "Valid", "slug": "valid-slug", "stt_provider": "remote"},
    {"name": "Valid", "slug": "valid-slug", "llm_reasoning_effort": "extreme"},
    {"name": "Valid", "slug": "valid-slug", "tts_voice_en": "hf_alpha"},
])
def test_create_agent_rejects_invalid_fields(api_client, payload):
    assert api_client.post("/api/agents", json=payload).status_code == 422


def test_update_agent_rejects_explicit_null_and_invalid_fields(api_client):
    created = api_client.post("/api/agents", json={"name": "Valid", "slug": "valid-update"})
    agent_id = created.json()["id"]
    assert api_client.put(f"/api/agents/{agent_id}", json={"name": None}).status_code == 422
    assert api_client.put(f"/api/agents/{agent_id}", json={"stt_provider": "remote"}).status_code == 422
    assert api_client.put(f"/api/agents/{agent_id}", json={"llm_reasoning_effort": ""}).status_code == 200


def test_prompt_update_requires_text_but_accepts_empty_string(api_client):
    assert api_client.put("/api/prompts/base", json={"change_reason": "missing text"}).status_code == 422
    response = api_client.put("/api/prompts/base", json={"prompt_text": "", "change_reason": "clear"})
    assert response.status_code == 200
    assert response.json()["prompt_text"] == ""


def test_model_catalog_uses_configured_local_llm_url(monkeypatch):
    import asyncio
    from bot.api import routes_models
    from bot.config import Settings

    requested = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"models": [{"name": "local-custom:7b"}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            requested.append(url)
            return Response()

    monkeypatch.setattr(routes_models.httpx, "AsyncClient", Client)
    settings = Settings(llm_base_url="http://127.0.0.1:12345/v1", stt_provider="mlx-whisper")
    result = asyncio.run(routes_models.list_models(settings))
    assert requested == ["http://127.0.0.1:12345/api/tags"]
    assert "local-custom:7b" in result["llm_models"]
    assert settings.llm_model in result["llm_models"]
    assert result["default_llm_model"] == settings.llm_model
    assert "mlx-whisper" in {provider["id"] for provider in result["stt_providers"]}


def test_model_catalog_never_fetches_non_loopback_url(monkeypatch):
    import asyncio
    from bot.api import routes_models
    from bot.config import Settings

    def forbidden_client(**kwargs):
        raise AssertionError("model discovery attempted a non-local request")

    monkeypatch.setattr(routes_models.httpx, "AsyncClient", forbidden_client)
    result = asyncio.run(routes_models.list_models(Settings(llm_base_url="https://example.invalid/v1")))
    assert result["llm_models"]


def test_model_catalog_skips_ollama_for_openai_compatible_provider(monkeypatch):
    import asyncio
    from bot.api import routes_models
    from bot.config import Settings

    def forbidden_client(**kwargs):
        raise AssertionError("non-Ollama provider must not use Ollama discovery")

    monkeypatch.setattr(routes_models.httpx, "AsyncClient", forbidden_client)
    settings = Settings(
        llm_provider="openai-compatible",
        llm_model="my-local-model",
        llm_base_url="http://localhost:1234/v1",
    )
    result = asyncio.run(routes_models.list_models(settings))
    assert "my-local-model" in result["llm_models"]
