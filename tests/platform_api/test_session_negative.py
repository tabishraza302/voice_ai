"""Negative behavioral tests for platform database session dependency.

Verifies:
- get_db raises RuntimeError when request.app.state.session_factory is absent
- No global session factory fallback exists
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest
from fastapi import Request

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
async def test_get_db_rejects_none_request():
    """get_db must require a request with app.state and not fall back to globals."""
    with pytest.raises(RuntimeError):
        gen = get_db(None)  # type: ignore
        await anext(gen)
