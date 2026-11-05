import asyncio
import os

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .auth import Credentials, hasher
from .db import Session
from .models import User


def _credentials_from_environment():
    required = ("ADMIN_EMAIL", "ADMIN_PASSWORD")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    try:
        return Credentials(email=os.environ["ADMIN_EMAIL"], password=os.environ["ADMIN_PASSWORD"])
    except ValidationError:
        raise RuntimeError("ADMIN_EMAIL and ADMIN_PASSWORD must contain valid credentials") from None


def _require_existing_admin(user: User):
    if user.role != "admin":
        raise RuntimeError("Email belongs to an existing non-admin account; refusing promotion")


async def bootstrap():
    credentials = _credentials_from_environment()
    async with Session() as session:
        existing = await session.scalar(select(User).where(User.email == credentials.email))
        if existing is not None:
            _require_existing_admin(existing)
            print("Administrator already exists")
            return

        session.add(User(email=credentials.email, password_hash=hasher.hash(credentials.password), role="admin"))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(select(User).where(User.email == credentials.email))
            if existing is None:
                raise
            _require_existing_admin(existing)
            print("Administrator already exists")
            return
        print("Administrator created")

if __name__ == "__main__":
    asyncio.run(bootstrap())
