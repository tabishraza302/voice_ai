"""Negative behavioral tests for platform health and readiness endpoints.

Verifies:
- Readiness uses app.state (settings, engine, session_factory), not global settings or engine
- Readiness suppresses exception secrets (no connection strings, passwords, or stack traces leak)
- Readiness fails closed (503) when schema readiness returns False
- Readiness fails closed (503) when no active administrator with enrolled MFA exists
- Readiness fails closed (503) when enabled dependencies are missing
- Lifespan cleanly disposes app.state.engine inside finally block even if errors occur
"""

from __future__ import annotations

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from platform_api.app import create_app, lifespan
from platform_api.config import PlatformSettings
from platform_api.health import router as health_router


@pytest.fixture
def mock_settings() -> PlatformSettings:
    return PlatformSettings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai_platform",
        schema_version="platform-v1",
        auth_csrf_key=base64.urlsafe_b64encode(b"C" * 32).decode("ascii"),
        auth_rate_key=base64.urlsafe_b64encode(b"R" * 32).decode("ascii"),
        auth_mfa_key=base64.urlsafe_b64encode(b"M" * 32).decode("ascii"),
    )


def test_readiness_fails_if_app_state_uninitialized():
    """Readiness probe must fail with 503 if app state engine/settings are not mounted."""
    app = FastAPI()
    app.include_router(health_router)
    client = TestClient(app)

    response = client.get("/health/ready")
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    data = response.json()
    assert data["status"] == "not_ready"
    assert response.headers.get("Retry-After") == "5"


def test_readiness_suppresses_exception_secrets(mock_settings):
    """Readiness probe must suppress DB connection strings and internal exceptions."""
    app = FastAPI()
    app.state.settings = mock_settings

    # Mock engine whose connection raises an exception with secret credentials
    secret_url = "postgresql+asyncpg://secret_user:super_secret_password@db.internal:5432/db"

    class ErrorAsyncContextManager:
        async def __aenter__(self):
            raise ConnectionRefusedError(f"Failed to connect to {secret_url}: access denied")
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"
    mock_engine.connect.side_effect = lambda *args, **kwargs: ErrorAsyncContextManager()
    mock_engine.dispose = AsyncMock()
    app.state.engine = mock_engine
    app.state.session_factory = MagicMock()
    app.include_router(health_router)
    client = TestClient(app)

    response = client.get("/health/ready")
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    data = response.json()
    assert data["status"] == "not_ready"
    # Secrets must not appear anywhere in response text
    assert "super_secret_password" not in response.text
    assert "secret_user" not in response.text
    assert "db.internal" not in response.text
    assert "unavailable" in data["error"].lower() or "database" in data["error"].lower()
    assert response.headers.get("Retry-After") == "5"


def test_readiness_fails_closed_when_schema_not_ready(mock_settings):
    """Readiness probe must return 503 when schema does not match expected version."""
    app = FastAPI()
    app.state.settings = mock_settings

    with patch("platform_api.health.check_schema_readiness", new=AsyncMock(return_value=False)):
        app.state.engine = MagicMock()
        app.include_router(health_router)
        client = TestClient(app)

        response = client.get("/health/ready")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        data = response.json()
        assert data["status"] == "not_ready"
        assert "schema" in data["error"].lower()
        assert response.headers.get("Retry-After") == "5"


def test_readiness_fails_closed_absent_enrolled_admin(mock_settings):
    """Readiness probe must return 503 when no active admin with enrolled MFA exists."""
    app = FastAPI()
    app.state.settings = mock_settings
    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"
    app.state.engine = mock_engine
    app.state.session_factory = MagicMock()
    # Schema is ready, but no admin enrolled
    with patch("platform_api.health.check_schema_readiness", new=AsyncMock(return_value=True)), \
         patch("platform_api.health.check_enrolled_admin_exists", new=AsyncMock(return_value=False)):
        app.include_router(health_router)
        client = TestClient(app)

        response = client.get("/health/ready")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        data = response.json()
        assert data["status"] == "not_ready"
        assert "enrolled admin" in data["error"].lower()
        assert response.headers.get("Retry-After") == "5"


def test_readiness_fails_closed_when_enabled_dependency_missing(mock_settings):
    """Readiness probe must return 503 if an enabled feature flag lacks required dependency."""
    app = FastAPI()
    # Enable gateway without gateway configuration
    settings = PlatformSettings(
        database_url=mock_settings.database_url,
        auth_csrf_key=mock_settings.auth_csrf_key,
        auth_rate_key=mock_settings.auth_rate_key,
        auth_mfa_key=mock_settings.auth_mfa_key,
        enable_gateway=True,
    )
    app.state.settings = settings
    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"
    app.state.engine = mock_engine
    app.state.session_factory = MagicMock()
    with patch("platform_api.health.check_schema_readiness", new=AsyncMock(return_value=True)), \
         patch("platform_api.health.check_enrolled_admin_exists", new=AsyncMock(return_value=True)):
        app.include_router(health_router)
        client = TestClient(app)

        response = client.get("/health/ready")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        data = response.json()
        assert data["status"] == "not_ready"
        assert "gateway" in data["error"].lower()


@pytest.mark.asyncio
async def test_lifespan_dispose_in_finally(mock_settings):
    """Lifespan must cleanly dispose engine in finally block even if startup error occurs."""
    app = FastAPI()
    app.state.settings = mock_settings

    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with patch("platform_api.app.create_engine", return_value=mock_engine), \
         patch("platform_api.app.check_schema_readiness", side_effect=RuntimeError("Startup check failed")):
        # When lifespan runs and an exception occurs, engine.dispose must still be awaited
        try:
            async with lifespan(app):
                raise RuntimeError("Runtime crash inside app lifespan")
        except RuntimeError:
            pass

        assert mock_engine.dispose.await_count >= 1


@pytest.mark.asyncio
async def test_check_enrolled_admin_exists_logic():
    """Verify check_enrolled_admin_exists evaluates session query result."""
    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_res
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__.return_value = mock_session

    from platform_api.health import check_enrolled_admin_exists
    assert await check_enrolled_admin_exists(mock_factory) is False

    mock_res.scalar_one_or_none.return_value = uuid.uuid4()
    assert await check_enrolled_admin_exists(mock_factory) is True