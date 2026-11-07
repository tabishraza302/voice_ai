from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import select

from .auth import COOKIE, decode_session_token
from .db import Session as DbSession
from .goals import (
    FieldCondition,
    GoalField,
    GoalOutcome,
    GoalState,
    apply_answers,
    build_tool_schema,
    evaluate_goals,
    reset_for_next_request,
)
from .models import User
from .sessions import (
    CallAnswer,
    CallMessage,
    CallRequest,
    CallSession,
    Db,
    SessionWriter,
    Writer,
    _TERMINAL_STATUSES,
    _frozen_call_snapshot,
    _locked_agent,
    record_session_outcome,
    set_session_status,
)

logger = logging.getLogger(__name__)

_CHAT_IDLE_TIMEOUT_SECONDS = 1800
_RECENT_TURNS = 12
_TOOL_FALLBACK = "Sorry, I didn't catch that. Could you please repeat it?"

chat_router = APIRouter(tags=["chat"])
_active_chats: dict[uuid.UUID, ChatRuntime] = {}


class ChatSessionCreated(BaseModel):
    session_id: uuid.UUID


def _chat_publish_errors(
    snapshot: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, list[str]]:
    settings = os.environ if env is None else env
    errors: dict[str, list[str]] = {}

    def add(field_name: str, message: str) -> None:
        errors.setdefault(field_name, []).append(message)

    agent_data = snapshot.get("agent")
    if not isinstance(agent_data, Mapping):
        add("agent", "Agent data is missing.")
        return errors

    llm_provider = agent_data.get("llm_provider")
    if llm_provider != "openai":
        add("llm_provider", "Only OpenAI language models are available.")

    if not settings.get("OPENAI_API_KEY", "").strip():
        add("llm_provider", "OpenAI is unavailable because OPENAI_API_KEY is not configured on the server.")

    llm_model = agent_data.get("llm_model")
    verified_models = {
        item.strip()
        for item in settings.get("VERIFIED_OPENAI_MODELS", "").split(",")
        if item.strip()
    }
    if not llm_model or not isinstance(llm_model, str) or not llm_model.strip():
        add("llm_model", "An LLM model is required.")
    elif verified_models and llm_model not in verified_models:
        add("llm_model", "Choose an OpenAI model in the server's verified model allowlist.")

    global_prompt = snapshot.get("global_prompt")
    prompt_text = global_prompt.get("text", "") if isinstance(global_prompt, Mapping) else ""
    if not prompt_text or not str(prompt_text).strip():
        add("global_prompt", "Global prompt must be configured.")

    return errors


@chat_router.post(
    "/agents/{agent_id}/chat-sessions",
    status_code=201,
    response_model=ChatSessionCreated,
)
async def create_chat_session(
    agent_id: uuid.UUID,
    db: Db,
    user: Writer,
    payload: CallRequest | None = None,
):
    agent = await _locked_agent(db, agent_id=agent_id, owner_id=user.id)
    if agent.status == "suspended":
        raise HTTPException(409, "Suspended agents cannot start calls")

    selected_language = payload.language if payload else None
    if not selected_language:
        selected_language = agent.default_language
    if not selected_language and agent.languages:
        selected_language = agent.languages[0]
    if not selected_language or not agent.languages or selected_language not in agent.languages:
        raise HTTPException(422, {"language": ["Choose an enabled call language"]})

    snapshot = await _frozen_call_snapshot(db, agent, selected_language)
    errors = _chat_publish_errors(snapshot)
    if errors:
        raise HTTPException(422, errors)

    call = CallSession(
        id=uuid.uuid4(),
        owner_id=agent.owner_id,
        agent_id=agent.id,
        call_type="chat_test",
        status="created",
        snapshot=snapshot,
        signaling_expires_at=None,
    )
    db.add(call)
    await db.commit()
    return ChatSessionCreated(session_id=call.id)


def _conditions_from_snapshot(value: Any) -> tuple[FieldCondition, ...]:
    conditions: list[FieldCondition] = []
    for condition in value or ():
        if not isinstance(condition, Mapping):
            continue
        field_key = condition.get("field_key")
        if not field_key:
            continue
        conditions.append(
            FieldCondition(
                field_key=str(field_key),
                operator=str(condition.get("operator", "eq")),
                operand=condition.get("operand"),
            )
        )
    return tuple(conditions)


