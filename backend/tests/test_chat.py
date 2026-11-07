import asyncio
import json
import os
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.testclient import TestClient

from app.agents import Agent
from app.auth import COOKIE
from app.chat import ChatRuntime, _active_chats, _chat_publish_errors
from app.db import Base, Session, engine
from app.goals import GoalState
from app.main import app
from app.models import User
from app.sessions import CallAnswer, CallMessage, CallSession


class MockStream:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        async def gen():
            for item in self.items:
                yield item
        return gen()


class FakeWebSocket:
    def __init__(self):
        self.events = []

    async def send_json(self, data):
        self.events.append(data)


class FakeWriter:
    def __init__(self):
        self.messages = []
        self.answers = []
        self.drained = 0
        self.flushed = False
        self.on_drain = None

    def enqueue_message(self, role, content):
        self.messages.append((role, content))

    def enqueue_answer(self, field_key, value, accepted, validation_result):
        self.answers.append((field_key, value, accepted, validation_result))

    async def drain(self):
        self.drained += 1
        if self.on_drain is not None:
            self.on_drain()

    async def flush(self):
        self.flushed = True


def content_chunk(text):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
            )
        ]
    )


def tool_chunk(*, arguments=None, call_id=None, name=None, index=0):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=index,
                            id=call_id,
                            function=SimpleNamespace(name=name, arguments=arguments),
                        )
                    ],
                )
            )
        ]
    )


def condition(field_key, operator="eq", operand=None):
    return {"field_key": field_key, "operator": operator, "operand": operand}


def field_row(
    field_key,
    *,
    description=None,
    guidance=None,
    options=(),
    required=False,
    required_when=(),
    sort_order=0,
):
    return {
        "id": str(uuid.uuid4()),
        "field_key": field_key,
        "description": f"Collect {field_key}" if description is None else description,
        "guidance": f"Ask for {field_key}." if guidance is None else guidance,
        "options": list(options),
        "required": required,
        "required_when": list(required_when),
        "sort_order": sort_order,
    }


def outcome_row(label, *, conditions=(), required_fields=()):
    return {
        "id": str(uuid.uuid4()),
        "label": label,
        "conditions": list(conditions),
        "required_fields": list(required_fields),
    }


def chat_snapshot(*, fields=(), outcomes=()):
    return {
        "global_prompt": {"text": "You are a helpful voice assistant.", "version": 1},
        "policy": {
            "max_call_duration_seconds": 900,
            "max_concurrent_calls_per_agent": 2,
            "max_concurrent_calls_overall": 8,
        },
        "language": "en",
        "agent": {
            "id": str(uuid.uuid4()),
            "name": "Chat Agent",
            "slug": "chat-agent",
            "description": "",
            "instructions": "Help the caller.",
            "languages": ["en"],
            "default_language": "en",
            "stt_provider": None,
            "stt_model": None,
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "tts_provider": None,
            "tts_model": None,
            "voice_id": None,
            "status": "draft",
        },
        "fields": list(fields),
        "outcomes": list(outcomes),
    }


async def make_runtime(snapshot, responses):
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=responses)
    client.close = AsyncMock()
    with patch("app.chat.AsyncOpenAI", return_value=client):
        runtime = await ChatRuntime.from_snapshot(
            uuid.uuid4(),
            snapshot,
            env={"OPENAI_API_KEY": "test-key", "VERIFIED_OPENAI_MODELS": "gpt-4o-mini"},
        )
    await runtime.writer.flush()
    runtime.writer = FakeWriter()
    return runtime, client


def receive_until(ws, message_type, limit=40):
    seen = []
    for _ in range(limit):
        message = ws.receive_json()
        seen.append(message)
        if message["type"] == message_type:
            return seen
    raise AssertionError(f"Did not receive {message_type!r}; saw {seen!r}")


async def load_chat_persistence(session_id):
    async with Session() as db:
        call = await db.get(CallSession, uuid.UUID(session_id))
        assert call is not None
        answer = await db.scalar(
            select(CallAnswer).where(
                CallAnswer.session_id == call.id,
                CallAnswer.field_key == "user_name",
            )
        )
        messages = list(
            await db.scalars(
                select(CallMessage)
                .where(CallMessage.session_id == call.id)
                .order_by(CallMessage.sequence)
            )
        )
        return (
            None if answer is None else answer.value,
            None if answer is None else answer.accepted,
            [message.role for message in messages],
        )


