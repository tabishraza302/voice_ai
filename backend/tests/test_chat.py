import asyncio
import json
import os
import uuid
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
from app.main import app
from app.models import User
from app.sessions import CallMessage, CallSession


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
        self.closed = False

    async def send_json(self, data):
        self.events.append(data)

    async def close(self, code=1000, reason=None):
        self.closed = True


class FakeWriter:
    def __init__(self):
        self.messages = []
        self.drained = 0
        self.flushed = False

    def enqueue_message(self, role, content):
        self.messages.append((role, content))

    async def drain(self):
        self.drained += 1

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
def tool_call_chunk(name, arguments="{}", call_id="call_1"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id=call_id,
                            function=SimpleNamespace(name=name, arguments=arguments),
                        )
                    ],
                )
            )
        ]
    )


def chat_snapshot():
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
        },
    }
    env = {
        "OPENAI_API_KEY": "test-key",
        "VERIFIED_OPENAI_MODELS": "gpt-4o-mini",
    }
    assert _chat_publish_errors(snapshot, env) == {}

    bad_model = {
        "global_prompt": {"text": "You are a helpful voice assistant."},
        "agent": {
            "llm_provider": "openai",
            "llm_model": "unverified-model",
        },
    }
    errors = _chat_publish_errors(bad_model, env)
    assert "llm_model" in errors

    missing_key = {
        "global_prompt": {"text": "You are a helpful voice assistant."},
        "agent": {
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        },
    }
    errors = _chat_publish_errors(missing_key, {"OPENAI_API_KEY": ""})
    assert "llm_provider" in errors


@pytest.mark.anyio
async def test_create_chat_session_persists_session(client: AsyncClient):
    token = await login(client, "user@example.com")
    agent = await create_agent(client, token, "chat-test-agent")

    response = await client.post(
        f"/agents/{agent['id']}/chat-sessions",
        headers=mutation_headers(token),
    )
    assert response.status_code == 201
    data = response.json()
    assert "session_id" in data
    session_id = uuid.UUID(data["session_id"])

    async with Session() as db:
        session = await db.get(CallSession, session_id)
        assert session is not None
        assert session.agent_id == uuid.UUID(agent["id"])
        assert session.call_type == "chat_test"
        assert session.status == "created"
        assert session.signaling_expires_at is None


@pytest.mark.anyio
async def test_create_chat_session_checks_suspended_agent(client: AsyncClient):
    token = await login(client, "user2@example.com")
    agent = await create_agent(client, token, "suspended-agent")

    async with Session() as db:
        db_agent = await db.get(Agent, uuid.UUID(agent["id"]))
        db_agent.status = "suspended"
        await db.commit()

    response = await client.post(
        f"/agents/{agent['id']}/chat-sessions",
        headers=mutation_headers(token),
    )
    assert response.status_code == 409


@pytest.mark.anyio
async def test_runtime_from_snapshot_initializes_cleanly():
    snapshot = chat_snapshot()
    runtime, client = await make_runtime(snapshot, [])
    assert runtime.agent_name == "Chat Agent"
    assert runtime.agent_instructions == "Help the caller."
    assert runtime.global_prompt == "You are a helpful voice assistant."
    assert runtime.llm_model == "gpt-4o-mini"
    assert runtime.language == "en"
    assert len(runtime.recent_conversation) == 0


@pytest.mark.anyio
async def test_handle_user_message_streams_text():
    snapshot = chat_snapshot()
    runtime, client = await make_runtime(
        snapshot,
        [
            MockStream(
                [
                    content_chunk("Hello! "),
                    content_chunk("How can I "),
                    content_chunk("help you?"),
                ]
            )
        ],
    )
    ws = FakeWebSocket()
    terminal = await runtime.handle_user_message(ws, "Hello there")
    assert not terminal

    tokens = [e["content"] for e in ws.events if e.get("type") == "token"]
    assert "".join(tokens) == "Hello! How can I help you?"

    complete = next(e for e in ws.events if e.get("type") == "turn_complete")
    assert complete["content"] == "Hello! How can I help you?"
    assert runtime.writer.messages == [
        ("user", "Hello there"),
        ("assistant", "Hello! How can I help you?"),
    ]
@pytest.mark.anyio
async def test_chat_run_turn_with_end_call_tool_completes_session():
    snapshot = chat_snapshot()
    runtime, client = await make_runtime(
        snapshot,
        [
            MockStream(
                [
                    content_chunk("Thank you, "),
                    content_chunk("goodbye!"),
                    tool_call_chunk("end_call", '{"reason":"done"}'),
                ]
            )
        ],
    )
    ws = FakeWebSocket()
    with patch("app.chat.set_session_status", new_callable=AsyncMock) as mock_set_status:
        terminal = await runtime.handle_user_message(ws, "Goodbye")
        assert terminal is True
        mock_set_status.assert_awaited_once_with(runtime.session_id, "completed")

    tokens = [e["content"] for e in ws.events if e.get("type") == "token"]
    assert "".join(tokens) == "Thank you, goodbye!"

    complete = next(e for e in ws.events if e.get("type") == "turn_complete")
    assert complete["content"] == "Thank you, goodbye!"

    ended = next(e for e in ws.events if e.get("type") == "session_ended")
    assert ended["reason"] == "completed"
    assert ws.closed is True

