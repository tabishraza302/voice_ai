"""Platform database models registry.

Imports all platform model declarations onto Base.metadata.
Completely isolated from bot.db.
"""

from platform_api.db.base import Base
from platform_api.db.schema import SchemaInfo
from platform_api.models.auth import (
    AuditLog,
    AuthMail,
    AuthMfa,
    AuthRateLimit,
    AuthSession,
    AuthToken,
    User,
)
from platform_api.models.wallet import WalletAccount

__all__ = [
    "Base",
    "SchemaInfo",
    "User",
    "AuthSession",
    "AuthToken",
    "AuthMfa",
    "AuthRateLimit",
    "AuthMail",
    "AuditLog",
    "WalletAccount",
]