async def load_session_outcome(session_id):
    async with Session() as db:
        call = await db.get(CallSession, uuid.UUID(session_id))
        assert call is not None
        return call.status, call.outcome


@pytest.fixture
async def client():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:3000") as test_client:
        yield test_client


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def login(client: AsyncClient, email: str) -> str:
    password = "correct horse battery staple"
    await client.post("/auth/register", json={"email": email, "password": password})
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def mutation_headers(token: str) -> dict[str, str]:
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": token}


async def create_agent(client: AsyncClient, token: str, slug: str) -> dict:
    response = await client.post(
        "/agents",
        headers=mutation_headers(token),
        json={
            "name": "Chat Agent",
            "slug": slug,
            "instructions": "Test chat instructions.",
            "languages": ["en"],
            "default_language": "en",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_chat_publish_errors_validates_only_llm():
    snapshot = {
        "global_prompt": {"text": "You are a helpful voice assistant."},
        "agent": {
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "stt_provider": None,
            "tts_provider": None,
        },
    }
    env = {
        "OPENAI_API_KEY": "test-key",
        "VERIFIED_OPENAI_MODELS": "gpt-4o-mini,gpt-4.1-mini",
    }
    errors = _chat_publish_errors(snapshot, env)
    assert not errors

    errors_no_key = _chat_publish_errors(snapshot, {"OPENAI_API_KEY": ""})
    assert "llm_provider" in errors_no_key

    snapshot_bad_provider = {
        "global_prompt": {"text": "Prompt"},
        "agent": {"llm_provider": "anthropic", "llm_model": "claude"},
    }
    errors_bad_provider = _chat_publish_errors(snapshot_bad_provider, env)
    assert "llm_provider" in errors_bad_provider

    snapshot_unverified = {
        "global_prompt": {"text": "Prompt"},
        "agent": {"llm_provider": "openai", "llm_model": "gpt-99"},
    }
    errors_unverified = _chat_publish_errors(snapshot_unverified, env)
    assert "llm_model" in errors_unverified

    snapshot_no_prompt = {
        "global_prompt": {"text": " "},
        "agent": {"llm_provider": "openai", "llm_model": "gpt-4o-mini"},
    }
    errors_no_prompt = _chat_publish_errors(snapshot_no_prompt, env)
    assert "global_prompt" in errors_no_prompt


@pytest.mark.anyio
async def test_create_chat_session_persists_session(client: AsyncClient):
    token = await login(client, "user@example.com")
    agent = await create_agent(client, token, "chat-test-agent")

    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "VERIFIED_OPENAI_MODELS": "gpt-4o-mini"}):
        response = await client.post(
            f"/agents/{agent['id']}/chat-sessions",
            headers=mutation_headers(token),
            json={},
        )
        assert response.status_code == 201
        data = response.json()
        assert "session_id" in data
        session_id = uuid.UUID(data["session_id"])

        async with Session() as db:
            call = await db.get(CallSession, session_id)
            assert call is not None
            assert call.call_type == "chat_test"
            assert call.status == "created"
            assert call.signaling_expires_at is None
            assert call.agent_id == uuid.UUID(agent["id"])


@pytest.mark.anyio
async def test_create_chat_session_checks_suspended_agent(client: AsyncClient):
    token = await login(client, "user2@example.com")
    agent = await create_agent(client, token, "suspended-agent")

    async with Session() as db:
        agent_row = await db.get(Agent, uuid.UUID(agent["id"]))
        agent_row.status = "suspended"
        await db.commit()

    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "VERIFIED_OPENAI_MODELS": "gpt-4o-mini"}):
        response = await client.post(
            f"/agents/{agent['id']}/chat-sessions",
            headers=mutation_headers(token),
            json={},
        )
        assert response.status_code == 409
        assert "Suspended" in response.text


