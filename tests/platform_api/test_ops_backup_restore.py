from __future__ import annotations

from datetime import datetime, timezone
import os
import uuid

import asyncpg

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from platform_api.db.base import Base
from platform_api.db.schema import CURRENT_SCHEMA_VERSION, compute_metadata_hash
from platform_api.db.session import create_engine, get_session_factory
from platform_api.events.models import TranscriptSegment
from platform_api.models.auth import AuditLog, User
from platform_api.models.session import Session
from platform_api.models.tariff import ActiveSessionTariff, SessionTariffRecord
from platform_api.models.wallet import WalletAccount
from platform_api.billing.models import WalletEntry
from platform_api.ops.backup import (
    create_backup,
    restore_backup,
    validate_backup_database_url,
    validate_restore_target_database_url,
)
from tests.support.postgres import FreshPostgres


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_backup_restores_platform_data_into_fresh_owned_database(
    initialized_pg: FreshPostgres, tmp_path
):
    now = datetime.now(timezone.utc)
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    event_id = uuid.uuid4()
    async with initialized_pg.new_session() as db:
        async with db.begin():
            user = User(
                id=user_id,
                email=f"backup-{user_id}@example.test",
                email_key=f"backup-{user_id}@example.test",
                display_name="Backup Test",
                password_hash="private-password-hash",
                role="user",
                status="active",
                email_verified_at=now,
                preferred_language="en",
                timezone="UTC",
                low_balance_notifications=True,
                auth_version=1,
                created_at=now,
                updated_at=now,
            )
            db.add(user)
            await db.flush()
            wallet = WalletAccount(
                user_id=user_id,
                currency="INR",
                posted_minor=100,
                reserved_minor=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session = Session(
                id=session_id,
                owner_user_id=user_id,
                tariff_snapshot={"rate_minor_per_minute": 200},
                kind="voice",
                state="COMPLETED",
                version=1,
                created_at=now,
                ended_at=now,
            )
            db.add_all([wallet, session])
            await db.flush()
            db.add_all(
                [
                    WalletEntry(
                        id=uuid.uuid4(),
                        user_id=user_id,
                        kind="credit",
                        amount_minor=100,
                        session_id=None,
                        operation_id=f"backup-{user_id}",
                        actor="operator",
                        reason="synthetic round-trip seed",
                        created_at=now,
                    ),
                    TranscriptSegment(
                        event_id=event_id,
                        session_id=session_id,
                        sequence=1,
                        speaker="user",
                        final_text="TRANSCRIPT_RESTORED_EXACTLY",
                        start_ms=0,
                        end_ms=1000,
                        created_at=now,
                    ),
                    AuditLog(
                        id=uuid.uuid4(),
                        actor_kind="operator",
                        actor_id=None,
                        session_id=session_id,
                        action="test.backup.seed",
                        target_type="session",
                        target_id=session_id,
                        reason="backup round-trip seed",
                        result="success",
                        created_at=now,
                    ),
                ]
            )

    schema = "platform"
    metadata_hash = compute_metadata_hash(Base.metadata)
    backup_path = tmp_path / "platform-backup.zip"
    backup = await create_backup(
        initialized_pg.url,
        output_path=backup_path,
        schema=schema,
        schema_version=CURRENT_SCHEMA_VERSION,
        metadata_hash=metadata_hash,
        max_bytes=64 * 1024 * 1024,
    )
    assert backup_path.stat().st_mode & 0o777 == 0o600
    assert backup.row_counts["wallet_entries"] == 1
    assert backup.row_counts["audit_logs"] == 1

    db_name = f"platform_verify_restore_{uuid.uuid4().hex[:24]}"
    assert len(db_name) <= 63
    admin_url = make_url(initialized_pg.admin_url)
    admin_engine = create_async_engine(
        admin_url.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    target_url = admin_url.set(database=db_name).render_as_string(hide_password=False)
    ownership_token = os.environ["PLATFORM_TEST_DATABASE_OWNERSHIP_TOKEN"]
    try:
        async with admin_engine.connect() as conn:
            owned = await conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public._verification_cluster_ownership "
                    "WHERE token = :token)"
                ),
                {"token": ownership_token},
            )
            assert owned
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))

        restore = await restore_backup(
            backup_path,
            target_url,
            schema=schema,
            schema_version=CURRENT_SCHEMA_VERSION,
            metadata_hash=metadata_hash,
            max_bytes=64 * 1024 * 1024,
            reason="verify backup restore transaction",
        )
        assert restore.row_counts["wallet_entries"] == 1
        target_engine = create_engine(target_url, schema=schema)
        try:
            target_sessions = get_session_factory(target_engine)
            async with target_sessions() as db:
                assert await db.scalar(select(func.count()).select_from(User)) == 1
                assert await db.scalar(select(func.count()).select_from(WalletEntry)) == 1
                assert await db.scalar(select(func.count()).select_from(AuditLog)) == 2
                restored_session = await db.get(Session, session_id)
                assert restored_session.owner_user_id == user_id
                transcript = await db.get(TranscriptSegment, event_id)
                assert transcript.final_text == "TRANSCRIPT_RESTORED_EXACTLY"
                entry = (await db.execute(select(WalletEntry))).scalar_one()
                assert entry.amount_minor == 100
                active_tariff = await db.get(ActiveSessionTariff, 1)
                assert active_tariff is not None
                tariff = await db.get(SessionTariffRecord, active_tariff.current_tariff_id)
                assert tariff is not None
                assert tariff.rate_minor_per_minute == 6000
                assert tariff.billing_slice_ms == 1000
                assert tariff.permit_ttl_ms == 5000
        finally:
            await target_engine.dispose()

        with pytest.raises(ValueError, match="empty"):
            await restore_backup(
                backup_path,
                target_url,
                schema=schema,
                schema_version=CURRENT_SCHEMA_VERSION,
                metadata_hash=metadata_hash,
                max_bytes=64 * 1024 * 1024,
                reason="verify refusal to merge restored database",
            )
    finally:
        async with admin_engine.connect() as conn:
            owned = await conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public._verification_cluster_ownership "
                    "WHERE token = :token)"
                ),
                {"token": ownership_token},
            )
            assert owned
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": db_name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await admin_engine.dispose()

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_backup_requires_read_access_to_new_provider_secret_table(
    initialized_pg: FreshPostgres, tmp_path
):
    schema = "platform"
    metadata_hash = compute_metadata_hash(Base.metadata)
    denied_path = tmp_path / "provider-secrets-denied.zip"
    recovered_path = tmp_path / "provider-secrets-recovered.zip"
    role_name = f"backup_reader_{uuid.uuid4().hex}"
    role_password = uuid.uuid4().hex
    database_name = initialized_pg.db_name
    backup_url = make_url(initialized_pg.url).set(
        username=role_name, password=role_password
    ).render_as_string(hide_password=False)
    admin_url = make_url(initialized_pg.admin_url)
    admin_engine = create_async_engine(
        admin_url.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    platform_admin_engine = create_async_engine(
        admin_url.set(database=database_name).render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    role_created = False
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    f'CREATE ROLE "{role_name}" LOGIN PASSWORD \'{role_password}\''
                )
            )
            role_created = True
            await conn.execute(
                text(f'GRANT CONNECT ON DATABASE "{database_name}" TO "{role_name}"')
            )
        async with platform_admin_engine.connect() as conn:
            await conn.execute(
                text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role_name}"')
            )
            await conn.execute(
                text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "{role_name}"')
            )
            await conn.execute(
                text(
                    f'REVOKE SELECT ON TABLE "{schema}"."provider_secrets" '
                    f'FROM "{role_name}"'
                )
            )

        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await create_backup(
                backup_url,
                output_path=denied_path,
                schema=schema,
                schema_version=CURRENT_SCHEMA_VERSION,
                metadata_hash=metadata_hash,
                max_bytes=64 * 1024 * 1024,
            )
        assert not denied_path.exists()

        async with platform_admin_engine.connect() as conn:
            await conn.execute(
                text(
                    f'GRANT SELECT ON TABLE "{schema}"."provider_secrets" '
                    f'TO "{role_name}"'
                )
            )
            await conn.execute(
                text(
                    f'GRANT SELECT ON TABLE "{schema}"."provider_model_entries" '
                    f'TO "{role_name}"'
                )
            )
        backup = await create_backup(
            backup_url,
            output_path=recovered_path,
            schema=schema,
            schema_version=CURRENT_SCHEMA_VERSION,
            metadata_hash=metadata_hash,
            max_bytes=64 * 1024 * 1024,
        )
        assert recovered_path.is_file()
        assert backup.row_counts["provider_secrets"] == 0
    finally:
        if role_created:
            async with platform_admin_engine.connect() as conn:
                await conn.execute(text(f'DROP OWNED BY "{role_name}"'))
            async with admin_engine.connect() as conn:
                await conn.execute(
                    text(
                        f'REVOKE CONNECT ON DATABASE "{database_name}" '
                        f'FROM "{role_name}"'
                    )
                )
                await conn.execute(text(f'DROP ROLE "{role_name}"'))
        await platform_admin_engine.dispose()
        await admin_engine.dispose()


def test_operations_database_urls_require_separate_roles_and_safe_targets():
    source_url = "postgresql+asyncpg://app:secret@db.example:5432/platform"
    backup_url = "postgresql+asyncpg://backup:secret@db.example:5432/platform"
    restore_url = "postgresql+asyncpg://restore:secret@db.example:5432/platform_restore"

    assert validate_backup_database_url(source_url, backup_url) == backup_url
    assert validate_restore_target_database_url(source_url, restore_url) == restore_url
    with pytest.raises(ValueError, match="separate database role"):
        validate_backup_database_url(source_url, source_url)
    with pytest.raises(ValueError, match="DATABASE_URL database"):
        validate_backup_database_url(
            source_url, "postgresql+asyncpg://backup:secret@db.example:5432/other"
        )
    with pytest.raises(ValueError, match="other than DATABASE_URL"):
        validate_restore_target_database_url(
            source_url, "postgresql+asyncpg://restore:secret@db.example:5432/platform"
        )
    with pytest.raises(ValueError, match="query parameters"):
        validate_backup_database_url(
            source_url,
            "postgresql+asyncpg://backup:secret@db.example:5432/platform?host=elsewhere",
        )
