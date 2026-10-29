"""Strict wire contracts for platform operator APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from platform_api.sessions.history_schemas import SessionBillingView


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AdminSessionItem(StrictModel):
    session_id: uuid.UUID
    owner_user_id: uuid.UUID
    agent_version_id: uuid.UUID | None
    kind: Literal["voice", "chat", "voice_test"]
    state: str
    session_version: int
    runner_id: str | None
    connect_deadline: datetime | None
    hard_end_deadline: datetime | None
    created_at: datetime
    ended_at: datetime | None
    failure_code: str | None
    billing: SessionBillingView | None


class AdminSessionDetail(AdminSessionItem):
    tariff_snapshot: dict[str, Any] | None


class AdminSessionPage(StrictModel):
    items: list[AdminSessionItem]
    next_cursor: str | None


class AdminAgentItem(StrictModel):
    id: uuid.UUID
    owner_user_id: uuid.UUID
    name: str
    description: str
    enabled: bool
    draft_revision: int
    current_published_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class AdminAgentPage(StrictModel):
    items: list[AdminAgentItem]
    next_cursor: str | None


class AdminAgentDisableRequest(StrictModel):
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("reason must be between 10 and 500 characters")
        return trimmed


class AdminTemplateItem(StrictModel):
    id: uuid.UUID
    slug: str
    version: int
    name: str
    description: str
    flow_json: dict[str, Any]
    is_active: bool
    created_at: datetime


class AdminTemplatePage(StrictModel):
    items: list[AdminTemplateItem]
    next_cursor: str | None


class AdminPromptItem(StrictModel):
    id: uuid.UUID
    slug: str
    version: int
    prompt_text: str
    policy_hash: str
    is_active: bool
    created_at: datetime


class AdminPromptPage(StrictModel):
    items: list[AdminPromptItem]
    next_cursor: str | None


class AdminAuditItem(StrictModel):
    id: uuid.UUID
    actor_kind: str
    actor_id: uuid.UUID | None
    session_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: uuid.UUID | None
    reason: str | None
    request_id: uuid.UUID | None
    source_ip: str | None
    result: str
    created_at: datetime


class AdminAuditPage(StrictModel):
    items: list[AdminAuditItem]
    next_cursor: str | None


class AdminOverview(StrictModel):
    generated_at: datetime
    users_total: int
    users_active: int
    agents_total: int
    owner_enabled_agents: int
    sessions_total: int
    wallet_accounts: int
    wallet_posted_minor: int
    wallet_reserved_minor: int
    wallet_available_minor: int
    call_budgets_open: int
    call_budgets_closed: int
    provisional_charged_minor: int
    settled_charged_minor: int


class TranscriptAccessRequest(StrictModel):
    reason: str = Field(..., min_length=10, max_length=500)
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("reason must be between 10 and 500 characters")
        return trimmed


class TemplateVersionRequest(StrictModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    flow_json: dict[str, Any]
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("name must not be blank")
        return trimmed

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("reason must be between 10 and 500 characters")
        return trimmed


class PromptVersionRequest(StrictModel):
    prompt_text: str = Field(..., min_length=1, max_length=65_536)
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("prompt_text")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt_text must not be blank")
        return value

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("reason must be between 10 and 500 characters")
        return trimmed
