"""Test environment defaults.

This file is imported before any test module. It deliberately overrides
DATABASE_URL with a disposable test database so that running pytest can
never drop the application's tables, even when DATABASE_URL is set to the
production Compose database.
"""

import os

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://voice:voice@127.0.0.1:55432/voice_test",
)
os.environ.setdefault("SESSION_SECRET", "test-only-secret-with-at-least-32-bytes")
os.environ.setdefault("APP_ORIGIN", "http://localhost:3000")