def _fields_from_snapshot(snapshot: Mapping[str, Any]) -> tuple[GoalField, ...]:
    fields: list[GoalField] = []
    for row in snapshot.get("fields") or ():
        if not isinstance(row, Mapping) or not row.get("field_key"):
            continue
        field_key = str(row["field_key"])
        fields.append(
            GoalField(
                field_key=field_key,
                description=str(row.get("description") or field_key),
                options=tuple(str(option) for option in row.get("options") or ()),
                required=bool(row.get("required", False)),
                required_when=_conditions_from_snapshot(row.get("required_when")),
                sort_order=int(row.get("sort_order", 0)),
            )
        )
    return tuple(fields)


def _outcomes_from_snapshot(snapshot: Mapping[str, Any]) -> tuple[GoalOutcome, ...]:
    outcomes: list[GoalOutcome] = []
    for row in snapshot.get("outcomes") or ():
        if not isinstance(row, Mapping) or not row.get("label"):
            continue
        outcomes.append(
            GoalOutcome(
                label=str(row["label"]),
                conditions=_conditions_from_snapshot(row.get("conditions")),
                required_fields=tuple(str(key) for key in row.get("required_fields") or ()),
            )
        )
    return tuple(outcomes)


def _field_guidance(snapshot: Mapping[str, Any]) -> dict[str, str]:
    guidance: dict[str, str] = {}
    for row in snapshot.get("fields") or ():
        if isinstance(row, Mapping) and row.get("field_key"):
            guidance[str(row["field_key"])] = str(row.get("guidance") or "")
    return guidance


async def _load_confirmed_answers(session_id: uuid.UUID) -> list[CallAnswer]:
    async with DbSession() as db:
        return list(
            await db.scalars(
                select(CallAnswer)
                .where(CallAnswer.session_id == session_id, CallAnswer.accepted.is_(True))
                .order_by(CallAnswer.created_at.asc(), CallAnswer.id)
            )
        )


async def _load_session_outcomes(session_id: uuid.UUID) -> list[dict[str, Any]]:
    async with DbSession() as db:
        outcome = await db.scalar(select(CallSession.outcome).where(CallSession.id == session_id))
    return [dict(record) for record in outcome or () if isinstance(record, Mapping)]


async def _load_recent_conversation(session_id: uuid.UUID) -> deque[dict[str, str]]:
    async with DbSession() as db:
        messages = list(
            await db.scalars(
                select(CallMessage)
                .where(CallMessage.session_id == session_id)
                .order_by(CallMessage.sequence.desc())
                .limit(_RECENT_TURNS)
            )
        )
    messages.reverse()
    return deque(
        ({"role": message.role, "content": message.content} for message in messages),
        maxlen=_RECENT_TURNS,
    )


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


async def _restore_goal_state(
    session_id: uuid.UUID,
    fields: tuple[GoalField, ...],
    outcomes: tuple[GoalOutcome, ...],
    language: str,
) -> GoalState:
    """Rebuild answers and completed outcomes by replaying the session log."""
    valid_keys = {field.field_key for field in fields}
    events: list[tuple[datetime, int, str, Any]] = []
    for answer in await _load_confirmed_answers(session_id):
        if answer.field_key in valid_keys:
            events.append((_timestamp(answer.created_at), 1, "answer", answer))
    for record in await _load_session_outcomes(session_id):
        label = record.get("label")
        if isinstance(label, str) and label:
            events.append((_timestamp(record.get("completed_at")), 0, "outcome", label))
    events.sort(key=lambda event: (event[0], event[1]))

    answers: dict[str, Any] = {}
    completed: list[str] = []
    for _, _, kind, payload in events:
        if kind == "answer":
            answers[payload.field_key] = payload.value
            continue
        if payload in completed:
            continue
        completed.append(payload)
        # An outcome clears conditional fields; only universal fields survive.
        answers = {
            field.field_key: answers[field.field_key]
            for field in fields
            if field.required and field.field_key in answers
        }

    return GoalState(
        fields=fields,
        outcomes=outcomes,
        answers=answers,
        completed_outcomes=tuple(completed),
        language=language,
    )


