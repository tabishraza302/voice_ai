"""Test environment defaults.

This file is imported before any test module. It deliberately overrides
DATABASE_URL with a disposable test database so that running pytest can
never drop the application's tables, even when DATABASE_URL is set to the
production Compose database.
"""

from pathlib import Path
import os
import uuid

import pytest

# Read .env from repo root if present
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test",
)
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")

from app.db import engine  # noqa: E402
from app.redis import open_redis_resources, RedisResources  # noqa: E402


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


@pytest.fixture
async def redis_resources(anyio_backend):
    test_redis_url = os.environ.get("TEST_REDIS_URL", os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/0"))
    namespace = f"voice-ai-test:{uuid.uuid4().hex}"
    resources = await open_redis_resources(test_redis_url, namespace)
    try:
        await resources.control.ping()
        yield resources
    finally:
        async for key in resources.commands.scan_iter(match=f"{namespace}:*"):
            await resources.commands.unlink(key)
        await resources.close()
