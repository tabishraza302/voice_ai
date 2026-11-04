"""Encrypted deployment credentials for registered speech adapters."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


class ProviderSecret(Base):
    __tablename__ = "provider_secrets"

    provider_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    field_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