@dataclass(slots=True)
class ChatRuntime:
    session_id: uuid.UUID
    agent_name: str
    agent_instructions: str
    global_prompt: str
    llm_model: str
    language: str
    goal_state: GoalState
    field_guidance: dict[str, str]
    recent_conversation: deque[dict[str, str]]
    writer: SessionWriter
    client: AsyncOpenAI
    idle_deadline: float
    closed: bool = False

    @classmethod
    async def from_snapshot(
        cls,
        session_id: uuid.UUID,
        snapshot: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
    ) -> ChatRuntime:
        settings = os.environ if env is None else env
        global_prompt_data = snapshot.get("global_prompt") or {}
        global_prompt = str(global_prompt_data.get("text", ""))
        agent_data = snapshot.get("agent") or {}
        agent_name = str(agent_data.get("name", ""))
        agent_instructions = str(agent_data.get("instructions", ""))
        llm_model = str(agent_data.get("llm_model", ""))
        llm_api_key = settings.get("OPENAI_API_KEY", "").strip()
        language = str(snapshot.get("language", "en"))

        fields = _fields_from_snapshot(snapshot)
        outcomes = _outcomes_from_snapshot(snapshot)
        goal_state = await _restore_goal_state(session_id, fields, outcomes, language)
        recent = await _load_recent_conversation(session_id)
        writer = SessionWriter(session_id)
        client = AsyncOpenAI(api_key=llm_api_key, timeout=30.0)
        idle_deadline = time.monotonic() + _CHAT_IDLE_TIMEOUT_SECONDS

        return cls(
            session_id=session_id,
            agent_name=agent_name,
            agent_instructions=agent_instructions,
            global_prompt=global_prompt,
            llm_model=llm_model,
            language=language,
            goal_state=goal_state,
            field_guidance=_field_guidance(snapshot),
            recent_conversation=recent,
            writer=writer,
            client=client,
            idle_deadline=idle_deadline,
        )

    def reset_idle_deadline(self) -> None:
        self.idle_deadline = time.monotonic() + _CHAT_IDLE_TIMEOUT_SECONDS

    def _system_message(self, directive: str | None = None) -> dict[str, str]:
        evaluation = evaluate_goals(self.goal_state)
        lines: list[str] = []
        if self.global_prompt.strip():
            lines.append(self.global_prompt.strip())
        if self.agent_instructions.strip():
            lines.append(self.agent_instructions.strip())
        lines.append(f"Respond in language: {self.language}.")
        if evaluation.needed_fields:
            lines.append("Information to collect in this turn:")
            for field in evaluation.needed_fields:
                detail = f"- {field.field_key}: {field.description}"
                guidance = self.field_guidance.get(field.field_key)
                if guidance:
                    detail = f"{detail} {guidance}"
                if field.options:
                    detail = f"{detail} Options: {', '.join(field.options)}."
                lines.append(detail)
        if evaluation.correction_fields:
            lines.append(
                "The caller may correct previously confirmed information; update it when they do."
            )
        if self.goal_state.answers:
            lines.append(
                "Confirmed answers: "
                + json.dumps(dict(self.goal_state.answers), ensure_ascii=False)
            )
        if directive:
            lines.append(directive)
        return {"role": "system", "content": "\n".join(lines)}

    def _messages(self, directive: str | None = None) -> list[dict[str, Any]]:
        return [self._system_message(directive), *self.recent_conversation]

    async def _first_pass(
        self,
        messages: list[dict[str, Any]],
        tool_schema: dict[str, Any] | None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        kwargs: dict[str, Any] = {
            "model": self.llm_model,
            "messages": messages,
            "stream": True,
        }
        if tool_schema is not None:
            kwargs["tools"] = [tool_schema]
        response = await self.client.chat.completions.create(**kwargs)

        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if content:
                content_parts.append(content)
            for tool_delta in getattr(delta, "tool_calls", None) or ():
                index = int(getattr(tool_delta, "index", 0) or 0)
                entry = tool_calls.setdefault(
                    index, {"id": None, "name": None, "arguments": ""}
                )
                call_id = getattr(tool_delta, "id", None)
                if call_id:
                    entry["id"] = call_id
                function = getattr(tool_delta, "function", None)
                if function is not None:
                    name = getattr(function, "name", None)
                    if name:
                        entry["name"] = name
                    arguments = getattr(function, "arguments", None)
                    if arguments:
                        entry["arguments"] = f"{entry['arguments']}{arguments}"
        return content_parts, [tool_calls[index] for index in sorted(tool_calls)]

    async def _stream_second_pass(self, websocket: WebSocket, messages: list[dict[str, Any]]) -> None:
        response = await self.client.chat.completions.create(
            model=self.llm_model,
            messages=messages,
            stream=True,
        )
        parts: list[str] = []
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if not content:
                continue
            parts.append(content)
            await websocket.send_json({"type": "token", "content": content})
        text = "".join(parts)
        if not text.strip():
            text = "I'm here. What else can I help you with?"
            await websocket.send_json({"type": "token", "content": text})
        await self._finish_turn(websocket, text)

    async def _finish_turn(self, websocket: WebSocket, text: str) -> None:
        self.writer.enqueue_message("assistant", text)
        self.recent_conversation.append({"role": "assistant", "content": text})
        try:
            await self.writer.drain()
        except Exception:
            logger.exception("Failed to persist chat turn")
        await websocket.send_json({"type": "turn_complete", "content": text})

    async def _tool_failure(self, websocket: WebSocket, message: str) -> bool:
        await websocket.send_json({"type": "warning", "message": message})
        await websocket.send_json({"type": "token", "content": _TOOL_FALLBACK})
        await self._finish_turn(websocket, _TOOL_FALLBACK)
        return False

    async def _handle_tool_calls(
        self,
        websocket: WebSocket,
        messages: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
    ) -> bool:
        save_call = next(
            (call for call in tool_calls if call.get("name") == "save_answers"),
            None,
        )
        if save_call is None:
            return await self._tool_failure(websocket, "The assistant used an unsupported tool.")

        arguments = save_call.get("arguments") or ""
        try:
            parsed = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(parsed, dict):
                raise ValueError("tool arguments must be a JSON object")
            valid_keys = {field.field_key for field in self.goal_state.fields}
            saved = {
                str(key): value
                for key, value in parsed.items()
                if key in valid_keys and value is not None
            }
            self.goal_state = apply_answers(self.goal_state, saved)
            for key, value in saved.items():
                self.writer.enqueue_answer(
                    key,
                    value,
                    True,
                    {"reason": "accepted", "validation_errors": []},
                )
            evaluation = evaluate_goals(self.goal_state)
        except Exception:
            logger.exception("Failed to process save_answers tool call")
            return await self._tool_failure(websocket, "I couldn't save that information.")

        reached = evaluation.reached_outcome
        confirmed_answers = dict(self.goal_state.answers)
        directive: str | None = None
        if reached is not None:
            directive = (
                f"The outcome '{reached}' has been reached. Briefly confirm it to the caller, "
                "then ask if there is anything else you can help with."
            )
            try:
                await self.writer.drain()
                await record_session_outcome(self.session_id, reached)
            except Exception:
                logger.exception("Failed to persist outcome %s", reached)
            await websocket.send_json({"type": "outcome_reached", "outcome": reached})
            self.goal_state = reset_for_next_request(self.goal_state, reached)

        payload: dict[str, Any] = {
            "saved": saved,
            "confirmed_answers": confirmed_answers,
            "needed_fields": [
                {
                    "field_key": field.field_key,
                    "description": field.description,
                    "options": list(field.options),
                }
                for field in evaluation.needed_fields
            ],
            "outcome": reached,
        }
        if directive is not None:
            payload["instruction"] = directive

        call_id = save_call.get("id") or "call_0"
        second_messages = list(messages)
        second_messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "save_answers",
                            "arguments": arguments,
                        },
                    }
                ],
            }
        )
        second_messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(payload, ensure_ascii=False),
            }
        )
        if directive is not None:
            second_messages.append({"role": "system", "content": directive})

        await self._stream_second_pass(websocket, second_messages)
        return False

    async def _run_turn(self, websocket: WebSocket) -> bool:
        evaluation = evaluate_goals(self.goal_state)
        tool_schema = build_tool_schema(evaluation)
        messages = self._messages()
        content_parts, tool_calls = await self._first_pass(messages, tool_schema)
        if tool_calls:
            return await self._handle_tool_calls(websocket, messages, tool_calls)

        text = "".join(content_parts)
        if not text.strip():
            text = "I'm here. What else can I help you with?"
            content_parts = [text]
        for part in content_parts:
            await websocket.send_json({"type": "token", "content": part})
        await self._finish_turn(websocket, text)
        return False

    async def send_greeting(self, websocket: WebSocket) -> bool:
        if self.recent_conversation:
            return False
        return await self._run_turn(websocket)

    async def handle_user_message(self, websocket: WebSocket, text: str) -> bool:
        transcript = text.strip()
        if not transcript:
            return False

        self.reset_idle_deadline()
        self.writer.enqueue_message("user", transcript)
        self.recent_conversation.append({"role": "user", "content": transcript})
        return await self._run_turn(websocket)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.writer.flush()
        except Exception:
            logger.exception("Failed to flush chat session writer")
        try:
            await self.client.close()
        except Exception:
            pass


