"""Command-line entrypoints for the platform control plane.

Entrypoints:
- init-db / init-schema: Initialize fresh PostgreSQL platform database
- check-schema: Verify current database schema matches platform metadata
- bootstrap-admin: First-time admin creation under PostgreSQL advisory lock
- create-admin: Operator command to create additional administrators
- recover-admin-mfa: Reset lost admin factor and require fresh enrollment
- deliver-auth-mail: Dispatch pending transactional emails
- prune-auth: Cleanup expired rate limit counters, tokens, and sessions

All exceptions and configuration errors are sanitized to prevent credential leaks.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import getpass
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Sequence
import uuid

from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.accounts.service import initialize_user_account
from platform_api.audit import record_audit
from platform_api.auth.delivery import deliver_pending_auth_messages
from platform_api.auth.passwords import get_password_hasher, validate_password_rules
from platform_api.auth.service import normalize_email_address, validate_display_name
from platform_api.auth.sessions import revoke_all_user_sessions
from platform_api.config import PlatformSettings, get_settings
from platform_api.db.base import Base
from platform_api.db.schema import (
    SchemaMismatch,
    check_schema_readiness,
    compute_metadata_hash,
    initialize_schema,
)
from platform_api.db.session import create_engine, get_session_factory
from platform_api.ops.backup import (
    create_backup,
    restore_backup,
    validate_backup_database_url,
    validate_restore_target_database_url,
)
from platform_api.ops.privacy import (
    MAX_RETENTION_BATCH,
    apply_retention,
    export_user_data,
    redact_user_transcripts,
    sanitize_operator_reason,
    validate_retention_database_url,
)
import platform_api.models  # Ensure all platform models are registered
from platform_api.models.auth import (
    AuthMail,
    AuthMfa,
    AuthRateLimit,
    AuthSession,
    AuthToken,
    User,
)

PLATFORM_ADMIN_ADVISORY_LOCK_ID = 8100984920448107371


def _format_safe_validation_error(exc: ValidationError) -> str:
    """Format ValidationError without leaking raw input values, passwords, or secrets."""
    safe_parts = []
    for error in exc.errors():
        loc = " -> ".join(str(elem) for elem in error.get("loc", []))
        msg = error.get("msg", "invalid")
        safe_parts.append(f"{loc}: {msg}")
    return "; ".join(safe_parts)


async def _init_db_command(settings: PlatformSettings) -> int:
    """Execute schema initialization with advisory transaction lock."""
    engine = create_engine(settings)
    try:
        expected_hash = compute_metadata_hash(Base.metadata)
        await initialize_schema(
            engine,
            schema=settings.postgres_schema,
            expected_version=settings.schema_version,
            metadata_hash=expected_hash,
        )
        print(f"Platform database schema initialized (revision {settings.schema_version})")
        return 0
    except SchemaMismatch as exc:
        print(f"Schema mismatch error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Database operation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


async def _check_schema_command(settings: PlatformSettings) -> int:
    """Verify if database schema matches expected platform metadata."""
    engine = create_engine(settings)
    try:
        is_ready = await check_schema_readiness(
            engine,
            schema=settings.postgres_schema,
            expected_version=settings.schema_version,
        )
        if is_ready:
            print(f"Platform schema is ready and verified (version {settings.schema_version})")
            return 0
        else:
            print("Platform schema verification failed", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"Database operation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


async def _bootstrap_admin_command(
    settings: PlatformSettings,
    email_raw: str,
    display_name_raw: str,
    reason_raw: str,
    password_input: str | None = None,
) -> int:
    """Create the first system administrator under exclusive advisory lock."""
    display_email, email_key = normalize_email_address(email_raw)
    display_name = validate_display_name(display_name_raw)
    reason = reason_raw.strip()
    if len(reason) < 10:
        print("Reason must be at least 10 characters", file=sys.stderr)
        return 1

    if password_input is None:
        pw1 = getpass.getpass("Enter administrator password: ")
        pw2 = getpass.getpass("Confirm administrator password: ")
        if pw1 != pw2:
            print("Passwords do not match", file=sys.stderr)
            return 1
        password = pw1
    else:
        password = password_input

    try:
        validate_password_rules(password)
    except Exception as e:
        print(f"Password rejected: {e}", file=sys.stderr)
        return 1

    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    now = datetime.now(timezone.utc)

    try:
        async with session_factory() as session:
            # Exclusive PostgreSQL advisory lock
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": PLATFORM_ADMIN_ADVISORY_LOCK_ID},
            )

            # Check if any admin exists
            count_stmt = select(func.count(User.id)).where(User.role == "admin")
            admin_count = (await session.execute(count_stmt)).scalar_one()
            if admin_count > 0:
                print("Refused: administrator account already exists", file=sys.stderr)
                return 1

            # Check if email is in use
            user_stmt = select(User).where(User.email_key == email_key)
            existing_user = (await session.execute(user_stmt)).scalar_one_or_none()
            if existing_user is not None:
                print("Refused: email address is already registered", file=sys.stderr)
                return 1

            hasher = get_password_hasher(settings.auth_hash_concurrency)
            password_hash = await hasher.hash_password(password)

            admin_user = User(
                id=uuid.uuid4(),
                email=display_email,
                email_key=email_key,
                display_name=display_name,
                password_hash=password_hash,
                role="admin",
                status="active",
                email_verified_at=now,
                preferred_language="en",
                timezone="Asia/Kolkata",
                low_balance_notifications=True,
                auth_version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(admin_user)
            await session.flush()

            # Initialize empty MFA record (admin must enroll through web UI)
            mfa_record = AuthMfa(
                user_id=admin_user.id,
                secret_ciphertext=None,
                pending_secret_ciphertext=None,
                pending_expires_at=None,
                enabled_at=None,
                last_accepted_step=None,
            )
            session.add(mfa_record)
            await session.flush()

            # Initialize zero-credit wallet account
            await initialize_user_account(session, user_id=admin_user.id, now=now)

            # Record operator audit log
            await record_audit(
                session,
                actor_kind="operator",
                action="admin.bootstrap",
                target_type="user",
                target_id=admin_user.id,
                reason=reason,
                result="success",
                created_at=now,
            )

            await session.commit()
            print(json.dumps({"user_id": str(admin_user.id), "result": "bootstrapped"}))
            return 0
    finally:
        await engine.dispose()


async def _create_admin_command(
    settings: PlatformSettings,
    email_raw: str,
    display_name_raw: str,
    reason_raw: str,
    password_input: str | None = None,
) -> int:
    """Operator command to create additional administrator."""
    display_email, email_key = normalize_email_address(email_raw)
    display_name = validate_display_name(display_name_raw)
    reason = reason_raw.strip()
    if len(reason) < 10:
        print("Reason must be at least 10 characters", file=sys.stderr)
        return 1

    if password_input is None:
        pw1 = getpass.getpass("Enter administrator password: ")
        pw2 = getpass.getpass("Confirm administrator password: ")
        if pw1 != pw2:
            print("Passwords do not match", file=sys.stderr)
            return 1
        password = pw1
    else:
        password = password_input

    try:
        validate_password_rules(password)
    except Exception as e:
        print(f"Password rejected: {e}", file=sys.stderr)
        return 1

    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    now = datetime.now(timezone.utc)

    try:
        async with session_factory() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": PLATFORM_ADMIN_ADVISORY_LOCK_ID},
            )

            # Check if email is in use
            user_stmt = select(User).where(User.email_key == email_key)
            existing_user = (await session.execute(user_stmt)).scalar_one_or_none()
            if existing_user is not None:
                print("Refused: email address is already registered", file=sys.stderr)
                return 1

            hasher = get_password_hasher(settings.auth_hash_concurrency)
            password_hash = await hasher.hash_password(password)

            admin_user = User(
                id=uuid.uuid4(),
                email=display_email,
                email_key=email_key,
                display_name=display_name,
                password_hash=password_hash,
                role="admin",
                status="active",
                email_verified_at=now,
                preferred_language="en",
                timezone="Asia/Kolkata",
                low_balance_notifications=True,
                auth_version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(admin_user)
            await session.flush()

            mfa_record = AuthMfa(
                user_id=admin_user.id,
                secret_ciphertext=None,
                pending_secret_ciphertext=None,
                pending_expires_at=None,
                enabled_at=None,
                last_accepted_step=None,
            )
            session.add(mfa_record)
            await session.flush()

            await initialize_user_account(session, user_id=admin_user.id, now=now)

            await record_audit(
                session,
                actor_kind="operator",
                action="admin.created",
                target_type="user",
                target_id=admin_user.id,
                reason=reason,
                result="success",
                created_at=now,
            )

            await session.commit()
            print(json.dumps({"user_id": str(admin_user.id), "result": "created"}))
            return 0
    finally:
        await engine.dispose()


async def _recover_admin_mfa_command(
    settings: PlatformSettings,
    user_id_str: str,
    reason_raw: str,
) -> int:
    """Reset administrator MFA factor and require re-enrollment."""
    reason = reason_raw.strip()
    if len(reason) < 10:
        print("Reason must be at least 10 characters", file=sys.stderr)
        return 1

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        print("Invalid user ID UUID", file=sys.stderr)
        return 1

    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    now = datetime.now(timezone.utc)

    try:
        async with session_factory() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": PLATFORM_ADMIN_ADVISORY_LOCK_ID},
            )

            user_stmt = select(User).where(User.id == user_id).with_for_update()
            user = (await session.execute(user_stmt)).scalar_one_or_none()
            if user is None or user.role != "admin":
                print("Administrator not found", file=sys.stderr)
                return 1

            mfa_stmt = select(AuthMfa).where(AuthMfa.user_id == user_id).with_for_update()
            mfa = (await session.execute(mfa_stmt)).scalar_one_or_none()
            if mfa is not None:
                mfa.secret_ciphertext = None
                mfa.pending_secret_ciphertext = None
                mfa.pending_expires_at = None
                mfa.enabled_at = None
                mfa.last_accepted_step = None

            # Invalidate all existing sessions and bump auth_version
            user.auth_version += 1
            user.updated_at = now
            await revoke_all_user_sessions(session, user_id, now)

            await record_audit(
                session,
                actor_kind="operator",
                action="admin.mfa.recovered",
                target_type="user",
                target_id=user_id,
                reason=reason,
                result="success",
                created_at=now,
            )

            await session.commit()
            print(json.dumps({"user_id": str(user_id), "result": "mfa_recovered"}))
            return 0
    finally:
        await engine.dispose()


async def _deliver_auth_mail_command(settings: PlatformSettings, limit: int = 25) -> int:
    """Dispatch pending transactional emails."""
    try:
        result = await deliver_pending_auth_messages(settings, limit=limit)
        print(
            json.dumps(
                {
                    "sent": result.sent,
                    "deferred": result.deferred,
                    "expired": result.expired,
                }
            )
        )
        return 0
    except Exception as e:
        print(f"Mail delivery failed: {e}", file=sys.stderr)
        return 1


async def _prune_auth_command(
    settings: PlatformSettings,
    anonymous_ttl_seconds: int = 600,
    rate_limit_ttl_seconds: int = 3600,
    token_ttl_seconds: int = 86400,
) -> int:
    """Housekeeping job pruning expired rate counters, tokens, and anonymous sessions."""
    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    now = datetime.now(timezone.utc)

    try:
        async with session_factory() as session:
            # 1. Prune expired rate limit counters
            rate_stmt = delete(AuthRateLimit).where(AuthRateLimit.expires_at <= now)
            rate_res = await session.execute(rate_stmt)

            # 2. Prune expired or consumed auth tokens older than threshold
            token_cutoff = now - timedelta(seconds=token_ttl_seconds)
            token_stmt = delete(AuthToken).where(
                (AuthToken.expires_at <= now) | (AuthToken.consumed_at <= token_cutoff)
            )
            token_res = await session.execute(token_stmt)

            # 3. Prune expired or revoked anonymous sessions
            anon_stmt = delete(AuthSession).where(
                AuthSession.state == "anonymous",
                (AuthSession.revoked_at.is_not(None))
                | (AuthSession.absolute_expires_at <= now)
                | (AuthSession.idle_expires_at <= now),
            )
            anon_res = await session.execute(anon_stmt)

            await session.commit()
            print(
                json.dumps(
                    {
                        "pruned_rate_limits": rate_res.rowcount,
                        "pruned_tokens": token_res.rowcount,
                        "pruned_anonymous_sessions": anon_res.rowcount,
                    }
                )
            )
            return 0
    finally:
        await engine.dispose()



def _parse_cutoff(value: str) -> datetime:
    try:
        cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Cutoff must be an ISO-8601 timestamp") from exc
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("Cutoff must include a timezone")
    return cutoff.astimezone(timezone.utc)


async def _export_user_command(
    settings: PlatformSettings,
    *,
    user_id: uuid.UUID,
    output_path: Path,
    reason: str,
    include_transcripts: bool,
    max_bytes: int,
    max_records: int,
) -> int:
    started = perf_counter()
    engine = create_engine(settings)
    session_factory = get_session_factory(engine)
    summary = None
    try:
        async with session_factory() as session:
            async with session.begin():
                summary = await export_user_data(
                    session,
                    user_id=user_id,
                    output_path=output_path,
                    reason=reason,
                    include_transcripts=include_transcripts,
                    max_bytes=max_bytes,
                    max_records=max_records,
                )
        print(
            json.dumps(
                {
                    "exported_user_id": str(summary.user_id),
                    "output": str(summary.output_path),
                    "record_counts": summary.record_counts,
                    "size_bytes": summary.size_bytes,
                    "elapsed_seconds": round(perf_counter() - started, 3),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        if summary is not None:
            try:
                summary.output_path.unlink()
            except FileNotFoundError:
                pass
        print(f"User export failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


async def _delete_user_transcripts_command(
    settings: PlatformSettings,
    *,
    user_id: uuid.UUID,
    reason: str,
    batch_size: int,
) -> int:
    started = perf_counter()
    try:
        maintenance_url = validate_retention_database_url(
            settings.database_url, os.environ.get("PLATFORM_RETENTION_DATABASE_URL", "")
        )
        engine = create_engine(maintenance_url, schema=settings.postgres_schema)
        try:
            session_factory = get_session_factory(engine)
            async with session_factory() as session:
                async with session.begin():
                    counts = await redact_user_transcripts(
                        session,
                        user_id=user_id,
                        reason=reason,
                        batch_size=batch_size,
                    )
            print(
                json.dumps(
                    {
                        "redacted_counts": counts,
                        "elapsed_seconds": round(perf_counter() - started, 3),
                    },
                    sort_keys=True,
                )
            )
            return 0
        finally:
            await engine.dispose()
    except Exception as exc:
        print(f"Transcript deletion failed: {type(exc).__name__}", file=sys.stderr)
        return 1


async def _apply_retention_command(
    settings: PlatformSettings,
    *,
    before: str,
    reason: str,
    batch_size: int,
) -> int:
    started = perf_counter()
    try:
        cutoff = _parse_cutoff(before)
        maintenance_url = validate_retention_database_url(
            settings.database_url, os.environ.get("PLATFORM_RETENTION_DATABASE_URL", "")
        )
        engine = create_engine(maintenance_url, schema=settings.postgres_schema)
        try:
            session_factory = get_session_factory(engine)
            async with session_factory() as session:
                async with session.begin():
                    counts = await apply_retention(
                        session,
                        before=cutoff,
                        reason=reason,
                        batch_size=batch_size,
                    )
            print(
                json.dumps(
                    {
                        "cutoff": cutoff.isoformat(),
                        "redacted_counts": counts,
                        "elapsed_seconds": round(perf_counter() - started, 3),
                    },
                    sort_keys=True,
                )
            )
            return 0
        finally:
            await engine.dispose()
    except Exception as exc:
        print(f"Retention failed: {type(exc).__name__}", file=sys.stderr)
        return 1


async def _audit_backup_creation(
    settings: PlatformSettings, *, reason: str, row_counts: dict[str, int], size_bytes: int
) -> None:
    engine = create_engine(settings)
    try:
        session_factory = get_session_factory(engine)
        async with session_factory() as session:
            async with session.begin():
                await record_audit(
                    session,
                    actor_kind="operator",
                    action="ops.backup.created",
                    target_type="platform_database",
                    reason=sanitize_operator_reason(reason),
                    result="success",
                    change_summary={"row_counts": row_counts, "size_bytes": size_bytes},
                )
    finally:
        await engine.dispose()


async def _create_backup_command(
    settings: PlatformSettings,
    *,
    output_path: Path,
    reason: str,
    max_bytes: int,
) -> int:
    started = perf_counter()
    summary = None
    try:
        backup_url = validate_backup_database_url(
            settings.database_url, os.environ.get("PLATFORM_BACKUP_DATABASE_URL", "")
        )
        summary = await create_backup(
            backup_url,
            output_path=output_path,
            schema=settings.postgres_schema,
            schema_version=settings.schema_version,
            metadata_hash=compute_metadata_hash(Base.metadata),
            max_bytes=max_bytes,
        )
        try:
            await _audit_backup_creation(
                settings,
                reason=reason,
                row_counts=summary.row_counts,
                size_bytes=summary.size_bytes,
            )
        except Exception:
            summary.output_path.unlink()
            raise
        print(
            json.dumps(
                {
                    "output": str(summary.output_path),
                    "size_bytes": summary.size_bytes,
                    "row_counts": summary.row_counts,
                    "elapsed_seconds": round(perf_counter() - started, 3),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(f"Backup failed: {type(exc).__name__}", file=sys.stderr)
        return 1


async def _restore_backup_command(
    settings: PlatformSettings,
    *,
    backup_path: Path,
    reason: str,
    max_bytes: int,
) -> int:
    started = perf_counter()
    try:
        target_url = validate_restore_target_database_url(
            settings.database_url, os.environ.get("PLATFORM_RESTORE_DATABASE_URL", "")
        )
        summary = await restore_backup(
            backup_path,
            target_url,
            schema=settings.postgres_schema,
            schema_version=settings.schema_version,
            metadata_hash=compute_metadata_hash(Base.metadata),
            max_bytes=max_bytes,
            reason=reason,
        )
        print(
            json.dumps(
                {
                    "schema": summary.schema,
                    "row_counts": summary.row_counts,
                    "restored_at": summary.restored_at.isoformat(),
                    "elapsed_seconds": round(perf_counter() - started, 3),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(f"Restore failed: {type(exc).__name__}", file=sys.stderr)
        return 1


def _positive_batch_size(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch size must be an integer") from exc
    if not 1 <= parsed <= MAX_RETENTION_BATCH:
        raise argparse.ArgumentTypeError(f"batch size must be between 1 and {MAX_RETENTION_BATCH}")
    return parsed


def _configured_limit(requested: int | None, configured: int, option: str) -> int:
    if requested is None:
        return configured
    if requested < 1:
        raise argparse.ArgumentTypeError(f"{option} must be positive")
    if requested > configured:
        raise argparse.ArgumentTypeError(f"{option} exceeds the configured maximum of {configured}")
    return requested
def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and dispatch commands."""
    parser = argparse.ArgumentParser(
        prog="python -m platform_api.cli",
        description="Platform API Control Plane CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-db / init-schema
    subparsers.add_parser("init-db", help="Initialize platform PostgreSQL database")
    subparsers.add_parser("init-schema", help="Initialize platform PostgreSQL database (alias)")

    # check-schema
    subparsers.add_parser("check-schema", help="Verify schema readiness and version")

    # bootstrap-admin
    bootstrap_p = subparsers.add_parser("bootstrap-admin", help="Bootstrap initial administrator account")
    bootstrap_p.add_argument("--email", required=True, help="Administrator email address")
    bootstrap_p.add_argument("--display-name", required=True, help="Administrator display name")
    bootstrap_p.add_argument("--reason", required=True, help="Operator reason for audit logging")
    bootstrap_p.add_argument("--password", required=False, default=None, help=argparse.SUPPRESS)

    # create-admin
    create_admin_p = subparsers.add_parser("create-admin", help="Create an additional administrator account")
    create_admin_p.add_argument("--email", required=True, help="Administrator email address")
    create_admin_p.add_argument("--display-name", required=True, help="Administrator display name")
    create_admin_p.add_argument("--reason", required=True, help="Operator reason for audit logging")
    create_admin_p.add_argument("--password", required=False, default=None, help=argparse.SUPPRESS)

    # recover-admin-mfa
    recover_p = subparsers.add_parser("recover-admin-mfa", help="Reset admin MFA factor")
    recover_p.add_argument("--user-id", required=True, help="Target administrator user UUID")
    recover_p.add_argument("--reason", required=True, help="Operator recovery justification")

    # deliver-auth-mail
    deliver_p = subparsers.add_parser("deliver-auth-mail", help="Deliver pending transactional emails")
    deliver_p.add_argument("--once", action="store_true", help="Run one batch and exit")
    deliver_p.add_argument("--limit", type=int, default=25, help="Batch limit")

    # prune-auth
    prune_p = subparsers.add_parser("prune-auth", help="Prune expired tokens and rate limits")
    prune_p.add_argument("--anonymous-ttl", type=int, default=600)
    prune_p.add_argument("--rate-limit-ttl", type=int, default=3600)
    prune_p.add_argument("--tokens-ttl", type=int, default=86400)

    export_p = subparsers.add_parser("export-user", help="Write an audited, redacted user data export")
    export_p.add_argument("--user-id", required=True, type=uuid.UUID)
    export_p.add_argument("--output", required=True, type=Path)
    export_p.add_argument("--reason", required=True)
    export_p.add_argument(
        "--include-transcripts",
        action="store_true",
        help="Include transcript text; the output remains private and sensitive",
    )
    export_p.add_argument("--max-bytes", type=int, default=None)
    export_p.add_argument("--max-records", type=int, default=None)

    delete_transcripts_p = subparsers.add_parser(
        "delete-user-transcripts",
        help="Redact one bounded batch of a user's transcript and chat-test text",
    )
    delete_transcripts_p.add_argument("--user-id", required=True, type=uuid.UUID)
    delete_transcripts_p.add_argument("--reason", required=True)
    delete_transcripts_p.add_argument(
        "--batch-size", type=_positive_batch_size, default=None
    )
    retention_p = subparsers.add_parser(
        "apply-retention", help="Redact one bounded batch of transcript data before a cutoff"
    )
    retention_p.add_argument("--before", required=True, help="ISO-8601 UTC cutoff")
    retention_p.add_argument("--reason", required=True)
    retention_p.add_argument("--batch-size", type=_positive_batch_size, default=None)
    backup_p = subparsers.add_parser(
        "backup", help="Create an audited, consistent platform database backup"
    )
    backup_p.add_argument("--output", required=True, type=Path)
    backup_p.add_argument("--reason", required=True)
    backup_p.add_argument("--max-bytes", type=int, default=None)

    restore_p = subparsers.add_parser(
        "restore", help="Restore a backup into an empty, separate platform database"
    )
    restore_p.add_argument("--backup", required=True, type=Path)
    restore_p.add_argument("--reason", required=True)
    restore_p.add_argument("--max-bytes", type=int, default=None)

    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Configuration error: {_format_safe_validation_error(exc)}", file=sys.stderr)
        return 1

    try:
        if args.command == "export-user":
            args.max_bytes = _configured_limit(
                args.max_bytes, settings.ops_export_max_bytes, "--max-bytes"
            )
            args.max_records = _configured_limit(
                args.max_records, settings.ops_export_max_records, "--max-records"
            )
        elif args.command in ("delete-user-transcripts", "apply-retention"):
            args.batch_size = _configured_limit(
                args.batch_size, settings.ops_retention_batch_size, "--batch-size"
            )
        elif args.command == "backup":
            args.max_bytes = _configured_limit(
                args.max_bytes, settings.ops_backup_max_bytes, "--max-bytes"
            )
        elif args.command == "restore":
            args.max_bytes = _configured_limit(
                args.max_bytes, settings.ops_restore_max_bytes, "--max-bytes"
            )
    except argparse.ArgumentTypeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.command in ("init-db", "init-schema"):
        return asyncio.run(_init_db_command(settings))
    elif args.command == "check-schema":
        return asyncio.run(_check_schema_command(settings))
    elif args.command == "bootstrap-admin":
        return asyncio.run(
            _bootstrap_admin_command(
                settings,
                email_raw=args.email,
                display_name_raw=args.display_name,
                reason_raw=args.reason,
                password_input=args.password,
            )
        )
    elif args.command == "create-admin":
        return asyncio.run(
            _create_admin_command(
                settings,
                email_raw=args.email,
                display_name_raw=args.display_name,
                reason_raw=args.reason,
                password_input=args.password,
            )
        )
    elif args.command == "recover-admin-mfa":
        return asyncio.run(
            _recover_admin_mfa_command(
                settings,
                user_id_str=args.user_id,
                reason_raw=args.reason,
            )
        )
    elif args.command == "deliver-auth-mail":
        return asyncio.run(_deliver_auth_mail_command(settings, limit=args.limit))
    elif args.command == "prune-auth":
        return asyncio.run(
            _prune_auth_command(
                settings,
                anonymous_ttl_seconds=args.anonymous_ttl,
                rate_limit_ttl_seconds=args.rate_limit_ttl,
                token_ttl_seconds=args.tokens_ttl,
            )
        )
    elif args.command == "export-user":
        return asyncio.run(
            _export_user_command(
                settings,
                user_id=args.user_id,
                output_path=args.output,
                reason=args.reason,
                include_transcripts=args.include_transcripts,
                max_bytes=(
                    settings.ops_export_max_bytes
                    if args.max_bytes is None
                    else min(args.max_bytes, settings.ops_export_max_bytes)
                ),
                max_records=(
                    settings.ops_export_max_records
                    if args.max_records is None
                    else min(args.max_records, settings.ops_export_max_records)
                ),
            )
        )
    elif args.command == "delete-user-transcripts":
        return asyncio.run(
            _delete_user_transcripts_command(
                settings,
                user_id=args.user_id,
                reason=args.reason,
                batch_size=(
                    args.batch_size
                    if args.batch_size is not None
                    else settings.ops_retention_batch_size
                ),
            )
        )
    elif args.command == "apply-retention":
        return asyncio.run(
            _apply_retention_command(
                settings,
                before=args.before,
                reason=args.reason,
                batch_size=(
                    args.batch_size
                    if args.batch_size is not None
                    else settings.ops_retention_batch_size
                ),
            )
        )
    elif args.command == "backup":
        return asyncio.run(
            _create_backup_command(
                settings,
                output_path=args.output,
                reason=args.reason,
                max_bytes=(
                    settings.ops_backup_max_bytes
                    if args.max_bytes is None
                    else min(args.max_bytes, settings.ops_backup_max_bytes)
                ),
            )
        )
    elif args.command == "restore":
        return asyncio.run(
            _restore_backup_command(
                settings,
                backup_path=args.backup,
                reason=args.reason,
                max_bytes=(
                    settings.ops_restore_max_bytes
                    if args.max_bytes is None
                    else min(args.max_bytes, settings.ops_restore_max_bytes)
                ),
            )
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
