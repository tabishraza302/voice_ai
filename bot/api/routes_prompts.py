from fastapi import APIRouter, Depends, HTTPException
from bot.agents.schemas import (
    BasePromptResponse,
    BasePromptUpdate,
    PromptRevisionResponse,
    PromptPreviewRequest,
    PromptPreviewResponse,
)
from bot.agents.service import AgentService
from bot.agents.compiler import compile_system_prompt
from bot.db.session import get_db_session
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.get("/base", response_model=BasePromptResponse)
async def get_base_prompt(session: AsyncSession = Depends(get_db_session)):
    service = AgentService(session)
    await service.seed_default_presets()
    base = await service.get_base_prompt()
    revisions = await service.get_base_prompt_revisions()
    return BasePromptResponse(
        id=base.id,
        name=base.name,
        prompt_text=base.prompt_text,
        content=base.prompt_text,
        version=base.version,
        created_at=base.created_at,
        updated_at=base.updated_at,
        revisions=[
            PromptRevisionResponse(
                id=r.id,
                base_prompt_id=r.base_prompt_id,
                version=r.version,
                prompt_text=r.prompt_text,
                content=r.prompt_text,
                change_reason=r.change_reason,
                change_summary=r.change_reason,
                created_at=r.created_at,
            )
            for r in revisions
        ],
    )


@router.put("/base", response_model=BasePromptResponse)
async def update_base_prompt(
    update_data: BasePromptUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    service = AgentService(session)
    base = await service.update_base_prompt(update_data)
    revisions = await service.get_base_prompt_revisions()
    return BasePromptResponse(
        id=base.id,
        name=base.name,
        prompt_text=base.prompt_text,
        content=base.prompt_text,
        version=base.version,
        created_at=base.created_at,
        updated_at=base.updated_at,
        revisions=[
            PromptRevisionResponse(
                id=r.id,
                base_prompt_id=r.base_prompt_id,
                version=r.version,
                prompt_text=r.prompt_text,
                content=r.prompt_text,
                change_reason=r.change_reason,
                change_summary=r.change_reason,
                created_at=r.created_at,
            )
            for r in revisions
        ],
    )


@router.post("/preview", response_model=PromptPreviewResponse)
async def preview_prompt(
    req: PromptPreviewRequest,
    session: AsyncSession = Depends(get_db_session),
):
    service = AgentService(session)
    base_text = req.base_prompt
    persona_text = req.persona_prompt

    if req.agent_id and (base_text is None or persona_text is None):
        agent = await service.get_agent(req.agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        if persona_text is None:
            persona_text = agent.persona_prompt
        if base_text is None:
            base = await service.get_base_prompt()
            base_text = base.prompt_text

    if base_text is None:
        base = await service.get_base_prompt()
        base_text = base.prompt_text

    compiled = compile_system_prompt(
        base_prompt=base_text,
        persona_prompt=persona_text or "",
        variables=req.variables,
        strict=False,
    )
    token_est = int(len(compiled.split()) * 1.35)
    return PromptPreviewResponse(compiled_prompt=compiled, token_estimate=token_est)
