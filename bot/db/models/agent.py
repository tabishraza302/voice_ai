import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Float, Integer, Boolean, DateTime, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from bot.db.base import Base


class Agent(Base):
    __tablename__ = "agents"
    # At most one active agent, enforced by PostgreSQL's partial unique
    # index. There is no SQLite predicate: SQLite is not a runtime store.
    __table_args__ = (
        Index(
            "uq_agents_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    persona_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # LLM Settings
    llm_model: Mapped[str] = mapped_column(String(64), nullable=False, default="qwen3.5:4b")
    llm_temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    llm_reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    llm_max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=150)

    # STT & TTS Settings
    stt_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="mlx-audio")
    tts_voice_en: Mapped[str] = mapped_column(String(32), nullable=False, default="af_heart")
    tts_voice_hi: Mapped[str] = mapped_column(String(32), nullable=False, default="hf_alpha")
    tts_speed: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    # Flags
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    is_preset: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
