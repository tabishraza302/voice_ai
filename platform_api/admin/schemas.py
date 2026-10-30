"""Strict wire contracts for platform operator APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from platform_api.sessions.history_schemas import ConfigSnapshotView, SessionBillingView


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
    pinned_config_snapshot: ConfigSnapshotView | None
    transcript_visible_to_owner: bool
    transcript_policy_revision: int


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
    current_published_version: int | None
    created_at: datetime
    updated_at: datetime


class AdminAgentDetail(AdminAgentItem):
    owner_email: str
    owner_display_name: str
    immutable_config: dict[str, Any] | None
    flow_hash: str | None
    artifact_hash: str | None
    authoring_hash: str | None

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
 
class AdminAdmissionPauseItem(StrictModel):
    id: uuid.UUID
    scope: Literal["global", "user", "agent"]
    owner_user_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    paused: bool
    revision: int
    reason: str
    updated_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class AdminAdmissionPausePage(StrictModel):
    items: list[AdminAdmissionPauseItem]
    next_cursor: str | None


class AdminAdmissionPauseRequest(StrictModel):
    scope: Literal["global", "user", "agent"]
    owner_user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    paused: bool
    expected_revision: int = Field(..., ge=0)
    reason: str = Field(..., min_length=10, max_length=500)

    @model_validator(mode="after")
    def validate_target(self) -> AdminAdmissionPauseRequest:
        if self.scope == "global" and (
            self.owner_user_id is not None or self.agent_id is not None
        ):
            raise ValueError("global scope must not include a target")
        if self.scope == "user" and (
            self.owner_user_id is None or self.agent_id is not None
        ):
            raise ValueError("user scope requires only owner_user_id")
        if self.scope == "agent" and (
            self.owner_user_id is not None or self.agent_id is None
        ):
            raise ValueError("agent scope requires only agent_id")
        return self

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("reason must be between 10 and 500 characters")
        return trimmed


class AdminTariffVersionItem(StrictModel):
    id: uuid.UUID
    revision: int
    tariff_version: str
    rate_minor_per_minute: int
    billing_slice_ms: int
    permit_ttl_ms: int
    is_current: bool
    created_at: datetime


class AdminTariffPage(StrictModel):
    current: AdminTariffVersionItem
    items: list[AdminTariffVersionItem]
    next_cursor: str | None


class AdminTariffVersionRequest(StrictModel):
    expected_revision: int = Field(..., ge=1)
    rate_minor_per_minute: int = Field(..., ge=0, le=9_223_372_036_854_775_807)
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("reason must be between 10 and 500 characters")
        return trimmed


class AdminTranscriptCorrectionRequest(StrictModel):
    event_id: uuid.UUID = Field(..., strict=False)
    expected_revision: int = Field(..., ge=0)
    replacement_text: str = Field(..., min_length=1, max_length=16_384)
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("replacement_text")
    @classmethod
    def validate_replacement_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("replacement_text must not be blank")
        return value

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("reason must be between 10 and 500 characters")
        return trimmed


class AdminTranscriptCorrectionItem(StrictModel):
    id: uuid.UUID
    session_id: uuid.UUID
    event_id: uuid.UUID
    policy_revision: int
    created_at: datetime


class AdminTranscriptVisibilityRequest(StrictModel):
    visible_to_owner: bool
    expected_revision: int = Field(..., ge=0)
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("reason must be between 10 and 500 characters")
        return trimmed


class AdminTranscriptPolicyView(StrictModel):
    session_id: uuid.UUID
    visible_to_owner: bool
    policy_revision: int
    updated_at: datetime | None


class AdminSessionTerminationRequest(StrictModel):
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("reason must be between 10 and 500 characters")
        return trimmed
