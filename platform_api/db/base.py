"""SQLAlchemy Base and independent metadata for the platform control plane.

This Base is isolated and must NEVER import or share tables with bot.db.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

DEFAULT_PLATFORM_SCHEMA = "platform"

metadata = MetaData(schema=DEFAULT_PLATFORM_SCHEMA)


class Base(DeclarativeBase):
    """Isolated declarative base for all platform_api models."""

    metadata = metadata
