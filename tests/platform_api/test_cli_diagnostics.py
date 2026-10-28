"""Tests for CLI entrypoints, command set, and secret suppression in diagnostics.

Verifies:
- bootstrap-admin and deliver-auth-mail are removed and not recognized as commands until Task 1
- CLI configuration errors format safely without leaking raw input values, passwords, or connection strings
- Database exceptions in init-db and check-schema suppress credentials and secret connection URLs
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from platform_api.cli import _check_schema_command, _init_db_command, main
from platform_api.config import PlatformSettings

VALID_CSRF_KEY = "Q0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0M"
VALID_RATE_KEY = "UlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlI"
VALID_MFA_KEY = "TU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU1NTU0="


def test_cli_rejects_removed_task1_commands(capsys):
    """bootstrap-admin and deliver-auth-mail must not be present as commands in CLI."""
    with pytest.raises(SystemExit) as exc_info:
        main(["bootstrap-admin", "--email", "admin@example.com"])
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        main(["deliver-auth-mail"])
    assert exc_info.value.code == 2


def test_cli_config_error_suppresses_secrets(monkeypatch, capsys):
    """Configuration validation errors in CLI must not leak secrets or raw connection strings."""
    secret_pw = "super_secret_operator_password_xyz123"
    secret_url = f"postgresql+asyncpg://voice_ai:{secret_pw}@127.0.0.1:5439/voice_ai_platform"

    monkeypatch.setenv("PLATFORM_DATABASE_URL", secret_url)
    monkeypatch.delenv("PLATFORM_AUTH_CSRF_KEY", raising=False)
    monkeypatch.delenv("PLATFORM_AUTH_RATE_KEY", raising=False)
    monkeypatch.delenv("PLATFORM_AUTH_MFA_KEY", raising=False)

    exit_code = main(["init-db"])
    assert exit_code == 1

    captured = capsys.readouterr()
    assert secret_pw not in captured.err
    assert secret_pw not in captured.out
    assert "Configuration error:" in captured.err
    # Location name should be reported safely
    assert "input_value" not in captured.err


@pytest.mark.asyncio
async def test_init_db_suppresses_db_exceptions(capsys):
    """Database exceptions during init-db must not leak secret connection strings."""
    secret_pw = "super_secret_db_pass_999"
    secret_url = f"postgresql+asyncpg://admin:{secret_pw}@127.0.0.1:5439/voice_ai_platform"

    settings = PlatformSettings(
        database_url=secret_url,
        auth_csrf_key=VALID_CSRF_KEY,
        auth_rate_key=VALID_RATE_KEY,
        auth_mfa_key=VALID_MFA_KEY,
    )

    class ErrorAsyncContextManager:
        async def __aenter__(self):
            raise ConnectionRefusedError(f"FATAL: password authentication failed for user 'admin' on {secret_url}")
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"
    mock_engine.begin.side_effect = lambda *args, **kwargs: ErrorAsyncContextManager()
    mock_engine.connect.side_effect = lambda *args, **kwargs: ErrorAsyncContextManager()
    mock_engine.dispose = AsyncMock()

    with patch("platform_api.cli.create_engine", return_value=mock_engine):
        exit_code = await _init_db_command(settings)
        assert exit_code == 2

    captured = capsys.readouterr()
    assert secret_pw not in captured.err
    assert "admin:super_secret" not in captured.err
    assert "Database operation failed" in captured.err


@pytest.mark.asyncio
async def test_check_schema_suppresses_db_exceptions(capsys):
    """Database exceptions during check-schema must not leak secret connection strings."""
    secret_pw = "super_secret_db_pass_888"
    secret_url = f"postgresql+asyncpg://admin:{secret_pw}@127.0.0.1:5439/voice_ai_platform"

    settings = PlatformSettings(
        database_url=secret_url,
        auth_csrf_key=VALID_CSRF_KEY,
        auth_rate_key=VALID_RATE_KEY,
        auth_mfa_key=VALID_MFA_KEY,
    )

    class ErrorAsyncContextManager:
        async def __aenter__(self):
            raise ConnectionRefusedError(f"Connection failed to {secret_url}")
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"
    mock_engine.connect.side_effect = lambda *args, **kwargs: ErrorAsyncContextManager()
    mock_engine.dispose = AsyncMock()

    with patch("platform_api.cli.create_engine", return_value=mock_engine):
        exit_code = await _check_schema_command(settings)
        assert exit_code == 2

    captured = capsys.readouterr()
    assert secret_pw not in captured.err
    assert "Database operation failed" in captured.err
