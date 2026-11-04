"""Negative behavioral tests for platform database session dependency.

Verifies:
- get_db raises RuntimeError when request.app.state.session_factory is absent
- No global session factory fallback exists
- Database-backed routes fail closed after schema readiness fails
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest
from fastapi import Request

from platform_api.auth.errors import DependencyUnavailableError
from platform_api.db.session import get_db


@pytest.mark.asyncio
async def test_get_db_rejects_missing_app_state_session_factory():
    """get_db must raise RuntimeError if session_factory is not present on app.state."""
    mock_request = MagicMock(spec=Request)
    mock_request.app.state = MagicMock()
    # Deliberately omit session_factory
    del mock_request.app.state.session_factory

    with pytest.raises(RuntimeError):
        gen = get_db(mock_request)
        await anext(gen)


@pytest.mark.asyncio
async def test_get_db_fails_closed_after_schema_readiness_failure():
    """Database-backed routes cannot use the session factory after failed schema startup."""
    mock_request = MagicMock(spec=Request)
    mock_request.app.state = MagicMock()
    mock_request.app.state.schema_ready = False
    mock_request.app.state.session_factory = MagicMock()

    with pytest.raises(DependencyUnavailableError):
        await anext(get_db(mock_request))
    mock_request.app.state.session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_database_route_fails_closed_when_schema_is_unready(app_client):
    app_client._transport.app.state.schema_ready = False

    response = await app_client.get("/api/v1/admin/providers")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

    liveness = await app_client.get("/health/live")
    assert liveness.status_code == 200


@pytest.mark.asyncio
async def test_get_db_rejects_none_request():
    """get_db must require a request with app.state and not fall back to globals."""
    with pytest.raises(RuntimeError):
        gen = get_db(None)  # type: ignore
        await anext(gen)
