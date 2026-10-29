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
from bot.control import mount_control
from bot.config import Settings
from bot.db.session import StorageUnavailableError
from bot.services import LlmModelNotAvailableError, SttLanguageNotSupportedError


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
    mount_api(app, settings, profile_resolver=profile_resolver)
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
    client = TestClient(app, base_url="http://127.0.0.1:7860")

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
    client = TestClient(app, base_url="http://127.0.0.1:7860")

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
    client = TestClient(app, base_url="http://127.0.0.1:7860")

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


def test_offer_preflight_reports_uninstalled_model_as_unavailable(monkeypatch):
    """A model the local server does not list returns 503 before the call starts."""
    async def mock_resolver(s, agent_id):
        raise LlmModelNotAvailableError("llm_model 'missing:1b' is not installed")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", mock_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=mock_resolver)
    client = TestClient(app, base_url="http://127.0.0.1:7860")

    response = client.post(
        "/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "saved-agent"}},
    )

    assert response.status_code == 503
    detail = response.json().get("detail", "").lower()
    assert "model" in detail
    assert "unavailable" in detail


def test_offer_preflight_fails_closed_on_database_error(monkeypatch):
    """Database storage failure returns HTTP 500 Internal Server Error without falling back."""
    async def mock_resolver(s, agent_id):
        raise OperationalError("SELECT 1", {}, Exception("database connection lost"))

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", mock_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=mock_resolver)
    client = TestClient(app, base_url="http://127.0.0.1:7860")

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
        raise SttLanguageNotSupportedError(
            "stt_provider=mlx-audio-realtime has no language switch: "
            "/v1/realtime takes model, format and turn_detection only. "
            "Set stt_language=auto, or use stt_provider=mlx-audio"
        )

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", mock_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=mock_resolver)
    client = TestClient(app, base_url="http://127.0.0.1:7860")

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
    client = TestClient(app, base_url="http://127.0.0.1:7860")

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
    client = TestClient(app, base_url="http://127.0.0.1:7860")

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
    client = TestClient(app, base_url="http://127.0.0.1:7860")

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
    client = TestClient(app, base_url="http://127.0.0.1:7860")

    response = client.post(
        "/start",
        headers={"Origin": "http://localhost:3000"},
        json={"transport": "webrtc", "body": {"agent_id": "nonexistent"}},
    )

    assert response.status_code == 404


def test_offer_preflight_rejects_top_level_agent_id(monkeypatch):
    """Top-level agent_id in /api/offer payload is rejected with 400 Bad Request."""
    async def unused_resolver(s, agent_id=None):
        raise AssertionError("Resolver should not be reached when payload structure is invalid")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", unused_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=unused_resolver)
    client = TestClient(app, base_url="http://127.0.0.1:7860")

    response = client.post(
        "/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "agent_id": "profile-1"},
    )

    assert response.status_code == 400
    assert "agent_id must be sent inside request_data" in response.json().get("detail", "")


def test_offer_preflight_rejects_body_field_in_offer_route(monkeypatch):
    """agent_id inside body on /api/offer route is rejected with 400 Bad Request."""
    async def unused_resolver(s, agent_id=None):
        raise AssertionError("Resolver should not be reached when body field is used for offer")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", unused_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=unused_resolver)
    client = TestClient(app, base_url="http://127.0.0.1:7860")

    response = client.post(
        "/api/offer",
        headers={"Origin": "http://localhost:3000"},
        json={"sdp": "offer-sdp", "type": "offer", "body": {"agent_id": "profile-1"}},
    )

    assert response.status_code == 400
    detail = response.json().get("detail", "")
    assert "agent_id must be sent inside request_data for /api/offer" in detail
    assert "not inside body" in detail


def test_start_preflight_rejects_request_data_field_in_start_route(monkeypatch):
    """agent_id inside request_data on /start route is rejected with 400 Bad Request."""
    async def unused_resolver(s, agent_id=None):
        raise AssertionError("Resolver should not be reached when request_data field is used for start")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", unused_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=unused_resolver)
    client = TestClient(app, base_url="http://127.0.0.1:7860")

    response = client.post(
        "/start",
        headers={"Origin": "http://localhost:3000"},
        json={"transport": "webrtc", "request_data": {"agent_id": "profile-1"}},
    )

    assert response.status_code == 400
    detail = response.json().get("detail", "")
    assert "agent_id must be sent inside body for /start" in detail
    assert "not inside request_data" in detail

def test_cors_headers_omitted_for_disallowed_origin(monkeypatch):
    """A foreign Origin is rejected before preflight, with no CORS headers."""
    async def unused_resolver(s, agent_id=None):
        raise AssertionError("Resolver should not be reached for malformed agent_id")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", unused_resolver)

    settings = Settings(_env_file=None, web_port=3000)
    app = _create_test_app(settings, profile_resolver=unused_resolver)
    client = TestClient(app, base_url="http://127.0.0.1:7860")

    response = client.post(
        "/api/offer",
        headers={"Origin": "https://attacker.example"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "  "}},
    )

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers



def test_offer_preflight_forbidden_host_or_origin_cannot_trigger_resolver(monkeypatch):
    """Forbidden Host or Origin is rejected by local guard before offer resolver or preflight runs."""
    resolver_called = False

    async def side_effect_resolver(s, agent_id=None):
        nonlocal resolver_called
        resolver_called = True
        raise AssertionError("Resolver must not be called when request is rejected by local state guard")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", side_effect_resolver)

    settings = Settings(_env_file=None, port=7860, web_port=3000)
    app = _create_test_app(settings, profile_resolver=side_effect_resolver)
    client = TestClient(app, base_url="http://127.0.0.1:7860")

    # 1. Rejected Origin
    response_origin = client.post(
        "/api/offer",
        headers={"Origin": "https://malicious.example"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "test-agent"}},
    )
    assert response_origin.status_code == 403
    assert resolver_called is False
    assert "access-control-allow-origin" not in response_origin.headers

    # 2. Rejected Host
    response_host = client.post(
        "/api/offer",
        headers={"Host": "attacker.example:7860"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "test-agent"}},
    )
    assert response_host.status_code == 403
    assert resolver_called is False

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


def test_offer_preflight_runner_main_sequence_rejects_cross_origin_before_resolver(monkeypatch):
    """Exact production runner sequence (mount_control + mount_api) rejects forbidden request before resolver."""
    resolver_called = False

    async def side_effect_resolver(s, agent_id=None):
        nonlocal resolver_called
        resolver_called = True
        raise AssertionError("Resolver must not be called when request is rejected by local state guard")

    monkeypatch.setattr("bot.agents.service.apply_active_agent_to_settings", side_effect_resolver)

    settings = Settings(_env_file=None, port=7860, web_port=3000)
    app = FastAPI()
    # Exact production runner sequence where mount_control may run before or after mount_api:
    mount_control(app, settings)
    mount_api(app, settings, profile_resolver=side_effect_resolver)

    client = TestClient(app, base_url="http://127.0.0.1:7860")

    response = client.post(
        "/api/offer",
        headers={"Origin": "https://attacker.example"},
        json={"sdp": "offer-sdp", "type": "offer", "request_data": {"agent_id": "test-agent"}},
    )
    assert response.status_code == 403
    assert resolver_called is False
    assert "access-control-allow-origin" not in response.headers


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
