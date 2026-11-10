import asyncio
import os

from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import ValidationError
from sqlalchemy import select

from .auth import Credentials, hasher
from .db import Session, engine
from .models import User


async def seed():
    if os.environ.get("ENVIRONMENT") != "development":
        raise RuntimeError("Account seeding requires ENVIRONMENT=development; use app.bootstrap for a production administrator")

    accounts = []
    for role, prefix in (("admin", "ADMIN"), ("user", "DEV_USER")):
        email_key, password_key = f"{prefix}_EMAIL", f"{prefix}_PASSWORD"
        try:
            credentials = Credentials(
                email=os.environ.get(email_key, ""), password=os.environ.get(password_key, "")
            )
        except ValidationError:
            raise RuntimeError(f"{email_key} and {password_key} must contain valid credentials (password at least 12 characters)") from None
        accounts.append((role, prefix, credentials))

    if accounts[0][2].email == accounts[1][2].email:
        raise RuntimeError("ADMIN_EMAIL and DEV_USER_EMAIL must identify different accounts")

    results = []
    async with Session.begin() as session:
        for role, prefix, credentials in accounts:
            user = await session.scalar(select(User).where(User.email == credentials.email))
            if user is not None:
                if user.role != role:
                    raise RuntimeError(f"{prefix}_EMAIL belongs to an account with a different role; refusing role change")
                try:
                    valid = hasher.verify(user.password_hash, credentials.password)
                except (VerificationError, InvalidHashError):
                    valid = False
                if not valid:
                    raise RuntimeError(f"{prefix}_PASSWORD does not match the existing account; refusing password reset")
                results.append(f"{role}: account already exists and credentials match")
            else:
                session.add(User(email=credentials.email, password_hash=hasher.hash(credentials.password), role=role))
                results.append(f"{role}: account created")

    for result in results:
        print(result)


async def main():
    try:
        await seed()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
