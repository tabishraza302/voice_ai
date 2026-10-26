from fastapi import APIRouter, HTTPException, Depends
from bot.agents.schemas import AgentCreate, AgentUpdate, AgentResponse
from bot.agents.service import (
    AgentNotFoundError,
    AgentService,
    DuplicateAgentSlugError,
    PresetAgentDeleteError,
)
from bot.db.session import get_db_session
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentResponse])
async def list_agents(session: AsyncSession = Depends(get_db_session)):
    service = AgentService(session)
    await service.seed_default_presets()
    agents = await service.list_agents()
    return [AgentResponse.model_validate(a) for a in agents]


@router.post("", response_model=AgentResponse, status_code=201)
async def create_agent(agent_in: AgentCreate, session: AsyncSession = Depends(get_db_session)):
    service = AgentService(session)
    try:
        agent = await service.create_agent(agent_in)
    except DuplicateAgentSlugError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AgentResponse.model_validate(agent)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str, session: AsyncSession = Depends(get_db_session)):
    service = AgentService(session)
    agent = await service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return AgentResponse.model_validate(agent)


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: str, agent_in: AgentUpdate, session: AsyncSession = Depends(get_db_session)):
    service = AgentService(session)
    try:
        agent = await service.update_agent(agent_id, agent_in)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc
    return AgentResponse.model_validate(agent)


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, session: AsyncSession = Depends(get_db_session)):
    service = AgentService(session)
    try:
        success = await service.delete_agent(agent_id)
    except PresetAgentDeleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "deleted", "id": agent_id}


@router.post("/{agent_id}/activate", response_model=AgentResponse)
async def activate_agent(agent_id: str, session: AsyncSession = Depends(get_db_session)):
    service = AgentService(session)
    try:
        agent = await service.activate_agent(agent_id)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc
    return AgentResponse.model_validate(agent)
