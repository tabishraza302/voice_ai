"""Shared fixtures for platform_api test suites.

Provides configured PlatformSettings, cryptography keys, app factories,
and test client builders.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import os
from typing import AsyncIterator
import uuid

from cryptography.fernet import Fernet
import httpx
import pytest
import pytest_asyncio

from platform_api.app import create_app
from platform_api.config import PlatformSettings
from platform_api.db.base import Base
from platform_api.db.schema import compute_metadata_hash, initialize_schema
from tests.support.postgres import FreshPostgres

TEST_CSRF_KEY = base64.urlsafe_b64encode(b"C" * 32).decode("ascii")
TEST_RATE_KEY = base64.urlsafe_b64encode(b"R" * 32).decode("ascii")
TEST_MFA_KEY = Fernet.generate_key().decode("ascii")


@pytest.fixture
def test_platform_settings(fresh_pg: FreshPostgres, tmp_path) -> PlatformSettings:
    """Return verified PlatformSettings connected to fresh test PostgreSQL database."""
    mail_dir = str(tmp_path / "test-mail")
    return PlatformSettings(
        database_url=fresh_pg.url,
        auth_csrf_key=TEST_CSRF_KEY,
        auth_rate_key=TEST_RATE_KEY,
        auth_mfa_key=TEST_MFA_KEY,
        env="development",
        web_origin="http://127.0.0.1:3001",
        cors_origins=["http://127.0.0.1:3001"],
        cookie_secure=False,
        session_cookie_name="platform_dev_session",
        csrf_cookie_name="platform_csrf",
        auth_mail_mode="file",
        auth_mail_dir=mail_dir,
    )


@pytest_asyncio.fixture
async def initialized_pg(fresh_pg: FreshPostgres) -> FreshPostgres:
    """Initialize platform schema on fresh_pg database and verify."""
    await fresh_pg.initialize()
    return fresh_pg


@pytest_asyncio.fixture
async def app_client(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield httpx.AsyncClient interacting with a fully lifespan-initialized platform app."""
    app = create_app(test_platform_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            yield client