@pytest.mark.anyio
@pytest.mark.parametrize("fragments", [
    ['{"message":"Thank you for calling. Have a wonderful day!","reason":"done"}'],
    ['{"message":"Thank you for calling.', ' Have a wonderful day!",', '"reason":"done"}'],
])
async def test_chat_run_turn_with_end_call_message_argument(fragments):
    snapshot = chat_snapshot()
    runtime, client = await make_runtime(
        snapshot,
        [
            MockStream(
                [
                    tool_call_chunk("end_call" if index == 0 else None, fragment)
                    for index, fragment in enumerate(fragments)
                ]
            )
        ],
    )
    ws = FakeWebSocket()
    with patch("app.chat.set_session_status", new_callable=AsyncMock) as mock_set_status:
        terminal = await runtime.handle_user_message(ws, "I don't need help anymore")
        assert terminal is True
        mock_set_status.assert_awaited_once_with(runtime.session_id, "completed")

    tokens = [e["content"] for e in ws.events if e.get("type") == "token"]
    assert "".join(tokens) == "Thank you for calling. Have a wonderful day!"

    complete = next(e for e in ws.events if e.get("type") == "turn_complete")
    assert complete["content"] == "Thank you for calling. Have a wonderful day!"

    ended = next(e for e in ws.events if e.get("type") == "session_ended")
    assert ended["reason"] == "completed"
    assert ws.closed is True



@pytest.mark.anyio
@pytest.mark.parametrize(("response_text", "expected"), [
    ("Your booking is confirmed.", "Your booking is confirmed.\n\nGoodbye!"),
    ("Your booking is confirmed. Goodbye!", "Your booking is confirmed. Goodbye!"),
])
async def test_end_call_preserves_tool_farewell_after_response_text(response_text, expected):
    runtime, _ = await make_runtime(chat_snapshot(), [
        MockStream([
            content_chunk(response_text),
            tool_call_chunk("end_call", '{"message":"Goodbye!"}'),
        ]),
    ])
    ws = FakeWebSocket()
    with patch("app.chat.set_session_status", new_callable=AsyncMock):
        terminal = await runtime.handle_user_message(ws, "I'm done, thanks.")

    assert terminal is True
    assert "".join(event["content"] for event in ws.events if event["type"] == "token") == expected
    assert next(event for event in ws.events if event["type"] == "turn_complete")["content"] == expected
    assert runtime.writer.messages[-1] == ("assistant", expected)
    assert [event["type"] for event in ws.events][-2:] == ["turn_complete", "session_ended"]
    assert ws.closed is True


def test_websocket_agent_ends_chat_on_end_call():
    sync_client = TestClient(app)
    email = f"wsuser-{uuid.uuid4().hex[:8]}@example.com"
    sync_client.post("/auth/register", json={"email": email, "password": "password123456"})
    login_resp = sync_client.post("/auth/login", json={"email": email, "password": "password123456"})
    csrf_token = login_resp.json()["csrf_token"]
    auth_headers = {"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token}

    agent_resp = sync_client.post(
        "/agents",
        json={"name": "End Agent", "instructions": "test", "languages": ["en"], "llm_provider": "openai", "llm_model": "gpt-4o-mini"},
        headers=auth_headers,
    )
    agent_id = agent_resp.json()["id"]

    session_resp = sync_client.post(f"/agents/{agent_id}/chat-sessions", headers=auth_headers)
    session_id = session_resp.json()["session_id"]

    stream = MockStream([
        content_chunk("Goodbye! Have a great day!"),
        tool_call_chunk("end_call", '{"reason":"finished"}'),
    ])
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[
        MockStream([content_chunk("How can I help?")]),
        stream,
    ])
    mock_client.close = AsyncMock()

    with patch("app.chat.AsyncOpenAI", return_value=mock_client):
        with sync_client.websocket_connect(
            f"/agents/{agent_id}/chat-sessions/{session_id}/ws"
        ) as ws:
            greeting = receive_until(ws, "turn_complete")
            assert greeting[-1]["content"] == "How can I help?"
            ws.send_json({"type": "message", "content": "I'm done, bye!"})
            events = receive_until(ws, "session_ended")
            ended = next(e for e in events if e.get("type") == "session_ended")
            assert ended["reason"] == "completed"
            assert next(e for e in events if e["type"] == "turn_complete")["content"] == "Goodbye! Have a great day!"

    async def check_session():
        async with Session() as db:
            session = await db.get(CallSession, uuid.UUID(session_id))
            messages = (await db.scalars(
                select(CallMessage)
                .where(CallMessage.session_id == uuid.UUID(session_id))
                .order_by(CallMessage.sequence)
            )).all()
            return session.status, [(message.role, message.content) for message in messages]

    status, messages = asyncio.run(check_session())
    assert status == "completed"
    assert messages == [
        ("assistant", "How can I help?"),
        ("user", "I'm done, bye!"),
        ("assistant", "Goodbye! Have a great day!"),
    ]


