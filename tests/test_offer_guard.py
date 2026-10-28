"""Behavioral tests for WebRTC offer preflight failure handling and peer teardown."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from bot.agents.service import AgentNotFoundError
from bot.bot import (
    bot,
    mount_api,
    mount_offer_guard,
)
from bot.config import Settings
from bot.db.session import StorageUnavailableError


@pytest.fixture(autouse=True)
def prevent_real_database_access(monkeypatch):
    """Ensure no test in this module ever touches a real or configured database."""
    def forbidden_db(*args, **kwargs):
        raise AssertionError("Real database access is strictly forbidden in test_offer_guard.py")

    monkeypatch.setattr("bot.db.session.get_engine", forbidden_db)
    monkeypatch.setattr("bot.db.session.get_session_factory", forbidden_db)


def _create_test_app(
    settings: Settings,
    profile_resolver=None,
    mock_offer_handler=None,
) -> FastAPI:
    """Create a FastAPI test app with the offer guard and mock endpoints."""
    app = FastAPI()
    mount_offer_guard(app, settings, profile_resolver=profile_resolver)
    mount_api(app, settings)

    @app.post("/api/offer")
    async def offer_endpoint(request: Request):
        if mock_offer_handler:
            return await mock_offer_handler(request)
        return {"sdp": "v=0\r\n...", "type": "answer", "pc_id": "pc-test-123"}

    @app.post("/sessions/{session_id}/api/offer")
    async def proxy_offer_endpoint(session_id: str, request: Request):
        if mock_offer_handler:
            return await mock_offer_handler(request)
        return {"sdp": "v=0\r\n...", "type": "answer", "pc_id": f"pc-{session_id}"}

    @app.post("/start")
    async def start_endpoint(request: Request):
        return {"sessionId": "session-test-456"}

    return app


def test_offer_preflight_rejects_malformed_agent_id(monkeypatch):
    """Malformed agent_id in offer payload returns HTTP 400 Bad Request before offer answer."""
    async def unused_resolver(s, agent_id=None):
        raise AssertionError("Resolver should not be reached for malformed agent_id")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", unused_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=unused_resolver)
    client = TestClient(app)

    response = client.post(
        "/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "   "}},
    )

    assert response.status_code == 400
    assert "agent_id" in response.json().get("detail", "").lower()
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_offer_preflight_rejects_missing_agent_id_with_404(monkeypatch):
    """Unknown or deleted agent profile returns HTTP 404 Not Found before offer answer."""
    async def mock_resolver(s, agent_id):
        raise AgentNotFoundError(f"Agent with id {agent_id} not found")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", mock_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=mock_resolver)
    client = TestClient(app)

    response = client.post(
        "/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "stale-agent-id"}},
    )

    assert response.status_code == 404
    assert "not found" in response.json().get("detail", "").lower()
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_offer_preflight_reports_storage_fallback_as_unavailable_not_deleted(monkeypatch):
    """An explicit profile on the SQLite fallback returns 503, not 404."""
    async def mock_resolver(s, agent_id):
        raise StorageUnavailableError("requested profile is not available from fallback")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", mock_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=mock_resolver)
    client = TestClient(app)

    response = client.post(
        "/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "saved-agent"}},
    )

    assert response.status_code == 503
    detail = response.json().get("detail", "").lower()
    assert "storage" in detail
    assert "deleted" not in detail
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_offer_preflight_fails_closed_on_database_error(monkeypatch):
    """Database storage failure returns HTTP 500 Internal Server Error without falling back."""
    async def mock_resolver(s, agent_id):
        raise OperationalError("SELECT 1", {}, Exception("database connection lost"))

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", mock_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=mock_resolver)
    client = TestClient(app)

    response = client.post(
        "/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "any-agent-id"}},
    )

    assert response.status_code == 500
    assert "database error" in response.json().get("detail", "").lower()
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_offer_preflight_rejects_incompatible_realtime_pinned_language(monkeypatch):
    """Incompatible realtime STT + pinned non-auto language returns HTTP 400 Bad Request."""
    async def mock_resolver(s, agent_id):
        raise ValueError(
            "stt_provider=mlx-audio-realtime has no language switch: "
            "/v1/realtime takes model, format and turn_detection only. "
            "Set stt_language=auto, or use stt_provider=mlx-audio"
        )

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", mock_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=mock_resolver)
    client = TestClient(app)

    response = client.post(
        "/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "realtime-pinned-hi"}},
    )

    assert response.status_code == 400
    assert "mlx-audio-realtime" in response.json().get("detail", "").lower()
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_offer_preflight_catches_language_incompatibility_introduced_by_apply_mode(monkeypatch):
    """A pinned language valid under batch is rejected when apply_mode forces realtime."""
    from bot import control
    monkeypatch.setattr(control, "effective_provider", lambda s: "mlx-audio-realtime")

    settings = Settings(_env_file=None, web_port=3000, stt_provider="mlx-audio-realtime")

    async def batch_profile_resolver(s, agent_id):
        updated = s.model_copy(deep=True)
        updated.stt_provider = "mlx-audio"
        updated.stt_language = "hi"
        return updated

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", batch_profile_resolver)

    app = _create_test_app(settings, profile_resolver=batch_profile_resolver)
    client = TestClient(app)

    response = client.post(
        "/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "batch-profile-hi"}},
    )

    assert response.status_code == 400
    assert "mlx-audio-realtime" in response.json().get("detail", "").lower()


def test_offer_preflight_preserves_baseline_legacy_client(monkeypatch):
    """Offers without explicit agent_id pass through untouched for legacy prebuilt client."""
    async def baseline_resolver(s, agent_id=None):
        return s

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", baseline_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    offer_called = False

    async def mock_offer_handler(request: Request):
        nonlocal offer_called
        offer_called = True
        return {"sdp": "legacy-answer-sdp", "type": "answer", "pc_id": "pc-legacy"}

    app = _create_test_app(settings, profile_resolver=baseline_resolver, mock_offer_handler=mock_offer_handler)
    client = TestClient(app)

    response = client.post(
        "/api/offer",
        json={"sdp": "legacy-offer-sdp", "type": "offer"},
    )

    assert response.status_code == 200
    assert response.json() == {"sdp": "legacy-answer-sdp", "type": "answer", "pc_id": "pc-legacy"}
    assert offer_called is True


def test_offer_proxy_route_preflight(monkeypatch):
    """Session proxy route /sessions/{session_id}/api/offer is guarded by preflight."""
    async def mock_resolver(s, agent_id):
        raise AgentNotFoundError(f"Agent with id {agent_id} not found")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", mock_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=mock_resolver)
    client = TestClient(app)

    response = client.post(
        "/sessions/sess-123/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "missing-agent"}},
    )

    assert response.status_code == 404
    assert "not found" in response.json().get("detail", "").lower()


def test_start_route_preflight(monkeypatch):
    """POST /start with invalid agent body is rejected before session creation."""
    async def mock_resolver(s, agent_id):
        raise AgentNotFoundError(f"Agent with id {agent_id} not found")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", mock_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=mock_resolver)
    client = TestClient(app)

    response = client.post(
        "/start",
        headers={"Origin": "http://localhost:3000"},
        json={"transport": "webrtc", "body": {"agent_id": "nonexistent"}},
    )

    assert response.status_code == 404


def test_cors_headers_omitted_for_disallowed_origin(monkeypatch):
    """Error responses do not leak CORS headers to unauthorized origins."""
    async def unused_resolver(s, agent_id=None):
        raise AssertionError("Resolver should not be reached for malformed agent_id")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", unused_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=unused_resolver)
    client = TestClient(app)

    response = client.post(
        "/api/offer",
        headers={"Origin": "https://attacker.example"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "  "}},
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_bot_startup_failure_tears_down_peer_connection(monkeypatch):
    """bot() closes webrtc_connection when pipeline initialization raises an error."""
    async def failing_resolver(s, agent_id=None):
        raise AgentNotFoundError(f"Agent with id {agent_id} not found")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", failing_resolver)

    mock_connection = MagicMock()
    mock_connection.disconnect = AsyncMock()

    runner_args = MagicMock()
    runner_args.webrtc_connection = mock_connection
    runner_args.body = {"agent_id": "failing-agent"}

    with pytest.raises(AgentNotFoundError):
        await bot(runner_args)

    mock_connection.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_startup_rejects_incompatible_language_after_apply_mode(monkeypatch):
    """bot() rejects pinned language when apply_mode forces realtime STT, tearing down peer."""
    from bot import control
    monkeypatch.setattr(control, "effective_provider", lambda s: "mlx-audio-realtime")

    mock_connection = MagicMock()
    mock_connection.disconnect = AsyncMock()

    runner_args = MagicMock()
    runner_args.webrtc_connection = mock_connection
    runner_args.body = {"agent_id": "profile-with-pinned-hi"}

    async def batch_profile_resolver(s, agent_id):
        updated = s.model_copy(deep=True)
        updated.stt_provider = "mlx-audio"
        updated.stt_language = "hi"
        return updated

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", batch_profile_resolver)

    with pytest.raises(ValueError, match="mlx-audio-realtime"):
        await bot(runner_args)

    mock_connection.disconnect.assert_awaited_once()
