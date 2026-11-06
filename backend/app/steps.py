import uuid
from collections.abc import Iterable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from .auth import csrf, current_user
from .db import Base, get_db
from .models import User

class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("agent_id", "field_key", name="steps_agent_field_key_key"),
        UniqueConstraint("agent_id", "sort_order", name="steps_agent_sort_order_key"),
        CheckConstraint("sort_order >= 0", name="steps_sort_order_check"),
        CheckConstraint("field_key IS NULL OR btrim(field_key) <> ''", name="steps_field_key_nonempty_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default=text("''"))
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    branches: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )


class BranchCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_key: str | None = None
    operator: str | None = None
    operand: Any = None


class StepBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conditions: list[BranchCondition] = Field(default_factory=list)
    target_step_id: uuid.UUID | None = None
    outcome: str | None = None


class StepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_key: str | None = Field(default=None, max_length=128)
    title: str = Field(default="", max_length=255)
    objective: str = ""
    branches: list[StepBranch] = Field(default_factory=list)

    @field_validator("field_key")
    @classmethod
    def normalize_field_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("field_key must not be empty")
        return value

class StepCreate(StepInput):
    sort_order: int | None = Field(default=None, ge=0)


class StepPatch(StepInput):
    pass


class StepRead(StepInput):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    agent_id: uuid.UUID
    sort_order: int


class StepOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_ids: list[uuid.UUID]


router = APIRouter(prefix="/agents", tags=["steps"])
Db = Annotated[AsyncSession, Depends(get_db)]
Reader = Annotated[User, Depends(current_user)]
Writer = Annotated[User, Depends(csrf)]


def _step_path(step: Any, index: int) -> str:
    step_id = getattr(step, "id", None)
    return f"steps.{step_id}" if step_id is not None else f"steps.{index}"


def _add_error(errors: dict[str, list[str]], field: str, message: str) -> None:
    errors.setdefault(field, []).append(message)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _cycle_nodes(graph: dict[uuid.UUID, set[uuid.UUID]]) -> set[uuid.UUID]:
    state: dict[uuid.UUID, int] = {}
    active_index: dict[uuid.UUID, int] = {}
    path: list[uuid.UUID] = []
    cyclic: set[uuid.UUID] = set()

    for start in graph:
        if state.get(start, 0) != 0:
            continue
        state[start] = 1
        active_index[start] = len(path)
        path.append(start)
        stack: list[tuple[uuid.UUID, Iterable[uuid.UUID]]] = [(start, iter(graph[start]))]
        while stack:
            node, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                stack.pop()
                path.pop()
                active_index.pop(node)
                state[node] = 2
            elif target not in graph:
                continue
            elif state.get(target, 0) == 1:
                cyclic.update(path[active_index[target] :])
            elif state.get(target, 0) == 0:
                state[target] = 1
                active_index[target] = len(path)
                path.append(target)
                stack.append((target, iter(graph[target])))
    return cyclic


