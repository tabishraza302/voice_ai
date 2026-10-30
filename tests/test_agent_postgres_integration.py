"""Optional PostgreSQL integration tests against a disposable schema only."""

import asyncio

import pytest
import pytest_asyncio

from bot.agents.schemas import AgentCreate, BasePromptUpdate
from bot.agents.service import AgentService
from bot.db.seeds.presets import DEFAULT_BASE_PROMPT
from tests.support.postgres import disposable_postgres_schema
pytestmark = pytest.mark.postgres

@pytest_asyncio.fixture
async def postgres_schema():
    async with disposable_postgres_schema() as (_engine, factory, _schema):
        yield factory

@pytest.mark.asyncio
async def test_postgres_concurrent_prompt_saves_allocate_distinct_versions(postgres_schema):
    factory = postgres_schema
    async with factory() as session:
        await AgentService(session).seed_default_presets()

    async def save(prompt):
        async with factory() as session:
            return await AgentService(session).update_base_prompt(
                BasePromptUpdate(prompt_text=prompt, change_reason=prompt)
            )

    await asyncio.gather(save("Postgres prompt A"), save("Postgres prompt B"))
    async with factory() as session:
        revisions = await AgentService(session).get_base_prompt_revisions()
        assert [revision.version for revision in revisions] == [3, 2, 1]
        assert {revision.prompt_text for revision in revisions} == {
            "Postgres prompt A", "Postgres prompt B", DEFAULT_BASE_PROMPT.strip()
        }


@pytest.mark.asyncio
async def test_postgres_activation_refreshes_stale_agent_state(postgres_schema):
    factory = postgres_schema
    async with factory() as session:
        service = AgentService(session)
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
        active = [agent for agent in await AgentService(verify_session).list_agents() if agent.is_active]
        assert [agent.id for agent in active] == [original.id]


@pytest.mark.asyncio
async def test_postgres_concurrent_activation_keeps_one_active_agent(postgres_schema):
    factory = postgres_schema
    async with factory() as session:
        service = AgentService(session)
        await service.seed_default_presets()
        first = await service.create_agent(AgentCreate(name="First concurrent", slug="first-concurrent"))
        second = await service.create_agent(AgentCreate(name="Second concurrent", slug="second-concurrent"))

    async def activate(agent_id):
        async with factory() as session:
            await AgentService(session).activate_agent(agent_id)

    await asyncio.gather(activate(first.id), activate(second.id))
    async with factory() as session:
        active = [agent for agent in await AgentService(session).list_agents() if agent.is_active]
        assert len(active) == 1
        assert active[0].id in {first.id, second.id}


@pytest.mark.asyncio
async def test_postgres_concurrent_seeding_creates_one_preset_set(postgres_schema):
    factory = postgres_schema

    async def seed():
        async with factory() as session:
            await AgentService(session).seed_default_presets()

    await asyncio.gather(seed(), seed())
    async with factory() as session:
        service = AgentService(session)
        agents = await service.list_agents()
        revisions = await service.get_base_prompt_revisions()
        assert len(agents) == 3
        assert sum(agent.is_active for agent in agents) == 1
        assert [revision.version for revision in revisions] == [1]