@pytest.mark.anyio
async def test_runtime_from_snapshot_builds_goal_state():
    snapshot = chat_snapshot(
        fields=[
            field_row(
                "intent",
                options=["order", "cancel"],
                required=True,
                sort_order=0,
            ),
            field_row(
                "items",
                required_when=[condition("intent", "eq", "order")],
                sort_order=1,
            ),
        ],
        outcomes=[
            outcome_row(
                "order_placed",
                conditions=[condition("intent", "eq", "order")],
                required_fields=["items"],
            )
        ],
    )

    runtime, _ = await make_runtime(snapshot, [])

    assert isinstance(runtime.goal_state, GoalState)
    assert [field.field_key for field in runtime.goal_state.fields] == ["intent", "items"]
    assert runtime.goal_state.fields[0].options == ("order", "cancel")
    assert runtime.goal_state.fields[1].required_when[0].field_key == "intent"
    assert runtime.goal_state.outcomes[0].required_fields == ("items",)
    assert runtime.goal_state.answers == {}
    await runtime.close()


@pytest.mark.anyio
async def test_handle_user_message_streams_text_when_model_skips_tool():
    snapshot = chat_snapshot(fields=[field_row("user_name", required=True)])
    runtime, client = await make_runtime(
        snapshot,
        [MockStream([content_chunk("Hello! "), content_chunk("How can I help?")])],
    )
    websocket = FakeWebSocket()

    terminal = await runtime.handle_user_message(websocket, "Hi there")

    assert terminal is False
    assert [event for event in websocket.events if event["type"] == "token"] == [
        {"type": "token", "content": "Hello! "},
        {"type": "token", "content": "How can I help?"},
    ]
    assert websocket.events[-1] == {"type": "turn_complete", "content": "Hello! How can I help?"}
    assert runtime.goal_state.answers == {}
    assert runtime.writer.messages[-1] == ("assistant", "Hello! How can I help?")
    call = client.chat.completions.create.await_args_list[0]
    assert call.kwargs["tools"][0]["function"]["name"] == "save_answers"
    assert "user_name" in call.kwargs["tools"][0]["function"]["parameters"]["properties"]
    await runtime.close()


@pytest.mark.anyio
async def test_handle_user_message_saves_tool_answers_and_runs_second_pass():
    snapshot = chat_snapshot(fields=[field_row("user_name", required=True)])
    runtime, client = await make_runtime(
        snapshot,
        [
            MockStream(
                [
                    tool_chunk(
                        call_id="call_1",
                        name="save_answers",
                        arguments='{"user_name":',
                    ),
                    tool_chunk(arguments='"Alice"}'),
                ]
            ),
            MockStream([content_chunk("Nice to meet you, Alice!")]),
        ],
    )
    websocket = FakeWebSocket()

    terminal = await runtime.handle_user_message(websocket, "My name is Alice")

    assert terminal is False
    assert runtime.goal_state.answers == {"user_name": "Alice"}
    assert runtime.writer.answers == [
        ("user_name", "Alice", True, {"reason": "accepted", "validation_errors": []})
    ]
    assert websocket.events == [
        {"type": "token", "content": "Nice to meet you, Alice!"},
        {"type": "turn_complete", "content": "Nice to meet you, Alice!"},
    ]
    assert client.chat.completions.create.await_count == 2
    second = client.chat.completions.create.await_args_list[1]
    assert "tools" not in second.kwargs
    tool_messages = [message for message in second.kwargs["messages"] if message["role"] == "tool"]
    assert len(tool_messages) == 1
    payload = json.loads(tool_messages[0]["content"])
    assert payload["saved"] == {"user_name": "Alice"}
    assert payload["confirmed_answers"] == {"user_name": "Alice"}
    assistant_tool_messages = [
        message
        for message in second.kwargs["messages"]
        if message["role"] == "assistant" and message.get("tool_calls")
    ]
    assert assistant_tool_messages[0]["tool_calls"][0]["function"]["name"] == "save_answers"
    await runtime.close()