def validate_publish(agent: Any, steps: Iterable[Step]) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    rows = list(steps)
    indexed = list(enumerate(rows))

    def sort_key(pair: tuple[int, Step]) -> tuple[int, int]:
        index, step = pair
        order = getattr(step, "sort_order", None)
        return (order, index) if isinstance(order, int) and not isinstance(order, bool) else (index, index)

    ordered = [step for _, step in sorted(indexed, key=sort_key)]
    if not ordered:
        _add_error(errors, "steps", "at least one step is required before publishing")
        return errors

    seen_ids: set[uuid.UUID] = set()
    field_keys: dict[str, list[tuple[Step, int]]] = {}
    sort_orders: dict[int, list[tuple[Step, int]]] = {}
    for index, step in enumerate(ordered):
        path = _step_path(step, index)
        step_id = _as_uuid(getattr(step, "id", None))
        if step_id is not None:
            if step_id in seen_ids:
                _add_error(errors, f"{path}.id", "must be unique within the agent")
            seen_ids.add(step_id)
        field_key = getattr(step, "field_key", None)
        if not isinstance(field_key, str) or not field_key.strip():
            _add_error(errors, f"{path}.field_key", "is required")
        else:
            field_keys.setdefault(field_key, []).append((step, index))
        order = getattr(step, "sort_order", None)
        if not isinstance(order, int) or isinstance(order, bool) or order < 0:
            _add_error(errors, f"{path}.sort_order", "must be a non-negative integer")
        else:
            sort_orders.setdefault(order, []).append((step, index))

    for duplicates in field_keys.values():
        if len(duplicates) > 1:
            for step, index in duplicates:
                _add_error(errors, f"{_step_path(step, index)}.field_key", "must be unique within the agent")
    for duplicates in sort_orders.values():
        if len(duplicates) > 1:
            for step, index in duplicates:
                _add_error(errors, f"{_step_path(step, index)}.sort_order", "must be unique within the agent")

    steps_by_id = {
        step_id: step
        for step in ordered
        if (step_id := _as_uuid(getattr(step, "id", None))) is not None
    }

    graph: dict[uuid.UUID, set[uuid.UUID]] = {}
    for position, step in enumerate(ordered):
        path = _step_path(step, position)
        step_id = _as_uuid(getattr(step, "id", None))
        if step_id is not None:
            graph.setdefault(step_id, set())

        if not isinstance(getattr(step, "title", None), str) or not step.title.strip():
            _add_error(errors, f"{path}.title", "is required")
        if not isinstance(getattr(step, "objective", None), str) or not step.objective.strip():
            _add_error(errors, f"{path}.objective", "is required")

        branches = getattr(step, "branches", None)
        if not isinstance(branches, list):
            _add_error(errors, f"{path}.branches", "must be an ordered list")
            branches = []
        has_unconditional = False
        for branch_index, branch in enumerate(branches):
            if not isinstance(branch, dict):
                _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} must be an object")
                continue
            conditions = branch.get("conditions", [])
            if not isinstance(conditions, list):
                _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} conditions must be a list")
                conditions = []
            branch_can_route = bool(conditions) or not has_unconditional
            if not conditions:
                has_unconditional = True
            for condition_index, condition in enumerate(conditions):
                if not isinstance(condition, dict):
                    _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} condition {condition_index + 1} must be an object")
                    continue
                condition_operator = condition.get("operator")
                if not isinstance(condition_operator, str) or condition_operator not in {
                    "eq",
                    "ne",
                    "gt",
                    "gte",
                    "lt",
                    "lte",
                    "in",
                }:
                    _add_error(
                        errors,
                        f"{path}.branches",
                        f"branch {branch_index + 1} condition {condition_index + 1} has an unsupported operator",
                    )
                condition_key = condition.get("field_key")
                if not isinstance(condition_key, str) or not condition_key.strip():
                    _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} condition {condition_index + 1} needs a field_key")
                    continue
                matches = field_keys.get(condition_key, [])
                if not matches:
                    _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} references missing field_key '{condition_key}'")
                    continue
                if len(matches) != 1:
                    _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} references a non-unique field_key '{condition_key}'")
                    continue
                if "operand" not in condition or condition.get("operand") is None:
                    _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} condition {condition_index + 1} needs an operand")
                    continue

            target_id_value = branch.get("target_step_id")
            outcome_value = branch.get("outcome")
            has_target_id = target_id_value is not None
            has_outcome = outcome_value is not None
            if has_target_id == has_outcome:
                _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} must have exactly one target_step_id or outcome")
                continue
            if has_target_id:
                target_id = _as_uuid(target_id_value)
                target = steps_by_id.get(target_id) if target_id is not None else None
                if target is None:
                    _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} targets a missing step")
                elif target_id in steps_by_id and branch_can_route and step_id is not None:
                    graph[step_id].add(target_id)
            elif not isinstance(outcome_value, str) or not outcome_value.strip():
                _add_error(errors, f"{path}.branches", f"branch {branch_index + 1} outcome must not be empty")

        if not has_unconditional and position + 1 < len(ordered) and step_id is not None:
            next_id = _as_uuid(getattr(ordered[position + 1], "id", None))
            if next_id is not None:
                graph[step_id].add(next_id)

    for cyclic_step_id in _cycle_nodes(graph):
        step = steps_by_id.get(cyclic_step_id)
        if step is not None:
            index = next((i for i, item in enumerate(ordered) if item is step), 0)
            _add_error(errors, f"{_step_path(step, index)}.branches", "creates a cycle in the step flow")
    return errors


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


