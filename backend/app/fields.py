import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from .auth import csrf, current_user
from .db import Base, get_db
from .models import User

_OPERATORS = frozenset({"eq", "ne", "in", "gt", "gte", "lt", "lte"})


class AgentField(Base):
    __tablename__ = "fields"
    __table_args__ = (
        UniqueConstraint("agent_id", "field_key", name="fields_agent_field_key_key"),
        CheckConstraint("field_key <> '' AND field_key = btrim(field_key)", name="fields_field_key_nonempty_check"),
        CheckConstraint("sort_order >= 0", name="fields_sort_order_check"),
        CheckConstraint("jsonb_typeof(options) = 'array'", name="fields_options_array_check"),
        CheckConstraint("jsonb_typeof(required_when) = 'array'", name="fields_required_when_array_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    guidance: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    options: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    required_when: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class AgentOutcome(Base):
    __tablename__ = "outcomes"
    __table_args__ = (
        UniqueConstraint("agent_id", "label", name="outcomes_agent_label_key"),
        CheckConstraint("label <> '' AND label = btrim(label)", name="outcomes_label_nonempty_check"),
        CheckConstraint("jsonb_typeof(conditions) = 'array'", name="outcomes_conditions_array_check"),
        CheckConstraint("jsonb_typeof(required_fields) = 'array'", name="outcomes_required_fields_array_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    required_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )


class FieldConditionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(max_length=128)
    operator: str = "eq"
    operand: Any = None

    @field_validator("field_key")
    @classmethod
    def normalize_field_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field_key must not be empty")
        return value

    @field_validator("operator")
    @classmethod
    def validate_operator(cls, value: str) -> str:
        if value not in _OPERATORS:
            raise ValueError(f"operator must be one of {', '.join(sorted(_OPERATORS))}")
        return value


class FieldInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_key: str | None = Field(default=None, max_length=128)
    description: str = ""
    guidance: str = ""
    options: list[str] = Field(default_factory=list)
    required: bool = False
    required_when: list[FieldConditionInput] = Field(default_factory=list)

    @field_validator("field_key")
    @classmethod
    def normalize_field_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("field_key must not be empty")
        return value

    @field_validator("description", "guidance")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("options")
    @classmethod
    def normalize_options(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for option in value:
            option = option.strip()
            if not option:
                raise ValueError("options must not contain blank values")
            if option in normalized:
                raise ValueError("options must be unique")
            normalized.append(option)
        return normalized


class FieldCreate(FieldInput):
    sort_order: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_field_key(self) -> "FieldCreate":
        if self.field_key is None:
            raise ValueError("field_key is required")
        return self


class FieldPatch(FieldInput):
    @model_validator(mode="after")
    def reject_null_field_key(self) -> "FieldPatch":
        if "field_key" in self.model_fields_set and self.field_key is None:
            raise ValueError("field_key must not be null")
        return self


class FieldRead(FieldInput):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    agent_id: uuid.UUID
    field_key: str
    sort_order: int


class FieldOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_ids: list[uuid.UUID]


class OutcomeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=128)
    conditions: list[FieldConditionInput] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("label must not be empty")
        return value

    @field_validator("required_fields")
    @classmethod
    def normalize_required_fields(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for field_key in value:
            field_key = field_key.strip()
            if not field_key:
                raise ValueError("required_fields must not contain blank values")
            if len(field_key) > 128:
                raise ValueError("required_fields entries must be at most 128 characters")
            if field_key in normalized:
                raise ValueError("required_fields must be unique")
            normalized.append(field_key)
        return normalized


class OutcomeCreate(OutcomeInput):
    @model_validator(mode="after")
    def require_label(self) -> "OutcomeCreate":
        if self.label is None:
            raise ValueError("label is required")
        return self


class OutcomePatch(OutcomeInput):
    @model_validator(mode="after")
    def reject_null_label(self) -> "OutcomePatch":
        if "label" in self.model_fields_set and self.label is None:
            raise ValueError("label must not be null")
        return self


class OutcomeRead(OutcomeInput):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    agent_id: uuid.UUID
    label: str


router = APIRouter(prefix="/agents", tags=["fields"])
Db = Annotated[AsyncSession, Depends(get_db)]
Reader = Annotated[User, Depends(current_user)]
Writer = Annotated[User, Depends(csrf)]


async def _owned_agent(db: AsyncSession, owner_id: uuid.UUID, agent_id: uuid.UUID):
    from .agents import owned_agent

    agent = await owned_agent(db, owner_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _lock_agent(db: AsyncSession, agent_id: uuid.UUID) -> None:
    from .agents import Agent

    agent = await db.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")


async def _owned_field(db: AsyncSession, agent_id: uuid.UUID, field_id: uuid.UUID) -> AgentField:
    field = await db.scalar(select(AgentField).where(AgentField.agent_id == agent_id, AgentField.id == field_id))
    if field is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return field


async def _owned_outcome(db: AsyncSession, agent_id: uuid.UUID, outcome_id: uuid.UUID) -> AgentOutcome:
    outcome = await db.scalar(
        select(AgentOutcome).where(AgentOutcome.agent_id == agent_id, AgentOutcome.id == outcome_id)
    )
    if outcome is None:
        raise HTTPException(status_code=404, detail="Outcome not found")
    return outcome


def _field_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="A field with that field key already exists")


def _outcome_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="An outcome with that label already exists")


@router.get("/{agent_id}/fields", response_model=list[FieldRead])
async def list_fields(agent_id: uuid.UUID, db: Db, user: Reader):
    agent = await _owned_agent(db, user.id, agent_id)
    return list(
        await db.scalars(
            select(AgentField).where(AgentField.agent_id == agent.id).order_by(AgentField.sort_order, AgentField.id)
        )
    )


@router.post("/{agent_id}/fields", response_model=FieldRead, status_code=201)
async def create_field(agent_id: uuid.UUID, payload: FieldCreate, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    data = payload.model_dump(mode="json")
    order = data.pop("sort_order")
    if order is None:
        current_max = await db.scalar(select(func.max(AgentField.sort_order)).where(AgentField.agent_id == agent.id))
        order = 0 if current_max is None else current_max + 1
    field = AgentField(agent_id=agent.id, sort_order=order, **data)
    db.add(field)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _field_conflict()
    await db.refresh(field)
    return field


@router.patch("/{agent_id}/fields/{field_id}", response_model=FieldRead)
async def patch_field(agent_id: uuid.UUID, field_id: uuid.UUID, payload: FieldPatch, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    field = await _owned_field(db, agent.id, field_id)
    for name, value in payload.model_dump(mode="json", exclude_unset=True).items():
        setattr(field, name, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _field_conflict()
    await db.refresh(field)
    return field


@router.delete("/{agent_id}/fields/{field_id}", status_code=204, response_class=Response)
async def delete_field(agent_id: uuid.UUID, field_id: uuid.UUID, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    field = await _owned_field(db, agent.id, field_id)
    await db.delete(field)
    await db.commit()
    return Response(status_code=204)


@router.put("/{agent_id}/fields/order", response_model=list[FieldRead])
async def reorder_fields(agent_id: uuid.UUID, payload: FieldOrder, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    if len(payload.field_ids) != len(set(payload.field_ids)):
        raise HTTPException(status_code=422, detail="field_ids must contain each field exactly once")
    fields = list(
        await db.scalars(
            select(AgentField)
            .where(AgentField.agent_id == agent.id)
            .order_by(AgentField.sort_order, AgentField.id)
            .with_for_update()
        )
    )
    field_by_id = {field.id: field for field in fields}
    if len(payload.field_ids) != len(fields) or set(payload.field_ids) != set(field_by_id):
        raise HTTPException(status_code=422, detail="field_ids must contain every field for this agent exactly once")
    if fields:
        offset = max(field.sort_order for field in fields) + len(fields) + 1
        for field in fields:
            field.sort_order += offset
        try:
            await db.flush()
            for position, field_id in enumerate(payload.field_ids):
                field_by_id[field_id].sort_order = position
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise _field_conflict()
    return [field_by_id[field_id] for field_id in payload.field_ids]


@router.get("/{agent_id}/outcomes", response_model=list[OutcomeRead])
async def list_outcomes(agent_id: uuid.UUID, db: Db, user: Reader):
    agent = await _owned_agent(db, user.id, agent_id)
    return list(
        await db.scalars(
            select(AgentOutcome).where(AgentOutcome.agent_id == agent.id).order_by(AgentOutcome.label, AgentOutcome.id)
        )
    )


@router.post("/{agent_id}/outcomes", response_model=OutcomeRead, status_code=201)
async def create_outcome(agent_id: uuid.UUID, payload: OutcomeCreate, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    outcome = AgentOutcome(agent_id=agent.id, **payload.model_dump(mode="json"))
    db.add(outcome)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _outcome_conflict()
    await db.refresh(outcome)
    return outcome


@router.patch("/{agent_id}/outcomes/{outcome_id}", response_model=OutcomeRead)
async def patch_outcome(
    agent_id: uuid.UUID,
    outcome_id: uuid.UUID,
    payload: OutcomePatch,
    db: Db,
    user: Writer,
):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    outcome = await _owned_outcome(db, agent.id, outcome_id)
    for name, value in payload.model_dump(mode="json", exclude_unset=True).items():
        setattr(outcome, name, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _outcome_conflict()
    await db.refresh(outcome)
    return outcome


@router.delete("/{agent_id}/outcomes/{outcome_id}", status_code=204, response_class=Response)
async def delete_outcome(agent_id: uuid.UUID, outcome_id: uuid.UUID, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    outcome = await _owned_outcome(db, agent.id, outcome_id)
    await db.delete(outcome)
    await db.commit()
    return Response(status_code=204)
