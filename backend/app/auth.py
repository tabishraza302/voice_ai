import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User


router = APIRouter(prefix="/auth", tags=["auth"])
hasher = PasswordHasher()
COOKIE = "voice_session"
MAX_AGE = 12 * 60 * 60
ALGORITHM = "HS256"


def _signing_secret() -> str:
    secret = os.environ.get("SESSION_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    return secret


def create_session_token(user_id: uuid.UUID, csrf_token: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "csrf": csrf_token,
        "iat": now,
        "exp": now + timedelta(seconds=MAX_AGE),
    }
    return jwt.encode(payload, _signing_secret(), algorithm=ALGORITHM)


def decode_session_token(token: str) -> dict:
    payload = jwt.decode(token, _signing_secret(), algorithms=[ALGORITHM])
    if not isinstance(payload, dict):
        raise ValueError("Invalid session token")
    return payload


def secure_cookie() -> bool:
    return os.environ.get("ENVIRONMENT", "").casefold() == "production"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320)
    password: str = Field(min_length=12, max_length=1024)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().casefold()
        if len(value) > 320 or value.count("@") != 1 or any(c.isspace() for c in value) or not all(value.split("@")):
            raise ValueError("Invalid email address")
        return value


def public_user(user: User):
    return {"id": str(user.id), "email": user.email, "role": user.role}


async def current_user(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> User:
    try:
        data = decode_session_token(request.cookies.get(COOKIE, ""))
        user_id_value = data.get("sub")
        csrf_token = data.get("csrf")
        if not isinstance(user_id_value, str) or not isinstance(csrf_token, str) or not csrf_token:
            raise ValueError
        user_id = uuid.UUID(user_id_value)
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise HTTPException(401, "Authentication required")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(401, "Authentication required")
    request.state.session = {"user_id": str(user_id), "csrf": csrf_token}
    return user


async def csrf(request: Request, user: Annotated[User, Depends(current_user)]) -> User:
    origin = os.environ.get("APP_ORIGIN", "http://localhost:3000").rstrip("/")
    token = request.headers.get("x-csrf-token", "")
    expected_token = request.state.session["csrf"]
    if request.headers.get("origin") != origin or not secrets.compare_digest(token.encode("utf-8"), expected_token.encode("utf-8")):
        raise HTTPException(403, "CSRF validation failed")
    return user


async def admin_user(user: Annotated[User, Depends(current_user)]):
    if user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


@router.post("/register", status_code=201)
async def register(credentials: Credentials, db: Annotated[AsyncSession, Depends(get_db)]):
    user = User(email=credentials.email, password_hash=hasher.hash(credentials.password), role="user")
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        existing_user_id = await db.scalar(select(User.id).where(User.email == credentials.email))
        if existing_user_id is not None:
            raise HTTPException(409, "Email already registered") from error
        raise
    return public_user(user)


@router.post("/login")
async def login(credentials: Credentials, response: Response, db: Annotated[AsyncSession, Depends(get_db)]):
    user = await db.scalar(select(User).where(User.email == credentials.email))
    try:
        valid = user is not None and hasher.verify(user.password_hash, credentials.password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        valid = False
    if not valid:
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_urlsafe(32)
    value = create_session_token(user.id, token)
    response.set_cookie(COOKIE, value, max_age=MAX_AGE, httponly=True, secure=secure_cookie(), samesite="lax", path="/")
    return {**public_user(user), "csrf_token": token}


class DevLoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "admin"]


def dev_login_enabled() -> bool:
    return (
        os.environ.get("ENVIRONMENT") == "development"
        and os.environ.get("ALLOW_DEV_LOGIN", "").casefold() == "true"
    )


def _dev_credentials(role: str) -> tuple[str, str]:
    if role == "admin":
        return os.environ.get("ADMIN_EMAIL", ""), os.environ.get("ADMIN_PASSWORD", "")
    return os.environ.get("DEV_USER_EMAIL", ""), os.environ.get("DEV_USER_PASSWORD", "")


def _check_dev_origin(request: Request) -> None:
    origin = os.environ.get("APP_ORIGIN", "http://localhost:3000").rstrip("/")
    if request.headers.get("origin") != origin:
        raise HTTPException(403, "Dev login must use the configured frontend origin")


@router.get("/dev-login/available")
async def dev_login_available():
    if not dev_login_enabled():
        raise HTTPException(404, "Not found")
    return {"enabled": True}


@router.post("/dev-login")
async def dev_login(payload: DevLoginInput, request: Request, response: Response, db: Annotated[AsyncSession, Depends(get_db)]):
    if not dev_login_enabled():
        raise HTTPException(404, "Not found")
    _check_dev_origin(request)
    email, password = _dev_credentials(payload.role)
    if not email.strip() or len(password) < 12:
        raise HTTPException(404, "Not found")
    normalized_email = email.strip().casefold()
    user = await db.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        raise HTTPException(401, "Invalid seeded credentials")
    try:
        valid = hasher.verify(user.password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        valid = False
    expected_role = "admin" if payload.role == "admin" else "user"
    if not valid or user.role != expected_role:
        raise HTTPException(401, "Invalid seeded credentials")
    token = secrets.token_urlsafe(32)
    value = create_session_token(user.id, token)
    response.set_cookie(COOKIE, value, max_age=MAX_AGE, httponly=True, secure=secure_cookie(), samesite="lax", path="/")
    return {**public_user(user), "csrf_token": token}


@router.get("/me")
async def me(request: Request, user: Annotated[User, Depends(current_user)]):
    return {**public_user(user), "csrf_token": request.state.session["csrf"]}


@router.post("/logout", status_code=204)
async def logout(response: Response, user: Annotated[User, Depends(csrf)]):
    response.delete_cookie(COOKIE, path="/", secure=secure_cookie(), httponly=True, samesite="lax")