async def _owned_step(db: AsyncSession, agent_id: uuid.UUID, step_id: uuid.UUID) -> Step:
    step = await db.scalar(select(Step).where(Step.agent_id == agent_id, Step.id == step_id))
    if step is None:
        raise HTTPException(status_code=404, detail="Step not found")
    return step


def _integrity_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="A step with that field key or order already exists")


async def _validate_published_step_edit(db: AsyncSession, agent: Any) -> None:
    """Keep a published agent publishable after any of its steps change."""
    if agent.status != "published":
        return
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise _integrity_conflict()
    steps = list(await db.scalars(select(Step).where(Step.agent_id == agent.id)))
    errors = validate_publish(agent, steps)
    if errors:
        await db.rollback()
        raise HTTPException(status_code=422, detail=errors)


@router.get("/{agent_id}/steps", response_model=list[StepRead])
async def list_steps(agent_id: uuid.UUID, db: Db, user: Reader):
    agent = await _owned_agent(db, user.id, agent_id)
    return list(
        await db.scalars(
            select(Step).where(Step.agent_id == agent.id).order_by(Step.sort_order, Step.id)
        )
    )


@router.post("/{agent_id}/steps", response_model=StepRead, status_code=201)
async def create_step(agent_id: uuid.UUID, payload: StepCreate, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    data = payload.model_dump(mode="json")
    order = data.pop("sort_order")
    if order is None:
        current_max = await db.scalar(select(func.max(Step.sort_order)).where(Step.agent_id == agent.id))
        order = 0 if current_max is None else current_max + 1
    step = Step(agent_id=agent.id, sort_order=order, **data)
    db.add(step)
    await _validate_published_step_edit(db, agent)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _integrity_conflict()
    await db.refresh(step)
    return step


@router.patch("/{agent_id}/steps/{step_id}", response_model=StepRead)
async def patch_step(agent_id: uuid.UUID, step_id: uuid.UUID, payload: StepPatch, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    step = await _owned_step(db, agent.id, step_id)
    for field, value in payload.model_dump(mode="json", exclude_unset=True).items():
        setattr(step, field, value)
    await _validate_published_step_edit(db, agent)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _integrity_conflict()
    await db.refresh(step)
    return step


@router.delete("/{agent_id}/steps/{step_id}", status_code=204, response_class=Response)
async def delete_step(agent_id: uuid.UUID, step_id: uuid.UUID, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    step = await _owned_step(db, agent.id, step_id)
    await db.delete(step)
    await _validate_published_step_edit(db, agent)
    await db.commit()
    return Response(status_code=204)


@router.put("/{agent_id}/steps/order", response_model=list[StepRead])
async def reorder_steps(agent_id: uuid.UUID, payload: StepOrder, db: Db, user: Writer):
    agent = await _owned_agent(db, user.id, agent_id)
    await _lock_agent(db, agent.id)
    if len(payload.step_ids) != len(set(payload.step_ids)):
        raise HTTPException(status_code=422, detail="step_ids must contain each step exactly once")
    steps = list(
        await db.scalars(
            select(Step)
            .where(Step.agent_id == agent.id)
            .order_by(Step.sort_order, Step.id)
            .with_for_update()
        )
    )
    step_by_id = {step.id: step for step in steps}
    if len(payload.step_ids) != len(steps) or set(payload.step_ids) != set(step_by_id):
        raise HTTPException(status_code=422, detail="step_ids must contain every step for this agent exactly once")
    if steps:
        offset = max(step.sort_order for step in steps) + len(steps) + 1
        for step in steps:
            step.sort_order += offset
        try:
            await db.flush()
            for position, step_id in enumerate(payload.step_ids):
                step_by_id[step_id].sort_order = position
        except IntegrityError:
            await db.rollback()
            raise _integrity_conflict()
    try:
        await _validate_published_step_edit(db, agent)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _integrity_conflict()
    return [step_by_id[step_id] for step_id in payload.step_ids]
