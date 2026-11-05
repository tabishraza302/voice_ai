import os
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test")
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.agents import Agent
from app.db import Base, Session, engine
from app.main import app
from app.models import User
from app.observability import (
    RedactingExporter,
    TraceUnavailableError,
    fetch_session_trace,
    public_trace_span,
    redact_span,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExportResult, SpanProcessor
from opentelemetry.trace import Status, StatusCode


class _Capture(SpanProcessor):
    def __init__(self) -> None:
        self.spans = []

    def on_end(self, span) -> None:
        self.spans.append(span)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


class _Delegate:
    def __init__(self) -> None:
        self.exported: list[object] = []

    def export(self, spans):
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def test_redacting_exporter_removes_private_payloads_and_derives_session_id() -> None:
    capture = _Capture()
    provider = TracerProvider()
    provider.add_span_processor(capture)
    span = provider.get_tracer("test").start_span("conversation")
    span.set_attribute("conversation.id", "session-123")
    span.set_attribute("gen_ai.request.model", "gpt-4o-mini")
    span.set_attribute("input", "private prompt")
    span.set_attribute("output", "private answer")
    span.set_attribute("transcript", "caller said a secret")
    span.set_attribute("gen_ai.system_instructions", "system prompt")
    span.set_attribute("gen_ai.completion", "private completion")
    span.set_attribute("gen_ai.tool.call.arguments", "private tool arguments")
    span.set_attribute("gen_ai.tool.call.result", "private tool result")
    span.set_attribute("settings.api_key", "sk-secret")
    span.set_attribute("metrics.ttfb", 0.2)
    span.set_status(Status(StatusCode.ERROR, "provider failed for a private prompt"))
    span.end()
    assert len(capture.spans) == 1
    delegate = _Delegate()

    result = RedactingExporter(delegate).export([capture.spans[0]])

    assert result == SpanExportResult.SUCCESS
    assert len(delegate.exported) == 1
    exported = delegate.exported[0]
    assert exported is not capture.spans[0]
    assert dict(exported.attributes) == {
        "conversation.id": "session-123",
        "gen_ai.request.model": "gpt-4o-mini",
        "session.id": "session-123",
        "metrics.ttfb": 0.2,
    }
    assert exported.status.status_code == StatusCode.ERROR
    assert exported.status.description is None


def test_public_trace_span_exposes_only_timing_status_and_error_type() -> None:
    projected = public_trace_span(
        {
            "name": "llm",
            "start_time": "2026-11-04T10:00:00Z",
            "end_time": "2026-11-04T10:00:01.250Z",
            "status_code": "ERROR",
            "attributes": {
                "error.type": "provider_timeout",
                "input": "private prompt",
                "output": "private answer",
            },
        }
    )

    assert projected == {
        "name": "llm",
        "started_at": "2026-11-04T10:00:00Z",
        "ended_at": "2026-11-04T10:00:01.250Z",
        "duration_ms": 1250,
        "status": "ERROR",
        "error_type": "provider_timeout",
    }


@pytest.mark.anyio
async def test_fetch_session_trace_scopes_queries_and_drops_attributes() -> None:
    calls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        query = request.url.params
        if "trace_id" in query:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "conversation",
                            "context": {"trace_id": "trace-1", "span_id": "span-1"},
                            "start_time": "2026-11-04T10:00:00Z",
                            "end_time": "2026-11-04T10:00:02Z",
                            "status_code": "OK",
                            "attributes": {"session.id": "session-1", "input": "secret"},
                        },
                        {
                            "name": "tts",
                            "context": {"trace_id": "trace-1", "span_id": "span-2"},
                            "start_time": "2026-11-04T10:00:00.500Z",
                            "end_time": "2026-11-04T10:00:01Z",
                            "status_code": "ERROR",
                            "attributes": {"error.type": "tts_failed", "text": "secret"},
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "conversation",
                        "context": {"trace_id": "trace-1", "span_id": "span-1"},
                        "start_time": "2026-11-04T10:00:00Z",
                        "end_time": "2026-11-04T10:00:02Z",
                        "status_code": "OK",
                        "attributes": {"session.id": "session-1"},
                    }
                ]
            },
        )

    spans = await fetch_session_trace(
        "session-1",
        env={"PHOENIX_URL": "http://phoenix:6006", "PHOENIX_PROJECT_NAME": "voice-agent"},
        transport=httpx.MockTransport(handler),
    )

    assert [span["name"] for span in spans] == ["conversation", "tts"]
    assert spans[1]["error_type"] == "tts_failed"
    assert all("attributes" not in span for span in spans)
    assert calls[0].params["attribute"] == "session.id:session-1"
    assert calls[0].path == "/v1/projects/voice-agent/spans"
    assert calls[1].params["trace_id"] == "trace-1"


