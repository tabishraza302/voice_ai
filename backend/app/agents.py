import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .auth import csrf, current_user
from .db import Base, get_db
from .models import User

router = APIRouter(prefix="/agents", tags=["agents"])

_LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        Index("ix_agents_owner_id", "owner_id"),
        UniqueConstraint("slug", name="agents_slug_key"),
        CheckConstraint("name <> '' AND name = btrim(name)", name="agents_name_check"),
        CheckConstraint("slug <> '' AND slug = lower(btrim(slug))", name="agents_slug_normalized_check"),
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="agents_slug_format_check"),
        CheckConstraint("status IN ('draft', 'published', 'suspended')", name="agents_status_check"),
        CheckConstraint("jsonb_typeof(languages) = 'array'", name="agents_languages_array_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    languages: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    default_language: Mapped[str | None] = mapped_column(String(35), nullable=True)
    stt_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stt_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tts_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tts_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    voice_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=8000)
    languages: list[str] | None = Field(default=None, max_length=12)
    default_language: str | None = Field(default=None, max_length=35)
    stt_provider: str | None = Field(default=None, max_length=40)
    stt_model: str | None = Field(default=None, max_length=80)
    llm_provider: str | None = Field(default=None, max_length=40)
    llm_model: str | None = Field(default=None, max_length=80)
    tts_provider: str | None = Field(default=None, max_length=40)
    tts_model: str | None = Field(default=None, max_length=80)
    voice_id: str | None = Field(default=None, max_length=120)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Name cannot be null")
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Slug cannot be null")
        value = value.strip().casefold()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("Slug must contain lowercase letters, numbers, and single hyphens")
        return value

    @field_validator(
        "description",
        "stt_provider",
        "stt_model",
        "llm_provider",
        "llm_model",
        "tts_provider",
        "tts_model",
        "voice_id",
    )
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("languages")
    @classmethod
    def normalize_languages(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            raise ValueError("Languages cannot be null")
        normalized = [language.strip().casefold() for language in value]
        if any(not _LANGUAGE_TAG.fullmatch(language) for language in normalized):
            raise ValueError("Languages must be valid language tags")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Languages cannot contain duplicates")
        return normalized

    @field_validator("default_language")
    @classmethod
    def normalize_default_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().casefold()
        if not value:
            return None
        if not _LANGUAGE_TAG.fullmatch(value):
            raise ValueError("Default language must be a valid language tag")
        return value


class AgentCreate(AgentPatch):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80)
    languages: list[str] = Field(default_factory=list, max_length=12)


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    instructions: str | None
    languages: list[str]
    default_language: str | None
    stt_provider: str | None
    stt_model: str | None
    llm_provider: str | None
    llm_model: str | None
    tts_provider: str | None
    tts_model: str | None
    voice_id: str | None
    status: Literal["draft", "published", "suspended"]
    created_at: datetime
    updated_at: datetime


async def _get_owned_agent(
    db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID, *, lock: bool
) -> Agent:
    statement = select(Agent).where(Agent.owner_id == owner_id, Agent.id == agent_id)
    if lock:
        statement = statement.with_for_update()
    agent = await db.scalar(statement)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    return agent


async def owned_agent(db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID) -> Agent:
    return await _get_owned_agent(db, owner_id, agent_id, lock=False)


async def _locked_owned_agent(db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID) -> Agent:
    return await _get_owned_agent(db, owner_id, agent_id, lock=True)

async def _commit_agent(db: AsyncSession, agent: Agent) -> None:
    slug = agent.slug
    agent_id = agent.id
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        conflict = await db.scalar(select(Agent.id).where(Agent.slug == slug, Agent.id != agent_id))
        if conflict is not None:
            raise HTTPException(409, "Agent slug is already in use")
        raise


def _agent_publish_errors(agent: Agent) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}

    def add(field: str, message: str) -> None:
        errors.setdefault(f"agent.{field}", []).append(message)

    if not agent.instructions or not agent.instructions.strip():
        add("instructions", "Agent instructions are required")
    if not agent.default_language:
        add("default_language", "Choose a default language")
    elif agent.default_language not in agent.languages:
        add("default_language", "Default language must be enabled")

    from .voice import provider_publish_errors

    for field, messages in provider_publish_errors(agent).items():
        key = f"agent.{field}"
        existing = errors.setdefault(key, [])
        existing.extend(message for message in messages if message not in existing)

    return errors


async def _publish_errors(db: AsyncSession, agent: Agent) -> dict[str, list[str]]:
    from .steps import Step, validate_publish

    steps = list(await db.scalars(select(Step).where(Step.agent_id == agent.id).order_by(Step.sort_order)))
    errors = _agent_publish_errors(agent)
    for field, messages in validate_publish(agent, steps).items():
        errors.setdefault(field, []).extend(messages)
    return errors


@router.get("", response_model=list[AgentRead])
async def list_agents(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(current_user)],
):
    result = await db.scalars(
        select(Agent).where(Agent.owner_id == user.id).order_by(Agent.created_at.desc(), Agent.id)
    )
    return list(result)


@router.post("", status_code=201, response_model=AgentRead)
async def create_agent(
    data: AgentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(csrf)],
):
    agent = Agent(id=uuid.uuid4(), owner_id=user.id, **data.model_dump())
    db.add(agent)
    await _commit_agent(db, agent)
    await db.refresh(agent)
    return agent


@router.get("/{agent_id}", response_model=AgentRead)
async def get_agent(
    agent_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(current_user)],
):
    return await owned_agent(db, user.id, agent_id)


@router.patch("/{agent_id}", response_model=AgentRead)
async def update_agent(
    agent_id: uuid.UUID,
    data: AgentPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(csrf)],
):
    agent = await _locked_owned_agent(db, user.id, agent_id)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(agent, field, value)
    if agent.status == "published" and updates:
        errors = await _publish_errors(db, agent)
        if errors:
            await db.rollback()
            raise HTTPException(422, errors)
    if updates:
        await _commit_agent(db, agent)
        await db.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(csrf)],
):
    agent = await _locked_owned_agent(db, user.id, agent_id)
    from .sessions import CallSession

    active_call = await db.scalar(
        select(CallSession.id)
        .where(CallSession.agent_id == agent.id, CallSession.status == "active")
        .limit(1)
    )
    if active_call is not None:
        raise HTTPException(409, "Cannot delete an agent with an active call")
    await db.delete(agent)
    await db.commit()
    return Response(status_code=204)


@router.post("/{agent_id}/publish", response_model=AgentRead)
async def publish_agent(
    agent_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(csrf)],
):
    agent = await _locked_owned_agent(db, user.id, agent_id)
    if agent.status == "suspended":
        raise HTTPException(409, "A suspended agent cannot be published")
    errors = await _publish_errors(db, agent)
    if errors:
        raise HTTPException(422, errors)
    agent.status = "published"
    await db.commit()
    await db.refresh(agent)
    return agent


@router.post("/{agent_id}/unpublish", response_model=AgentRead)
async def unpublish_agent(
    agent_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(csrf)],
):
    agent = await _locked_owned_agent(db, user.id, agent_id)
    if agent.status == "suspended":
        raise HTTPException(409, "A suspended agent cannot be unpublished by its owner")
    if agent.status != "draft":
        agent.status = "draft"
        await db.commit()
        await db.refresh(agent)
    return agent
