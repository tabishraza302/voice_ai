import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.agents.service import AgentService, apply_active_agent_to_settings
from bot.config import Settings
from bot.db.base import Base
import bot.db.models  # noqa: F401


@pytest_asyncio.fixture
async def integration_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_active_agent_to_settings(integration_session):
    service = AgentService(integration_session)
    await service.seed_default_presets()
    active = await service.get_active_agent()
    assert active is not None

    settings = Settings()
    updated_settings = await apply_active_agent_to_settings(settings, session=integration_session)

    assert updated_settings is not settings
    assert updated_settings.llm_model == active.llm_model
    assert updated_settings.llm_temperature == active.llm_temperature
    assert updated_settings.llm_reasoning_effort == active.llm_reasoning_effort
    assert updated_settings.llm_max_tokens == active.llm_max_tokens
    assert updated_settings.stt_provider == active.stt_provider
    assert active.persona_prompt in updated_settings.system_instruction


@pytest.mark.asyncio
async def test_explicit_missing_agent_is_not_hidden(integration_session):
    with pytest.raises(LookupError, match="not found"):
        await apply_active_agent_to_settings(
            Settings(), session=integration_session,
            agent_id="00000000-0000-0000-0000-000000000000",
        )


@pytest.mark.asyncio
async def test_explicit_agent_sqlalchemy_error_fails_closed(monkeypatch):
    from sqlalchemy.exc import OperationalError

    class FailingSessionContext:
        async def __aenter__(self):
            raise OperationalError("SELECT 1", {}, Exception("database unavailable"))

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

    def failing_sessionmaker():
        return FailingSessionContext()

    async def get_failing_factory():
        return failing_sessionmaker

    monkeypatch.setattr("bot.db.session.get_session_factory", get_failing_factory)

    with pytest.raises(OperationalError):
        await apply_active_agent_to_settings(
            Settings(),
            agent_id="00000000-0000-0000-0000-000000000000",
        )


@pytest.mark.asyncio
async def test_unrequested_agent_sqlalchemy_error_falls_back_to_defaults(monkeypatch):
    from sqlalchemy.exc import OperationalError

    class FailingSessionContext:
        async def __aenter__(self):
            raise OperationalError("SELECT 1", {}, Exception("database unavailable"))

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

    def failing_sessionmaker():
        return FailingSessionContext()

    async def get_failing_factory():
        return failing_sessionmaker

    monkeypatch.setattr("bot.db.session.get_session_factory", get_failing_factory)

    default_settings = Settings()
    resolved = await apply_active_agent_to_settings(default_settings)
    assert resolved == default_settings

@pytest.mark.asyncio
async def test_apply_active_agent_rejects_incompatible_realtime_non_auto_language(integration_session):
    service = AgentService(integration_session)
    await service.seed_default_presets()
    from bot.agents.schemas import AgentCreate
    agent = await service.create_agent(
        AgentCreate(
            slug="realtime-agent",
            name="Realtime Agent",
            persona_prompt="You are a real-time voice assistant.",
            llm_model="qwen3.5:4b",
            stt_provider="mlx-audio-realtime",
        )
    )
    settings = Settings(stt_language="hi")
    assert settings.stt_language == "hi"

    with pytest.raises(ValueError, match="stt_provider=mlx-audio-realtime has no language switch"):
        await apply_active_agent_to_settings(
            settings, session=integration_session, agent_id=agent.id
        )
