import asyncio
import logging
import weakref
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from bot.db.models import Agent, BasePrompt, PromptRevision
from bot.db.seeds.presets import DEFAULT_BASE_PROMPT, DEFAULT_PRESET_AGENTS
from bot.agents.schemas import AgentCreate, AgentUpdate, BasePromptUpdate
from bot.agents.compiler import compile_system_prompt

logger = logging.getLogger(__name__)
_SQLITE_SEED_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SQLITE_ACTIVATION_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SQLITE_PROMPT_UPDATE_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _lock_for_loop(lock_map: weakref.WeakKeyDictionary) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = lock_map.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        lock_map[loop] = lock
    return lock


class AgentNotFoundError(LookupError):
    pass


class DuplicateAgentSlugError(ValueError):
    pass


class PresetAgentDeleteError(ValueError):
    pass


class AgentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def seed_default_presets(self) -> None:
        if self.session.get_bind().dialect.name == "sqlite":
            async with _lock_for_loop(_SQLITE_SEED_LOCKS):
                await self._seed_default_presets()
            return
        await self._seed_default_presets()

    async def _seed_default_presets(self) -> None:
        """Seed default base prompt and agent archetypes if table is empty."""
        stmt = select(BasePrompt).where(BasePrompt.name == "default")
        res = await self.session.execute(stmt)
        base = res.scalar_one_or_none()
        if not base:
            candidate = BasePrompt(
                name="default",
                prompt_text=DEFAULT_BASE_PROMPT.strip(),
                version=1,
            )
            try:
                async with self.session.begin_nested():
                    self.session.add(candidate)
                    await self.session.flush()
                base = candidate
            except IntegrityError:
                base = (await self.session.execute(stmt)).scalar_one()

        # Serialize seeders on PostgreSQL; the partial unique active-agent
        # index provides the same invariant for SQLite concurrent writers.
        await self.session.execute(
            select(BasePrompt.id).where(BasePrompt.id == base.id).with_for_update()
        )

        revision_stmt = select(PromptRevision.id).where(
            PromptRevision.base_prompt_id == base.id,
            PromptRevision.version == 1,
        )
        if (await self.session.execute(revision_stmt)).scalar_one_or_none() is None:
            revision = PromptRevision(
                base_prompt_id=base.id,
                version=1,
                prompt_text=base.prompt_text,
                change_reason="Initial seed",
            )
            try:
                async with self.session.begin_nested():
                    self.session.add(revision)
                    await self.session.flush()
            except IntegrityError:
                pass

        # Check existing presets
        has_active = await self.get_active_agent()
        for preset_data in DEFAULT_PRESET_AGENTS:
            slug = preset_data["slug"]
            stmt = select(Agent).where(Agent.slug == slug)
            res = await self.session.execute(stmt)
            existing = res.scalar_one_or_none()
            if not existing:
                data = dict(preset_data)
                if has_active:
                    data["is_active"] = False
                agent = Agent(**data)
                try:
                    async with self.session.begin_nested():
                        self.session.add(agent)
                        await self.session.flush()
                    if data.get("is_active"):
                        has_active = agent
                except IntegrityError:
                    pass

        await self.session.commit()

    async def get_base_prompt(self) -> BasePrompt:
        stmt = select(BasePrompt).where(BasePrompt.name == "default")
        res = await self.session.execute(stmt)
        base = res.scalar_one_or_none()
        if not base:
            await self.seed_default_presets()
            res = await self.session.execute(stmt)
            base = res.scalar_one()
        return base

    async def update_base_prompt(self, update_data: BasePromptUpdate) -> BasePrompt:
        if self.session.get_bind().dialect.name == "sqlite":
            async with _lock_for_loop(_SQLITE_PROMPT_UPDATE_LOCKS):
                return await self._update_base_prompt(update_data)
        return await self._update_base_prompt(update_data)

    async def _update_base_prompt(self, update_data: BasePromptUpdate) -> BasePrompt:
        base = await self.get_base_prompt()
        result = await self.session.execute(
            update(BasePrompt)
            .where(BasePrompt.id == base.id)
            .values(
                prompt_text=update_data.get_text(),
                version=BasePrompt.version + 1,
            )
            .returning(BasePrompt.version)
            .execution_options(synchronize_session=False)
        )
        new_version = result.scalar_one()

        # Record revision
        rev = PromptRevision(
            base_prompt_id=base.id,
            version=new_version,
            prompt_text=update_data.get_text(),
            change_reason=update_data.get_reason() or f"Update to v{new_version}",
        )
        self.session.add(rev)
        await self.session.commit()
        await self.session.refresh(base)
        return base

    async def get_base_prompt_revisions(self) -> list[PromptRevision]:
        base = await self.get_base_prompt()
        stmt = select(PromptRevision).where(PromptRevision.base_prompt_id == base.id).order_by(PromptRevision.version.desc())
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def list_agents(self) -> list[Agent]:
        stmt = select(Agent).order_by(Agent.created_at.asc())
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def get_agent(self, agent_id: str) -> Agent | None:
        stmt = select(Agent).where(Agent.id == agent_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_agent_by_slug(self, slug: str) -> Agent | None:
        stmt = select(Agent).where(Agent.slug == slug)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_active_agent(self) -> Agent | None:
        stmt = select(Agent).where(Agent.is_active == True)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def create_agent(self, data: AgentCreate) -> Agent:
        agent = Agent(
            slug=data.slug,
            name=data.name,
            description=data.description,
            persona_prompt=data.persona_prompt,
            llm_model=data.llm_model,
            llm_temperature=data.llm_temperature,
            llm_reasoning_effort=data.llm_reasoning_effort,
            llm_max_tokens=data.llm_max_tokens,
            stt_provider=data.stt_provider,
            tts_voice_en=data.tts_voice_en,
            tts_voice_hi=data.tts_voice_hi,
            tts_speed=data.tts_speed,
            is_active=False,
            is_preset=False,
        )
        self.session.add(agent)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            if await self.get_agent_by_slug(data.slug):
                raise DuplicateAgentSlugError(f"Agent slug {data.slug!r} already exists") from exc
            raise
        await self.session.refresh(agent)
        return agent

    async def update_agent(self, agent_id: str, data: AgentUpdate) -> Agent:
        agent = await self.get_agent(agent_id)
        if not agent:
            raise AgentNotFoundError(f"Agent with id {agent_id} not found")

        update_dict = data.model_dump(exclude_unset=True)
        for key, value in update_dict.items():
            setattr(agent, key, value)

        await self.session.commit()
        await self.session.refresh(agent)
        return agent

    async def delete_agent(self, agent_id: str) -> bool:
        agent = await self.get_agent(agent_id)
        if not agent:
            return False
        if agent.is_preset:
            raise PresetAgentDeleteError("Preset agents cannot be deleted.")

        await self.session.delete(agent)
        await self.session.commit()
        return True

    async def activate_agent(self, agent_id: str) -> Agent:
        if self.session.get_bind().dialect.name == "sqlite":
            async with _lock_for_loop(_SQLITE_ACTIVATION_LOCKS):
                return await self._activate_agent(agent_id)
        return await self._activate_agent(agent_id)

    async def _activate_agent(self, agent_id: str) -> Agent:
        agent = await self.get_agent(agent_id)
        if not agent:
            raise AgentNotFoundError(f"Agent with id {agent_id} not found")

        # Lock the singleton prompt row as a portable serialization point.
        # PostgreSQL honors FOR UPDATE; SQLite serializes the following write
        # transaction. This keeps concurrent activations from both remaining active.
        base = await self.get_base_prompt()
        await self.session.execute(
            select(BasePrompt.id).where(BasePrompt.id == base.id).with_for_update()
        )
        await self.session.execute(
            update(Agent).where(Agent.id != agent_id).values(is_active=False)
        )
        result = await self.session.execute(
            select(Agent)
            .where(Agent.id == agent_id)
            .execution_options(populate_existing=True)
        )
        agent = result.scalar_one()
        agent.is_active = True
        await self.session.commit()
        await self.session.refresh(agent)
        return agent

    async def get_compiled_prompt_for_agent(
        self, agent_id: str, variables: dict[str, str] | None = None
    ) -> str:
        agent = await self.get_agent(agent_id)
        if not agent:
            raise ValueError(f"Agent with id {agent_id} not found")

        base = await self.get_base_prompt()
        return compile_system_prompt(base.prompt_text, agent.persona_prompt, variables)


async def apply_active_agent_to_settings(
    settings,
    session: AsyncSession | None = None,
    agent_id: str | None = None,
):
    """Resolve the active or requested agent profile and apply it to session Settings."""
    from bot.db.session import get_session_factory
    from bot.services import resolve_models, validate_stt_language
    async def _resolve_with_session(sess: AsyncSession):
        service = AgentService(sess)
        await service.seed_default_presets()
        if agent_id:
            agent = await service.get_agent(agent_id)
        else:
            agent = await service.get_active_agent()

        if not agent and agent_id:
            raise AgentNotFoundError(f"Agent with id {agent_id} not found")
        if not agent:
            return settings

        compiled = await service.get_compiled_prompt_for_agent(agent.id)

        updated = settings.model_copy(deep=True)
        updated.system_instruction = compiled
        updated.llm_model = agent.llm_model
        updated.llm_temperature = agent.llm_temperature
        updated.llm_reasoning_effort = agent.llm_reasoning_effort
        updated.llm_max_tokens = agent.llm_max_tokens
        updated.stt_provider = agent.stt_provider
        updated.tts_voice_en = agent.tts_voice_en
        updated.tts_voice_hi = agent.tts_voice_hi
        updated.tts_speed = agent.tts_speed
        resolved = resolve_models(updated)
        validate_stt_language(resolved)
        return resolved
    if session is not None:
        return await _resolve_with_session(session)

    try:
        factory = await get_session_factory()
        async with factory() as sess:
            return await _resolve_with_session(sess)
    except AgentNotFoundError:
        raise
    except SQLAlchemyError as exc:
        if agent_id:
            raise
        logger.warning(
            "Failed resolving agent configuration from database (%s); using default settings",
            type(exc).__name__,
        )
        return settings
