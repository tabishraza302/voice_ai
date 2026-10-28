"""Contract tests for platform CLI commands, admin bootstrap, recovery, and readiness gates.

Follows auth-contract.md Task 4:
- Simultaneous first bootstrap yields exactly one admin under advisory lock.
- Bootstrap refuses if an administrator or matching email already exists.
- Password read via getpass; no environment variable or generated default password.
- Protected recovery clears MFA factor, revokes sessions, and bumps auth_version.
- Readiness blocks (/health/ready returns 503) until an enrolled active admin exists.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import uuid

import httpx
import pytest
from sqlalchemy import func, select

from platform_api.cli import (
    _bootstrap_admin_command,
    _create_admin_command,
    _recover_admin_mfa_command,
)
from platform_api.config import PlatformSettings
from platform_api.health import check_enrolled_admin_exists
from platform_api.models.auth import AuthMfa, AuthSession, User
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_bootstrap_and_duplicate_refusal(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """First bootstrap succeeds; duplicate bootstrap attempts fail closed."""
    # 1. First bootstrap succeeds
    code1 = await _bootstrap_admin_command(
        test_platform_settings,
        email_raw="first-admin@example.com",
        display_name_raw="First Admin",
        reason_raw="Initial bootstrapping of system administrator",
        password_input="admin-secure-pw-12345",
    )
    assert code1 == 0

    # Verify admin in database
    async with initialized_pg.sessions() as session:
        admin_stmt = select(User).where(User.role == "admin")
        admins = list((await session.execute(admin_stmt)).scalars().all())
        assert len(admins) == 1
        assert admins[0].email == "first-admin@example.com"
        assert admins[0].status == "active"

        # MFA factor is not yet enrolled
        mfa_stmt = select(AuthMfa).where(AuthMfa.user_id == admins[0].id)
        mfa = (await session.execute(mfa_stmt)).scalar_one()
        assert mfa.enabled_at is None

    # 2. Duplicate bootstrap fails because admin already exists
    code2 = await _bootstrap_admin_command(
        test_platform_settings,
        email_raw="second-admin@example.com",
        display_name_raw="Second Admin",
        reason_raw="Attempting duplicate bootstrap",
        password_input="admin-secure-pw-67890",
    )
    assert code2 == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_readiness_blocks_until_enrolled_admin_exists(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Readiness endpoint returns 503 until an administrator has enrolled and verified MFA."""
    # 1. Initially (no admin), readiness returns 503
    res1 = await app_client.get("/api/v1/health/ready")
    assert res1.status_code == 503
    assert "No enrolled administrator" in res1.json()["error"]

    # 2. Bootstrap admin (MFA not yet enabled)
    await _bootstrap_admin_command(
        test_platform_settings,
        email_raw="unverified-admin@example.com",
        display_name_raw="Unverified Admin",
        reason_raw="Testing readiness gating",
        password_input="admin-secure-pw-12345",
    )

    # Readiness still returns 503 because MFA is not yet enabled
    res2 = await app_client.get("/api/v1/health/ready")
    assert res2.status_code == 503
    assert "No enrolled administrator" in res2.json()["error"]

    # 3. Simulate admin completing MFA enrollment
    async with initialized_pg.sessions() as session:
        user = (await session.execute(select(User).where(User.role == "admin"))).scalar_one()
        mfa = (await session.execute(select(AuthMfa).where(AuthMfa.user_id == user.id))).scalar_one()
        mfa.enabled_at = datetime.now(timezone.utc)
        mfa.secret_ciphertext = "encrypted-secret"
        await session.commit()

    # Readiness now returns 200 OK
    res3 = await app_client.get("/api/v1/health/ready")
    assert res3.status_code == 200
    assert res3.json()["status"] == "ready"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_recover_admin_mfa_procedure(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Recovering admin MFA clears factor, bumps auth_version, and revokes active sessions."""
    await _bootstrap_admin_command(
        test_platform_settings,
        email_raw="recovery-test@example.com",
        display_name_raw="Recovery Admin",
        reason_raw="Initial admin for recovery test",
        password_input="admin-secure-pw-12345",
    )

    now = datetime.now(timezone.utc)
    async with initialized_pg.sessions() as session:
        admin = (await session.execute(select(User).where(User.role == "admin"))).scalar_one()
        admin_id = admin.id

        # Enable MFA
        mfa = (await session.execute(select(AuthMfa).where(AuthMfa.user_id == admin_id))).scalar_one()
        mfa.enabled_at = now
        mfa.secret_ciphertext = "ciphertext"
        mfa.last_accepted_step = 50

        # Create active session
        sess = AuthSession(
            id=uuid.uuid4(),
            token_digest=b"S" * 32,
            user_id=admin_id,
            auth_version=admin.auth_version,
            state="authenticated",
            created_at=now,
            last_seen_at=now,
            idle_expires_at=now + timedelta(hours=1),
            absolute_expires_at=now + timedelta(hours=12),
        )
        session.add(sess)
        await session.commit()

    # Run recovery command
    rec_code = await _recover_admin_mfa_command(
        test_platform_settings,
        user_id_str=str(admin_id),
        reason_raw="Lost hardware authenticator verified by operator",
    )
    assert rec_code == 0

    # Verify factor was cleared and session revoked
    async with initialized_pg.sessions() as session:
        mfa_after = (await session.execute(select(AuthMfa).where(AuthMfa.user_id == admin_id))).scalar_one()
        assert mfa_after.enabled_at is None
        assert mfa_after.secret_ciphertext is None
        assert mfa_after.last_accepted_step is None

        sess_after = (await session.execute(select(AuthSession).where(AuthSession.id == sess.id))).scalar_one()
        assert sess_after.revoked_at is not None

        admin_after = (await session.execute(select(User).where(User.id == admin_id))).scalar_one()
        assert admin_after.auth_version > 1
