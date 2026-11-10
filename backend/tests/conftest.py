"""Test environment defaults.

This file is imported before any test module. It deliberately overrides
DATABASE_URL with a disposable test database so that running pytest can
never drop the application's tables, even when DATABASE_URL is set to the
production Compose database.
"""

import os

import pytest

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test",
)
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.db import engine  # noqa: E402


@pytest.fixture(autouse=True)
async def dispose_engine_after_test(anyio_backend, request):
    """Close pooled connections in the loop that opened them.

    AnyIO gives each async test its own event loop.  asyncpg connections are
    bound to the loop that created them, so the pool must be emptied before
    that loop closes.  Sync tests never touch the database engine, so there is
    nothing to dispose for them.
    """
    yield
    if request.node.get_closest_marker("anyio") is not None:
        await engine.dispose()
