import pytest
import pytest_asyncio

from bot.agents.schemas import AgentCreate
from bot.agents.service import AgentNotFoundError, AgentService, apply_active_agent_to_settings
from bot.config import SYSTEM_PROMPT, Settings
from bot.session_settings import resolve_session_settings
from tests.support.postgres import disposable_postgres_schema
pytestmark = pytest.mark.postgres

@pytest.fixture(autouse=True)
def isolate_ollama_catalog(monkeypatch):
    """Isolate non-live profile integration tests from actual Ollama by injecting catalog."""
    async def fake_fetch_local_llm_models(settings, *, timeout=2.0):
        return {"qwen3.5:4b", "configured-local-model"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", fake_fetch_local_llm_models)



@pytest_asyncio.fixture
async def session():
    async with disposable_postgres_schema() as (_engine, factory, _schema):
        async with factory() as database_session:
            yield database_session

@pytest.mark.asyncio
async def test_selected_profile_changes_only_the_requested_voice_session(session):
    service = AgentService(session)
    await service.seed_default_presets()
    profile = await service.create_agent(
        AgentCreate(
            slug="session-profile",
            name="Session Profile",
            persona_prompt="You explain energy choices in plain spoken language.",
            llm_model="qwen3.5:4b",
            llm_temperature=0.4,
            llm_reasoning_effort="none",
            llm_max_tokens=177,
            stt_provider="mlx-audio",
            tts_voice_en="af_heart",
            tts_voice_hi="hf_alpha",
            tts_speed=1.0,
        )
    )
    configured = Settings(
        _env_file=None,
        llm_model="configured-local-model",
        llm_max_tokens=333,
        stt_provider="mlx-audio-realtime",
    )
    resolver_calls = []

    async def resolve_profile(settings, *, agent_id):
        resolver_calls.append(agent_id)
        return await apply_active_agent_to_settings(
            settings, session=session, agent_id=agent_id
        )

    default_session = await resolve_session_settings(configured, {}, resolve_profile)
    selected_session = await resolve_session_settings(
        configured, {"agent_id": profile.id}, resolve_profile
    )

    assert resolver_calls == [profile.id]
    assert default_session.llm_model == "configured-local-model"
    assert default_session.llm_max_tokens == 333
    assert default_session.stt_provider == "mlx-audio-realtime"
    assert default_session.system_instruction == SYSTEM_PROMPT
    assert selected_session.llm_model == "qwen3.5:4b"
    assert selected_session.llm_max_tokens == 177
    assert selected_session.llm_temperature == 0.4
    assert "You explain energy choices" in selected_session.system_instruction

    with pytest.raises(AgentNotFoundError):
        await resolve_session_settings(
            configured, {"agent_id": "missing-profile"}, resolve_profile
        )
