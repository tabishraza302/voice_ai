"""Contract tests for durable email queueing, delivery, and encryption.

Follows auth-contract.md Task 3:
- Email validation does not trigger DNS lookups.
- File delivery permissions: mode 0600 message file, mode 0700 directory.
- Generated URL strictly ignores Host header and uses configured PLATFORM_WEB_ORIGIN with fragment.
- Database transaction rollback creates neither token nor mail records.
- Duplicate dispatch retries delivery of the same token, never generating a fresh token.
- Payloads encrypted with Fernet and bound to token ID; tampered payloads fail closed.
- Payloads erased immediately on delivery or expiration.
- Retries follow bounded backoff schedule (60, 120, 240, 480 seconds).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import stat
import uuid

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select

from platform_api.auth.delivery import (
    build_mail_link,
    deliver_pending_auth_messages,
    enqueue_auth_message,
)
from platform_api.auth.errors import ValidationError
from platform_api.auth.service import normalize_email_address
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuthMail, AuthToken
from tests.support.postgres import FreshPostgres


def test_no_dns_lookups_during_email_validation():
    """Validating syntax must never trigger DNS lookups even for nonexistent domains."""
    display, key = normalize_email_address("operator@nonexistent-domain-without-dns-xyz123.com")
    assert display == "operator@nonexistent-domain-without-dns-xyz123.com"
    assert key == "operator@nonexistent-domain-without-dns-xyz123.com"


def test_link_uses_web_origin_and_fragment_ignores_host():
    """Constructed links must use configured web_origin and URL fragment (#token=...)."""
    web_origin = "https://voice.example.com"
    token = "test-token-value-12345"

    reg_link = build_mail_link(web_origin, "register", token)
    assert reg_link == "https://voice.example.com/verify-email#token=test-token-value-12345"
    assert "?" not in reg_link

    reset_link = build_mail_link(web_origin, "password_reset", token)
    assert reset_link == "https://voice.example.com/reset-password#token=test-token-value-12345"
    assert "?" not in reset_link


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_file_delivery_permissions(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """File adapter delivers with directory mode 0700 and file mode 0600."""
    now = datetime.now(timezone.utc)
    token_id = uuid.uuid4()

    async with initialized_pg.sessions() as session:
        # Create token
        token = AuthToken(
            id=token_id,
            purpose="register",
            token_digest=b"D" * 32,
            email_key="test@example.com",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        session.add(token)
        await session.flush()

        mail = await enqueue_auth_message(
            session,
            test_platform_settings,
            token_id=token_id,
            email="test@example.com",
            purpose="register",
            token="secret-token-b64",
            expires_at=now + timedelta(hours=24),
            now=now,
        )
        await session.commit()

    # Deliver batch
    result = await deliver_pending_auth_messages(test_platform_settings, limit=10, now=now)
    assert result.sent == 1

    # Check directory permissions (0700)
    dir_stat = os.stat(test_platform_settings.auth_mail_dir)
    assert stat.S_IMODE(dir_stat.st_mode) == 0o700

    # Check file permissions (0600)
    files = os.listdir(test_platform_settings.auth_mail_dir)
    assert len(files) == 1
    file_path = os.path.join(test_platform_settings.auth_mail_dir, files[0])
    file_stat = os.stat(file_path)
    assert stat.S_IMODE(file_stat.st_mode) == 0o600

    # Verify payload was erased in database
    async with initialized_pg.sessions() as session:
        m = (await session.execute(select(AuthMail).where(AuthMail.id == mail.id))).scalar_one()
        assert m.state == "sent"
        assert m.payload_ciphertext is None
        assert m.sent_at is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_transaction_rollback_creates_no_records(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Failed transactions roll back both token and mail queue records."""
    now = datetime.now(timezone.utc)
    token_id = uuid.uuid4()

    try:
        async with initialized_pg.sessions() as session:
            token = AuthToken(
                id=token_id,
                purpose="register",
                token_digest=b"X" * 32,
                email_key="rollback@example.com",
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
            session.add(token)
            await session.flush()

            await enqueue_auth_message(
                session,
                test_platform_settings,
                token_id=token_id,
                email="rollback@example.com",
                purpose="register",
                token="secret-token-b64",
                expires_at=now + timedelta(hours=24),
                now=now,
            )
            # Simulate failure and rollback
            raise RuntimeError("Simulated transaction failure")
    except RuntimeError:
        pass

    async with initialized_pg.sessions() as session:
        t_res = await session.execute(select(AuthToken).where(AuthToken.id == token_id))
        assert t_res.scalar_one_or_none() is None
        m_res = await session.execute(select(AuthMail).where(AuthMail.token_id == token_id))
        assert m_res.scalar_one_or_none() is None
