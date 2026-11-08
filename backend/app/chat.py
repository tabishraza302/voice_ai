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
from .models import User
from .sessions import (
    CallMessage,
    CallRequest,
    CallSession,
    Db,
    SessionWriter,
    Writer,
    _TERMINAL_STATUSES,
    _frozen_call_snapshot,
    _locked_agent,
    set_session_status,
)

logger = logging.getLogger(__name__)

_CHAT_IDLE_TIMEOUT_SECONDS = 1800
_RECENT_TURNS = 12

_END_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "end_call",
        "description": "End the call or chat session immediately. You MUST call this whenever the conversation reaches its conclusion, the user confirms they no longer need help, or the user says goodbye. Deliver your final closing sign-off in the message parameter or in your response so the call terminates directly after the closing message.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The final polite goodbye or closing sign-off message to deliver to the user before ending the session (e.g. 'Thank you for calling. Have a great day!').",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason for ending the session (e.g. booking confirmed, inquiry resolved, caller no longer needs help, caller said goodbye).",
                },
            },
            "required": [],
        },
    },
}

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


@dataclass(slots=True)
class ChatRuntime:
    session_id: uuid.UUID
    agent_name: str
    agent_instructions: str
    global_prompt: str
    llm_model: str
    language: str
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
            recent_conversation=recent,
            writer=writer,
            client=client,
            idle_deadline=idle_deadline,
        )

    def reset_idle_deadline(self) -> None:
        self.idle_deadline = time.monotonic() + _CHAT_IDLE_TIMEOUT_SECONDS

    def _system_message(self, directive: str | None = None) -> dict[str, str]:
        lines: list[str] = []
        if self.global_prompt.strip():
            lines.append(self.global_prompt.strip())
        if self.agent_instructions.strip():
            lines.append(self.agent_instructions.strip())
        lines.append(f"Respond in language: {self.language}.")
        lines.append(
            "Call Termination Rule: Once the user confirms they no longer need help, says goodbye, "
            "or the conversation reaches its end state, you MUST call the end_call tool immediately. "
            "Deliver your final polite closing message (either via the message argument of end_call or "
            "in your response) and end the session directly. Never wait for another user response after "
            "delivering the closing message."
        )
        if directive:
            lines.append(directive)
        return {"role": "system", "content": "\n\n".join(lines)}
    def _messages(self, directive: str | None = None) -> list[dict[str, Any]]:
        return [self._system_message(directive), *self.recent_conversation]

    async def _finish_turn(self, websocket: WebSocket, text: str) -> None:
        self.writer.enqueue_message("assistant", text)
        self.recent_conversation.append({"role": "assistant", "content": text})
        try:
            await self.writer.drain()
        except Exception:
            logger.exception("Failed to persist chat turn")
        await websocket.send_json({"type": "turn_complete", "content": text})

    async def _run_turn(self, websocket: WebSocket) -> bool:
        messages = self._messages()
        response = await self.client.chat.completions.create(
            model=self.llm_model,
            messages=messages,
            tools=[_END_CALL_TOOL],
            tool_choice="auto",
            stream=True,
        )
        content_parts: list[str] = []
        tool_call_args_parts: list[str] = []
        end_call_invoked = False
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if content:
                content_parts.append(content)
                await websocket.send_json({"type": "token", "content": content})

            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                    fn_name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
                    if fn_name == "end_call":
                        end_call_invoked = True
                    args_chunk = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
                    if args_chunk:
                        tool_call_args_parts.append(args_chunk)

        text = "".join(content_parts)
        if end_call_invoked:
            raw_args = "".join(tool_call_args_parts)
            try:
                parsed_args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                parsed_args = {}
            message = parsed_args.get("message") if isinstance(parsed_args, dict) else None
            if isinstance(message, str) and message.strip():
                farewell = message.strip()
                if not text.rstrip().endswith(farewell):
                    closing_text = ("\n\n" if text.strip() else "") + farewell
                    text += closing_text
                    await websocket.send_json({"type": "token", "content": closing_text})
            if not text.strip():
                text = "Thank you. Have a great day!"
                await websocket.send_json({"type": "token", "content": text})
        elif not text.strip():
            text = "I'm here. How can I help you?"
            await websocket.send_json({"type": "token", "content": text})

        await self._finish_turn(websocket, text)

        if end_call_invoked:
            await set_session_status(self.session_id, "completed")
            await websocket.send_json({"type": "session_ended", "reason": "completed"})
            close_fn = getattr(websocket, "close", None)
            if callable(close_fn):
                try:
                    await websocket.close()
                except Exception:
                    pass
            return True

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
                terminal = await runtime.send_greeting(websocket)
                if terminal:
                    return
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
