"""Frozen Principal and auth session types for the platform control plane.

Implements the stable interface specified in auth-contract.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal
import uuid

if TYPE_CHECKING:
    from platform_api.models.auth import AuthSession, User


@dataclass(frozen=True)
class Principal:
    """Frozen verified caller identity produced by require_user / require_admin."""

    user_id: uuid.UUID
    role: Literal["user", "admin"]
    session_id: uuid.UUID
    email_verified_at: datetime
    password_verified_at: datetime
    mfa_verified_at: datetime | None
    step_up_at: datetime | None


@dataclass
class AuthSessionContext:
    """Session context produced by require_csrf dependency."""

    session: AuthSession
    user: User | None
    csrf_token: str