@pytest.mark.anyio
async def test_outcome_reached_emits_event_drains_writer_and_resets_conditional_fields():
    snapshot = chat_snapshot(
        fields=[
            field_row("intent", options=["order"], required=True, sort_order=0),
            field_row(
                "items",
                required_when=[condition("intent", "eq", "order")],
                sort_order=1,
            ),
        ],
        outcomes=[
            outcome_row(
                "order_placed",
                conditions=[condition("intent", "eq", "order")],
                required_fields=["items"],
            )
        ],
    )
    runtime, client = await make_runtime(
        snapshot,
        [
            MockStream(
                [
                    tool_chunk(
                        call_id="call_2",
                        name="save_answers",
                        arguments='{"items": "2 pizzas"}',
                    )
                ]
            ),
            MockStream([content_chunk("Order placed. Anything else?")]),
        ],
    )
    runtime.goal_state = replace(runtime.goal_state, answers={"intent": "order"})
    websocket = FakeWebSocket()
    drain_state = []
    runtime.writer.on_drain = lambda: drain_state.append(dict(runtime.goal_state.answers))

    with patch("app.chat.record_session_outcome", AsyncMock(return_value=True)) as record:
        terminal = await runtime.handle_user_message(websocket, "Two pizzas, please")

    assert terminal is False
    assert drain_state[0] == {"intent": "order", "items": "2 pizzas"}
    assert runtime.writer.drained >= 1
    assert runtime.goal_state.answers == {"intent": "order"}
    assert runtime.goal_state.completed_outcomes == ("order_placed",)
    assert [event for event in websocket.events if event["type"] == "outcome_reached"] == [
        {"type": "outcome_reached", "outcome": "order_placed"}
    ]
    record.assert_awaited_once()
    assert record.await_args.args[0] == runtime.session_id
    assert record.await_args.args[1] == "order_placed"
    directives = [
        message["content"]
        for message in client.chat.completions.create.await_args_list[1].kwargs["messages"]
        if message["role"] == "system"
    ]
    assert any("anything else" in directive for directive in directives)
    await runtime.close()


@pytest.mark.anyio
async def test_outcome_does_not_fire_again_and_followup_question_stays_conversational():
    snapshot = chat_snapshot(
        fields=[
            field_row("intent", options=["order"], required=True, sort_order=0),
            field_row(
                "items",
                required_when=[condition("intent", "eq", "order")],
                sort_order=1,
            ),
        ],
        outcomes=[
            outcome_row(
                "order_placed",
                conditions=[condition("intent", "eq", "order")],
                required_fields=["items"],
            )
        ],
    )
    runtime, client = await make_runtime(
        snapshot,
        [
            MockStream(
                [
                    tool_chunk(
                        call_id="call_2",
                        name="save_answers",
                        arguments='{"items": "2 pizzas"}',
                    )
                ]
            ),
            MockStream([content_chunk("Order placed. Anything else?")]),
            MockStream([content_chunk("We close at 9.")]),
        ],
    )
    runtime.goal_state = replace(runtime.goal_state, answers={"intent": "order"})
    websocket = FakeWebSocket()

    with patch("app.chat.record_session_outcome", AsyncMock(return_value=True)) as record:
        await runtime.handle_user_message(websocket, "Two pizzas, please")
        websocket.events.clear()
        await runtime.handle_user_message(websocket, "What time do you close?")

    assert record.await_count == 1
    assert runtime.goal_state.answers == {"intent": "order"}
    assert runtime.goal_state.completed_outcomes == ("order_placed",)
    assert [event for event in websocket.events if event["type"] == "outcome_reached"] == []
    assert websocket.events[-1] == {"type": "turn_complete", "content": "We close at 9."}
    third = client.chat.completions.create.await_args_list[2]
    assert "tools" in third.kwargs
    await runtime.close()


@pytest.mark.anyio
async def test_malformed_tool_arguments_send_warning_and_fallback_without_crashing():
    snapshot = chat_snapshot(fields=[field_row("user_name", required=True)])
    runtime, client = await make_runtime(
        snapshot,
        [
            MockStream(
                [
                    tool_chunk(
                        call_id="call_bad",
                        name="save_answers",
                        arguments="{not json",
                    )
                ]
            )
        ],
    )
    websocket = FakeWebSocket()

    terminal = await runtime.handle_user_message(websocket, "My name is Alice")

    assert terminal is False
    assert websocket.events[0]["type"] == "warning"
    assert websocket.events[-1]["type"] == "turn_complete"
    assert websocket.events[-1]["content"]
    assert runtime.goal_state.answers == {}
    assert client.chat.completions.create.await_count == 1
    await runtime.close()


