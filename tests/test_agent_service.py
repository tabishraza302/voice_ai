import pytest
import pytest_asyncio
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from bot.db.base import Base
from bot.db.models import Agent, BasePrompt, PromptRevision, VoiceSession, SessionTurn
from bot.agents.service import AgentService
from bot.agents.schemas import AgentCreate, AgentUpdate, BasePromptUpdate
from bot.db.seeds.presets import DEFAULT_BASE_PROMPT


@pytest_asyncio.fixture
async def async_session():
    # In-memory SQLite async engine for tests
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_initialization(async_session: AsyncSession):
    service = AgentService(async_session)
    await service.seed_default_presets()

    # Base prompt should exist
    base = await service.get_base_prompt()
    assert base is not None
    assert len(base.prompt_text) > 0

    # 3 presets should exist
    agents = await service.list_agents()
    assert len(agents) >= 3
    slugs = {a.slug for a in agents}
    assert "bilingual-assistant" in slugs
    assert "tech-tutor" in slugs
    assert "creative-storyteller" in slugs


@pytest.mark.asyncio
async def test_concurrent_preset_seeding_is_idempotent(tmp_path):
    db_path = tmp_path / "seed-race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async def seed():
        async with factory() as session:
            await AgentService(session).seed_default_presets()

    try:
        await asyncio.gather(seed(), seed())
        async with factory() as session:
            agents = await AgentService(session).list_agents()
            assert len(agents) == 3
            assert sum(agent.is_active for agent in agents) == 1
            revisions = await AgentService(session).get_base_prompt_revisions()
            assert [revision.version for revision in revisions] == [1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_base_prompt_updates_allocate_distinct_versions(tmp_path):
    db_path = tmp_path / "prompt-update-race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            await AgentService(session).seed_default_presets()

        async def update(text):
            async with factory() as session:
                return await AgentService(session).update_base_prompt(
                    BasePromptUpdate(prompt_text=text, change_reason=text)
                )

        await asyncio.gather(update("revision A"), update("revision B"))
        async with factory() as session:
            revisions = await AgentService(session).get_base_prompt_revisions()
            assert [revision.version for revision in revisions] == [3, 2, 1]
            assert {revision.prompt_text for revision in revisions} == {
                DEFAULT_BASE_PROMPT.strip(), "revision A", "revision B"
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_activation_reloads_agent_after_waiting_session_change(tmp_path):
    db_path = tmp_path / "activation-refresh.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as seed_session:
            service = AgentService(seed_session)
            await service.seed_default_presets()
            agents = await service.list_agents()
            original = next(agent for agent in agents if agent.is_active)
            other = next(agent for agent in agents if agent.id != original.id)

        async with factory() as session_a, factory() as session_b:
            cached_original = await AgentService(session_a).get_agent(original.id)
            assert cached_original.is_active is True
            await AgentService(session_b).activate_agent(other.id)
            await AgentService(session_a).activate_agent(original.id)

        async with factory() as verify_session:
            active_agents = [agent for agent in await AgentService(verify_session).list_agents() if agent.is_active]
            assert [agent.id for agent in active_agents] == [original.id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_crud(async_session: AsyncSession):
    service = AgentService(async_session)
    await service.seed_default_presets()

    create_data = AgentCreate(
        slug="custom-agent",
        name="Custom Agent",
        description="A test agent",
        persona_prompt="You are a custom assistant.",
        llm_model="qwen3.5:4b",
        llm_temperature=0.7,
        llm_reasoning_effort="none",
        llm_max_tokens=150,
        stt_provider="mlx-audio",
        tts_voice_en="af_heart",
        tts_voice_hi="hf_alpha",
        tts_speed=1.0,
    )
    agent = await service.create_agent(create_data)
    assert agent.id is not None
    assert agent.slug == "custom-agent"
    assert agent.is_active is False
    assert agent.is_preset is False

    # Get agent by slug
    fetched = await service.get_agent_by_slug("custom-agent")
    assert fetched is not None
    assert fetched.id == agent.id

    # Update agent
    update_data = AgentUpdate(name="Updated Name", llm_temperature=0.5)
    updated = await service.update_agent(agent.id, update_data)
    assert updated.name == "Updated Name"
    assert updated.llm_temperature == 0.5

    # Activate agent (should make it the only active agent)
    activated = await service.activate_agent(agent.id)
    assert activated.is_active is True

    active = await service.get_active_agent()
    assert active is not None
    assert active.id == agent.id

    # Delete agent
    deleted = await service.delete_agent(agent.id)
    assert deleted is True
    assert await service.get_agent(agent.id) is None


@pytest.mark.asyncio
async def test_base_prompt_revisions(async_session: AsyncSession):
    service = AgentService(async_session)
    await service.seed_default_presets()

    base = await service.get_base_prompt()
    orig_text = base.prompt_text

    # Update base prompt with change reason
    update_data = BasePromptUpdate(
        prompt_text="New updated base prompt content",
        change_reason="Sharpen conversational boundaries",
    )
    updated_base = await service.update_base_prompt(update_data)
    assert updated_base.prompt_text == "New updated base prompt content"
    assert updated_base.version == 2

    # Check revisions: newest first, and the newest must be the v2 update.
    revisions = await service.get_base_prompt_revisions()
    assert len(revisions) >= 1
    assert revisions[0].version == 2
    assert revisions[0].prompt_text == "New updated base prompt content"
    assert revisions[0].change_reason == "Sharpen conversational boundaries"
    # The seed revision still records the original text at v1.
    assert any(revision.prompt_text == orig_text for revision in revisions)
