"""Pydantic wire models and schemas for platform_api.agents.

Follows master spec III.5 and flow-contract.md:
- Router schemas reject unknown keys (ConfigDict(extra="forbid"))
- Direct typed objects for success responses
- RFC 6901 pointer diagnostics
- Compare-and-swap expected revisions
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from platform_api.agents.defaults import DEFAULT_PERSONA_PROMPT

AcknowledgedWarningCode = Annotated[str, Field(min_length=1, max_length=128)]


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field("", max_length=1024)
    persona_prompt: str = Field(DEFAULT_PERSONA_PROMPT, max_length=20000)
    source: Literal["manual", "template", "generated"] = "manual"
    template_version_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value

    @field_validator("persona_prompt")
    @classmethod
    def persona_prompt_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("persona_prompt must not be blank")
        return value

class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = Field(None, max_length=1024)
    persona_prompt: str | None = Field(None, max_length=20000)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must not be blank")
        return value

    @field_validator("persona_prompt")
    @classmethod
    def persona_prompt_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("persona_prompt must not be blank")
        return value

class AgentRuntimeSettingsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_model: str
    stt_provider: str
    tts_voice_en: str
    tts_voice_hi: str


class AgentView(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    name: str
    description: str
    persona_prompt: str
    enabled: bool
    draft_revision: int
    published_version_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    runtime_settings: AgentRuntimeSettingsView | None = None


class AgentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AgentView]
    next_cursor: str | None = None


class DiagnosticView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    path: str
    message: str
    severity: Literal["error", "warning"] = "error"


class ValidationSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    errors: list[DiagnosticView] = Field(default_factory=list)
    warnings: list[DiagnosticView] = Field(default_factory=list)


class DraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(..., ge=1)
    config: dict[str, Any]


class DraftView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    draft_revision: int
    authoring_hash: str
    snapshot_hash: str | None = None
    config: dict[str, Any]
    validation: ValidationSummaryView
    updated_at: datetime


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(None, ge=1)
    expected_authoring_hash: str | None = None
    config: dict[str, Any] | None = None


class ValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    eligible_for_publish: bool
    authoring_hash: str
    flow_hash: str | None = None
    diagnostics: list[DiagnosticView] = Field(default_factory=list)
    errors: list[DiagnosticView] = Field(default_factory=list)
    warnings: list[DiagnosticView] = Field(default_factory=list)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(..., ge=1)
    changelog: str = Field("", max_length=512)
    acknowledged_warning_codes: list[AcknowledgedWarningCode] = Field(
        default_factory=list,
        max_length=64,
    )


class VersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    version_id: uuid.UUID
    agent_id: uuid.UUID
    version: int
    flow_hash: str
    artifact_hash: str
    authoring_hash: str
    compiler_version: str
    changelog: str
    created_at: datetime


class VersionDetailView(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    version_id: uuid.UUID
    agent_id: uuid.UUID
    version: int
    flow_hash: str
    artifact_hash: str
    authoring_hash: str
    compiler_version: str
    changelog: str
    immutable_config: dict[str, Any]
    policy_version_id: uuid.UUID | None = None
    created_at: datetime


class VersionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[VersionView]
    next_cursor: str | None = None


class RestoreDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: uuid.UUID | None = None
    version: int | None = None
    expected_revision: int = Field(..., ge=1)


class DuplicateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(None, max_length=1024)
    persona_prompt: str | None = Field(None, max_length=20000)
    source_revision_or_version: int | str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value

    @field_validator("persona_prompt")
    @classmethod
    def persona_prompt_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("persona_prompt must not be blank")
        return value

class TemplateView(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    slug: str
    version: int
    name: str
    description: str
    created_at: datetime


class TemplateDetailView(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    slug: str
    version: int
    name: str
    description: str
    flow_json: dict[str, Any]
    created_at: datetime


class TemplateListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TemplateView]
    next_cursor: str | None = None