@pytest.mark.anyio
async def test_fetch_session_trace_treats_a_missing_project_as_an_empty_trace() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Project with name voice-agent not found")

    spans = await fetch_session_trace(
        "session-1",
        env={"PHOENIX_URL": "http://phoenix:6006", "PHOENIX_PROJECT_NAME": "voice-agent"},
        transport=httpx.MockTransport(handler),
    )

    assert spans == []


@pytest.mark.anyio
async def test_fetch_session_trace_fails_closed_without_phoenix() -> None:
    with pytest.raises(TraceUnavailableError):
        await fetch_session_trace("session-1", env={})


@pytest.mark.anyio
async def test_fetch_session_trace_reports_phoenix_errors() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no phoenix")

    with pytest.raises(TraceUnavailableError):
        await fetch_session_trace(
            "session-1",
            env={"PHOENIX_URL": "http://phoenix:6006"},
            transport=httpx.MockTransport(handler),
        )


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


def mutation_headers(token: str) -> dict[str, str]:
    return {"Origin": "http://localhost:3000", "X-CSRF-Token": token}


async def login(client: AsyncClient, email: str) -> str:
    password = "correct horse battery staple"
    response = await client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201
    response = await client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return response.json()["csrf_token"]


async def create_call_session(client: AsyncClient, token: str) -> str:
    slug = f"trace-{uuid.uuid4().hex[:12]}"
    response = await client.post(
        "/agents",
        headers=mutation_headers(token),
        json={
            "name": "Trace agent",
            "slug": slug,
            "instructions": "Help the caller.",
            "languages": ["en"],
            "default_language": "en",
            "stt_provider": "deepgram",
            "stt_model": "nova-3",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "tts_provider": "cartesia",
            "tts_model": "sonic-3.6",
            "voice_id": "voice-test",
        },
    )
    assert response.status_code == 201
    agent_id = response.json()["id"]
    async with Session() as db:
        agent = await db.get(Agent, uuid.UUID(agent_id))
        assert agent is not None
        agent.status = "published"
        await db.commit()
    response = await client.post(
        f"/agents/{agent_id}/test-calls",
        headers=mutation_headers(token),
        json={"language": "en"},
    )
    assert response.status_code == 201
    return response.json()["session_id"]


@pytest.mark.anyio
async def test_owner_trace_route_returns_safe_spans_and_hides_other_owners(client, monkeypatch):
    owner = await login(client, f"{uuid.uuid4()}@example.com")
    session_id = await create_call_session(client, owner)

    async def fake_trace(session: str, **_: object):
        assert session == session_id
        return [
            {
                "name": "conversation",
                "started_at": "2026-11-04T10:00:00Z",
                "ended_at": "2026-11-04T10:00:02Z",
                "duration_ms": 2000,
                "status": "OK",
                "error_type": None,
            }
        ]

    monkeypatch.setattr("app.sessions.fetch_session_trace", fake_trace)
    response = await client.get(f"/sessions/{session_id}/trace")
    assert response.status_code == 200
    assert response.json() == {
        "session_id": session_id,
        "spans": [
            {
                "name": "conversation",
                "started_at": "2026-11-04T10:00:00Z",
                "ended_at": "2026-11-04T10:00:02Z",
                "duration_ms": 2000,
                "status": "OK",
                "error_type": None,
            }
        ],
    }

    other = await login(client, f"{uuid.uuid4()}@example.com")
    assert other
    response = await client.get(f"/sessions/{session_id}/trace")
    assert response.status_code == 404


@pytest.mark.anyio
async def test_trace_route_reports_phoenix_unavailable(client, monkeypatch):
    owner = await login(client, f"{uuid.uuid4()}@example.com")
    session_id = await create_call_session(client, owner)

    async def unavailable(*_: object, **__: object):
        raise TraceUnavailableError("offline")

    monkeypatch.setattr("app.sessions.fetch_session_trace", unavailable)
    response = await client.get(f"/sessions/{session_id}/trace")
    assert response.status_code == 503
    assert response.json() == {"detail": "Phoenix is not available right now."}


@pytest.mark.anyio
async def test_trace_route_requires_sign_in(client):
    response = await client.get(f"/sessions/{uuid.uuid4()}/trace")
    assert response.status_code == 401
