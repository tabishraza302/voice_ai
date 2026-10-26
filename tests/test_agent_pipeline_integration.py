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
