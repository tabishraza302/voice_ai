"""Administrator-managed model entries grouped by their serving provider."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base


class ProviderModelEntry(Base):
    """One explicitly managed model identifier for a serving provider."""

    __tablename__ = "provider_model_entries"

    provider_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    qualified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "length(trim(provider_id)) BETWEEN 1 AND 32",
            name="ck_provider_model_entries_provider_id_length",
        ),
        CheckConstraint(
            "model_id = trim(model_id) AND length(trim(model_id)) BETWEEN 1 AND 64",
            name="ck_provider_model_entries_model_id_length",
        ),
        CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 255",
            name="ck_provider_model_entries_name_length",
        ),
    )