@pytest.mark.anyio
async def test_runtime_restores_answers_and_completed_outcomes_from_database():
    email = f"{uuid.uuid4()}@example.com"
    password = "correct horse battery staple"
    async with Session() as db:
        user = User(email=email, password_hash="unused", role="user")
        db.add(user)
        await db.flush()
        agent = Agent(owner_id=user.id, slug=f"restore-{uuid.uuid4().hex}", name="Restore")
        db.add(agent)
        await db.flush()
        call = CallSession(
            id=uuid.uuid4(),
            owner_id=user.id,
            agent_id=agent.id,
            call_type="chat_test",
            status="active",
            snapshot={"language": "en"},
            signaling_expires_at=None,
            outcome=[{"label": "order_placed", "completed_at": "2026-11-08T10:00:00+00:00"}],
        )
        db.add(call)
        await db.flush()
        db.add(
            CallAnswer(
                session_id=call.id,
                field_key="intent",
                value="order",
                accepted=True,
                validation_result={"reason": "accepted"},
            )
        )
        await db.commit()
        session_id = call.id
        agent_id = agent.id

    snapshot = chat_snapshot(
        fields=[field_row("intent", options=["order"], required=True)],
        outcomes=[
            outcome_row(
                "order_placed",
                conditions=[condition("intent", "eq", "order")],
                required_fields=[],
            )
        ],
    )
    snapshot["agent"]["id"] = str(agent_id)

    with patch("app.chat.AsyncOpenAI", return_value=MagicMock(close=AsyncMock())):
        runtime = await ChatRuntime.from_snapshot(
            session_id,
            snapshot,
            env={"OPENAI_API_KEY": "test-key"},
        )

    assert runtime.goal_state.answers == {"intent": "order"}
    assert runtime.goal_state.completed_outcomes == ("order_placed",)
    await runtime.close()


def test_websocket_chat_turn_processing():
    sync_client = TestClient(app)
    unique_email = f"wsuser-{uuid.uuid4().hex[:8]}@example.com"
    assert (
        sync_client.post(
            "/auth/register", json={"email": unique_email, "password": "password123456"}
        ).status_code
        == 201
    )
    log = sync_client.post(
        "/auth/login", json={"email": unique_email, "password": "password123456"}
    )
    csrf_token = log.json()["csrf_token"]
    cookie_val = log.cookies.get(COOKIE)

    agent_resp = sync_client.post(
        "/agents",
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token},
        json={
            "name": "WS Agent",
            "slug": f"ws-agent-{uuid.uuid4().hex[:8]}",
            "instructions": "Be helpful.",
            "languages": ["en"],
            "default_language": "en",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        },
    )
    assert agent_resp.status_code == 201
    agent_id = agent_resp.json()["id"]
    field_resp = sync_client.post(
        f"/agents/{agent_id}/fields",
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token},
        json={
            "field_key": "user_name",
            "description": "Caller name",
            "guidance": "Ask for the caller's name.",
            "required": True,
        },
    )
    assert field_resp.status_code == 201

    mock_openai = MagicMock()
    mock_openai.chat.completions.create = AsyncMock(
        side_effect=[
            MockStream([content_chunk("Hello! What is your name?")]),
            MockStream(
                [
                    tool_chunk(
                        call_id="call_name",
                        name="save_answers",
                        arguments='{"user_name": "Alice"}',
                    )
                ]
            ),
            MockStream([content_chunk("Nice to meet you, Alice!")]),
        ]
    )
    mock_openai.close = AsyncMock()

    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "VERIFIED_OPENAI_MODELS": "gpt-4o-mini"}):
        create_resp = sync_client.post(
            f"/agents/{agent_id}/chat-sessions",
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token},
            json={},
        )
        assert create_resp.status_code == 201
        session_id = create_resp.json()["session_id"]

        with patch("app.chat.AsyncOpenAI", return_value=mock_openai):
            with sync_client.websocket_connect(
                f"/agents/{agent_id}/chat-sessions/{session_id}/ws",
                cookies={COOKIE: cookie_val},
            ) as ws:
                greeting = receive_until(ws, "turn_complete")
                assert "".join(
                    message["content"] for message in greeting if message["type"] == "token"
                ) == "Hello! What is your name?"

                ws.send_json({"type": "message", "content": "My name is Alice"})
                turn = receive_until(ws, "turn_complete")
                assert [message for message in turn if message["type"] == "token"] == [
                    {"type": "token", "content": "Nice to meet you, Alice!"}
                ]
                assert not any(message["type"] == "session_ended" for message in turn)

        answer_value, answer_accepted, message_roles = asyncio.run(
            load_chat_persistence(session_id)
        )
        assert answer_value == "Alice"
        assert answer_accepted is True
        assert message_roles == ["assistant", "user", "assistant"]


