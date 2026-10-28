"""SQLAlchemy Base and independent metadata for the platform control plane.

This Base is isolated and must NEVER import or share tables with bot.db.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Isolated declarative base for all platform_api models."""
    pass