def test_websocket_chat_turn_processing():
    sync_client = TestClient(app)
    unique_email = f"wsuser-{uuid.uuid4().hex[:8]}@example.com"
    assert (
        sync_client.post(
            "/auth/register",
            json={"email": unique_email, "password": "password123456"},
        ).status_code
        == 201
    )
    login_resp = sync_client.post(
        "/auth/login",
        json={"email": unique_email, "password": "password123456"},
    )
    assert login_resp.status_code == 200
    csrf_token = login_resp.json()["csrf_token"]
    auth_headers = {"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token}

    agent_resp = sync_client.post(
        "/agents",
        json={
            "name": "Live Assistant",
            "instructions": "You assist callers.",
            "languages": ["en"],
            "default_language": "en",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
        },
        headers=auth_headers,
    )
    assert agent_resp.status_code == 201
    agent_id = agent_resp.json()["id"]

    session_resp = sync_client.post(
        f"/agents/{agent_id}/chat-sessions",
        headers=auth_headers,
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["session_id"]

    stream = MockStream([content_chunk("Hello, "), content_chunk("I can help!")])
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=stream)
    mock_client.close = AsyncMock()

    with patch("app.chat.AsyncOpenAI", return_value=mock_client):
        with sync_client.websocket_connect(
            f"/agents/{agent_id}/chat-sessions/{session_id}/ws"
        ) as ws:
            ws.send_json({"type": "message", "content": "Hi there"})
            events = receive_until(ws, "turn_complete")
            tokens = [e["content"] for e in events if e.get("type") == "token"]
            assert "".join(tokens) == "Hello, I can help!"


def test_websocket_user_ends_chat():
    sync_client = TestClient(app)
    email = f"wsuser-{uuid.uuid4().hex[:8]}@example.com"
    sync_client.post("/auth/register", json={"email": email, "password": "password123456"})
    login_resp = sync_client.post("/auth/login", json={"email": email, "password": "password123456"})
    csrf_token = login_resp.json()["csrf_token"]
    auth_headers = {"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token}

    agent_resp = sync_client.post(
        "/agents",
        json={"name": "End Agent", "instructions": "test", "languages": ["en"], "llm_provider": "openai", "llm_model": "gpt-4o-mini"},
        headers=auth_headers,
    )
    agent_id = agent_resp.json()["id"]

    session_resp = sync_client.post(f"/agents/{agent_id}/chat-sessions", headers=auth_headers)
    session_id = session_resp.json()["session_id"]

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=MockStream([]))
    mock_client.close = AsyncMock()

    with patch("app.chat.AsyncOpenAI", return_value=mock_client):
        with sync_client.websocket_connect(
            f"/agents/{agent_id}/chat-sessions/{session_id}/ws"
        ) as ws:
            ws.send_json({"type": "end"})
            events = receive_until(ws, "session_ended")
            ended = next(e for e in events if e.get("type") == "session_ended")
            assert ended["reason"] == "cancelled"

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
    login_resp = sync_client.post("/auth/login", json={"email": email, "password": "password123456"})
    csrf_token = login_resp.json()["csrf_token"]
    auth_headers = {"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token}

    agent_resp = sync_client.post(
        "/agents",
        json={"name": "Timeout Agent", "instructions": "test", "languages": ["en"], "llm_provider": "openai", "llm_model": "gpt-4o-mini"},
        headers=auth_headers,
    )
    agent_id = agent_resp.json()["id"]

    session_resp = sync_client.post(f"/agents/{agent_id}/chat-sessions", headers=auth_headers)
    session_id = session_resp.json()["session_id"]

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=MockStream([]))
    mock_client.close = AsyncMock()

    with patch("app.chat.AsyncOpenAI", return_value=mock_client):
        with patch("app.chat._CHAT_IDLE_TIMEOUT_SECONDS", 0.05):
            with sync_client.websocket_connect(
                f"/agents/{agent_id}/chat-sessions/{session_id}/ws"
            ) as ws:
                events = receive_until(ws, "session_ended", limit=10)
                ended = next(e for e in events if e.get("type") == "session_ended")
                assert ended["reason"] == "expired"