@chat_router.websocket("/agents/{agent_id}/chat-sessions/{session_id}/ws")
async def chat_websocket(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
):
    token = websocket.cookies.get(COOKIE, "")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Authentication required")
        return
    try:
        data = decode_session_token(token)
        user_id = uuid.UUID(data["sub"])
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Authentication required")
        return

    async with DbSession() as db:
        user = await db.get(User, user_id)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Authentication required")
            return
        session = await db.scalar(
            select(CallSession).where(
                CallSession.id == session_id,
                CallSession.agent_id == agent_id,
                CallSession.owner_id == user.id,
            )
        )
        if session is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Session not found")
            return
        if session.status in _TERMINAL_STATUSES:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Session is closed")
            return

    if session_id in _active_chats:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Session is already active")
        return

    await websocket.accept()

    if session.status == "created":
        await set_session_status(session_id, "active")

    try:
        runtime = await ChatRuntime.from_snapshot(session_id, session.snapshot)
    except Exception as error:
        await websocket.send_json({"type": "error", "message": f"Failed to initialize chat runtime: {error}"})
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    _active_chats[session_id] = runtime

    async def _idle_monitor():
        try:
            while True:
                now = time.monotonic()
                remaining = runtime.idle_deadline - now
                if remaining <= 0:
                    await set_session_status(session_id, "expired")
                    try:
                        await websocket.send_json({"type": "session_ended", "reason": "expired"})
                        await websocket.close()
                    except Exception:
                        pass
                    break
                await asyncio.sleep(min(remaining, 5.0))
        except asyncio.CancelledError:
            pass

    idle_task = asyncio.create_task(_idle_monitor())

    try:
        if not runtime.recent_conversation:
            try:
                await runtime.send_greeting(websocket)
            except Exception as error:
                logger.exception("Initial greeting failed")
                await websocket.send_json(
                    {"type": "warning", "message": f"Initial greeting failed: {error}"}
                )

        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            msg_type = data.get("type")
            if msg_type == "message":
                content = str(data.get("content", ""))
                try:
                    terminal = await runtime.handle_user_message(websocket, content)
                    if terminal:
                        break
                except Exception as error:
                    logger.exception("Chat turn failed")
                    await websocket.send_json({"type": "warning", "message": str(error)})
            elif msg_type == "end":
                await set_session_status(session_id, "cancelled")
                try:
                    await websocket.send_json({"type": "session_ended", "reason": "cancelled"})
                    await websocket.close()
                except Exception:
                    pass
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Chat websocket failed")
    finally:
        idle_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await idle_task

        _active_chats.pop(session_id, None)
        await runtime.close()

        async with DbSession() as db:
            call = await db.get(CallSession, session_id)
            if call and call.status == "active":
                await set_session_status(session_id, "cancelled")
