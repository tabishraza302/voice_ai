"""Platform database models for agents, drafts, versions, templates, prompts, and idempotency.

Follows master spec III.3 & III.5 and flow-contract.md:
- UUID identifiers
- UTC timestamptz instants
- Explicit constraints (CheckConstraint, UniqueConstraint, ForeignKey)
- Immutable agent versions and templates
- Draft revision CAS tracking
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from platform_api.db.base import Base
from platform_api.agents.defaults import DEFAULT_PERSONA_PROMPT


class Agent(Base):
    """Owner agent metadata and state pointer."""

    __tablename__ = "platform_agents"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    persona_prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=DEFAULT_PERSONA_PROMPT,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_published_version_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey(
            "platform_agent_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_platform_agents_current_published_version_id",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "draft_revision >= 1", name="ck_platform_agents_draft_revision"
        ),
        CheckConstraint(
            "length(persona_prompt) <= 20000",
            name="ck_platform_agents_persona_prompt_length",
        ),
        CheckConstraint(
            "length(trim(persona_prompt)) > 0",
            name="ck_platform_agents_persona_prompt_nonblank",
        ),
    )

    @property
    def published_version_id(self) -> uuid.UUID | None:
        return self.current_published_version_id


class AgentDraft(Base):
    """Mutable draft configuration with compare-and-swap revision tracking."""

    __tablename__ = "platform_agent_drafts"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("platform_agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    authoring_json: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(),
        nullable=False,
    )
    authoring_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("platform_agent_template_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_platform_agent_drafts_revision"),
        CheckConstraint(
            "source IN ('manual', 'template', 'generated')",
            name="ck_platform_agent_drafts_source",
        ),
    )

    @property
    def draft_revision(self) -> int:
        return self.revision

    @property
    def config(self) -> dict[str, Any]:
        return self.authoring_json


class AgentVersion(Base):
    """Immutable compiled agent version artifact."""

    __tablename__ = "platform_agent_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("platform_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    immutable_config_json: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(),
        nullable=False,
    )
    authoring_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    flow_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("platform_prompt_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    compiler_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="flow-compiler/1"
    )
    changelog: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "agent_id", "version", name="uq_platform_agent_versions_agent_version"
        ),
        CheckConstraint("version >= 1", name="ck_platform_agent_versions_version"),
    )

    @property
    def version_id(self) -> uuid.UUID:
        return self.id

    @property
    def immutable_config(self) -> dict[str, Any]:
        return self.immutable_config_json

    @property
    def persona_prompt(self) -> str:
        return self.immutable_config_json.get("persona_prompt", "")


class AgentTemplateVersion(Base):
    """Immutable agent template version for creating new agents."""

    __tablename__ = "platform_agent_template_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    flow_json: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "slug", "version", name="uq_platform_agent_template_versions_slug_version"
        ),
        CheckConstraint(
            "version >= 1", name="ck_platform_agent_template_versions_version"
        ),
    )


class PlatformPromptVersion(Base):
    """Platform system/base prompt version for immutable policy binding."""

    __tablename__ = "platform_prompt_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_platform_prompt_versions_slug_version"),
        CheckConstraint("version >= 1", name="ck_platform_prompt_versions_version"),
    )


class IdempotencyRecord(Base):
    """Durable idempotency journal for mutating platform operations."""

    __tablename__ = "idempotency_records"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_headers: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(),
        nullable=False,
        default=dict,
    )
    response_json: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            "action",
            name="uq_idempotency_records_user_key_action",
        ),
    )
