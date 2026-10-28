"""Durable auth transactional mail delivery service and dispatcher.

Follows auth-contract.md:
- Enqueue participates in the caller's database transaction.
- Bounded queue cap enforced under PostgreSQL transaction advisory lock.
- Payloads encrypted with Fernet and erased immediately on delivery or expiration.
- Links use PLATFORM_WEB_ORIGIN and place tokens in URL fragments (#token=...).
- File adapter for local development (mode 0600 files, mode 0700 directory).
- Authenticated SMTP with STARTTLS or TLS for staging/production.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import smtplib
from email.message import EmailMessage
from typing import Literal
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.auth.errors import RateLimitedError
from platform_api.config import PlatformSettings
from platform_api.db.session import create_engine, get_session_factory
from platform_api.models.auth import AuthMail, AuthToken

PLATFORM_MAIL_QUEUE_ADVISORY_LOCK_ID = 8100984920448107369
RETRY_DELAYS = [60, 120, 240, 480]


@dataclass(frozen=True)
class DeliveryBatchResult:
    """Summary of dispatcher batch processing."""

    sent: int
    deferred: int
    expired: int


def build_mail_link(web_origin: str, purpose: str, token: str) -> str:
    """Build fragment-based verification or reset link using configured web origin."""
    base = web_origin.rstrip("/")
    if purpose == "register":
        path = "/verify-email"
    elif purpose == "password_reset":
        path = "/reset-password"
    else:
        path = "/auth"
    # Never put token in query string: use URL fragment
    return f"{base}{path}#token={token}"


async def enqueue_auth_message(
    db: AsyncSession,
    settings: PlatformSettings,
    *,
    token_id: uuid.UUID,
    email: str,
    purpose: Literal["register", "password_reset"],
    token: str,
    expires_at: datetime,
    display_name: str | None = None,
    now: datetime | None = None,
) -> AuthMail:
    """Enqueue transactional message in the caller's transaction.

    Enforces global mail bound under a transaction-level advisory lock.
    """
    current_time = now or datetime.now(timezone.utc)

    # Acquire transaction advisory lock for global queue bound check
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": PLATFORM_MAIL_QUEUE_ADVISORY_LOCK_ID},
    )

    # Check total active queued messages
    count_stmt = select(func.count(AuthMail.id)).where(AuthMail.state == "queued")
    result = await db.execute(count_stmt)
    queued_count = result.scalar_one()

    if queued_count >= settings.auth_mail_queue_max:
        raise RateLimitedError(
            "Transactional email queue is full; please try again later",
            retry_after=60,
        )

    # Encrypt payload bound to token_id
    f = Fernet(settings.auth_mfa_key.encode("ascii"))
    payload = json.dumps(
        {
            "token_id": str(token_id),
            "email": email,
            "purpose": purpose,
            "token": token,
            "display_name": display_name,
        }
    ).encode("utf-8")
    ciphertext = f.encrypt(payload).decode("ascii")

    mail = AuthMail(
        id=uuid.uuid4(),
        token_id=token_id,
        payload_ciphertext=ciphertext,
        state="queued",
        attempts=0,
        available_at=current_time,
        lease_expires_at=None,
        sent_at=None,
        expires_at=expires_at,
        last_error_code=None,
        created_at=current_time,
        updated_at=current_time,
    )
    db.add(mail)
    await db.flush()
    return mail


def _send_file_mail(
    settings: PlatformSettings,
    mail_id: uuid.UUID,
    email: str,
    purpose: str,
    link: str,
) -> None:
    """Deliver message to secure local development directory."""
    os.makedirs(settings.auth_mail_dir, mode=0o700, exist_ok=True)
    file_path = os.path.join(settings.auth_mail_dir, f"{mail_id}.json")

    content = {
        "mail_id": str(mail_id),
        "to": email,
        "purpose": purpose,
        "link": link,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    raw = json.dumps(content, indent=2).encode("utf-8")

    # Write file with mode 0600
    fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
    except Exception:
        os.close(fd)
        raise


def _send_smtp_mail(
    settings: PlatformSettings,
    to_email: str,
    purpose: str,
    link: str,
) -> None:
    """Deliver message via authenticated SMTP with STARTTLS or TLS."""
    if not (
        settings.auth_smtp_host
        and settings.auth_smtp_port
        and settings.auth_smtp_username
        and settings.auth_smtp_password
        and settings.auth_mail_from
    ):
        raise RuntimeError("Incomplete SMTP settings")

    msg = EmailMessage()
    msg["From"] = settings.auth_mail_from
    msg["To"] = to_email

    if purpose == "register":
        msg["Subject"] = "Verify your Voice AI account"
        body = f"Welcome to Voice AI.\n\nPlease verify your email by opening the link below:\n\n{link}\n"
    else:
        msg["Subject"] = "Reset your Voice AI password"
        body = f"A password reset was requested for your Voice AI account.\n\nReset your password here:\n\n{link}\n\nIf you did not request this, please ignore this email.\n"

    msg.set_content(body)

    timeout = settings.auth_smtp_timeout_seconds
    if settings.auth_smtp_tls_mode == "tls":
        with smtplib.SMTP_SSL(
            settings.auth_smtp_host, settings.auth_smtp_port, timeout=timeout
        ) as server:
            server.login(settings.auth_smtp_username, settings.auth_smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(
            settings.auth_smtp_host, settings.auth_smtp_port, timeout=timeout
        ) as server:
            server.starttls()
            server.login(settings.auth_smtp_username, settings.auth_smtp_password)
            server.send_message(msg)


async def deliver_pending_auth_messages(
    settings: PlatformSettings,
    *,
    limit: int = 25,
    now: datetime | None = None,
) -> DeliveryBatchResult:
    """Claim and deliver pending auth messages.

    Uses FOR UPDATE SKIP LOCKED and bounded lease.
    Erases payloads immediately upon successful delivery or expiration.
    """
    current_time = now or datetime.now(timezone.utc)
    engine = create_engine(settings)
    session_factory = get_session_factory(engine)

    sent = 0
    deferred = 0
    expired = 0

    try:
        async with session_factory() as session:
            # Reclaim stale sending leases
            reclaim_stmt = (
                select(AuthMail)
                .where(
                    AuthMail.state == "sending",
                    AuthMail.lease_expires_at.is_not(None),
                    AuthMail.lease_expires_at <= current_time,
                )
                .with_for_update(skip_locked=True)
            )
            reclaim_res = await session.execute(reclaim_stmt)
            for m in reclaim_res.scalars().all():
                m.state = "queued"
                m.lease_expires_at = None

            # Claim queued messages
            stmt = (
                select(AuthMail)
                .where(
                    AuthMail.state == "queued",
                    AuthMail.available_at <= current_time,
                )
                .order_by(AuthMail.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            messages = list(result.scalars().all())

            for msg in messages:
                if msg.expires_at <= current_time:
                    msg.state = "expired"
                    msg.payload_ciphertext = None
                    msg.updated_at = current_time
                    expired += 1
                    continue

                if not msg.payload_ciphertext:
                    msg.state = "expired"
                    msg.updated_at = current_time
                    expired += 1
                    continue

                msg.state = "sending"
                msg.lease_expires_at = current_time + timedelta(
                    seconds=settings.auth_mail_lease_seconds
                )
                await session.flush()

                # Decrypt payload
                try:
                    f = Fernet(settings.auth_mfa_key.encode("ascii"))
                    payload_bytes = f.decrypt(msg.payload_ciphertext.encode("ascii"))
                    payload = json.loads(payload_bytes.decode("utf-8"))
                    email = payload["email"]
                    purpose = payload["purpose"]
                    token = payload["token"]
                    link = build_mail_link(settings.web_origin, purpose, token)

                    # Deliver
                    if settings.auth_mail_mode == "file":
                        _send_file_mail(settings, msg.id, email, purpose, link)
                    else:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None, _send_smtp_mail, settings, email, purpose, link
                        )

                    # Delivery success: erase payload
                    msg.state = "sent"
                    msg.payload_ciphertext = None
                    msg.lease_expires_at = None
                    msg.sent_at = current_time
                    msg.updated_at = current_time
                    msg.last_error_code = None
                    sent += 1

                except Exception as e:
                    msg.attempts += 1
                    error_code = type(e).__name__[:40]
                    msg.last_error_code = error_code
                    msg.lease_expires_at = None

                    if msg.attempts >= settings.auth_mail_attempts:
                        msg.state = "expired"
                        msg.payload_ciphertext = None
                        msg.updated_at = current_time
                        expired += 1
                    else:
                        delay_idx = min(msg.attempts - 1, len(RETRY_DELAYS) - 1)
                        delay = RETRY_DELAYS[delay_idx]
                        next_available = current_time + timedelta(seconds=delay)
                        if next_available > msg.expires_at:
                            msg.state = "expired"
                            msg.payload_ciphertext = None
                            msg.updated_at = current_time
                            expired += 1
                        else:
                            msg.state = "queued"
                            msg.available_at = next_available
                            msg.updated_at = current_time
                            deferred += 1

            await session.commit()
    finally:
        await engine.dispose()

    return DeliveryBatchResult(sent=sent, deferred=deferred, expired=expired)
