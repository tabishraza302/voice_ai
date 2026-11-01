"""Create explicitly requested demo accounts in a local development database."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from sqlalchemy import select, text

from platform_api.accounts.service import initialize_user_account
from platform_api.auth.passwords import get_password_hasher
from platform_api.config import PlatformSettings
from platform_api.db.session import create_engine, get_session_factory
from platform_api.models.auth import AuthMfa, User


def validate_target(settings: PlatformSettings) -> None:
    host = urlsplit(settings.database_url).hostname
    if settings.env != "development" or host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Demo accounts require a local development database")


async def seed() -> None:
    settings = PlatformSettings()
    validate_target(settings)
    accounts = json.loads((Path(__file__).resolve().parents[1] / "web/src/lib/dev-accounts.json").read_text())
    engine = create_engine(settings)
    try:
        async with get_session_factory(engine)() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 8100984920448107371})
            hasher = get_password_hasher(settings.auth_hash_concurrency)
            pending = []
            for account in accounts:
                existing = (await session.execute(select(User).where(User.email_key == account["email"]))).scalar_one_or_none()
                if existing:
                    if existing.role != account["role"] or existing.status != "active" or not await hasher.verify_password(account["password"], existing.password_hash):
                        raise ValueError(f"Account {account['email']} already exists with different credentials or role; refusing to change it")
                else:
                    pending.append(account)
            now = datetime.now(timezone.utc)
            for account in pending:
                user = User(id=uuid.uuid4(), email=account["email"], email_key=account["email"],
                            display_name=f"Development {account['label']}",
                            password_hash=await hasher.hash_password(account["password"]),
                            role=account["role"], status="active", email_verified_at=now)
                session.add(user)
                await session.flush()
                session.add(AuthMfa(user_id=user.id))
                await initialize_user_account(session, user_id=user.id, now=now)
            await session.commit()
        print("Local development accounts are ready.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