def test_websocket_outcome_reached_keeps_session_open_and_persists_records():
    sync_client = TestClient(app)
    email = f"wsuser-{uuid.uuid4().hex[:8]}@example.com"
    sync_client.post("/auth/register", json={"email": email, "password": "password123456"})
    log = sync_client.post("/auth/login", json={"email": email, "password": "password123456"})
    csrf_token = log.json()["csrf_token"]
    cookie_val = log.cookies.get(COOKIE)

    agent_resp = sync_client.post(
        "/agents",
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token},
        json={
            "name": "Outcome Agent",
            "slug": f"outcome-agent-{uuid.uuid4().hex[:8]}",
            "instructions": "Be helpful.",
            "languages": ["en"],
            "default_language": "en",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        },
    )
    agent_id = agent_resp.json()["id"]
    headers = {"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token}
    assert (
        sync_client.post(
            f"/agents/{agent_id}/fields",
            headers=headers,
            json={
                "field_key": "intent",
                "description": "Caller intent",
                "guidance": "Ask what they want.",
                "options": ["order"],
                "required": True,
            },
        ).status_code
        == 201
    )
    assert (
        sync_client.post(
            f"/agents/{agent_id}/fields",
            headers=headers,
            json={
                "field_key": "items",
                "description": "Order items",
                "guidance": "Ask what they want to order.",
                "required_when": [condition("intent", "eq", "order")],
            },
        ).status_code
        == 201
    )
    assert (
        sync_client.post(
            f"/agents/{agent_id}/outcomes",
            headers=headers,
            json={
                "label": "order_placed",
                "conditions": [condition("intent", "eq", "order")],
                "required_fields": ["items"],
            },
        ).status_code
        == 201
    )

    mock_openai = MagicMock()
    mock_openai.chat.completions.create = AsyncMock(
        side_effect=[
            MockStream([content_chunk("Hi! How can I help?")]),
            MockStream(
                [
                    tool_chunk(
                        call_id="call_intent",
                        name="save_answers",
                        arguments='{"intent": "order"}',
                    )
                ]
            ),
            MockStream([content_chunk("What would you like to order?")]),
            MockStream(
                [
                    tool_chunk(
                        call_id="call_items",
                        name="save_answers",
                        arguments='{"items": "2 pizzas"}',
                    )
                ]
            ),
            MockStream([content_chunk("Order placed. Anything else?")]),
            MockStream([content_chunk("We close at 9.")]),
        ]
    )
    mock_openai.close = AsyncMock()

    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "VERIFIED_OPENAI_MODELS": "gpt-4o-mini"}):
        create_resp = sync_client.post(
            f"/agents/{agent_id}/chat-sessions",
            headers=headers,
            json={},
        )
        session_id = create_resp.json()["session_id"]

        with patch("app.chat.AsyncOpenAI", return_value=mock_openai):
            with sync_client.websocket_connect(
                f"/agents/{agent_id}/chat-sessions/{session_id}/ws",
                cookies={COOKIE: cookie_val},
            ) as ws:
                receive_until(ws, "turn_complete")

                ws.send_json({"type": "message", "content": "I want to order"})
                first_turn = receive_until(ws, "turn_complete")
                assert not any(message["type"] == "outcome_reached" for message in first_turn)

                ws.send_json({"type": "message", "content": "Two pizzas, please"})
                outcome_turn = receive_until(ws, "turn_complete")
                assert [message for message in outcome_turn if message["type"] == "outcome_reached"] == [
                    {"type": "outcome_reached", "outcome": "order_placed"}
                ]
                assert not any(message["type"] == "session_ended" for message in outcome_turn)

                ws.send_json({"type": "message", "content": "What time do you close?"})
                followup = receive_until(ws, "turn_complete")
                assert not any(message["type"] == "outcome_reached" for message in followup)
                assert not any(message["type"] == "session_ended" for message in followup)

                ws.send_json({"type": "end"})
                ended = receive_until(ws, "session_ended")
                assert ended[-1]["reason"] == "cancelled"

        status, outcome = asyncio.run(load_session_outcome(session_id))
        assert status == "cancelled"
        assert [record["label"] for record in outcome] == ["order_placed"]


def test_websocket_user_ends_chat():
    sync_client = TestClient(app)
    email = f"wsuser-{uuid.uuid4().hex[:8]}@example.com"
    sync_client.post("/auth/register", json={"email": email, "password": "password123456"})
    log = sync_client.post("/auth/login", json={"email": email, "password": "password123456"})
    csrf_token = log.json()["csrf_token"]
    cookie_val = log.cookies.get(COOKIE)

    agent_resp = sync_client.post(
        "/agents",
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token},
        json={
            "name": "WS Agent",
            "slug": f"ws-agent-{uuid.uuid4().hex[:8]}",
            "instructions": "Be helpful.",
            "languages": ["en"],
            "default_language": "en",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        },
    )
    agent_id = agent_resp.json()["id"]

    mock_openai = MagicMock()
    mock_openai.chat.completions.create = AsyncMock(
        return_value=MockStream([content_chunk("Hello!")])
    )
    mock_openai.close = AsyncMock()

    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "VERIFIED_OPENAI_MODELS": "gpt-4o-mini"}):
        create_resp = sync_client.post(
            f"/agents/{agent_id}/chat-sessions",
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token},
            json={},
        )
        session_id = create_resp.json()["session_id"]

        with patch("app.chat.AsyncOpenAI", return_value=mock_openai):
            with sync_client.websocket_connect(
                f"/agents/{agent_id}/chat-sessions/{session_id}/ws",
                cookies={COOKIE: cookie_val},
            ) as ws:
                receive_until(ws, "turn_complete")
                ws.send_json({"type": "end"})
                ended = receive_until(ws, "session_ended")
                assert ended[-1]["reason"] == "cancelled"


def test_websocket_auth_rejection():
    sync_client = TestClient(app)
    with pytest.raises(Exception):
        with sync_client.websocket_connect(
            f"/agents/{uuid.uuid4()}/chat-sessions/{uuid.uuid4()}/ws"
        ):
            pass


def test_websocket_inactivity_timeout():
    sync_client = TestClient(app)
    email = f"wsuser-{uuid.uuid4().hex[:8]}@example.com"
    sync_client.post("/auth/register", json={"email": email, "password": "password123456"})
    log = sync_client.post("/auth/login", json={"email": email, "password": "password123456"})
    csrf_token = log.json()["csrf_token"]
    cookie_val = log.cookies.get(COOKIE)

    agent_resp = sync_client.post(
        "/agents",
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token},
        json={
            "name": "WS Agent",
            "slug": f"ws-agent-{uuid.uuid4().hex[:8]}",
            "instructions": "Be helpful.",
            "languages": ["en"],
            "default_language": "en",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        },
    )
    agent_id = agent_resp.json()["id"]

    mock_openai = MagicMock()
    mock_openai.chat.completions.create = AsyncMock(
        return_value=MockStream([content_chunk("Hello!")])
    )
    mock_openai.close = AsyncMock()

    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "VERIFIED_OPENAI_MODELS": "gpt-4o-mini"}):
        create_resp = sync_client.post(
            f"/agents/{agent_id}/chat-sessions",
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token},
            json={},
        )
        session_id = create_resp.json()["session_id"]

        with patch("app.chat._CHAT_IDLE_TIMEOUT_SECONDS", 0.05):
            with patch("app.chat.AsyncOpenAI", return_value=mock_openai):
                with sync_client.websocket_connect(
                    f"/agents/{agent_id}/chat-sessions/{session_id}/ws",
                    cookies={COOKIE: cookie_val},
                ) as ws:
                    receive_until(ws, "turn_complete")
                    ended = receive_until(ws, "session_ended")
                    assert ended[-1]["reason"] == "expired"
